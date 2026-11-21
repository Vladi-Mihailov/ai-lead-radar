"""ClientDeliveryService — доставка обнаруженных штрафов клиентам
@GEShtrafbot (owner/trusted_operator), см. design report Stage 4.

Работает ИСКЛЮЧИТЕЛЬНО поверх уже существующих DetectedFineRepository/
FineSubscriptionRepository/ClientFineDeliveryRepository — не создаёт
отдельную систему штрафов, не трогает detected_fines.notification_sent_at
(операторский канал, см. reader/fines/notification_coordinator.py,
полностью независим и не меняется).

Retry — bounded backoff (НЕ N одинаковых попыток подряд, см. явную
корректировку задачи): растущая пауза перед каждой следующей попыткой,
terminal give-up после исчерпания расписания — без единой новой колонки
в БД, только из уже существующих attempt_count/last_attempt_at.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from telethon.errors import FloodWaitError

from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import DetectedFine
from reader.fines.notification_control import (
    EXCLUSION_KNOWN_BEFORE_BOUNDARY,
    NotificationControlState,
)
from reader.fines.suppression import is_fine_output_suppressed
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository, RecipientRole
from reader.public_bot.delivery_texts import (
    format_owner_fine_message,
    format_trusted_operator_fine_message,
)
from reader.public_bot.keyboards import owner_fine_cta_buttons
from reader.public_bot.models import ClientFineDelivery, FineMonitoringSubscription
from reader.public_bot.subscription_repository import FineSubscriptionRepository

logger = logging.getLogger(__name__)

# Bounded backoff — initial, +1 мин, +5 мин, +15 мин, +1 час, +3 часа, затем
# terminal exhaustion (см. явную корректировку задачи: "не 20 одинаковых
# попыток каждые несколько минут"). Индекс — attempt_count ДО текущей
# попытки: 0 -> отправить сразу (первая попытка), 1 -> подождать 1 минуту
# после первой неудачи перед второй, и т.д. attempt_count >= len(...) —
# попытки исчерпаны, доставка больше никогда не ретраится (terminal).
RETRY_BACKOFF = [
    timedelta(seconds=0),
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=3),
]
MAX_DELIVERY_ATTEMPTS = len(RETRY_BACKOFF)


class BotMessageSenderLike(Protocol):
    """Ровно то, что нужно отсюда от Telethon bot-mode клиента — тот же
    приём Protocol, что и везде в проекте (см.
    reader/public_bot/owner_resolution.py::OwnerUsernameResolverLike).

    buttons — опционально, ТОЛЬКО для owner-CTA (см.
    reader/public_bot/keyboards.py::owner_fine_cta_buttons); None для всех
    остальных уведомлений (trusted_operator, любые другие)."""

    async def send_message(self, chat_id: int, text: str, *, buttons: list[list] | None = None) -> None: ...


@dataclass(frozen=True)
class DeliveryTickResult:
    delivered: int
    failed: int
    flood_wait_hit: bool


def _as_aware_utc(value: datetime) -> datetime:
    """last_attempt_at приходит из SQLite CURRENT_TIMESTAMP — naive-но-
    фактически-UTC ISO-строка (тот же случай, что и
    FineMonitoringTask.last_checked_at, см. reader/time_display.py::
    to_tbilisi) — трактуем naive явно как UTC, а не как локальное время,
    иначе сравнение с aware `now` упадёт с TypeError."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _as_naive_utc(value: datetime) -> datetime:
    return value if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)


def _is_due_for_attempt(delivery: ClientFineDelivery | None, *, now: datetime) -> bool:
    """None (ни разу не пытались) — всегда due. Уже доставлено — никогда
    (delivered fine никогда повторно не отправляется — явное требование
    задачи). attempt_count исчерпан (>= MAX_DELIVERY_ATTEMPTS) — никогда
    (terminal retry exhaustion). Иначе — сравниваем now с last_attempt_at +
    соответствующая пауза из RETRY_BACKOFF."""
    if delivery is None:
        return True
    if delivery.delivered_at is not None or delivery.excluded_at is not None:
        return False
    if delivery.attempt_count >= MAX_DELIVERY_ATTEMPTS:
        return False

    delay = RETRY_BACKOFF[delivery.attempt_count]
    last_attempt_at = _as_aware_utc(delivery.last_attempt_at) if delivery.last_attempt_at else now
    return now >= last_attempt_at + delay


