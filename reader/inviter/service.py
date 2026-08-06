import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from telethon.errors import FloodWaitError, PeerFloodError, RPCError, UserAlreadyParticipantError
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.types import Channel, InputPeerUser

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


@dataclass
class InviteStats:
    """Единая структура счётчиков — используется и для отчёта по аккаунту
    (_notify_account_result), и для итогового отчёта по кампании
    (_notify_campaign_result), чтобы не дублировать подсчёт статистики.

    invalid пока всегда 0 — отдельного статуса 'invalid' в
    user_campaign_invites ещё нет (см. задачу); поле уже здесь, чтобы им
    можно было начать пользоваться без изменения структуры счётчиков и
    отчётов, когда такой статус появится.

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
    connect()/get_entity()/disconnect() (dry-run и execute) и __call__()
    (только execute — фактическая отправка запроса Telethon). Ни
    ImportChatInviteRequest, ни какой-либо другой мутирующий метод сверх
    ровно одного приглашения на кандидата здесь не вызывается."""

    async def connect(self) -> None: ...

    async def get_entity(self, entity): ...

    async def __call__(self, request): ...

    async def disconnect(self) -> None: ...


TelegramClientFactory = Callable[[TelegramAccount], DryRunTelegramClient]


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
    elapsed_seconds: float,
) -> str:
    separator = "=" * 32
    return (
        f"{separator}\n\n"
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


def _build_invite_request(target_entity, input_peer: InputPeerUser):
    """InviteToChannelRequest — для каналов/супергрупп (Channel), иначе
    (обычный small group chat) — AddChatUserRequest. Ровно один из двух, ни
    один другой Telegram-мутирующий метод не вызывается."""
    if isinstance(target_entity, Channel):
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

    run(execute=True) — реальные приглашения: ровно один
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
    ):
        self._account_repository = account_repository
        self._campaign_repository = campaign_repository
        self._invite_repository = invite_repository
        self._client_factory = client_factory
        # None — уведомления оператору просто не отправляются (например,
        # если main.py не смог поднять OperatorNotifier); это не должно
        # мешать самим приглашениям, см. _safe_notify().
        self._notifier = notifier

    async def run(self, *, execute: bool = False) -> None:
        """execute=False (по умолчанию) — только dry-run, без единого
        изменения в Telegram (см. _dry_run_account). execute=True — реальные
        приглашения (см. _execute_account); включается только явным
        --execute в reader/inviter/main.py, никогда неявно."""
        campaigns = [c for c in self._campaign_repository.list() if c.enabled]
        accounts = [a for a in self._account_repository.list() if a.enabled]

        if not accounts:
            return

        # Один вызов select_candidates() на кампанию, а не один на аккаунт —
        # иначе каждый аккаунт независимо получал бы кандидатов 1..daily_limit
        # с начала одного и того же отсортированного списка, и несколько
        # аккаунтов раздавали бы приглашения одним и тем же пользователям.
        # Вместо этого выбираем total_limit = SUM(daily_limit) кандидатов ОДИН
        # раз и делим список между аккаунтами последовательно, без пересечений
        # (Account1 — [0:d1], Account2 — [d1:d1+d2], и т.д.).
        total_limit = sum(account.daily_limit for account in accounts)

        for campaign in campaigns:
            campaign_started_at = time.monotonic()
            found = self._invite_repository.count_candidates(campaign.id)
            candidates = self._invite_repository.select_candidates(campaign.id, limit=total_limit)

            campaign_stats = InviteStats()
            accounts_processed = 0

            offset = 0
            for account in accounts:
                account_candidates = candidates[offset : offset + account.daily_limit]
                offset += account.daily_limit
                logger.info(_format_candidates_block(campaign, account, account_candidates, found))
                if execute:
                    account_stats = await self._execute_account(campaign, account, account_candidates)
                    if account_stats is not None:
                        campaign_stats = campaign_stats + account_stats
                        accounts_processed += 1
                else:
                    await self._dry_run_account(campaign, account, account_candidates)

            if execute and accounts_processed:
                remaining = self._invite_repository.count_candidates(campaign.id)
                await self._notify_campaign_result(
                    campaign, accounts_processed, campaign_stats, remaining,
                    time.monotonic() - campaign_started_at,
                )

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
        candidates: list[InviteCandidate],
    ) -> InviteStats | None:
        """Как _dry_run_account (подключение, резолв target_chat, disconnect
        в finally — обрыв одного аккаунта не должен мешать остальным), но
        реально приглашает каждого кандидата (см. _invite_candidate) и
        сохраняет результат в user_campaign_invites сразу после каждого —
        не дожидаясь конца партии.

        Возвращает None, если для этого аккаунта не было ни одного
        кандидата (аккаунт не считается "обработанным" — см. run()), иначе
        накопленную InviteStats; уведомление оператору (см.
        _notify_account_result) отправляется ровно один раз в конце — и при
        обрыве connect()/резолва target_chat тоже (со стартовыми, скорее
        всего нулевыми, счётчиками), и при обычном завершении."""
        if not candidates:
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
                    for candidate in candidates:
                        should_stop = await self._invite_candidate(
                            client, campaign, account, target_entity, candidate, stats,
                        )
                        if should_stop:
                            # PeerFlood или FloodWait >= _MAX_TOLERABLE_FLOOD_WAIT_SECONDS
                            # (см. _invite_candidate) — остальные кандидаты этого
                            # аккаунта не трогаем, переходим к следующему аккаунту.
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
        обработку ОСТАЛЬНЫХ кандидатов (PeerFloodError или FloodWaitError
        >= _MAX_TOLERABLE_FLOOD_WAIT_SECONDS — см. run()/_execute_account) —
        обрыв connect() отдельно этого аккаунта уже обрабатывается выше по
        стеку, здесь же он не встречается. Во всех остальных случаях —
        False, и после случайной паузы (_MIN_INVITE_PAUSE_SECONDS..
        _MAX_INVITE_PAUSE_SECONDS) обработка продолжается со следующего
        кандидата."""
        user_label = f"{candidate.user_id} {_format_username(candidate.username)}"
        input_peer = InputPeerUser(user_id=candidate.user_id, access_hash=candidate.access_hash)
        request = _build_invite_request(target_entity, input_peer)

        try:
            await client(request)
        except UserAlreadyParticipantError:
            # Кандидат уже состоит в target_chat — цель кампании для него уже
            # достигнута: status='invited', чтобы select_candidates() больше
            # не выбирал его для этой кампании повторно (см. требование о
            # UserAlreadyParticipantError в задаче).
            logger.info(
                _format_execute_block(
                    account, user_label, campaign.target_chat,
                    status="invited", reason="уже состоит в target_chat",
                )
            )
            self._record_invite_result(campaign, account, candidate, status="invited")
            stats.already_participant += 1
            await self._pause_between_invites()
            return False
        except PeerFloodError as exc:
            # Telegram считает поведение аккаунта похожим на спам —
            # продолжать приглашать этим же аккаунтом дальше только усугубит
            # ситуацию (риск бана). Останавливаем именно этот аккаунт, без
            # паузы (смысла ждать нет — переходим к следующему аккаунту), и
            # уведомляем оператора отдельно от обычной статистики.
            logger.warning(
                _format_execute_block(
                    account, user_label, campaign.target_chat, status="failed", reason=str(exc),
                )
            )
            self._record_invite_result(campaign, account, candidate, status="failed", error=str(exc))
            stats.errors += 1
            await self._safe_notify(
                _format_account_stopped_notification(account, "Получен PeerFlood.")
            )
            return True
        except FloodWaitError as exc:
            logger.warning(
                _format_execute_block(
                    account, user_label, campaign.target_chat, status="failed",
                    reason=f"FloodWaitError: жду {exc.seconds} сек.",
                )
            )
            self._record_invite_result(
                campaign, account, candidate, status="failed",
                error=f"FloodWaitError: {exc.seconds} сек.",
            )
            # Не увеличиваем errors — это общее временное ограничение API, а
            # не отказ конкретному пользователю (см. InviteStats).
            if exc.seconds >= _MAX_TOLERABLE_FLOOD_WAIT_SECONDS:
                await self._safe_notify(
                    _format_account_stopped_notification(
                        account, f"FloodWait: {exc.seconds} сек.",
                    )
                )
                return True
            await asyncio.sleep(exc.seconds)
            return False
        except RPCError as exc:
            logger.warning(
                _format_execute_block(
                    account, user_label, campaign.target_chat, status="failed", reason=str(exc),
                )
            )
            self._record_invite_result(campaign, account, candidate, status="failed", error=str(exc))
            stats.errors += 1
            await self._pause_between_invites()
            return False
        except Exception as exc:
            logger.warning(
                _format_execute_block(
                    account, user_label, campaign.target_chat, status="failed", reason=str(exc),
                )
            )
            self._record_invite_result(campaign, account, candidate, status="failed", error=str(exc))
            stats.errors += 1
            await self._pause_between_invites()
            return False
        else:
            logger.info(
                _format_execute_block(account, user_label, campaign.target_chat, status="invited")
            )
            self._record_invite_result(campaign, account, candidate, status="invited")
            stats.invited += 1
            await self._pause_between_invites()
            return False

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
        elapsed_seconds: float,
    ) -> None:
        await self._safe_notify(
            _format_campaign_summary_notification(
                campaign, accounts_processed, stats, remaining, elapsed_seconds,
            )
        )

    async def _safe_notify(self, text: str) -> None:
        """Сбой уведомления оператору (в т.ч. отсутствие notifier вовсе) не
        должен останавливать сам сервис приглашений — только логируется."""
        if self._notifier is None:
            return
        try:
            await self._notifier.notify_text(text)
        except Exception:
            logger.warning("Не удалось отправить уведомление оператору", exc_info=True)
