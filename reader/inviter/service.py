import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from telethon.errors import (
    BotChannelsNaError,
    BotGroupsBlockedError,
    BotMissingError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InputUserDeactivatedError,
    PeerFloodError,
    PeerIdInvalidError,
    RPCError,
    UserAlreadyParticipantError,
    UserBlockedError,
    UserBotError,
    UserBotInvalidError,
    UserBotRequiredError,
    UserChannelsTooMuchError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UserIdInvalidError,
    UserIsBotError,
    UserKickedError,
    UserNotMutualContactError,
    UserPrivacyRestrictedError,
    UsersTooMuchError,
)
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.types import Channel, ChannelForbidden, InputPeerUser, User

from reader.inviter.models import InviteCampaign, InviteCandidate, TelegramAccount
from reader.inviter.repository import (
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)

logger = logging.getLogger(__name__)

# "Разогрев" — один раз после connect(), перед резолвом target_chat и до
# первого приглашения (см. _execute_account) — только что подключившийся
# аккаунт, который сразу начинает рассылать приглашения, выглядит подозрительно.
_WARMUP_MIN_SECONDS = 5
_WARMUP_MAX_SECONDS = 15

# Пауза после каждого приглашения (независимо от результата —
# invited/already_participant/failed/invalid), чтобы аккаунт не рассылал
# приглашения через равные интервалы — это и есть один из самых заметных
# признаков автоматизации для Telegram. Большинство пауз короткие, но
# примерно 20% — длинные (см. _choose_invite_pause_seconds), как у живого
# человека, который иногда отвлекается. FloodWait/PeerFlood — отдельная
# пауза/остановка (см. _invite_candidate), эта не добавляется поверх них.
_SHORT_PAUSE_MIN_SECONDS = 20
_SHORT_PAUSE_MAX_SECONDS = 60
_LONG_PAUSE_MIN_SECONDS = 90
_LONG_PAUSE_MAX_SECONDS = 180
_LONG_PAUSE_PROBABILITY = 0.2

# exc.seconds >= это значение — FloodWait считается слишком большим, чтобы
# просто подождать и продолжить тем же аккаунтом (см. _invite_candidate).
_MAX_TOLERABLE_FLOOD_WAIT_SECONDS = 300

# --test (main.py) — тестовый прогон: как только текущий вызов run()
# выполнит это количество успешных приглашений (status='invited', см.
# InviteStats.invited) — остановить ВЕСЬ запуск (текущий аккаунт
# заканчивается штатно, без прерывания уже выполняющегося приглашения,
# остальные аккаунты и кампании больше не трогаются, см. run()/
# _invite_candidate). already_participant/errors в счёт не идут.
TEST_MODE_MAX_SUCCESSFUL_INVITES = 30


def _choose_invite_pause_seconds() -> float:
    """~_LONG_PAUSE_PROBABILITY (по умолчанию 20%) случаев — длинная пауза
    (_LONG_PAUSE_MIN_SECONDS.._LONG_PAUSE_MAX_SECONDS), иначе — короткая
    (_SHORT_PAUSE_MIN_SECONDS.._SHORT_PAUSE_MAX_SECONDS). Отдельная функция —
    чтобы её можно было проверить в тестах моком random.random()/
    random.uniform(), не полагаясь на статистику по множеству прогонов."""
    if random.random() < _LONG_PAUSE_PROBABILITY:
        return random.uniform(_LONG_PAUSE_MIN_SECONDS, _LONG_PAUSE_MAX_SECONDS)
    return random.uniform(_SHORT_PAUSE_MIN_SECONDS, _SHORT_PAUSE_MAX_SECONDS)


class OperatorNotifierLike(Protocol):
    """Ровно то, что нужно InviterService от OperatorNotifier
    (reader/notifications/operator_notifier.py) — не импортируем сам класс,
    чтобы не тянуть Telethon-клиент в тесты, которым нужен только фейк."""

    async def notify_text(self, text: str) -> bool: ...


class UserAccessHashUpdaterLike(Protocol):
    """Ровно то, что нужно InviterService от UserRepository
    (reader/users/repository.py) — не импортируем сам класс, чтобы не
    тянуть всю таблицу users в тесты, которым нужен только фейк. Вся
    работа с SQL остаётся в самом UserRepository (см.
    UserRepository.update_access_hash/mark_as_bot)."""

    def update_access_hash(
        self, user_id: int, access_hash: int, username: str | None = None,
        is_bot: bool | None = None,
    ) -> bool: ...

    def mark_as_bot(self, user_id: int) -> bool: ...


@dataclass
class InviteStats:
    """Единая структура счётчиков — используется и для отчёта по аккаунту
    (_notify_account_result), и для итогового отчёта по кампании
    (_notify_campaign_result), чтобы не дублировать подсчёт статистики.

    invalid — кандидат, про которого достоверно известно, что приглашать
    его нельзя ПРИНЦИПИАЛЬНО (сейчас — только подтверждённый Telegram-бот,
    см. _classify_invite_error/_CandidateIsBotError) — status='invalid' в
    user_campaign_invites, отдельно от обычных failed/errors.

    FloodWaitError сюда не попадает ни в одно поле (ни invited, ни errors):
    это не отказ конкретному пользователю, а общее временное ограничение
    API (см. _invite_candidate) — кандидат остаётся кандидатом для
    следующего прогона, а не считается ни успехом, ни ошибкой."""

    invited: int = 0
    already_participant: int = 0
    invalid: int = 0
    errors: int = 0

    def __add__(self, other: "InviteStats") -> "InviteStats":
        return InviteStats(
            invited=self.invited + other.invited,
            already_participant=self.already_participant + other.already_participant,
            invalid=self.invalid + other.invalid,
            errors=self.errors + other.errors,
        )


class DryRunTelegramClient(Protocol):
    """Подмножество TelegramClient, которое использует InviterService —
    connect()/get_entity()/get_input_entity()/disconnect() (dry-run и
    execute) и __call__() (только execute — фактическая отправка запроса
    Telethon). get_input_entity() используется только в execute (см.
    _resolve_input_peer) — dry-run её не трогает. Ни ImportChatInviteRequest,
    ни какой-либо другой мутирующий метод сверх ровно одного приглашения на
    кандидата здесь не вызывается."""

    async def connect(self) -> None: ...

    async def get_entity(self, entity): ...

    async def get_input_entity(self, entity): ...

    async def __call__(self, request): ...

    async def disconnect(self) -> None: ...


TelegramClientFactory = Callable[[TelegramAccount], DryRunTelegramClient]


