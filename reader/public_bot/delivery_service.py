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
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository, RecipientRole
from reader.public_bot.delivery_texts import (
    format_owner_fine_message,
    format_trusted_operator_fine_message,
)
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
    reader/public_bot/owner_resolution.py::OwnerUsernameResolverLike)."""

    async def send_message(self, chat_id: int, text: str) -> None: ...


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


def _is_due_for_attempt(delivery: ClientFineDelivery | None, *, now: datetime) -> bool:
    """None (ни разу не пытались) — всегда due. Уже доставлено — никогда
    (delivered fine никогда повторно не отправляется — явное требование
    задачи). attempt_count исчерпан (>= MAX_DELIVERY_ATTEMPTS) — никогда
    (terminal retry exhaustion). Иначе — сравниваем now с last_attempt_at +
    соответствующая пауза из RETRY_BACKOFF."""
    if delivery is None:
        return True
    if delivery.delivered_at is not None:
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


class ClientDeliveryService:
    def __init__(
        self,
        detected_fine_repository: DetectedFineRepository,
        subscription_repository: FineSubscriptionRepository,
        delivery_repository: ClientFineDeliveryRepository,
        sender: BotMessageSenderLike,
        *,
        tz: ZoneInfo,
    ):
        self._detected_fine_repository = detected_fine_repository
        self._subscription_repository = subscription_repository
        self._delivery_repository = delivery_repository
        self._sender = sender
        self._tz = tz

    async def run_once(self, *, now: datetime | None = None) -> DeliveryTickResult:
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(self._tz).date()

        # Гигиена статуса (см. design report) — НЕ источник истины: все
        # выборки ниже и так учитывают end_date >= today независимо от
        # того, вызван ли этот метод.
        self._subscription_repository.expire_elapsed(today=today)

        delivered = 0
        failed = 0

        subscriptions = self._subscription_repository.list_all_deliverable(today=today)
        for subscription in subscriptions:
            fines = self._detected_fine_repository.list_by_car_number(subscription.car_number)
            for fine in fines:
                for role in _applicable_roles(subscription):
                    outcome = await self._attempt_delivery(fine, subscription, role, now=now)
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

        text = (
            format_owner_fine_message(car_number=subscription.car_number, fine=fine)
            if role == "owner"
            else format_trusted_operator_fine_message(
                car_number=subscription.car_number, fine=fine,
                owner_display=subscription.telegram_username or subscription.owner_username_hint,
            )
        )

        # Фиксируем попытку ДО отправки (см. ClientFineDeliveryRepository.
        # record_attempt) — attempt_count/last_attempt_at отражают попытку
        # независимо от исхода, что и двигает backoff вперёд.
        self._delivery_repository.record_attempt(fine.id, subscription.id, role)

        try:
            await self._sender.send_message(chat_id, text)
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
