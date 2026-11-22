"""AmountIncreaseDeliveryService — уведомления о подтверждённом увеличении
суммы к оплате (georgia_fine_amount_events, см. reader/public_bot/
payable_amount_service.py). Тикает в том же поллере, что и ClientDeliveryService.

Правила:
- пауза уведомлений Грузии — ничего не отправляется и не отмечается;
- событие, созданное не позже границы (то есть во время паузы), не
  рассылается: получателям записывается явное исключение
  (notification_boundary), без отметки о доставке;
- одно сообщение на событие и получателя (owner — клиент подписки,
  trusted_operator — менеджер, создавший её, как у «новых штрафов»); чат,
  уже получивший это событие по другой подписке, второй раз не получает;
- подписка, созданная после события, его не получает: новый подписчик видит
  текущую сумму в карточке;
- подписка, которая снова стала действующей после события (▶️ менеджера
  вернул её, клиент включил уведомления сам, приглашение принято), его тоже
  не получает: явное исключение (subscription_changed_after_event) — пока она
  была выключена, это событие для неё не происходило;
- уведомления об увеличении включаются отдельным флагом; событие, созданное
  до включения (в том числе при первом включении функции), не рассылается
  никогда: явное исключение (before_increase_notifications);
- квитанция, ставшая к моменту отправки 🟢, ⚪ или ⏳, — явное исключение;
- событие старше max_event_age не рассылается (не «историческая» рассылка
  после долгого простоя или паузы): получателям — явное исключение
  (event_too_old);
- повторы — тот же ограниченный backoff, что у «новых штрафов».

Оператору (AmountIncreaseOperatorNotifier, main-процесс) — те же правила
события, своя строка доставки и своя граница включения.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telethon.errors import FloodWaitError

from reader.fines.notification_control import EXCLUSION_NOTIFICATION_BOUNDARY
from reader.fines.payable_amount import AmountEvent, PayableAmountRepository
from reader.fines.payment_status import DISPLAY_GONE, DISPLAY_UNVERIFIED, PAYMENT_PAID
from reader.fines.suppression import is_fine_output_suppressed
from reader.public_bot.delivery_service import (
    DeliveryTickResult,
    NotificationControlLike,
    _applicable_roles,
    _is_due_for_attempt,
)
from reader.public_bot.delivery_texts import (
    format_operator_amount_increase_message,
    format_owner_amount_increase_message,
    format_trusted_operator_amount_increase_message,
)
from reader.public_bot.keyboards import payable_amount_card_buttons
from reader.public_bot.models import FineMonitoringSubscription

logger = logging.getLogger(__name__)

EXCLUSION_RECEIPT_NOT_PAYABLE = "receipt_not_payable"
EXCLUSION_DUPLICATE_CHAT = "duplicate_chat"
EXCLUSION_SHOWN_IN_REPLY = "shown_in_refresh_reply"
EXCLUSION_EVENT_TOO_OLD = "event_too_old"
EXCLUSION_BEFORE_NOTIFICATIONS = "before_increase_notifications"
EXCLUSION_SUBSCRIPTION_CHANGED = "subscription_changed_after_event"
EXCLUSION_BEFORE_OPERATOR_NOTIFICATIONS = "before_operator_notifications"

# Доставка оператору не привязана к подписке: одна строка на событие.
OPERATOR_ROLE = "operator"
OPERATOR_SUBSCRIPTION_ID = 0
_OPERATOR_CHAT_ID = 0

DEFAULT_MAX_EVENT_AGE = timedelta(days=3)
# Насколько назад смотреть, чтобы явно исключить устаревшие события.
_EXCLUSION_LOOKBACK = timedelta(days=30)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _event_exclusion(
    event: AmountEvent, status, *, boundary_at: datetime | None, enabled_since: datetime, too_old_before: datetime,
) -> str | None:
    """Почему событие не рассылается никому (клиенту и оператору — одинаково)."""
    if boundary_at is not None and event.created_at <= boundary_at:
        return EXCLUSION_NOTIFICATION_BOUNDARY
    if event.created_at <= enabled_since:
        return EXCLUSION_BEFORE_NOTIFICATIONS
    if event.created_at < too_old_before:
        return EXCLUSION_EVENT_TOO_OLD
    if status is None or status.display_status in (PAYMENT_PAID, DISPLAY_GONE, DISPLAY_UNVERIFIED):
        return EXCLUSION_RECEIPT_NOT_PAYABLE
    return None


class AmountIncreaseDeliveryService:
    def __init__(
        self,
        repository: PayableAmountRepository,
        subscription_repository,
        status_tracker,
        sender,
        *,
        tz: ZoneInfo,
        payment_help_contact_username: str = "tplgee",
        notification_control: NotificationControlLike | None = None,
        max_event_age: timedelta = DEFAULT_MAX_EVENT_AGE,
    ):
        self._repository = repository
        self._subscriptions = subscription_repository
        self._statuses = status_tracker
        self._sender = sender
        self._tz = tz
        self._payment_help_contact_username = payment_help_contact_username
        self._notification_control = notification_control
        self._max_event_age = max_event_age

    async def run_once(self, *, now: datetime | None = None) -> DeliveryTickResult:
        now = now or datetime.now(timezone.utc)
        control = self._notification_control.state() if self._notification_control is not None else None
        if control is not None and control.paused:
            return DeliveryTickResult(delivered=0, failed=0, flood_wait_hit=False)
        boundary_at = control.boundary_at if control is not None else None

        enabled_since = self._repository.increase_notifications_since()
        if enabled_since is None:
            # Уведомления об увеличении выключены — ничего не отправляется и
            # не отмечается (при включении всё прежнее будет исключено).
            return DeliveryTickResult(delivered=0, failed=0, flood_wait_hit=False)
        too_old_before = _naive_utc(now) - self._max_event_age
        events = self._repository.events_since(_naive_utc(now) - _EXCLUSION_LOOKBACK)
        if not events:
            return DeliveryTickResult(delivered=0, failed=0, flood_wait_hit=False)
        today = now.astimezone(self._tz).date()
        by_car: dict[str, list[FineMonitoringSubscription]] = {}
        for subscription in self._subscriptions.list_all_deliverable(today=today):
            by_car.setdefault(subscription.car_number, []).append(subscription)

        delivered = failed = 0
        for event in events:
            if is_fine_output_suppressed(event.car_number):
                continue
            status = self._statuses.statuses_by_protocol(event.car_number).get(event.protocol_no)
            exclusion = _event_exclusion(
                event, status, boundary_at=boundary_at, enabled_since=enabled_since, too_old_before=too_old_before,
            )
            for subscription in by_car.get(event.car_number, []):
                if subscription.created_at is not None and _naive_utc(subscription.created_at) > event.created_at:
                    continue
                # Стала действующей позже события (▶️ менеджера, включение
                # клиентом, принятие приглашения — все пишут updated_at).
                changed_after = (
                    subscription.updated_at is not None and _naive_utc(subscription.updated_at) >= event.created_at
                )
                for role in _applicable_roles(subscription):
                    chat_id = (
                        subscription.telegram_chat_id if role == "owner" else subscription.created_by_telegram_chat_id
                    )
                    if chat_id is None:
                        continue
                    existing = self._repository.get_delivery(event.id, subscription.id, role)
                    if not _is_due_for_attempt(existing, now=now):
                        continue
                    # И при ожидающем повторе: если подписку выключили и снова
                    # включили после неудачной первой отправки, старое событие
                    # для неё тоже уже не новое.
                    reason = exclusion or (EXCLUSION_SUBSCRIPTION_CHANGED if changed_after else None)
                    if reason is not None:
                        self._repository.exclude_delivery(event.id, subscription.id, role, chat_id, reason=reason)
                        continue
                    if self._repository.chat_handled(event.id, chat_id):
                        self._repository.exclude_delivery(
                            event.id, subscription.id, role, chat_id, reason=EXCLUSION_DUPLICATE_CHAT,
                        )
                        continue
                    outcome = await self._attempt(event, subscription, role, chat_id, base_amount=status.amount)
                    if outcome == "delivered":
                        delivered += 1
                    elif outcome == "failed":
                        failed += 1
                    elif outcome == "flood_wait":
                        return DeliveryTickResult(delivered=delivered, failed=failed, flood_wait_hit=True)
        return DeliveryTickResult(delivered=delivered, failed=failed, flood_wait_hit=False)

    def exclude_for_chat(self, event_id: int, *, chat_id: int, today, reason: str = EXCLUSION_SHOWN_IN_REPLY) -> int:
        """Событие уже показано в ответе на «🔄 Уточнить сумму» этому чату —
        поллер его туда не отправляет (явное исключение, не доставка)."""
        event = self._repository.get_event(event_id)
        if event is None:
            return 0
        excluded = 0
        for subscription in self._subscriptions.list_all_deliverable(today=today):
            if subscription.car_number != event.car_number:
                continue
            for role in _applicable_roles(subscription):
                role_chat = subscription.telegram_chat_id if role == "owner" else subscription.created_by_telegram_chat_id
                if role_chat != chat_id:
                    continue
                existing = self._repository.get_delivery(event.id, subscription.id, role)
                if existing is None or (existing.delivered_at is None and existing.excluded_at is None):
                    self._repository.exclude_delivery(event.id, subscription.id, role, chat_id, reason=reason)
                    excluded += 1
        return excluded

    async def _attempt(
        self, event: AmountEvent, subscription: FineMonitoringSubscription, role: str, chat_id: int, *,
        base_amount: float | None,
    ) -> str:
        if role == "owner":
            text = format_owner_amount_increase_message(
                car_number=subscription.car_number, protocol_no=event.protocol_no, base_amount=base_amount,
                old_amount=event.old_amount, new_amount=event.new_amount, observed_at=event.observed_at, tz=self._tz,
            )
            buttons = payable_amount_card_buttons(
                subscription.id, contact_username=self._payment_help_contact_username, include_payment=True,
            )
        else:
            text = format_trusted_operator_amount_increase_message(
                car_number=subscription.car_number, protocol_no=event.protocol_no, base_amount=base_amount,
                old_amount=event.old_amount, new_amount=event.new_amount, observed_at=event.observed_at, tz=self._tz,
                owner_display=subscription.telegram_username or subscription.owner_username_hint,
            )
            buttons = None
        self._repository.record_delivery_attempt(event.id, subscription.id, role, chat_id)
        try:
            await self._sender.send_message(chat_id, text, buttons=buttons)
        except FloodWaitError as exc:
            logger.warning(
                "FloodWaitError (%ss) при уведомлении о сумме event=%s (%s) — прекращаю тик", exc.seconds, event.id, role,
            )
            return "flood_wait"
        except Exception:
            logger.exception("Не удалось отправить уведомление о сумме event=%s (%s)", event.id, role)
            return "failed"
        self._repository.mark_delivered(event.id, subscription.id, role)
        return "delivered"


class AmountIncreaseOperatorNotifier:
    """Подтверждённое увеличение суммы — оператору: тем же каналом и тем же
    получателям, что и новые штрафы (TelegramNotificationService main-процесса,
    тик NotificationFlushJob). Правила события — те же, что у клиента
    (_event_exclusion: пауза и граница Грузии, флаг уведомлений об увеличении,
    срок давности, квитанция к оплате), плюс своя граница: событие, созданное
    до первого запуска операторских уведомлений, оператору не отправляется
    никогда. Одна строка доставки на событие (role operator) — повтор той же
    суммы события не создаёт, доставленное не повторяется; неудачная попытка
    повторяется с тем же backoff, что у «новых штрафов». Клиентская доставка
    (AmountIncreaseDeliveryService, бот-процесс) от неё не зависит. Подписки
    клиентов на отправку оператору не влияют — как и о новых штрафах, оператор
    узнаёт о каждом увеличении."""

    def __init__(
        self,
        repository: PayableAmountRepository,
        status_lookup,
        notification_service,
        *,
        user_repository=None,
        notification_control: NotificationControlLike | None = None,
        max_event_age: timedelta = DEFAULT_MAX_EVENT_AGE,
    ):
        self._repository = repository
        self._statuses = status_lookup
        self._notification_service = notification_service
        self._users = user_repository
        self._notification_control = notification_control
        self._max_event_age = max_event_age

    async def run_once(self, *, now: datetime | None = None) -> DeliveryTickResult:
        now = now or datetime.now(timezone.utc)
        control = self._notification_control.state() if self._notification_control is not None else None
        if control is not None and control.paused:
            return DeliveryTickResult(delivered=0, failed=0, flood_wait_hit=False)
        enabled_since = self._repository.increase_notifications_since()
        operator_since = self._repository.operator_increase_notifications_since()
        if enabled_since is None or operator_since is None:
            return DeliveryTickResult(delivered=0, failed=0, flood_wait_hit=False)
        too_old_before = _naive_utc(now) - self._max_event_age
        delivered = failed = 0
        for event in self._repository.events_since(_naive_utc(now) - _EXCLUSION_LOOKBACK):
            if is_fine_output_suppressed(event.car_number):
                continue
            existing = self._repository.get_delivery(event.id, OPERATOR_SUBSCRIPTION_ID, OPERATOR_ROLE)
            if not _is_due_for_attempt(existing, now=now):
                continue
            status = self._statuses.statuses_by_protocol(event.car_number).get(event.protocol_no)
            reason = _event_exclusion(
                event, status, boundary_at=control.boundary_at if control is not None else None,
                enabled_since=enabled_since, too_old_before=too_old_before,
            )
            if reason is None and event.created_at <= operator_since:
                reason = EXCLUSION_BEFORE_OPERATOR_NOTIFICATIONS
            if reason is not None:
                self._repository.exclude_delivery(
                    event.id, OPERATOR_SUBSCRIPTION_ID, OPERATOR_ROLE, _OPERATOR_CHAT_ID, reason=reason,
                )
                continue
            text = format_operator_amount_increase_message(
                car_number=event.car_number, base_amount=status.amount, old_amount=event.old_amount,
                new_amount=event.new_amount,
                clients=self._users.find_by_car_number(event.car_number) if self._users is not None else [],
            )
            self._repository.record_delivery_attempt(
                event.id, OPERATOR_SUBSCRIPTION_ID, OPERATOR_ROLE, _OPERATOR_CHAT_ID,
            )
            try:
                sent = await self._notification_service.notify_text(text)
            except Exception:
                logger.exception("Не удалось отправить оператору уведомление о сумме event=%s", event.id)
                sent = False
            if sent:
                self._repository.mark_delivered(event.id, OPERATOR_SUBSCRIPTION_ID, OPERATOR_ROLE)
                delivered += 1
            else:
                failed += 1
        return DeliveryTickResult(delivered=delivered, failed=failed, flood_wait_hit=False)
