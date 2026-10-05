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
  потолок аккаунта (sender_daily_cap) и лимит пары кампания-аккаунт, если
  задан. telegram_accounts.enabled (включатель инвайтера) здесь не
  учитывается.

send() — под asyncio.Lock (одна отправка за раз в процессе): повтор plan()
-> атомарный claim draft->sending (второй клик получает "уже обработан") ->
connect -> is_user_authorized -> резолв получателя ВЫБРАННЫМ отправителем
(username: resolve + сверка числового id; source_message: исходное
сообщение читается самим отправителем, peer = InputPeerUserFromMessage,
чужой access_hash не используется и не хранится) -> ОДИН send_message ->
sent. Исходы ошибок:
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
    dm_sender_eligible,
)

logger = logging.getLogger(__name__)

# ---- причины (коды хранятся в dm_outreach.send_error) ----
SYNTHETIC = "synthetic"
NOT_FOUND = "not_found"
NOT_DRAFT = "not_draft"
CAMPAIGN_NOT_MANUAL = "campaign_not_manual"
NO_RECIPIENT = "no_recipient"
NO_TEXT = "no_text"
NO_ELIGIBLE_SENDER = "no_eligible_sender"
NO_PREMIUM_SENDER = "no_eligible_premium_sender"
RECENT_DM = "blocked_recent_dm"
DAILY_LIMIT = "daily_limit_reached"
SENDER_FLOOD_WAIT = "sender_flood_wait"
SENDER_UNAUTHORIZED = "sender_session_unauthorized"
NOT_IN_SOURCE_CHAT = "sender_not_in_source_chat"
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
    errors.MsgIdInvalidError, errors.ChatAdminRequiredError,
)
_TRANSPORT_ERRORS = (ConnectionError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError)

_DAY = timedelta(hours=24)

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
        sender_daily_cap: int,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self._outreach = outreach_repository
        self._campaigns = campaign_repository
        self._accounts = account_repository
        self._states = sender_states
        self._client_factory = client_factory
        self._session_exists = session_exists
        self._cooldown = recipient_cooldown
        self._cap = sender_daily_cap
        self._clock = clock
        self._lock = asyncio.Lock()

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
        usable = []
        for entry in self._campaigns.list_campaign_accounts(campaign.id):
            account = self._accounts.get(entry.account_id)
            if account is None or account.is_old or not dm_sender_eligible(account, item.sendability):
                continue
            if account.blocked_until is not None and account.blocked_until > now:
                continue
            state = self._states.get(account.id)
            if state.blocked_reason or not self._session_exists(account):
                continue
            usable.append((account, entry, state))
        if not usable:
            return Plan(NO_PREMIUM_SENDER if item.sendability == SENDABILITY_PREMIUM_REQUIRED else NO_ELIGIBLE_SENDER)

        awake = [u for u in usable if not (u[2].flood_wait_until and u[2].flood_wait_until > now)]
        if not awake:
            return Plan(SENDER_FLOOD_WAIT)

        candidates = []
        for account, entry, _ in awake:
            total = self._outreach.sends_since(sender_account_id=account.id, since=now - _DAY)
            if total >= self._cap:
                continue
            if entry.daily_limit is not None and self._outreach.sends_since(
                sender_account_id=account.id, since=now - _DAY, campaign_id=campaign.id,
            ) >= entry.daily_limit:
                continue
            candidates.append((total, account.id, account))
        if not candidates:
            return Plan(DAILY_LIMIT)
        return Plan(None, sender=min(candidates, key=lambda c: (c[0], c[1]))[2])

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
            outcome = await self._deliver(item, sender)
            logger.info("dm_outreach id=%s: отправка -> %s (%s)", outreach_id, outcome.kind, outcome.reason)
            return outcome

    # ---------------- Telegram ----------------

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
            if problem:
                self._outreach.mark_send_blocked(item.id, now=self._clock(), reason=problem)
                return SendOutcome(KIND_BLOCKED, reason=problem, sender_label=label)
            stage = "send"
            message = await client.send_message(peer, item.primary_text)
            self._outreach.mark_sent(item.id, now=self._clock(), telegram_message_id=getattr(message, "id", None))
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