class _CandidateUnresolvableError(Exception):
    """candidate не известен текущему аккаунту и не может быть резолвлен
    (см. InviterService._resolve_input_peer) — не Telethon-ошибка, поэтому
    отдельный класс, не пересекающийся с их иерархией (RPCError и т.п.)."""


class _CandidateIsBotError(Exception):
    """candidate — подтверждённый (через живой запрос к Telegram, см.
    InviterService._resolve_input_peer) Telegram-бот — приглашение
    отменяется ДО отправки InviteToChannelRequest/AddChatUserRequest. Не
    Telethon-ошибка (см. _CandidateUnresolvableError)."""


def _format_username(username: str | None) -> str:
    return f"@{username}" if username else "(без username)"


def _format_duration(seconds: float) -> str:
    """secs (time.monotonic()-разность, а не datetime.now() — не зависит от
    перевода системных часов) -> "HH:MM:SS". Общая функция для отчёта по
    аккаунту и по кампании (см. _format_account_notification/
    _format_campaign_summary_notification) — не дублируется."""
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_candidates_block(
    campaign: InviteCampaign,
    account: TelegramAccount,
    candidates: list[InviteCandidate],
    found: int,
) -> str:
    lines = [f"Campaign: {campaign.name}", f"Account: {account.name}", ""]
    for candidate in candidates:
        lines.append(f"{candidate.user_id} {_format_username(candidate.username)}")
        lines.append(f"keywords: {', '.join(candidate.keywords)}")
        lines.append(
            f"last_seen_at: {candidate.last_seen_at.isoformat() if candidate.last_seen_at else '—'}"
        )
        lines.append("")
    lines.append(f"найдено кандидатов: {found}")
    lines.append(f"выбрано: {len(candidates)}")
    return "\n".join(lines)


def _format_dry_run_block(
    account: TelegramAccount,
    user_label: str,
    target_label: str,
    *,
    ready: bool,
    reason: str | None = None,
) -> str:
    lines = ["[DRY RUN]", f"Account: {account.name}", f"User: {user_label}", f"Target: {target_label}"]
    if ready:
        lines.append("READY")
    else:
        lines.append("FAILED")
        lines.append(reason or "неизвестная причина")
    return "\n".join(lines)


def _format_execute_block(
    account: TelegramAccount,
    user_label: str,
    target_label: str,
    *,
    status: str,
    reason: str | None = None,
) -> str:
    lines = ["[EXECUTE]", f"Account: {account.name}", f"User: {user_label}", f"Target: {target_label}"]
    lines.append(status.upper())
    if reason:
        lines.append(reason)
    return "\n".join(lines)


def _format_account_notification(
    campaign: InviteCampaign, account: TelegramAccount, stats: InviteStats, remaining: int,
    elapsed_seconds: float,
) -> str:
    return (
        f"📨 Кампания: {campaign.name}\n\n"
        f"👤 Аккаунт: {account.name}\n\n"
        f"Время выполнения: {_format_duration(elapsed_seconds)}\n\n"
        f"✅ Приглашено: {stats.invited}\n"
        f"☑️ Уже состояли в группе: {stats.already_participant}\n"
        f"🚫 Недоступны (invalid): {stats.invalid}\n"
        f"❌ Ошибок: {stats.errors}\n\n"
        f"Осталось кандидатов: {remaining}"
    )


def _format_campaign_summary_notification(
    campaign: InviteCampaign, accounts_processed: int, stats: InviteStats, remaining: int,
    elapsed_seconds: float, found_total: int, found_processable: int,
) -> str:
    """found_total — сколько подошло по keyword/access_hash/ещё-не-приглашён,
    БЕЗ фильтра по username (UserCampaignInviteRepository.
    count_found_candidates()); found_processable — то же самое, но С этим
    фильтром (count_candidates(), как и раньше). Разница между ними — те,
    кого нашли, но подготовить к приглашению физически нельзя (нет
    username для резолва этим аккаунтом, см.
    InviterService._resolve_input_peer) — считается прямо здесь, простым
    вычитанием двух уже посчитанных в SQL чисел, а не перебором
    пользователей в Python."""
    skipped_without_username = found_total - found_processable
    separator = "=" * 32
    return (
        f"{separator}\n\n"
        f"📋 Кампания: {campaign.name}\n\n"
        f"👥 Всего найдено: {found_total}\n"
        f"✅ Будет обработано: {found_processable}\n\n"
        f"🚫 Пропущено:\n"
        f"• без username: {skipped_without_username}\n\n"
        f'📊 Итоги кампании "{campaign.name}"\n\n'
        f"Время выполнения: {_format_duration(elapsed_seconds)}\n\n"
        f"Аккаунтов обработано: {accounts_processed}\n\n"
        f"✅ Приглашено: {stats.invited}\n"
        f"☑️ Уже состояли: {stats.already_participant}\n"
        f"🚫 Недоступны: {stats.invalid}\n"
        f"❌ Ошибок: {stats.errors}\n\n"
        f"Осталось кандидатов: {remaining}\n\n"
        f"{separator}"
    )


def _format_account_stopped_notification(account: TelegramAccount, reason: str) -> str:
    return (
        f"⚠️ Аккаунт: {account.name}\n\n"
        f"{reason}\n\n"
        f"Работа аккаунта остановлена.\n\n"
        f"Переход к следующему аккаунту."
    )


def _humanize_error(exc: Exception) -> str:
    """Понятное для оператора описание распространённых Telegram RPC-ошибок
    — используется ТОЛЬКО там, где текст ошибки уже показывается оператору
    (см. _format_account_stopped_notification). В логах (_format_execute_block)
    и в user_campaign_invites.error (см. _record_invite_result) продолжает
    храниться str(exc) — оригинальное имя/текст исключения, для диагностики.

    Порядок проверок не важен — все перечисленные классы независимы (ни
    один не является подклассом другого), кроме FloodWaitError, которому
    нужен exc.seconds. Ошибки, для которых понятного описания ещё нет,
    возвращаются как есть (str(exc)), а не теряются."""
    if isinstance(exc, FloodWaitError):
        return f"Telegram требует подождать {exc.seconds} секунд."
    if isinstance(exc, UserChannelsTooMuchError):
        return "Пользователь состоит в слишком большом количестве групп."
    if isinstance(exc, UserPrivacyRestrictedError):
        return "Пользователь запретил приглашения."
    if isinstance(exc, UserAlreadyParticipantError):
        return "Уже состоит в группе."
    if isinstance(exc, ChatAdminRequiredError):
        return "У аккаунта нет прав приглашать участников."
    if isinstance(exc, PeerFloodError):
        # exc.__init__ жёстко пишет "Too many requests" (см. Telethon —
        # отдельного TooManyRequestsError не существует, это и есть
        # PeerFloodError) — упоминаем исходную фразу, чтобы оператор мог
        # сопоставить её с тем, что видит в логах/БД (см. ниже — там
        # остаётся str(exc) без изменений).
        return "Telegram временно ограничил приглашения (Too many requests)."
    if isinstance(exc, ChatWriteForbiddenError):
        return "У аккаунта нет прав писать/приглашать в этот чат."
    if isinstance(exc, ChannelPrivateError):
        return "Канал/группа недоступны этому аккаунту (приватный канал или аккаунт исключён)."
    if isinstance(exc, UsersTooMuchError):
        return "В группе/канале достигнут лимит участников."
    return str(exc)


