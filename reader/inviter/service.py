import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    UserNotParticipantError,
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
# pending/joined/failed/invalid), чтобы аккаунт не рассылал
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

# После основной волны приглашений (см. _execute_account) ждём этот
# интервал ПЕРЕД проверкой pending (см. _verify_pending_invites) — Telegram
# может обрабатывать вступление не мгновенно, особенно для больших пачек.
# Порог — по количеству реально ОТПРАВЛЕННЫХ (успешных) приглашений этой
# волны, не по числу кандидатов вообще (см. задачу).
_PENDING_CHECK_BATCH_THRESHOLD = 20
_PENDING_CHECK_SHORT_WAIT_SECONDS = 60
_PENDING_CHECK_LONG_WAIT_SECONDS = 300

# --test (main.py) — тестовый прогон: как только текущий вызов run()
# выполнит это количество успешно ОТПРАВЛЕННЫХ приглашений (status='pending',
# см. InviteStats.sent — RPC-успех, не подтверждённое вступление) —
# остановить ВЕСЬ запуск (текущий аккаунт заканчивается штатно, без
# прерывания уже выполняющегося приглашения, остальные аккаунты и кампании
# больше не трогаются, см. run()/_invite_candidate). already_participant/
# errors в счёт не идут.
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

    Бизнесу важны реально вступившие пользователи, а не успешные RPC (см.
    задачу про подтверждение приглашений) — поэтому "успех" разделён на
    два счётчика вместо одного:

    sent — успешный InviteToChannelRequest/AddChatUserRequest, статус
    'pending' (Telegram принял приглашение, но участие ещё не проверено).
    joined — участие ДЕЙСТВИТЕЛЬНО подтверждено: либо через проверку
    pending (см. _verify_pending_invites), либо сразу же по
    UserAlreadyParticipantError (кандидат уже состоял в группе — тоже
    подтверждённый участник, см. _classify_invite_error).
    pending — из отправленных этой сессией (sent) осталось неподтверждённых
    на момент завершения _execute_account (после проверки основной волны
    и/или вся вторая волна, которую мы уже не проверяем, см. задачу: "после
    дополнительной волны повторных циклов проверки не делать").

    invalid — кандидат, про которого достоверно известно, что приглашать
    его нельзя ПРИНЦИПИАЛЬНО (сейчас — только подтверждённый Telegram-бот,
    см. _classify_invite_error/_CandidateIsBotError) — status='invalid' в
    user_campaign_invites, отдельно от обычных failed/errors.

    FloodWaitError сюда не попадает ни в одно поле (ни sent, ни errors):
    это не отказ конкретному пользователю, а общее временное ограничение
    API (см. _invite_candidate) — кандидат остаётся кандидатом для
    следующего прогона, а не считается ни успехом, ни ошибкой."""

    sent: int = 0
    joined: int = 0
    pending: int = 0
    invalid: int = 0
    errors: int = 0

    def __add__(self, other: "InviteStats") -> "InviteStats":
        return InviteStats(
            sent=self.sent + other.sent,
            joined=self.joined + other.joined,
            pending=self.pending + other.pending,
            invalid=self.invalid + other.invalid,
            errors=self.errors + other.errors,
        )


class DryRunTelegramClient(Protocol):
    """Подмножество TelegramClient, которое использует InviterService —
    connect()/get_entity()/get_input_entity()/disconnect() (dry-run и
    execute) и __call__() (только execute — фактическая отправка запроса
    Telethon). get_input_entity() используется только в execute (см.
    _resolve_input_peer) — dry-run её не трогает. get_permissions() —
    только для проверки pending (см. _verify_pending_invites), тоже не
    мутирующий вызов. Ни ImportChatInviteRequest, ни какой-либо другой
    мутирующий метод сверх ровно одного приглашения на кандидата здесь не
    вызывается."""

    async def connect(self) -> None: ...

    async def get_entity(self, entity): ...

    async def get_input_entity(self, entity): ...

    async def get_permissions(self, entity, user): ...

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
        f"📤 Отправлено приглашений: {stats.sent}\n"
        f"✅ Подтверждено участников: {stats.joined}\n"
        f"⏳ Ожидают подтверждения: {stats.pending}\n"
        f"🚫 Недоступны (invalid): {stats.invalid}\n"
        f"❌ Ошибок: {stats.errors}\n\n"
        f"Осталось кандидатов: {remaining}"
    )


def _format_campaign_summary_notification(
    campaign: InviteCampaign, accounts_processed: int, stats: InviteStats, remaining: int,
    elapsed_seconds: float, found_total: int, found_processable: int, accounts_blocked: int = 0,
) -> str:
    """found_total — сколько подошло по keyword/access_hash/ещё-не-приглашён,
    БЕЗ фильтра по username (UserCampaignInviteRepository.
    count_found_candidates()); found_processable — то же самое, но С этим
    фильтром (count_candidates(), как и раньше). Разница между ними — те,
    кого нашли, но подготовить к приглашению физически нельзя (нет
    username для резолва этим аккаунтом, см.
    InviterService._resolve_input_peer) — считается прямо здесь, простым
    вычитанием двух уже посчитанных в SQL чисел, а не перебором
    пользователей в Python.

    accounts_blocked — сколько enabled-аккаунтов этой кампании были
    полностью пропущены из-за активного account.blocked_until (см.
    _is_blocked_by_flood_wait/InviterService.run()) — не входят в
    accounts_processed (см. _execute_account: такой аккаунт возвращает
    None, как и при отсутствии сессии/лимита)."""
    skipped_without_username = found_total - found_processable
    separator = "=" * 32
    blocked_line = (
        f"⏸ Пропущено из-за FloodWait: {accounts_blocked}\n\n" if accounts_blocked else ""
    )
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
        f"{blocked_line}"
        f"📤 Отправлено приглашений: {stats.sent}\n"
        f"✅ Подтверждено участников: {stats.joined}\n"
        f"⏳ Ожидают подтверждения: {stats.pending}\n"
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


def _is_blocked_by_flood_wait(account: TelegramAccount, now: datetime) -> bool:
    """enabled=False (аккаунт отключён оператором) и blocked_until
    (временное ограничение самого Telegram, см. _persist_flood_wait_block)
    — независимые условия (см. TelegramAccount) — эта проверка касается
    только blocked_until. Просроченный blocked_until (<= now) считается
    снятым автоматически — никакого ручного enabled=True не требуется, и
    саму запись очищать необязательно (см. задачу)."""
    return account.blocked_until is not None and account.blocked_until > now


def _format_blocked_account_message(account: TelegramAccount, now: datetime) -> str:
    assert account.blocked_until is not None
    remaining_seconds = max((account.blocked_until - now).total_seconds(), 0.0)
    return (
        f"Account {account.name} пропущен:\n"
        f"Telegram FloodWait действует до "
        f"{account.blocked_until.strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"Осталось: {_format_duration(remaining_seconds)}"
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
    поле InviteStats увеличить ("sent"/"joined"/"pending"/"invalid"/
    "errors"; None — не увеличивать ничего, см. FloodWaitError).
    operator_message — человекочитаемый текст ТОЛЬКО для операторского
    уведомления при STOP_ACCOUNT/FATAL (см. _format_account_stopped_notification);
    в логах и в user_campaign_invites.error всегда остаётся str(exc) без
    изменений (см. InviterService._handle_invite_error).
    mark_as_bot — сохранить is_bot=1 в users.db (см.
    InviterService._mark_user_as_bot), чтобы этот кандидат больше никогда
    не попадал в выборку ни для одной кампании.
    mark_verified_now — db_status="joined" получает verified_at=now сразу
    же (см. UserAlreadyParticipantError — кандидат уже состоял в группе,
    подтверждать через GetParticipantRequest/get_permissions нечего, это
    уже само по себе подтверждение).
    wait_seconds — задаётся ТОЛЬКО для FloodWaitError (exc.seconds, в обоих
    исходах — RETRY_LATER и STOP_ACCOUNT), и используется двояко (см.
    InviterService._handle_invite_error): как длительность
    asyncio.sleep() при RETRY_LATER, и — независимо от action — как основа
    для account.blocked_until = now + wait_seconds (см.
    _persist_flood_wait_block), чтобы следующий запуск не повторил попытку
    этим аккаунтом раньше времени. Ни для одной другой ошибки (в т.ч.
    PeerFloodError — Telegram не сообщает точное время окончания
    ограничения) не задаётся — специально, чтобы не изобретать
    blocked_until там, где Telegram не дал на это оснований (см. задачу)."""

    action: InviteErrorAction
    db_status: str
    stat_field: str | None
    operator_message: str = ""
    wait_seconds: float | None = None
    mark_as_bot: bool = False
    mark_verified_now: bool = False


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
        # Уже состоит в группе — это ТОЖЕ подтверждённый участник, просто
        # без отдельной проверки через GetParticipantRequest/get_permissions
        # (Telegram уже сказал прямо) — joined сразу же, а не pending.
        return InviteErrorClassification(
            InviteErrorAction.SKIP_USER, db_status="joined", stat_field="joined",
            mark_verified_now=True,
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
    факту из БД (daily_limit минус уже ПОДТВЕРЖДЁННЫЕ сегодня участники
    этого аккаунта по всем кампаниям, см.
    UserCampaignInviteRepository.count_today_joined) — без единого
    счётчика в памяти: бизнесу важны реально вступившие, а не успешные
    RPC (см. задачу).

    Каждое приглашение — ровно один InviteToChannelRequest/AddChatUserRequest
    на кандидата (см. _build_invite_request), с немедленной записью
    status='pending' (не 'invited' — участие ещё не подтверждено) сразу
    после каждого кандидата, и случайной паузой после каждого (см.
    _pause_between_invites). После основной волны — пауза (60 сек. если
    отправлено < 20, иначе 5 мин., см. _wait_before_verifying_pending) и
    проверка всех pending (см. _verify_pending_invites): подтверждённые
    становятся 'joined'. Если после этого остаётся резерв дневного
    лимита — ровно одна дополнительная волна добора, без повторной
    проверки (см. _execute_account, максимум две волны за запуск).

    FloodWaitError < _MAX_TOLERABLE_FLOOD_WAIT_SECONDS ждётся (exc.seconds)
    и не прерывает ни аккаунт, ни весь сервис; FloodWaitError >=
    _MAX_TOLERABLE_FLOOD_WAIT_SECONDS и PeerFloodError останавливают
    текущий аккаунт (с уведомлением оператора, см.
    _format_account_stopped_notification) и переходят к следующему — в
    этом случае волна добора и проверка pending для этого аккаунта в этот
    раз не выполняются; сбой самого аккаунта (например, обрыв connect())
    не мешает остальным.

    ЛЮБОЙ FloodWaitError (независимо от того, ждём мы его или он
    останавливает аккаунт) сохраняет account.blocked_until = now +
    exc.seconds в БД (см. _persist_flood_wait_block) — перед обработкой
    КАЖДОГО аккаунта (execute и dry-run) _is_blocked_by_flood_wait
    проверяет это поле ДО connect()/резолва target_chat/выборки
    кандидатов и, если блокировка ещё активна, полностью пропускает
    аккаунт (см. _execute_account/_dry_run_account) — именно это не даёт
    уже заблокированному Telegram аккаунту повторно попытаться пригласить
    в СЛЕДУЮЩЕМ запуске (см. задачу про повторный FloodWait у
    @Mihailov_vm). enabled=False (отключён оператором) и blocked_until
    (временное ограничение самого Telegram) — независимые условия;
    просроченный blocked_until снимается автоматически, без участия
    оператора. PeerFloodError не даёт Telegram точного времени окончания
    ограничения — blocked_until для него не изобретается (см.
    _classify_invite_error/InviteErrorClassification.wait_seconds).

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
          пишет status='pending' (см. _record_invite_result), поэтому
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

        # Счётчик успешно ОТПРАВЛЕННЫХ приглашений (status='pending', RPC-
        # успех) именно этого вызова run() — сбрасывается на каждый запуск,
        # см. TEST_MODE_MAX_SUCCESSFUL_INVITES/_invite_candidate.
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
            accounts_blocked = 0

            if execute:
                for account in accounts:
                    if _is_blocked_by_flood_wait(account, datetime.now(timezone.utc)):
                        accounts_blocked += 1
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
                    found_total, found, accounts_blocked,
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

        now = datetime.now(timezone.utc)
        if _is_blocked_by_flood_wait(account, now):
            logger.info(_format_blocked_account_message(account, now))
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

    def _remaining_daily_budget(self, account: TelegramAccount) -> int:
        """daily_limit минус joined_today минус pending_today — а НЕ
        только минус joined_today (см. задачу про перелив лимита).

        pending — уже отправленное сегодня приглашение, которое пока не
        подтверждено (joined) и не опровергнуто (not_joined, см.
        _verify_pending_invites) — оно ВРЕМЕННО резервирует место в
        дневном лимите: пока неизвестно, чем закончится приглашение, это
        место нельзя отдать под нового кандидата, иначе суммарно
        joined + pending может превысить daily_limit. Место освобождается
        только когда pending становится joined (место остаётся занятым —
        просто по другой причине) или not_joined (место освобождается
        по-настоящему — Telegram достоверно подтвердил, что участия не
        произошло). Используется и для расчёта ПЕРЕД основной волной, и
        для расчёта ПЕРЕД единственной волной добора (см.
        _execute_account) — одна и та же формула в обоих местах."""
        joined = self._invite_repository.count_today_joined(account.id)
        pending = self._invite_repository.count_today_pending(account.id)
        return account.daily_limit - joined - pending

    async def _execute_account(
        self,
        campaign: InviteCampaign,
        account: TelegramAccount,
        found: int,
    ) -> InviteStats | None:
        """Порядок специально такой (см. задачу про бесполезную выборку
        кандидатов для неработающих аккаунтов и про daily_limit, который
        должен считаться по подтверждённым участникам, а не по успешным RPC):

        0. account.blocked_until (см. _is_blocked_by_flood_wait) — активный
           FloodWait самого Telegram (см. _persist_flood_wait_block) — САМАЯ
           первая проверка, даже перед .session-файлом: пока блокировка не
           истекла, аккаунт не должен ни подключаться, ни резолвить
           target_chat, ни выбирать кандидатов вообще (см. задачу про
           повторный FloodWait у @Mihailov_vm). Не путать с enabled=False
           (отключён оператором вручную) — это отдельное, самостоятельное
           условие (см. TelegramAccount), снимается автоматически по
           истечении blocked_until, без участия оператора.
        1. .session-файл (см. _default_session_checker) — самая дешёвая из
           оставшихся проверок, без единого запроса вообще.
        2. found == 0 — во всей кампании нет ни одного кандидата, ни один
           аккаунт не должен даже подключаться (см. test_execute_does_not_
           reinvite_user_on_next_run).
        3. Остаток дневного лимита ЭТОГО аккаунта (см. _remaining_daily_budget
           — daily_limit минус joined_today минус pending_today, без
           единого счётчика в памяти) — если <= 0, аккаунт полностью
           пропущен, тоже без единого SQL-запроса выборки кандидатов.
        4. connect(), резолв campaign.target_chat (убедиться, что аккаунтом
           вообще можно приглашать в эту группу/канал) — если аккаунт не
           может приглашать, кандидаты не выбираются вовсе.
        5. Основная волна (см. _run_invite_wave) — до remaining кандидатов.
        6. Если волна не была прервана (STOP_ACCOUNT/FATAL/лимит тестового
           режима) — подождать (см. _wait_before_verifying_pending, по
           числу реально отправленных) и проверить всех pending этого
           аккаунта в этой кампании (см. _verify_pending_invites), включая
           оставшихся с прошлых прогонов.
        7. Пересчитать остаток лимита ПОСЛЕ проверки (см.
           _remaining_daily_budget — уже с учётом того, что часть pending
           стала joined/not_joined) и, если он > 0, выполнить РОВНО ОДНУ
           дополнительную волну добора (см. задачу: максимум две волны за
           запуск, без повторной проверки после второй).

        Возвращает None, если аккаунт был пропущен ПОЛНОСТЬЮ (сессии нет,
        found == 0 или остаток лимита исчерпан — не считается
        "обработанным", см. run()), иначе накопленную InviteStats —
        уведомление оператору (см. _notify_account_result) отправляется
        ровно один раз в конце — и при обрыве connect()/резолва
        target_chat тоже (со стартовыми, скорее всего нулевыми,
        счётчиками), и при обычном завершении. stats.pending в конце —
        ВСЕГДА текущее фактическое число pending в БД для этого аккаунта и
        кампании (а не накопленный счётчик), независимо от того, как
        далеко продвинулось выполнение в этот раз."""
        now = datetime.now(timezone.utc)
        if _is_blocked_by_flood_wait(account, now):
            logger.info(_format_blocked_account_message(account, now))
            return None

        if not self._session_checker(account):
            logger.warning(_format_missing_session_message(account))
            return None

        if found == 0:
            return None

        remaining = self._remaining_daily_budget(account)
        if remaining <= 0:
            logger.info(
                f"[EXECUTE]\nAccount: {account.name}\n"
                f"Дневной лимит уже выполнен сегодня (с учётом joined и "
                f"ещё не подтверждённых pending) — аккаунт пропущен, "
                f"выборка кандидатов не выполняется."
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

                    # Кандидаты, уже обработанные ЭТИМ вызовом
                    # _execute_account (см. _run_invite_wave) — волна
                    # добора не должна тут же повторно пытаться того же
                    # кандидата, который только что провалился/оказался
                    # ботом (status='failed'/'invalid' сами по себе не
                    # исключаются из select_candidates() — это специально,
                    # чтобы они остались кандидатами для СЛЕДУЮЩЕГО прогона).
                    attempted_user_ids: set[int] = set()

                    # Волна №1 (основная) — лимит = остаток дневного лимита
                    # на момент старта (см. докстрок выше).
                    stopped = await self._run_invite_wave(
                        client, campaign, account, target_entity, remaining, found, stats,
                        attempted_user_ids,
                    )

                    if not stopped:
                        await self._wait_before_verifying_pending(stats.sent)
                        await self._verify_pending_invites(
                            client, campaign, account, target_entity, stats,
                        )

                        top_up_remaining = self._remaining_daily_budget(account)
                        if top_up_remaining > 0:
                            # Волна №2 (добор) — РОВНО одна, без повторной
                            # проверки/ожидания после неё (см. докстрок).
                            await self._run_invite_wave(
                                client, campaign, account, target_entity,
                                top_up_remaining, found, stats, attempted_user_ids,
                            )

                    # Фактическое число pending прямо сейчас — включает и
                    # неподтверждённые из волны №1, и всю волну №2 (её мы
                    # не проверяем в этом запуске, см. докстрок).
                    stats.pending = len(
                        self._invite_repository.list_pending(account.id, campaign.id)
                    )
        finally:
            await client.disconnect()

        await self._notify_account_result(campaign, account, stats, time.monotonic() - started_at)
        return stats

    async def _run_invite_wave(
        self,
        client: DryRunTelegramClient,
        campaign: InviteCampaign,
        account: TelegramAccount,
        target_entity,
        limit: int,
        found: int,
        stats: InviteStats,
        attempted_user_ids: set[int],
    ) -> bool:
        """Одна волна: выбрать до limit кандидатов (см.
        UserCampaignInviteRepository.select_candidates) и пригласить
        каждого (см. _invite_candidate) — используется и для основной
        волны, и для волны добора (см. _execute_account).

        attempted_user_ids — user_id, уже обработанные ЭТИМ вызовом
        _execute_account (в т.ч. предыдущей волной) — исключаются из ЭТОЙ
        волны в Python, а не в SQL: select_candidates() не исключает
        status='failed'/'invalid'/'not_joined' (это специально — они
        остаются кандидатами для СЛЕДУЮЩЕГО прогона, см. задачу), но волна
        добора не должна тут же, в рамках одного запуска, повторно
        пытаться того же кандидата, который только что провалился,
        оказался ботом или не подтвердил участие при проверке.
        Пополняется прямо здесь.

        limit + len(attempted_user_ids) запрашивается у select_candidates()
        (а не просто limit) — иначе, если самые "свежие" (last_seen_at
        DESC) записи в БД совпадут с уже обработанными в этом запуске (см.
        выше — они не исключены на уровне SQL), волна добора могла бы
        получить меньше limit НОВЫХ кандидатов, хотя более старые (но
        ещё не тронутые) в пуле есть. Запас размером len(attempted_user_ids)
        гарантирует, что после фильтрации в Python останется как минимум
        limit кандидатов, если они вообще существуют.

        Возвращает True, если аккаунт должен немедленно прекратить
        обработку ОСТАЛЬНЫХ кандидатов ЭТОЙ волны (см.
        _classify_invite_error — STOP_ACCOUNT/FATAL, либо достигнут лимит
        успешных отправок тестового режима, см. _invite_candidate) —
        вызывающий код (_execute_account) тогда не выполняет ни проверку
        pending, ни волну добора."""
        raw_candidates = self._invite_repository.select_candidates(
            campaign.id, limit=limit + len(attempted_user_ids),
        )
        candidates = [c for c in raw_candidates if c.user_id not in attempted_user_ids][:limit]
        logger.info(_format_candidates_block(campaign, account, candidates, found))
        for candidate in candidates:
            attempted_user_ids.add(candidate.user_id)
            should_stop = await self._invite_candidate(
                client, campaign, account, target_entity, candidate, stats,
            )
            if should_stop:
                return True
        return False

    async def _wait_before_verifying_pending(self, sent_count: int) -> None:
        """После волны приглашений (см. _execute_account) — ждём, чтобы
        Telegram успел обработать вступление, прежде чем проверять pending
        (см. _verify_pending_invites): большие пачки Telegram может
        обрабатывать не мгновенно (см. задачу), поэтому им — больше
        времени. sent_count — сколько реально ОТПРАВЛЕНО (успешных
        InviteToChannelRequest/AddChatUserRequest) именно этой волной, а
        не число кандидатов вообще."""
        wait_seconds = (
            _PENDING_CHECK_LONG_WAIT_SECONDS
            if sent_count >= _PENDING_CHECK_BATCH_THRESHOLD
            else _PENDING_CHECK_SHORT_WAIT_SECONDS
        )
        await asyncio.sleep(wait_seconds)

    async def _verify_pending_invites(
        self,
        client: DryRunTelegramClient,
        campaign: InviteCampaign,
        account: TelegramAccount,
        target_entity,
        stats: InviteStats,
    ) -> None:
        """Для каждого status='pending' этого аккаунта в этой кампании (см.
        UserCampaignInviteRepository.list_pending — включая оставшихся с
        прошлых прогонов, если процесс прерывался между отправкой и
        проверкой) — проверяет, стал ли кандидат подтверждённым участником
        через client.get_permissions(target_entity, user): тот же метод,
        которым сам Telethon реализует и GetParticipantRequest (для
        каналов/супергрупп), и проверку через GetFullChatRequest (для
        обычных group chat) — единый API, не завязанный на тип
        target_entity (см. задачу: "GetParticipantRequest или наиболее
        подходящий Telethon API").

        Три исхода — и только UserNotParticipantError освобождает
        зарезервированное этим pending место в дневном лимите (см.
        _remaining_daily_budget/count_today_pending и задачу про перелив
        лимита: место освобождается ТОЛЬКО когда мы ДОСТОВЕРНО знаем, что
        участия не произошло, а не при любом сбое проверки):

        - Подтверждён (get_permissions успешна) — status='joined',
          verified_at=сейчас, stats.joined. Место в лимите остаётся
          занятым — просто теперь учитывается count_today_joined, а не
          count_today_pending.
        - UserNotParticipantError — Telegram ЯВНО говорит "не участник":
          status='not_joined', verified_at=сейчас — единственный случай,
          когда занятое место по-настоящему освобождается (not_joined не
          считается ни в count_today_joined, ни в count_today_pending).
          Как и 'failed', не исключается из будущей выборки — кандидат
          может быть приглашён повторно в СЛЕДУЮЩЕМ прогоне.
        - Любой другой сбой самой проверки (сеть, таймаут и т.п.) — мы
          НЕ уверены, остаётся 'pending' без изменений, место остаётся
          зарезервированным; итоговое stats.pending считает
          _execute_account по факту из БД, а не здесь."""
        pending = self._invite_repository.list_pending(account.id, campaign.id)
        for invite in pending:
            try:
                user_ref = await client.get_input_entity(invite.user_id)
                await client.get_permissions(target_entity, user_ref)
            except UserNotParticipantError:
                self._invite_repository.update(
                    invite.id, status="not_joined", verified_at=datetime.now(timezone.utc),
                )
                continue
            except Exception as exc:
                logger.warning(
                    f"[EXECUTE]\nAccount: {account.name}\nUser: {invite.user_id}\n"
                    f"Не удалось проверить участие в группе: {exc}"
                )
                continue

            self._invite_repository.update(
                invite.id, status="joined", verified_at=datetime.now(timezone.utc),
            )
            stats.joined += 1

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
            # Telegram принял приглашение — это ещё не подтверждённое
            # участие (см. задачу): status='pending', пока
            # _verify_pending_invites не подтвердит вступление.
            logger.info(
                _format_execute_block(account, user_label, campaign.target_chat, status="pending")
            )
            self._record_invite_result(campaign, account, candidate, status="pending")
            stats.sent += 1
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

        if classification.wait_seconds is not None:
            # Только FloodWaitError задаёт wait_seconds (см.
            # InviteErrorClassification.wait_seconds) — сохраняем
            # blocked_until ДО записи результата и уведомления, чтобы обе
            # ссылались на уже обновлённый account (см. ниже).
            account = self._persist_flood_wait_block(account, classification.wait_seconds)

        log_fn = logger.info if classification.db_status in ("joined", "invalid") else logger.warning
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
            error=None if classification.db_status == "joined" else raw_text,
            verified_at=datetime.now(timezone.utc) if classification.mark_verified_now else None,
        )
        if classification.stat_field is not None:
            setattr(stats, classification.stat_field, getattr(stats, classification.stat_field) + 1)

        if classification.action == InviteErrorAction.RETRY_LATER:
            await asyncio.sleep(classification.wait_seconds)
            return False

        if classification.action in (InviteErrorAction.STOP_ACCOUNT, InviteErrorAction.FATAL):
            operator_message = classification.operator_message
            if account.blocked_until is not None:
                operator_message = (
                    f"{operator_message}\n\n"
                    f"Аккаунт заблокирован до "
                    f"{account.blocked_until.strftime('%Y-%m-%d %H:%M')} UTC."
                )
            await self._safe_notify(
                _format_account_stopped_notification(account, operator_message)
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

    def _persist_flood_wait_block(
        self, account: TelegramAccount, wait_seconds: float,
    ) -> TelegramAccount:
        """FloodWaitError — единственная ошибка, для которой Telegram
        сообщает точное время окончания ограничения (exc.seconds, см.
        InviteErrorClassification.wait_seconds) — только для неё
        сохраняем account.blocked_until, чтобы ни следующий запуск, ни
        следующий аккаунт в ЭТОМ же запуске (см. _execute_account/
        _dry_run_account) не пытались снова подключиться этим аккаунтом
        раньше времени (см. задачу про повторный FloodWait). Сбой записи
        не должен прерывать обработку ошибки — только предупреждение в
        лог, с исходным (не обновлённым) account."""
        blocked_until = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
        try:
            return self._account_repository.update(
                account.id, blocked_until=blocked_until, blocked_reason="flood_wait",
            )
        except Exception:
            logger.warning(
                f"Не удалось сохранить blocked_until для аккаунта {account.name}", exc_info=True,
            )
            return account

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
        verified_at: datetime | None = None,
    ) -> None:
        """invited_at — момент, когда InviteToChannelRequest/AddChatUserRequest
        был принят Telegram (status='pending'/'joined' — оба начинаются
        отправкой); verified_at — момент ДЕЙСТВИТЕЛЬНОГО подтверждения
        участия (только для status='joined', см. _classify_invite_error/
        _verify_pending_invites)."""
        self._invite_repository.create(
            user_id=candidate.user_id,
            campaign_id=campaign.id,
            account_id=account.id,
            status=status,
            error=error,
            invited_at=datetime.now(timezone.utc) if status in ("pending", "joined") else None,
            verified_at=verified_at,
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
        elapsed_seconds: float, found_total: int, found_processable: int, accounts_blocked: int = 0,
    ) -> None:
        await self._safe_notify(
            _format_campaign_summary_notification(
                campaign, accounts_processed, stats, remaining, elapsed_seconds,
                found_total, found_processable, accounts_blocked,
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