def _applicable_roles(subscription: FineMonitoringSubscription) -> list[RecipientRole]:
    """Какому получателю ЭТА подписка вообще может требовать доставки —
    независимо от того, есть ли уже что доставлять (см. design report):
    'owner' — только для claimed (status='active', telegram_user_id
    известен); 'trusted_operator' — для delegated в статусе active ИЛИ
    ещё pending_claim (см. "trusted creator при pending claim продолжает
    получать уведомления")."""
    roles: list[RecipientRole] = []
    if subscription.status == "active" and subscription.telegram_user_id is not None:
        roles.append("owner")
    if subscription.is_delegated() and subscription.status in ("active", "pending_claim"):
        roles.append("trusted_operator")
    return roles


class FineStatusLookupLike(Protocol):
    """Ровно то, что нужно отсюда от FineStatusTracker (см.
    reader/fines/payment_status.py)."""

    def undeliverable_protocols(self, car_number: str) -> set[str]: ...


class NotificationControlLike(Protocol):
    """Ровно то, что нужно отсюда от GeorgiaNotificationControl (см.
    reader/fines/notification_control.py)."""

    def state(self) -> NotificationControlState: ...

    def known_before(
        self, boundary_at: datetime, *, car_number: str, protocol_no: str | None,
        row_first_detected_at: datetime | None,
    ) -> bool: ...


def exclude_known_fines_for_subscription(
    subscription: FineMonitoringSubscription, *, detected_fine_repository: DetectedFineRepository,
    delivery_repository: ClientFineDeliveryRepository, reason: str,
) -> int:
    """Явно исключает из рассылки этой подписке ВСЕ уже сохранённые штрафы
    номера (каждая строка detected_fines, каждый получатель подписки) — для
    подписки, которую включили обратно не сами клиенты (▶️ менеджера, см.
    SubscriptionService.resume_task_with_client_subscriptions): известные
    штрафы новыми событиями не считаются. Уже доставленное и уже
    исключённое не меняется. Возвращает число новых исключений."""
    excluded = 0
    for fine in detected_fine_repository.list_by_car_number(subscription.car_number):
        for role in _applicable_roles(subscription):
            excluded += int(bool(delivery_repository.exclude(fine.id, subscription.id, role, reason=reason)))
    return excluded