class InviteErrorAction(Enum):
    """Что делать после ошибки при попытке пригласить кандидата — единая
    классификация вместо разбросанных except-веток (см.
    _classify_invite_error/InviterService._handle_invite_error)."""

    # Кандидат ни при чём — под сомнением сам аккаунт у Telegram (флуд,
    # админ-права, доступ к чату и т.п., см. _STOP_ACCOUNT_ERROR_TYPES) —
    # прекратить обработку ОСТАЛЬНЫХ кандидатов этим аккаунтом.
    STOP_ACCOUNT = "stop_account"
    # Проблема только в этом кандидате — риска для аккаунта нет, продолжаем
    # со следующим кандидатом тем же аккаунтом.
    SKIP_USER = "skip_user"
    # Временное общее ограничение API (небольшой FloodWait) — подождать
    # exc.seconds и продолжить тем же аккаунтом.
    RETRY_LATER = "retry_later"
    # Совсем не Telegram RPC-ошибка (не RPCError вовсе) — по определению
    # ничего о ней не известно, поэтому обрабатывается так же осторожно,
    # как STOP_ACCOUNT, но отдельно — для логирования с трассировкой.
    FATAL = "fatal"


@dataclass(frozen=True)
class InviteErrorClassification:
    """Результат _classify_invite_error(exc).

    db_status/stat_field — что записать в user_campaign_invites и какое
    поле InviteStats увеличить ("invited"/"already_participant"/"invalid"/
    "errors"; None — не увеличивать ничего, см. FloodWaitError).
    operator_message — человекочитаемый текст ТОЛЬКО для операторского
    уведомления при STOP_ACCOUNT/FATAL (см. _format_account_stopped_notification);
    в логах и в user_campaign_invites.error всегда остаётся str(exc) без
    изменений (см. InviterService._handle_invite_error).
    mark_as_bot — сохранить is_bot=1 в users.db (см.
    InviterService._mark_user_as_bot), чтобы этот кандидат больше никогда
    не попадал в выборку ни для одной кампании."""

    action: InviteErrorAction
    db_status: str
    stat_field: str | None
    operator_message: str = ""
    wait_seconds: float | None = None
    mark_as_bot: bool = False


# Проблема только в конкретном кандидате (устарел/удалён/заблокировал этот
# аккаунт/настройки приватности и т.п.) — риска для самого аккаунта нет.
_SKIP_USER_ERROR_TYPES = (
    UserPrivacyRestrictedError,
    UserChannelsTooMuchError,
    UserIdInvalidError,
    PeerIdInvalidError,
    UserDeactivatedError,
    UserDeactivatedBanError,
    InputUserDeactivatedError,
    UserKickedError,
    UserBlockedError,
    UserNotMutualContactError,
)

# Telegram подтвердил RPC-ошибкой, что кандидат — бот (см. также
# проактивную проверку в InviterService._resolve_input_peer, которая в
# норме должна отсеивать эти случаи ДО отправки приглашения) — запоминаем
# is_bot=1 в users.db, чтобы повторно этот бот уже никогда не попадал в
# выборку кандидатов ни для одной кампании (см. задачу об инциденте с
# приглашением Telegram-бота @Vlars_Bot и 3-дневным ограничением аккаунта).
_BOT_ERROR_TYPES = (
    UserBotError,
    UserIsBotError,
    UserBotInvalidError,
    UserBotRequiredError,
    BotGroupsBlockedError,
    BotChannelsNaError,
    BotMissingError,
)

# Ставят под сомнение сам статус аккаунта у Telegram или доступ к
# target_chat — продолжать этим же аккаунтом рискованно (см. задачу:
# именно ChatAdminRequiredError на попытке пригласить бота предшествовал
# 3-дневному ограничению приглашений у аккаунта в реальном прогоне).
_STOP_ACCOUNT_ERROR_TYPES = (
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    ChannelPrivateError,
    UsersTooMuchError,
)


def _classify_invite_error(exc: Exception) -> InviteErrorClassification:
    """Единая точка классификации любой ошибки, возникшей при попытке
    пригласить кандидата (резолв + сама отправка, см.
    InviterService._invite_candidate) — вместо набора except-веток.

    Порядок проверок важен только для FloodWaitError (двойной исход по
    exc.seconds) и для UserAlreadyParticipantError/_CandidateIsBotError/
    _CandidateUnresolvableError (не входят ни в один из трёх тюплов ниже) —
    остальные классы между собой не пересекаются.

    Если ошибка вообще не распознана (не входит ни в один из известных
    типов) — по умолчанию STOP_ACCOUNT, а не "пропустить и продолжить": по
    условию задачи ("если есть сомнения — лучше остановить аккаунт, чем
    продолжать") любая нераспознанная RPC-ошибка считается потенциальным
    риском для аккаунта. Единственное исключение — вообще не RPCError
    (FATAL): такая ошибка не может быть проанализирована вовсе, поэтому
    тоже останавливает аккаунт, но логируется отдельно, с трассировкой."""
    if isinstance(exc, UserAlreadyParticipantError):
        return InviteErrorClassification(
            InviteErrorAction.SKIP_USER, db_status="invited", stat_field="already_participant",
        )
    if isinstance(exc, _CandidateIsBotError):
        return InviteErrorClassification(
            InviteErrorAction.SKIP_USER, db_status="invalid", stat_field="invalid",
        )
    if isinstance(exc, _CandidateUnresolvableError):
        return InviteErrorClassification(
            InviteErrorAction.SKIP_USER, db_status="failed", stat_field="errors",
        )
    if isinstance(exc, FloodWaitError):
        if exc.seconds >= _MAX_TOLERABLE_FLOOD_WAIT_SECONDS:
            return InviteErrorClassification(
                InviteErrorAction.STOP_ACCOUNT, db_status="failed", stat_field=None,
                operator_message=_humanize_error(exc), wait_seconds=exc.seconds,
            )
        return InviteErrorClassification(
            InviteErrorAction.RETRY_LATER, db_status="failed", stat_field=None,
            wait_seconds=exc.seconds,
        )
    if isinstance(exc, PeerFloodError):
        return InviteErrorClassification(
            InviteErrorAction.STOP_ACCOUNT, db_status="failed", stat_field="errors",
            operator_message=_humanize_error(exc),
        )
    if isinstance(exc, _BOT_ERROR_TYPES):
        return InviteErrorClassification(
            InviteErrorAction.SKIP_USER, db_status="invalid", stat_field="invalid",
            mark_as_bot=True,
        )
    if isinstance(exc, _SKIP_USER_ERROR_TYPES):
        return InviteErrorClassification(
            InviteErrorAction.SKIP_USER, db_status="failed", stat_field="errors",
        )
    if isinstance(exc, _STOP_ACCOUNT_ERROR_TYPES):
        return InviteErrorClassification(
            InviteErrorAction.STOP_ACCOUNT, db_status="failed", stat_field="errors",
            operator_message=_humanize_error(exc),
        )
    if isinstance(exc, RPCError):
        return InviteErrorClassification(
            InviteErrorAction.STOP_ACCOUNT, db_status="failed", stat_field="errors",
            operator_message=_humanize_error(exc),
        )
    return InviteErrorClassification(
        InviteErrorAction.FATAL, db_status="failed", stat_field="errors",
        operator_message=_humanize_error(exc),
    )


