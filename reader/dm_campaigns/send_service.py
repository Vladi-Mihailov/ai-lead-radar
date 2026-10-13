"""Phase 3C — реальная отправка ЛС, ТОЛЬКО по явному действию trusted-
оператора (inviter_admin_bot: ✅ Отправить -> подтверждение -> send()).
Фонового отправителя нет: ничто не проходит по очереди само, approved-строки
(решения до Phase 3C) не отправляются никогда — отправка только из draft.

plan() — проверки по БД без сети:
  строка draft и не синтетическая; кампания в режиме manual; получатель
  определён; кулдаун получателя по ВСЕМ кампаниям (recipient_cooldown);
  отправитель — только из allowlist кампании: can_send_dm, не is_old, не
  заблокирован Telegram (blocked_until) и DM-состоянием (PeerFlood/
  FloodWait/неавторизованная сессия, см. sender_state.py), Premium для
  premium_required, session-файл есть, лимиты за скользящие 24 ч: общий
  потолок аккаунта (sender_daily_cap; None/0 — без потолка) и лимит пары
  кампания-аккаунт, если задан. telegram_accounts.enabled (включатель
  инвайтера) здесь не учитывается.

send() — под asyncio.Lock (одна отправка за раз в процессе): повтор plan()
-> атомарный claim draft->sending (второй клик получает "уже обработан") ->
connect -> is_user_authorized -> резолв получателя ВЫБРАННЫМ отправителем
(username: resolve + сверка числового id; source_message: исходное
сообщение читается самим отправителем, peer = InputPeerUserFromMessage,
чужой access_hash не используется и не хранится) -> ОДИН send_message ->
sent.

Доступ к source-чату проверяется самим резолвом (без send_message и без
проверки членства: публичный чат, который читается, подходит). Если
отправитель не смог прочитать исходное сообщение (sender_not_in_source_chat),
строка в том же статусе sending переписывается на следующего кандидата из
plan() (reassign_sender) и попытка продолжается; если не смог никто —
строка возвращается в draft с no_eligible_sender_for_source. Фолбэк только
для этой причины. Исходы ошибок:
  - FloodWait / PeerFlood / сбой до send_message — отправки точно не было:
    строка возвращается в draft (FloodWait — пауза отправителя до времени,
    PeerFlood — отправитель блокируется до ручной проверки);
  - приватность / блокировка / удалённый аккаунт / неверный peer, нет
    доступа к исходной группе, расхождение identity — blocked;
  - иной RPCError — send_failed;
  - транспорт упал ВО ВРЕМЯ send_message — send_failed с "uncertain…":
    исход неизвестен, автоматически не повторяется и считается отправленным
    для кулдауна/лимитов.
Клиент отправителя обязан иметь flood_sleep_threshold=0 и request_retries=1
(см. sender_client.py): ни сна на FloodWait внутри callback, ни скрытого
повтора запроса."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telethon import errors
from telethon.tl.types import InputPeerUserFromMessage

from reader.dm_campaigns.models import MODE_MANUAL
from reader.dm_campaigns.outreach_repository import STATUS_DRAFT, UNCERTAIN_PREFIX, DmOutreach, DmOutreachRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.sender_state import DmSenderStateRepository
from reader.dm_campaigns.sendability import (
    SENDABILITY_PREMIUM_REQUIRED,
    SENDABILITY_SOURCE_MESSAGE,
    SENDABILITY_USERNAME,
)
from reader.dm_campaigns.sender_selection import (  # noqa: F401 — коды причин реэкспортируются
    DAILY_LIMIT,
    NO_ELIGIBLE_SENDER,
    NO_PREMIUM_SENDER,
    SENDER_FLOOD_WAIT,
    SenderSelector,
)

logger = logging.getLogger(__name__)

# ---- причины (коды хранятся в dm_outreach.send_error) ----
SYNTHETIC = "synthetic"
NOT_FOUND = "not_found"
NOT_DRAFT = "not_draft"
CAMPAIGN_NOT_MANUAL = "campaign_not_manual"
NO_RECIPIENT = "no_recipient"
NO_TEXT = "no_text"
RECENT_DM = "blocked_recent_dm"
SENDER_UNAUTHORIZED = "sender_session_unauthorized"
NOT_IN_SOURCE_CHAT = "sender_not_in_source_chat"
NO_SENDER_FOR_SOURCE = "no_eligible_sender_for_source"
STATE_CHANGED = "send_state_changed"
IDENTITY_MISMATCH = "recipient_identity_mismatch"
USERNAME_NOT_FOUND = "username_not_found"
PRIVACY = "user_privacy_restricted"
USER_BLOCKED = "user_blocked_sender"
CHAT_WRITE_FORBIDDEN = "chat_write_forbidden"
PEER_INVALID = "peer_id_invalid"
USER_DEACTIVATED = "user_deactivated"
FLOOD_WAIT = "flood_wait"
PEER_FLOOD = "peer_flood"

# Временные причины: строка остаётся draft, оператор сможет повторить позже.
TEMPORARY_REASONS = frozenset({DAILY_LIMIT, SENDER_FLOOD_WAIT})

_RECIPIENT_ERRORS = (
    (errors.UserPrivacyRestrictedError, PRIVACY),
    (errors.UserIsBlockedError, USER_BLOCKED),
    (errors.ChatWriteForbiddenError, CHAT_WRITE_FORBIDDEN),
    (errors.PeerIdInvalidError, PEER_INVALID),
    (errors.InputUserDeactivatedError, USER_DEACTIVATED),
    (errors.UsernameInvalidError, USERNAME_NOT_FOUND),
    (errors.UsernameNotOccupiedError, USERNAME_NOT_FOUND),
)
_RECIPIENT_ERROR_TYPES = tuple(cls for cls, _ in _RECIPIENT_ERRORS)
_SOURCE_CHAT_ERRORS = (
    ValueError, errors.ChannelPrivateError, errors.ChannelInvalidError, errors.ChatIdInvalidError,
    errors.MsgIdInvalidError, errors.ChatAdminRequiredError, errors.UserNotParticipantError,
)
_TRANSPORT_ERRORS = (ConnectionError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError)

# Итог действия оператора.
KIND_READY = "ready"            # проверки пройдены, ждём подтверждения
KIND_SENT = "sent"
KIND_BLOCKED = "blocked"
KIND_FAILED = "failed"
KIND_DRAFT = "draft"            # не отправлено, строка осталась/вернулась в draft
KIND_ALREADY = "already_processed"


@dataclass(frozen=True)
class Plan:
    reason: str | None
    sender: object | None = None  # reader.inviter.models.TelegramAccount
    # Все подходящие по БД отправители в порядке выбора; sender — первый.
    candidates: tuple = ()

    @property
    def ok(self) -> bool:
        return self.reason is None


@dataclass(frozen=True)
class SendOutcome:
    kind: str
    reason: str | None = None
    sender_label: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sender_label(account) -> str:
    return account.name if account.name.startswith("@") else f"аккаунт #{account.id}"


class DmSendService:
    def __init__(
        self,
        outreach_repository: DmOutreachRepository,
        campaign_repository: DmCampaignRepository,
        account_repository,
        sender_states: DmSenderStateRepository,
        *,
        client_factory: Callable[[object], object],
        session_exists: Callable[[object], bool],
        recipient_cooldown: timedelta,
        sender_daily_cap: int | None,
        sender_daily_cap_overrides: dict[int, int] | None = None,
        clock: Callable[[], datetime] = _utcnow,
        feedback_repository=None,  # reader.dm_campaigns.feedback.DmFeedbackRepository
    ):
        self._outreach = outreach_repository
        self._campaigns = campaign_repository
        self._accounts = account_repository
        self._states = sender_states
        self._client_factory = client_factory
        self._session_exists = session_exists
        self._cooldown = recipient_cooldown
        self._cap = sender_daily_cap
        self._cap_overrides = dict(sender_daily_cap_overrides or {})
        self._selector = SenderSelector(
            outreach_repository, campaign_repository, account_repository, sender_states,
            session_exists=session_exists,
        )
        self._clock = clock
        self._lock = asyncio.Lock()
        self._feedback = feedback_repository

    # ---------------- проверки без сети ----------------

    def plan(self, outreach_id: int) -> Plan:
        now = self._clock()
        item = self._outreach.get(outreach_id)
        if item is None:
            return Plan(NOT_FOUND)
        if item.status != STATUS_DRAFT:
            return Plan(NOT_DRAFT)
        if item.is_synthetic:
            return Plan(SYNTHETIC)
        campaign = self._campaigns.get_campaign(item.campaign_id)
        if campaign is None or campaign.mode != MODE_MANUAL:
            return Plan(CAMPAIGN_NOT_MANUAL)
        if not (item.primary_text or "").strip():
            return Plan(NO_TEXT)
        if not self._recipient_resolvable(item):
            return Plan(NO_RECIPIENT)
        if self._outreach.recipient_recently_messaged(
            recipient_user_id=item.recipient_user_id, recipient_username=item.recipient_username,
            since=now - self._cooldown, exclude_id=item.id,
        ):
            return Plan(RECENT_DM)
        return self._select_sender(item, campaign, now)

    @staticmethod
    def _recipient_resolvable(item: DmOutreach) -> bool:
        by_message = (item.recipient_user_id is not None and item.source_message_id is not None
                      and (item.source_chat_identifier or item.source_chat_id))
        if item.sendability == SENDABILITY_USERNAME:
            return bool(item.recipient_username)
        if item.sendability == SENDABILITY_SOURCE_MESSAGE:
            return bool(by_message)
        if item.sendability == SENDABILITY_PREMIUM_REQUIRED:
            return bool(item.recipient_username) or bool(by_message)
        return False

    def _select_sender(self, item: DmOutreach, campaign, now: datetime) -> Plan:
        reason, ranked = self._selector.rank(item, campaign, now, cap=self._cap, cap_overrides=self._cap_overrides)
        if reason:
            return Plan(reason)
        return Plan(None, sender=ranked[0], candidates=ranked)

    # ---------------- действия оператора ----------------

    def preview(self, outreach_id: int, *, operator_id: int) -> SendOutcome:
        """Шаг "✅ Отправить": проверки без сети. Постоянная причина — сразу
        blocked; временная — строка остаётся draft; иначе ready (ждём
        подтверждения оператора)."""
        plan = self.plan(outreach_id)
        if plan.ok:
            return SendOutcome(KIND_READY, sender_label=_sender_label(plan.sender))
        return self._not_sendable(outreach_id, plan.reason, operator_id)

    def _not_sendable(self, outreach_id: int, reason: str, operator_id: int) -> SendOutcome:
        if reason in (NOT_DRAFT, NOT_FOUND):
            return SendOutcome(KIND_ALREADY, reason=reason)
        if reason in TEMPORARY_REASONS:
            return SendOutcome(KIND_DRAFT, reason=reason)
        if self._outreach.block_draft(outreach_id, reason=reason, operator_id=operator_id, now=self._clock()):
            logger.info("dm_outreach id=%s: не отправлено (%s)", outreach_id, reason)
            return SendOutcome(KIND_BLOCKED, reason=reason)
        return SendOutcome(KIND_ALREADY, reason=NOT_DRAFT)

    async def send(self, outreach_id: int, *, operator_id: int) -> SendOutcome:
        """Подтверждение оператора: одна реальная попытка отправки."""
        async with self._lock:
            plan = self.plan(outreach_id)
            if not plan.ok:
                return self._not_sendable(outreach_id, plan.reason, operator_id)
            sender = plan.sender
            if not self._outreach.claim_for_send(
                outreach_id, operator_id=operator_id, sender_account_id=sender.id, now=self._clock(),
            ):
                return SendOutcome(KIND_ALREADY, reason=NOT_DRAFT)
            item = self._outreach.get(outreach_id)
            outcome = await self._deliver_with_fallback(item, plan.candidates or (sender,))
            logger.info("dm_outreach id=%s: отправка -> %s (%s)", outreach_id, outcome.kind, outcome.reason)
            return outcome

    # ---------------- Telegram ----------------

    async def _deliver_with_fallback(self, item: DmOutreach, candidates) -> SendOutcome:
        """Строка уже в sending на candidates[0]. Следующий кандидат — только
        если предыдущий не смог прочитать исходное сообщение."""
        for index, sender in enumerate(candidates):
            if index and not self._outreach.reassign_sender(item.id, sender_account_id=sender.id, now=self._clock()):
                logger.warning("dm_outreach id=%s: статус изменился во время отправки — остановлено", item.id)
                return SendOutcome(KIND_ALREADY, reason=STATE_CHANGED)
            outcome = await self._deliver(item, sender)
            if outcome.reason != NOT_IN_SOURCE_CHAT or outcome.kind != KIND_DRAFT:
                return outcome
            logger.info("dm_outreach id=%s: у отправителя %s нет доступа к исходному сообщению — следующий",
                        item.id, _sender_label(sender))
        self._outreach.release_to_draft(item.id, now=self._clock(), error=NO_SENDER_FOR_SOURCE)
        return SendOutcome(KIND_DRAFT, reason=NO_SENDER_FOR_SOURCE)

    async def _deliver(self, item: DmOutreach, sender) -> SendOutcome:
        label = _sender_label(sender)
        client = self._client_factory(sender)
        stage = "connect"
        try:
            await client.connect()
            if not await client.is_user_authorized():
                self._states.block(sender.id, reason=SENDER_UNAUTHORIZED, now=self._clock())
                self._outreach.release_to_draft(item.id, now=self._clock(), error=SENDER_UNAUTHORIZED)
                return SendOutcome(KIND_DRAFT, reason=SENDER_UNAUTHORIZED, sender_label=label)
            stage = "resolve"
            peer, problem = await self._resolve(client, item)
            if problem == NOT_IN_SOURCE_CHAT:
                # Строку не трогаем: решает _deliver_with_fallback.
                return SendOutcome(KIND_DRAFT, reason=NOT_IN_SOURCE_CHAT, sender_label=label)
            if problem:
                self._outreach.mark_send_blocked(item.id, now=self._clock(), reason=problem)
                return SendOutcome(KIND_BLOCKED, reason=problem, sender_label=label)
            stage = "send"
            message = await client.send_message(peer, item.primary_text)
            if self._outreach.mark_sent(item.id, now=self._clock(), telegram_message_id=getattr(message, "id", None)):
                self._record_feedback(item.id)
            return SendOutcome(KIND_SENT, sender_label=label)
        except errors.FloodWaitError as exc:
            now = self._clock()
            self._states.set_flood_wait(sender.id, until=now + timedelta(seconds=exc.seconds), now=now)
            self._outreach.release_to_draft(item.id, now=now, error=f"{FLOOD_WAIT}:{exc.seconds}")
            return SendOutcome(KIND_DRAFT, reason=f"{FLOOD_WAIT}:{exc.seconds}", sender_label=label)
        except errors.PeerFloodError:
            now = self._clock()
            self._states.block(sender.id, reason=PEER_FLOOD, now=now)
            self._outreach.release_to_draft(item.id, now=now, error=PEER_FLOOD)
            return SendOutcome(KIND_DRAFT, reason=PEER_FLOOD, sender_label=label)
        except _RECIPIENT_ERROR_TYPES as exc:
            reason = next(code for cls, code in _RECIPIENT_ERRORS if isinstance(exc, cls))
            self._outreach.mark_send_blocked(item.id, now=self._clock(), reason=reason)
            return SendOutcome(KIND_BLOCKED, reason=reason, sender_label=label)
        except _TRANSPORT_ERRORS as exc:
            if stage == "send":
                reason = f"{UNCERTAIN_PREFIX}_transport:{type(exc).__name__}"
                self._outreach.mark_send_failed(item.id, now=self._clock(), error=reason)
                return SendOutcome(KIND_FAILED, reason=reason, sender_label=label)
            reason = f"transport_before_send:{type(exc).__name__}"
            self._outreach.release_to_draft(item.id, now=self._clock(), error=reason)
            return SendOutcome(KIND_DRAFT, reason=reason, sender_label=label)
        except errors.RPCError as exc:
            reason = f"rpc_error:{type(exc).__name__}"
            self._outreach.mark_send_failed(item.id, now=self._clock(), error=reason)
            return SendOutcome(KIND_FAILED, reason=reason, sender_label=label)
        except Exception as exc:  # noqa: BLE001
            logger.exception("dm_outreach id=%s: непредвиденная ошибка отправки", item.id)
            prefix = f"{UNCERTAIN_PREFIX}_internal" if stage == "send" else "internal_error"
            reason = f"{prefix}:{type(exc).__name__}"
            self._outreach.mark_send_failed(item.id, now=self._clock(), error=reason)
            return SendOutcome(KIND_FAILED, reason=reason, sender_label=label)
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                logger.warning("dm send: не удалось корректно отключить клиент отправителя")

    def _record_feedback(self, outreach_id: int) -> None:
        """Пример для feedback learning — только после mark_sent. Любой сбой
        здесь не влияет на исход отправки (сообщение уже ушло)."""
        if self._feedback is None:
            return
        try:
            item = self._outreach.get(outreach_id)
            campaign = self._campaigns.get_campaign(item.campaign_id) if item is not None else None
            if item is None or campaign is None or item.sent_at is None:
                return
            self._feedback.record_sent(item, campaign_key=campaign.key, sent_at=item.sent_at, now=self._clock())
        except Exception as exc:  # noqa: BLE001
            logger.warning("dm_outreach id=%s: не удалось сохранить feedback (%s)", outreach_id, type(exc).__name__)

    async def _resolve(self, client, item: DmOutreach):
        """(peer, None) либо (None, причина blocked). FloodWait/PeerFlood и
        транспорт пробрасываются (обрабатывает _deliver)."""
        if item.sendability == SENDABILITY_USERNAME or (
            item.sendability == SENDABILITY_PREMIUM_REQUIRED and item.recipient_username
        ):
            try:
                entity = await client.get_entity(item.recipient_username)
            except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError):
                return None, USERNAME_NOT_FOUND
            if type(entity).__name__ != "User" or getattr(entity, "bot", False):
                return None, IDENTITY_MISMATCH
            if item.recipient_user_id is not None and entity.id != item.recipient_user_id:
                return None, IDENTITY_MISMATCH
            return entity, None

        chat_ref = item.source_chat_identifier or item.source_chat_id
        if isinstance(chat_ref, str) and chat_ref.lstrip("-").isdigit():
            chat_ref = int(chat_ref)
        try:
            chat = await client.get_input_entity(chat_ref)
            message = await client.get_messages(chat, ids=item.source_message_id)
        except _SOURCE_CHAT_ERRORS:
            return None, NOT_IN_SOURCE_CHAT
        if message is None:
            return None, NOT_IN_SOURCE_CHAT
        if message.sender_id != item.recipient_user_id:
            return None, IDENTITY_MISMATCH
        return InputPeerUserFromMessage(peer=chat, msg_id=item.source_message_id, user_id=item.recipient_user_id), None