class ClientDeliveryService:
    def __init__(
        self,
        detected_fine_repository: DetectedFineRepository,
        subscription_repository: FineSubscriptionRepository,
        delivery_repository: ClientFineDeliveryRepository,
        sender: BotMessageSenderLike,
        *,
        tz: ZoneInfo,
        payment_help_contact_username: str = "tplgee",
        status_lookup: FineStatusLookupLike | None = None,
        notification_control: NotificationControlLike | None = None,
    ):
        # None — пауза и граница уведомлений не ведутся (прежнее поведение).
        self._notification_control = notification_control
        # None — статусы оплаты не ведутся: дедуп по квитанции работает,
        # пропуска исчезнувших из базы полиции квитанций нет.
        self._status_lookup = status_lookup
        self._detected_fine_repository = detected_fine_repository
        self._subscription_repository = subscription_repository
        self._delivery_repository = delivery_repository
        self._sender = sender
        self._tz = tz
        # Destination CTA-кнопок owner-уведомления — из config (см.
        # settings.public_bot.payment_help_contact_username), не hardcoded
        # (значение по умолчанию здесь — тот же fallback, что и в
        # reader/settings.py::PublicBotSettings, а не самостоятельный
        # источник истины).
        self._payment_help_contact_username = payment_help_contact_username

    async def run_once(self, *, now: datetime | None = None) -> DeliveryTickResult:
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(self._tz).date()

        # Гигиена статуса (см. design report) — НЕ источник истины: все
        # выборки ниже и так учитывают end_date >= today независимо от
        # того, вызван ли этот метод.
        self._subscription_repository.expire_elapsed(today=today)

        control = self._notification_control.state() if self._notification_control is not None else None
        if control is not None and control.paused:
            # Пауза уведомлений Грузии (см. reader/fines/notification_control.py):
            # ни попыток, ни отметок — неотправленное остаётся неотправленным.
            return DeliveryTickResult(delivered=0, failed=0, flood_wait_hit=False)
        boundary_at = control.boundary_at if control is not None else None

        delivered = 0
        failed = 0

        subscriptions = self._subscription_repository.list_all_deliverable(today=today)
        for subscription in subscriptions:
            if is_fine_output_suppressed(subscription.car_number):
                # Denylisted car_number (см. reader/fines/suppression.py) —
                # пропускается целиком, ДО чтения detected_fines: на случай,
                # если для него уже есть persisted штрафы с ДО появления
                # denylist'а (FineCheckService больше не создаёт новых, см.
                # check_service.py), этот поллер — единственный путь
                # клиентской доставки, независимый от notification_sent_at.
                continue
            fines = self._deliverable_fines(subscription)
            delivered_protocols = (
                self._delivery_repository.handled_protocols(subscription.id) if fines else {}
            )
            for fine in fines:
                for role in _applicable_roles(subscription):
                    if fine.external_fine_id and fine.external_fine_id in delivered_protocols.get(role, set()):
                        # Эта квитанция уже доставлена (или явно исключена)
                        # этому получателю по другой строке (другая задача
                        # того же номера) — второй "🚨 Обнаружен новый штраф"
                        # не нужен.
                        continue
                    if boundary_at is not None and self._known_before_boundary(
                        fine, subscription, role, boundary_at,
                    ):
                        # Квитанция известна с до границы включения
                        # уведомлений — не новое событие (см.
                        # reader/fines/notification_control.py).
                        if fine.external_fine_id:
                            delivered_protocols.setdefault(role, set()).add(fine.external_fine_id)
                        continue
                    outcome = await self._attempt_delivery(fine, subscription, role, now=now)
                    if outcome == "delivered" and fine.external_fine_id:
                        delivered_protocols.setdefault(role, set()).add(fine.external_fine_id)
                    if outcome == "delivered":
                        delivered += 1
                    elif outcome == "failed":
                        failed += 1
                    elif outcome == "flood_wait":
                        # FloodWaitError — ограничение всего Telegram-клиента,
                        # а не одного получателя (см. design report: "не
                        # пытаться обходить Telegram rate limits") —
                        # прекращаем ВЕСЬ текущий тик немедленно, а не
                        # только доставку этому получателю. Следующий тик
                        # (обычный интервал поллера) естественно выдержит
                        # паузу; если FloodWait окажется длиннее интервала —
                        # тик просто прервётся снова, без busy-loop.
                        return DeliveryTickResult(
                            delivered=delivered, failed=failed, flood_wait_hit=True,
                        )
                    # "not_due" — ничего не считаем, это не попытка.

        return DeliveryTickResult(delivered=delivered, failed=failed, flood_wait_hit=False)

    def _known_before_boundary(
        self, fine: DetectedFine, subscription: FineMonitoringSubscription, role: RecipientRole,
        boundary_at: datetime,
    ) -> bool:
        """True — тройку не отправлять: она уже доставлена или исключена,
        либо квитанцию впервые увидели раньше границы (тогда тройка явно
        исключается). Доставка квитанции, увиденной после границы, этим не
        затрагивается — в том числе уже начатая."""
        existing = self._delivery_repository.get(fine.id, subscription.id, role)
        if existing is not None and (
            existing.delivered_at is not None or existing.excluded_at is not None
            or existing.attempt_count >= MAX_DELIVERY_ATTEMPTS
        ):
            # Доставлено, исключено или попытки исчерпаны (история остаётся
            # как есть, см. exclude_pending) — отправлять нечего.
            return True
        first_detected_at = _as_naive_utc(fine.first_detected_at) if fine.first_detected_at else None
        if not self._notification_control.known_before(
            boundary_at, car_number=subscription.car_number, protocol_no=fine.external_fine_id,
            row_first_detected_at=first_detected_at,
        ):
            return False
        self._delivery_repository.exclude(
            fine.id, subscription.id, role, reason=EXCLUSION_KNOWN_BEFORE_BOUNDARY,
        )
        return True

    def exclude_pending(self, *, reason: str, now: datetime | None = None, dry_run: bool = False) -> int:
        """Явно исключает из рассылки всё, что сейчас ждёт отправки (см.
        scripts/georgia_notifications.py resume): ещё не доставленные и не
        исключённые тройки текущих подписок, включая ждущие повтора после
        неудачи. Исчерпавшие попытки не трогает — они и так больше не
        отправляются, их история остаётся как есть. ⏳/⚪ сюда не входят (их
        и сейчас не отправляют); квитанцию, подтверждённую позже, исключит
        граница при первой встрече. dry_run — только подсчёт."""
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(self._tz).date()
        excluded = 0
        for subscription in self._subscription_repository.list_all_deliverable(today=today):
            if is_fine_output_suppressed(subscription.car_number):
                continue
            fines = self._deliverable_fines(subscription)
            handled = self._delivery_repository.handled_protocols(subscription.id) if fines else {}
            for fine in fines:
                for role in _applicable_roles(subscription):
                    if fine.external_fine_id and fine.external_fine_id in handled.get(role, set()):
                        continue
                    existing = self._delivery_repository.get(fine.id, subscription.id, role)
                    if existing is not None and (
                        existing.delivered_at is not None or existing.excluded_at is not None
                        or existing.attempt_count >= MAX_DELIVERY_ATTEMPTS
                    ):
                        continue
                    if not dry_run:
                        self._delivery_repository.exclude(fine.id, subscription.id, role, reason=reason)
                    if fine.external_fine_id:
                        handled.setdefault(role, set()).add(fine.external_fine_id)
                    excluded += 1
        return excluded

    def _deliverable_fines(self, subscription: FineMonitoringSubscription) -> list[DetectedFine]:
        """Строки detected_fines номера подписки, по одной на квитанцию
        (самая ранняя строка — каноническая), без квитанций, которых police.ge
        больше не показывает (⚪), и без ⏳ — квитанций из истории, чьё
        присутствие новая проверка ещё не подтвердила: "новый штраф" о них
        был бы ложным (см. задачу — подписку могли создать уже после того,
        как штраф оплатили и он исчез). ⏳ подписчик видит отдельной строкой
        (итог добавления машины, "Проверить сейчас"); подтверждённая
        проверкой или feed'ом квитанция уйдёт на следующем тике, ⚪ — никогда.
        Не найденные один раз ('missing') доставляются как обычно — это ещё
        не доказательство. Строки без номера квитанции (старые записи) — как
        раньше, каждая отдельно."""
        fines = sorted(
            self._detected_fine_repository.list_by_car_number(subscription.car_number), key=lambda fine: fine.id,
        )
        undeliverable = (
            self._status_lookup.undeliverable_protocols(subscription.car_number)
            if self._status_lookup is not None and fines else set()
        )
        result: list[DetectedFine] = []
        seen_protocols: set[str] = set()
        for fine in fines:
            protocol_no = fine.external_fine_id
            if protocol_no:
                if protocol_no in seen_protocols or protocol_no in undeliverable:
                    continue
                seen_protocols.add(protocol_no)
            result.append(fine)
        return result

    async def _attempt_delivery(
        self,
        fine: DetectedFine,
        subscription: FineMonitoringSubscription,
        role: RecipientRole,
        *,
        now: datetime,
    ) -> str:
        existing = self._delivery_repository.get(fine.id, subscription.id, role)
        if not _is_due_for_attempt(existing, now=now):
            return "not_due"

        chat_id = (
            subscription.telegram_chat_id if role == "owner"
            else subscription.created_by_telegram_chat_id
        )
        if chat_id is None:
            # Не должно происходить штатно (см. _applicable_roles), но не
            # пытаемся отправить в никуда, если данные всё же неполные.
            return "not_due"

        if role == "owner":
            text = format_owner_fine_message(car_number=subscription.car_number, fine=fine)
            # CTA-кнопки — ТОЛЬКО owner (см. design report: trusted_operator
            # и операторский чат коммерческий блок не получают).
            buttons = owner_fine_cta_buttons(self._payment_help_contact_username)
        else:
            text = format_trusted_operator_fine_message(
                car_number=subscription.car_number, fine=fine,
                owner_display=subscription.telegram_username or subscription.owner_username_hint,
            )
            buttons = None

        # Фиксируем попытку ДО отправки (см. ClientFineDeliveryRepository.
        # record_attempt) — attempt_count/last_attempt_at отражают попытку
        # независимо от исхода, что и двигает backoff вперёд.
        self._delivery_repository.record_attempt(fine.id, subscription.id, role)

        try:
            await self._sender.send_message(chat_id, text, buttons=buttons)
        except FloodWaitError as exc:
            logger.warning(
                "FloodWaitError (%ss) при доставке штрафа id=%s получателю "
                "chat_id=%s (%s) — прекращаю доставку в этом тике",
                exc.seconds, fine.id, chat_id, role,
            )
            return "flood_wait"
        except Exception:
            # Ошибка ОДНОГО получателя не должна блокировать остальных (см.
            # явное требование задачи) — логируем и переходим дальше,
            # attempt_count уже увеличен, backoff учтёт это на следующем тике.
            logger.exception(
                "Не удалось доставить штраф id=%s получателю chat_id=%s (%s)",
                fine.id, chat_id, role,
            )
            return "failed"

        self._delivery_repository.mark_delivered(fine.id, subscription.id, role)
        return "delivered"