def _session_file_path(account: TelegramAccount) -> Path:
    """Telethon сам дописывает ".session" к переданному session_path —
    ровно тот же файл, что будет открывать TelegramClient(account.session_path, ...)."""
    return Path(f"{account.session_path}.session")


def _default_session_checker(account: TelegramAccount) -> bool:
    """Реальная проверка наличия .session-файла на диске — используется по
    умолчанию (main.py ничего специально передавать не обязан). Тесты
    подставляют свою функцию через InviterService(session_checker=...),
    чтобы не зависеть от реальных файлов (см. _run_service в тестах)."""
    return _session_file_path(account).exists()


def _format_missing_session_message(account: TelegramAccount) -> str:
    return (
        "Session not found:\n\n"
        f"Account: {account.name}\n\n"
        "Expected session:\n"
        f"{_session_file_path(account)}\n\n"
        "Please authorize this account first."
    )


def _build_invite_request(target_entity, input_peer: InputPeerUser):
    """InviteToChannelRequest — для каналов/супергрупп (Channel), иначе
    (обычный small group chat, Chat) — AddChatUserRequest. Ровно один из
    двух, ни один другой Telegram-мутирующий метод не вызывается.

    ChannelForbidden — тоже Channel-семейство (то же TL-объединение "Chat",
    что и Channel/Chat), а не Chat: get_entity() может вернуть его для
    канала/супергруппы без полного доступа у этого аккаунта. AddChatUserRequest
    для него ломается точно так же, как для обычного Channel (Telegram
    трактует chat_id как id обычной группы, а не канала/супергруппы — см.
    ChatIdInvalidError: "...if the request is designed for chats (not
    channels/megagroups)... An example working with a megagroup and
    AddChatUserRequest, it will fail because megagroups are channels. Use
    InviteToChannelRequest instead")."""
    if isinstance(target_entity, (Channel, ChannelForbidden)):
        return InviteToChannelRequest(channel=target_entity, users=[input_peer])
    return AddChatUserRequest(chat_id=target_entity.id, user_id=input_peer, fwd_limit=0)


class InviterService:
    """Отбор кандидатов на приглашение + Telethon-подготовка (подключение
    аккаунта, резолв target_chat кампании, построение InputPeerUser для
    каждого кандидата).

    По умолчанию (run(), run(execute=False)) — только dry-run: ни одного
    Telegram-мутирующего вызова (InviteToChannelRequest/AddChatUserRequest)
    и ни одной записи в user_campaign_invites (см.
    UserCampaignInviteRepository.select_candidates).

    run(execute=True) — реальные приглашения: кандидаты выбираются ТОЛЬКО
    после того, как аккаунт подтвердил, что может приглашать (сессия
    есть, остаток дневного лимита > 0, connect() и резолв target_chat
    успешны, см. _execute_account) — неработающий аккаунт не тратит ни
    одного SQL-запроса выборки. Остаток дневного лимита считается по
    факту из БД (daily_limit минус уже выполненные СЕГОДНЯ успешные
    приглашения этого аккаунта по всем кампаниям, см.
    UserCampaignInviteRepository.count_today_successful) — без единого
    счётчика в памяти. Дальше — ровно один
    InviteToChannelRequest/AddChatUserRequest на кандидата (см.
    _build_invite_request), с немедленной записью результата
    (status='invited'/'failed') сразу после каждого кандидата — не
    дожидаясь конца партии, и случайной паузой после каждого (см.
    _pause_between_invites). FloodWaitError < _MAX_TOLERABLE_FLOOD_WAIT_SECONDS
    ждётся (exc.seconds) и не прерывает ни аккаунт, ни весь сервис;
    FloodWaitError >= _MAX_TOLERABLE_FLOOD_WAIT_SECONDS и PeerFloodError
    останавливают текущий аккаунт (с уведомлением оператора, см.
    _format_account_stopped_notification) и переходят к следующему; сбой
    самого аккаунта (например, обрыв connect()) не мешает остальным.

    В режиме execute=True после каждого обработанного аккаунта и после
    каждой кампании оператору отправляется краткая статистика (см.
    InviteStats/_notify_account_result/_notify_campaign_result) через
    notifier (см. OperatorNotifierLike) — тем же существующим механизмом
    уведомлений, что и у остального приложения, просто отдельным
    подключением (см. reader/inviter/main.py). Сбой уведомления не
    прерывает сами приглашения (см. _safe_notify)."""

    def __init__(
        self,
        account_repository: TelegramAccountRepository,
        campaign_repository: InviteCampaignRepository,
        invite_repository: UserCampaignInviteRepository,
        client_factory: TelegramClientFactory,
        notifier: OperatorNotifierLike | None = None,
        session_checker: Callable[[TelegramAccount], bool] = _default_session_checker,
        user_repository: UserAccessHashUpdaterLike | None = None,
        max_successful_invites: int | None = None,
    ):
        self._account_repository = account_repository
        self._campaign_repository = campaign_repository
        self._invite_repository = invite_repository
        self._client_factory = client_factory
        # None — уведомления оператору просто не отправляются (например,
        # если main.py не смог поднять OperatorNotifier); это не должно
        # мешать самим приглашениям, см. _safe_notify().
        self._notifier = notifier
        # По умолчанию — реальная проверка файла на диске (см.
        # _default_session_checker); подставляется явно только в тестах.
        self._session_checker = session_checker
        # None — свежий access_hash (см. _resolve_input_peer) просто не
        # сохраняется в users.db; сам резолв и приглашение это не должно
        # останавливать (см. _update_user_access_hash).
        self._user_repository = user_repository
        # None (по умолчанию) — без ограничения, обычный режим. Иначе —
        # тестовый режим (--test в main.py, см. TEST_MODE_MAX_SUCCESSFUL_INVITES):
        # run() останавливается, как только наберёт столько успешных
        # приглашений, см. _successful_invites_count.
        self._max_successful_invites = max_successful_invites
        self._successful_invites_count = 0

    async def run(self, *, execute: bool = False) -> None:
        """execute=False (по умолчанию) — только dry-run, без единого
        изменения в Telegram (см. _dry_run_account). execute=True — реальные
        приглашения (см. _execute_account); включается только явным
        --execute в reader/inviter/main.py, никогда неявно.

        execute=True и execute=False выбирают кандидатов ПРИНЦИПИАЛЬНО
        по-разному (см. задачу о лишней выборке для неработающих
        аккаунтов):
        - dry-run ничего не пишет в user_campaign_invites (см.
          _dry_run_account), поэтому единственный способ не раздать одних
          и тех же кандидатов нескольким аккаунтам — выбрать их ОДИН раз
          на кампанию (total_limit = SUM(daily_limit)) и поделить список
          между аккаунтами по смещению (Account1 — [0:d1], Account2 —
          [d1:d1+d2], и т.д., как и раньше).
        - execute=True, наоборот, каждым успешным приглашением сразу
          пишет status='invited' (см. _record_invite_result), поэтому
          выборка кандидатов делается ОТДЕЛЬНО на каждый аккаунт, ПОСЛЕ
          того, как аккаунт подтвердил, что может приглашать (см.
          _execute_account) — и лимит для неё — остаток дневного лимита
          ЭТОГО аккаунта, а не daily_limit целиком (см. задачу про
          daily_limit). Пересечений между аккаунтами всё равно не
          возникает: к моменту выборки для второго аккаунта первый уже
          записал свои результаты, и NOT EXISTS в select_candidates() их
          исключает."""
        campaigns = [c for c in self._campaign_repository.list() if c.enabled]
        accounts = [a for a in self._account_repository.list() if a.enabled]

        if not accounts:
            return

        # Счётчик успешных приглашений (status='invited') именно этого
        # вызова run() — сбрасывается на каждый запуск, см.
        # TEST_MODE_MAX_SUCCESSFUL_INVITES/_invite_candidate.
        self._successful_invites_count = 0
        limit_reached = False

        for campaign in campaigns:
            if limit_reached:
                break

            campaign_started_at = time.monotonic()
            found = self._invite_repository.count_candidates(campaign.id)
            # "Всего найдено" для операторского отчёта (см.
            # _notify_campaign_result) — то же самое, но без фильтра по
            # username, чтобы показать, сколько отсеялось именно из-за него.
            found_total = self._invite_repository.count_found_candidates(campaign.id)

            campaign_stats = InviteStats()
            accounts_processed = 0

            if execute:
                for account in accounts:
                    account_stats = await self._execute_account(campaign, account, found)
                    if account_stats is not None:
                        campaign_stats = campaign_stats + account_stats
                        accounts_processed += 1
                    if (
                        self._max_successful_invites is not None
                        and self._successful_invites_count >= self._max_successful_invites
                    ):
                        # "Мягкая" остановка: текущий аккаунт уже завершён
                        # штатно (см. _execute_account/_invite_candidate) —
                        # просто не переходим к следующему аккаунту/кампании.
                        logger.info(
                            f"Тестовый режим: достигнут лимит "
                            f"{self._max_successful_invites} успешных приглашений — "
                            f"дальнейшая обработка остановлена."
                        )
                        limit_reached = True
                        break

                # ВСЕГДА, а не только если accounts_processed > 0 — иначе
                # итоговый отчёт по кампании молча пропадал бы целиком,
                # например если ни у одного аккаунта не нашлось кандидатов
                # или не было .session-файла (см. задачу про баг с
                # пропадающей статистикой).
                remaining = self._invite_repository.count_candidates(campaign.id)
                await self._notify_campaign_result(
                    campaign, accounts_processed, campaign_stats, remaining,
                    time.monotonic() - campaign_started_at,
                    found_total, found,
                )
            else:
                # Общая выборка на кампанию + деление по смещению — см.
                # докстрок run() про то, почему dry-run не может выбирать
                # кандидатов по аккаунту так же, как execute=True.
                total_limit = sum(account.daily_limit for account in accounts)
                candidates = self._invite_repository.select_candidates(campaign.id, limit=total_limit)
                offset = 0
                for account in accounts:
                    account_candidates = candidates[offset : offset + account.daily_limit]
                    offset += account.daily_limit
                    logger.info(_format_candidates_block(campaign, account, account_candidates, found))
                    await self._dry_run_account(campaign, account, account_candidates)

    async def _dry_run_account(
        self,
        campaign: InviteCampaign,
        account: TelegramAccount,
        candidates: list[InviteCandidate],
    ) -> None:
        """Подключает account.client, резолвит campaign.target_chat и для
        каждого candidate готовит (но не отправляет) InputPeerUser — падение
        одного аккаунта (например, обрыв подключения) не должно прерывать
        обработку остальных аккаунтов (см. InviterService.run()), поэтому
        любая ошибка здесь только логируется."""
        if not candidates:
            return

        if not self._session_checker(account):
            logger.warning(_format_missing_session_message(account))
            return

        client = self._client_factory(account)
        try:
            try:
                await client.connect()
            except Exception as exc:
                logger.warning(
                    f"[DRY RUN]\nAccount: {account.name}\nFAILED\nНе удалось подключиться: {exc}"
                )
                return

            try:
                await client.get_entity(campaign.target_chat)
                target_error: str | None = None
            except Exception as exc:
                target_error = f"target_chat '{campaign.target_chat}' не найден: {exc}"

            for candidate in candidates:
                logger.info(
                    self._dry_run_candidate_block(account, campaign, candidate, target_error)
                )
        finally:
            await client.disconnect()

    def _dry_run_candidate_block(
        self,
        account: TelegramAccount,
        campaign: InviteCampaign,
        candidate: InviteCandidate,
        target_error: str | None,
    ) -> str:
        user_label = f"{candidate.user_id} {_format_username(candidate.username)}"

        if target_error is not None:
            return _format_dry_run_block(
                account, user_label, campaign.target_chat, ready=False, reason=target_error,
            )

        try:
            InputPeerUser(user_id=candidate.user_id, access_hash=candidate.access_hash)
        except Exception as exc:
            return _format_dry_run_block(
                account, user_label, campaign.target_chat, ready=False,
                reason=f"не удалось построить InputPeerUser: {exc}",
            )

        return _format_dry_run_block(account, user_label, campaign.target_chat, ready=True)

    async def _execute_account(
        self,
        campaign: InviteCampaign,
        account: TelegramAccount,
        found: int,
    ) -> InviteStats | None:
        """Порядок специально такой (см. задачу про бесполезную выборку
        кандидатов для неработающих аккаунтов и про daily_limit, не
        учитывающий уже выполненные сегодня приглашения):

        1. .session-файл (см. _default_session_checker) — самая дешёвая
           проверка, без единого запроса вообще.
        2. found == 0 — во всей кампании нет ни одного кандидата, ни один
           аккаунт не должен даже подключаться (см. test_execute_does_not_
           reinvite_user_on_next_run).
        3. Остаток дневного лимита ЭТОГО аккаунта — daily_limit минус
           фактически выполненные сегодня приглашения (см.
           UserCampaignInviteRepository.count_today_successful, без
           единого счётчика в памяти) — если <= 0, аккаунт полностью
           пропущен, тоже без единого SQL-запроса выборки кандидатов.
        4. Только после этого — connect(), резолв campaign.target_chat
           (убедиться, что аккаунтом вообще можно приглашать в эту
           группу/канал), и ТОЛЬКО если это удалось — select_candidates()
           с limit=остаток. Если аккаунт не может приглашать (не
           подключился, target_chat не резолвится) — кандидаты не
           выбираются вовсе.

        Возвращает None, если аккаунт был пропущен ПОЛНОСТЬЮ (сессии нет,
        found == 0 или остаток лимита исчерпан — не считается
        "обработанным", см. run()), иначе накопленную InviteStats —
        уведомление оператору (см. _notify_account_result) отправляется
        ровно один раз в конце — и при обрыве connect()/резолва
        target_chat тоже (со стартовыми, скорее всего нулевыми,
        счётчиками), и при обычном завершении."""
        if not self._session_checker(account):
            logger.warning(_format_missing_session_message(account))
            return None

        if found == 0:
            return None

        today_successful = self._invite_repository.count_today_successful(account.id)
        remaining = account.daily_limit - today_successful
        if remaining <= 0:
            logger.info(
                f"[EXECUTE]\nAccount: {account.name}\n"
                f"Дневной лимит уже выполнен сегодня: {today_successful}/{account.daily_limit} "
                f"успешных приглашений — аккаунт пропущен, выборка кандидатов не выполняется."
            )
            return None

        started_at = time.monotonic()
        stats = InviteStats()
        client = self._client_factory(account)
        try:
            try:
                await client.connect()
            except Exception as exc:
                logger.warning(
                    f"[EXECUTE]\nAccount: {account.name}\nFAILED\nНе удалось подключиться: {exc}"
                )
            else:
                # "Разогрев" — один раз сразу после подключения, до резолва
                # target_chat и до первого приглашения (НЕ перед каждым
                # кандидатом, см. _warm_up_account).
                await self._warm_up_account()

                try:
                    target_entity = await client.get_entity(campaign.target_chat)
                except Exception as exc:
                    logger.warning(
                        f"[EXECUTE]\nAccount: {account.name}\nTarget: {campaign.target_chat}\n"
                        f"FAILED\nНе удалось найти target_chat: {exc}"
                    )
                else:
                    # ВРЕМЕННАЯ ДИАГНОСТИКА — какой именно тип вернул
                    # get_entity() (Channel/Chat/ChannelForbidden/другое) и
                    # выбрал ли _build_invite_request() из-за этого
                    # AddChatUserRequest вместо InviteToChannelRequest для
                    # каналов/супергрупп. Убрать после подтверждения на
                    # реальном прогоне.
                    logger.info(
                        f"[EXECUTE]\nAccount: {account.name}\nTarget: {campaign.target_chat}\n"
                        f"Resolved entity type: {type(target_entity).__module__}."
                        f"{type(target_entity).__name__}\n"
                        f"id={getattr(target_entity, 'id', None)}, "
                        f"megagroup={getattr(target_entity, 'megagroup', None)}, "
                        f"broadcast={getattr(target_entity, 'broadcast', None)}, "
                        f"gigagroup={getattr(target_entity, 'gigagroup', None)}, "
                        f"access_hash={getattr(target_entity, 'access_hash', None)}"
                    )
                    # Только теперь, когда подтверждено, что этим аккаунтом
                    # можно приглашать — выборка кандидатов, с лимитом =
                    # остаток дневного лимита (см. докстрок выше).
                    candidates = self._invite_repository.select_candidates(
                        campaign.id, limit=remaining,
                    )
                    logger.info(_format_candidates_block(campaign, account, candidates, found))
                    for candidate in candidates:
                        should_stop = await self._invite_candidate(
                            client, campaign, account, target_entity, candidate, stats,
                        )
                        if should_stop:
                            # PeerFlood, FloodWait >= _MAX_TOLERABLE_FLOOD_WAIT_SECONDS,
                            # либо достигнут лимит успешных приглашений
                            # тестового режима (см. _invite_candidate) —
                            # остальные кандидаты этого аккаунта не трогаем.
                            break
        finally:
            await client.disconnect()

        await self._notify_account_result(campaign, account, stats, time.monotonic() - started_at)
        return stats

    async def _invite_candidate(
        self,
        client: DryRunTelegramClient,
        campaign: InviteCampaign,
        account: TelegramAccount,
        target_entity,
        candidate: InviteCandidate,
        stats: InviteStats,
    ) -> bool:
        """Ровно один запрос Telethon на кандидата (InviteToChannelRequest
        или AddChatUserRequest, см. _build_invite_request) — результат
        сохраняется в user_campaign_invites немедленно, независимо от
        успеха/неудачи, чтобы обрыв ПОСЛЕ этого кандидата не потерял уже
        готовый результат.

        Возвращает True, если этот аккаунт должен немедленно прекратить
        обработку ОСТАЛЬНЫХ кандидатов (см. _classify_invite_error —
        InviteErrorAction.STOP_ACCOUNT/FATAL, либо достигнут лимит
        успешных приглашений тестового режима) — обрыв connect() отдельно
        этого аккаунта уже обрабатывается выше по стеку, здесь же он не
        встречается. Во всех остальных случаях — False, и после случайной
        паузы (см. _pause_between_invites) обработка продолжается со
        следующего кандидата.

        Перед отправкой резолвит candidate ИМЕННО этим аккаунтом и
        проверяет его на статус Telegram-бота (см. _resolve_input_peer) —
        access_hash из users.db получен читающим аккаунтом (sync_users.py/
        main.py), а не текущим инвайтящим, и часто невалиден для него.
        Любая ошибка резолва или самой отправки — через единый
        классификатор (см. _classify_invite_error/_handle_invite_error), а
        не разбросанные except-ветки."""
        user_label = f"{candidate.user_id} {_format_username(candidate.username)}"

        try:
            input_peer = await self._resolve_input_peer(client, candidate)
            request = _build_invite_request(target_entity, input_peer)
            await client(request)
        except Exception as exc:
            return await self._handle_invite_error(
                exc, campaign, account, candidate, stats, user_label,
            )
        else:
            logger.info(
                _format_execute_block(account, user_label, campaign.target_chat, status="invited")
            )
            self._record_invite_result(campaign, account, candidate, status="invited")
            stats.invited += 1
            self._successful_invites_count += 1
            await self._pause_between_invites()
            if (
                self._max_successful_invites is not None
                and self._successful_invites_count >= self._max_successful_invites
            ):
                # Лимит тестового режима достигнут — сигнализируем
                # _execute_account остановить ЭТОТ аккаунт (как при
                # STOP_ACCOUNT); переход к следующему аккаунту/кампании
                # останавливает уже run() (см. выше).
                return True
            return False

    async def _handle_invite_error(
        self,
        exc: Exception,
        campaign: InviteCampaign,
        account: TelegramAccount,
        candidate: InviteCandidate,
        stats: InviteStats,
        user_label: str,
    ) -> bool:
        """Единая обработка любой ошибки из _invite_candidate — классифицирует
        (см. _classify_invite_error) и применяет ровно то, что решил
        классификатор: запись в user_campaign_invites/InviteStats (raw
        str(exc) — техническая причина, для диагностики), опциональное
        is_bot=1 в users.db (см. _mark_user_as_bot), и одно из четырёх
        действий (STOP_ACCOUNT/SKIP_USER/RETRY_LATER/FATAL).

        Возвращает True, если аккаунт должен прекратить обработку
        ОСТАЛЬНЫХ кандидатов (STOP_ACCOUNT и FATAL — оператору отправляется
        _format_account_stopped_notification с человекочитаемой причиной,
        см. classification.operator_message), иначе False."""
        classification = _classify_invite_error(exc)
        raw_text = str(exc)

        if classification.mark_as_bot:
            self._mark_user_as_bot(candidate.user_id)

        log_fn = logger.info if classification.db_status in ("invited", "invalid") else logger.warning
        log_fn(
            _format_execute_block(
                account, user_label, campaign.target_chat,
                status=classification.db_status, reason=raw_text,
            ),
            exc_info=exc if classification.action == InviteErrorAction.FATAL else None,
        )
        self._record_invite_result(
            campaign, account, candidate,
            status=classification.db_status,
            error=None if classification.db_status == "invited" else raw_text,
        )
        if classification.stat_field is not None:
            setattr(stats, classification.stat_field, getattr(stats, classification.stat_field) + 1)

        if classification.action == InviteErrorAction.RETRY_LATER:
            await asyncio.sleep(classification.wait_seconds)
            return False

        if classification.action in (InviteErrorAction.STOP_ACCOUNT, InviteErrorAction.FATAL):
            await self._safe_notify(
                _format_account_stopped_notification(account, classification.operator_message)
            )
            return True

        # SKIP_USER — этот кандидат обработан, продолжаем тем же аккаунтом.
        await self._pause_between_invites()
        return False

    async def _resolve_input_peer(
        self, client: DryRunTelegramClient, candidate: InviteCandidate,
    ) -> InputPeerUser:
        """access_hash в users.db получен ЧИТАЮЩИМ аккаунтом (sync_users.py/
        main.py), а не текущим инвайтящим — Telegram привязывает access_hash
        к паре (аккаунт, пользователь), поэтому чужой access_hash часто
        отклоняется как "Invalid object ID for a user" (см. отчёт об
        ошибке). Перед приглашением проверяем, известен ли candidate ИМЕННО
        этому аккаунту:

        1. client.get_input_entity(candidate.user_id) — резолв через кэш
           этого аккаунта (см. её собственную документацию — для некоторых
           отношений, например контактов, может дорезолвить и без явного
           общего чата; в остальном чистый локальный lookup).
        2. Если не известен и есть username — резолвим этим же аккаунтом
           через client.get_entity(username): единственный надёжный способ
           получить access_hash, валидный именно для этого аккаунта, без
           общего чата с candidate.

        Отдельного хранилища access_hash "на аккаунт" не требуется —
        Telethon сам кэширует результат в .session-файле ЭТОГО аккаунта
        (account.session_path), поэтому следующий прогон того же аккаунта
        для того же пользователя снова попадёт в п.1, без единого RPC.

        ОБЯЗАТЕЛЬНАЯ проверка is_bot ПЕРЕД возвратом (см. задачу об
        инциденте: приглашение Telegram-бота @Vlars_Bot привело к
        3-дневному ограничению приглашений у аккаунта):
        - candidate.is_bot=True — не ожидается (см. _CANDIDATES_BASE_WHERE,
          is_bot=1 уже отсекается на этапе SQL-выборки), но на случай
          гонки/устаревшей выборки — отменяем без единого RPC.
        - candidate.is_bot=False — статус уже подтверждён Telethon при
          последней синхронизации (см. reader/users/sync.py,
          reader/users/history_sync.py, reader/sources/telegram_source.py) —
          повторная проверка не нужна, п.1 отдаёт input_peer как раньше.
        - candidate.is_bot=None (статус неизвестен) — после успешного п.1
          дополнительно убеждаемся ЖИВЫМ, безопасным (не мутирующим, не
          связанным с антиспам-эвристиками самих инвайтов) запросом
          client.get_entity(input_peer) — единственный способ узнать
          User.bot, раз локальный кэш Telethon (get_input_entity) его не
          хранит. Подтверждённый результат (True или False) сразу же
          сохраняется в users.db (см. _update_user_access_hash), чтобы со
          временем NULL исчезали и лишний запрос не повторялся.

        FloodWaitError/PeerFloodError, полученные при резолве, не
        перехватываются здесь — поднимаются в _invite_candidate и
        обрабатываются там точно так же, как если бы случились при самой
        отправке приглашения. Кандидат, которого не удалось резолвить
        никак — _CandidateUnresolvableError; подтверждённый бот —
        _CandidateIsBotError — оба обрабатываются в _invite_candidate через
        единый классификатор (см. _classify_invite_error)."""
        if candidate.is_bot:
            raise _CandidateIsBotError(f"{candidate.user_id} — известный Telegram-бот")

        try:
            input_peer = await client.get_input_entity(candidate.user_id)
        except (FloodWaitError, PeerFloodError):
            raise
        except Exception:
            input_peer = None

        if input_peer is not None and candidate.is_bot is False:
            return input_peer

        if input_peer is None:
            if not candidate.username:
                raise _CandidateUnresolvableError(
                    f"пользователь {candidate.user_id} не известен этому аккаунту "
                    f"и не имеет username для резолва"
                )
            try:
                entity = await client.get_entity(f"@{candidate.username}")
            except (FloodWaitError, PeerFloodError):
                raise
            except Exception as exc:
                raise _CandidateUnresolvableError(
                    f"не удалось резолвить @{candidate.username} этим аккаунтом: {exc}"
                ) from exc
        else:
            # candidate.is_bot is None — статус неизвестен, убеждаемся перед
            # отправкой приглашения (см. докстрок выше).
            try:
                entity = await client.get_entity(input_peer)
            except (FloodWaitError, PeerFloodError):
                raise
            except Exception as exc:
                raise _CandidateUnresolvableError(
                    f"не удалось проверить статус бота у {candidate.user_id}: {exc}"
                ) from exc

        if isinstance(entity, User):
            self._update_user_access_hash(entity)
            if entity.bot:
                raise _CandidateIsBotError(
                    f"{entity.id} — Telegram-бот (подтверждено при резолве), приглашение отменено"
                )

        return InputPeerUser(user_id=entity.id, access_hash=entity.access_hash)

    def _update_user_access_hash(self, entity: User) -> None:
        """Сохраняет свежий access_hash (и username/is_bot, если реально
        изменились) в users.db сразу после успешного get_entity() этим
        аккаунтом (см. _resolve_input_peer). is_bot=bool(entity.bot) —
        подтверждённый статус, сохраняется даже если он False (чтобы
        неизвестные ранее (NULL) пользователи со временем перестали
        требовать повторной проверки, см. _resolve_input_peer). Сбой записи
        не должен мешать самому приглашению — только предупреждение в лог
        (то же самое будет заново получено в следующий раз, просто без
        сохранения в кэш)."""
        if self._user_repository is None:
            return
        try:
            self._user_repository.update_access_hash(
                entity.id, entity.access_hash, getattr(entity, "username", None),
                is_bot=bool(getattr(entity, "bot", False)),
            )
        except Exception:
            logger.warning(
                f"Не удалось обновить access_hash пользователя {entity.id} в users.db",
                exc_info=True,
            )

    def _mark_user_as_bot(self, user_id: int) -> None:
        """Сохраняет is_bot=1 в users.db, когда Telegram подтверждает статус
        бота уже ПОСЛЕ попытки приглашения (см. _classify_invite_error) —
        без полноценного entity (в отличие от _update_user_access_hash).
        Сбой записи не должен мешать пропуску этого кандидата — только
        предупреждение в лог (при следующей выборке он попадёт в кандидаты
        снова и будет пойман тем же механизмом повторно)."""
        if self._user_repository is None:
            return
        try:
            self._user_repository.mark_as_bot(user_id)
        except Exception:
            logger.warning(
                f"Не удалось сохранить is_bot=1 для пользователя {user_id} в users.db",
                exc_info=True,
            )

    async def _warm_up_account(self) -> None:
        """Один раз сразу после connect() — до резолва target_chat и до
        первого приглашения (см. _execute_account), НЕ перед каждым
        кандидатом: только что подключившийся аккаунт, который сразу
        начинает рассылать приглашения, выглядит подозрительно."""
        await asyncio.sleep(random.uniform(_WARMUP_MIN_SECONDS, _WARMUP_MAX_SECONDS))

    async def _pause_between_invites(self) -> None:
        """Случайная пауза между приглашениями (см.
        _choose_invite_pause_seconds) — вещественная, не фиксированная
        задержка, чтобы интервалы между сообщениями аккаунта не выглядели
        равномерными/автоматическими."""
        await asyncio.sleep(_choose_invite_pause_seconds())

    def _record_invite_result(
        self,
        campaign: InviteCampaign,
        account: TelegramAccount,
        candidate: InviteCandidate,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        self._invite_repository.create(
            user_id=candidate.user_id,
            campaign_id=campaign.id,
            account_id=account.id,
            status=status,
            error=error,
            invited_at=datetime.now(timezone.utc) if status == "invited" else None,
        )

    async def _notify_account_result(
        self, campaign: InviteCampaign, account: TelegramAccount, stats: InviteStats,
        elapsed_seconds: float,
    ) -> None:
        remaining = self._invite_repository.count_candidates(campaign.id)
        await self._safe_notify(
            _format_account_notification(campaign, account, stats, remaining, elapsed_seconds)
        )

    async def _notify_campaign_result(
        self, campaign: InviteCampaign, accounts_processed: int, stats: InviteStats, remaining: int,
        elapsed_seconds: float, found_total: int, found_processable: int,
    ) -> None:
        await self._safe_notify(
            _format_campaign_summary_notification(
                campaign, accounts_processed, stats, remaining, elapsed_seconds,
                found_total, found_processable,
            )
        )

    async def _safe_notify(self, text: str) -> None:
        """Три отдельных шага, специально в этом порядке (см. задачу про
        баг: итоговая статистика по кампании пропадала совсем, если у
        OperatorNotifier не было ни одного получателя — text просто
        никуда не логировался, только пытался отправиться):

        1. logger.info(text) — ВСЕГДА, независимо от исхода следующих
           шагов. Отчёт (см. _format_account_notification/
           _format_campaign_summary_notification/
           _format_account_stopped_notification) формируется вызывающим
           кодом ОДИН раз и передаётся сюда уже готовым текстом — эта
           функция не должна знать, как он выглядит, только гарантировать,
           что он не потеряется.
        2. Если notifier не поднят вовсе (main.py не смог создать
           OperatorNotifier) — на этом всё, без исключения.
        3. Попытка отправки оператору — сбой (исключение) или отсутствие
           доставки (OperatorNotifier.notify_text() -> False, например
           "нет ни одного получателя уведомлений") — только warning, само
           содержимое отчёта уже в логе (см. шаг 1) и не теряется."""
        logger.info(text)
        if self._notifier is None:
            return
        try:
            delivered = await self._notifier.notify_text(text)
        except Exception:
            logger.warning("Не удалось отправить уведомление оператору", exc_info=True)
            return
        if not delivered:
            logger.warning(
                "Уведомление оператору не доставлено (нет получателей или сбой отправки)"
            )
