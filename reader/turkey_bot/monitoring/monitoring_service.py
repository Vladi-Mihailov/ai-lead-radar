"""TurkeyMonitoringService — плановый (13:00/21:00 Europe/Istanbul) цикл
проверки всех активных подписок (см. design report п.8/п.12 задачи
"Перестроить UX Turkey test bot"): использует ТОТ ЖЕ
UnifiedTurkeyCheckService, что и ручная "🔎 Проверить сейчас"
(reader/turkey_bot/conversation.py) — разница только в инициаторе
(initiator="monitoring_<slot>") и notification policy (см. ChangeDetector
ниже — ручная проверка её не запускает вообще, всегда просто показывает
live-результат)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol

from reader.turkey_bot.monitoring.change_detector import detect_changes
from reader.turkey_bot.monitoring.notification_texts import (
    format_change_notification,
)
from reader.turkey_bot.monitoring.scheduler_job import next_monitoring_slot
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscription,
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService
from reader.turkey_bot.unified.models import UnifiedCheckResult
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository

logger = logging.getLogger(__name__)

_PROVIDERS = ("gib", "avrasya", "kgm")


class NotifierLike(Protocol):
    async def send(self, *, telegram_chat_id: int, text: str) -> None: ...


class TurkeyMonitoringService:
    def __init__(
        self,
        check_service: UnifiedTurkeyCheckService,
        run_repository: TurkeyCheckRunRepository,
        subscription_repository: TurkeyMonitoringSubscriptionRepository,
        notifier: NotifierLike,
    ):
        self._check_service = check_service
        self._runs = run_repository
        self._subscriptions = subscription_repository
        self._notifier = notifier

    async def run_scheduled_batch(self, slot_label: str) -> None:
        now = datetime.now(timezone.utc)
        subscriptions = self._subscriptions.list_due(now=now)
        logger.info(
            "Turkey monitoring batch (slot=%s): %d активных подписок к проверке",
            slot_label, len(subscriptions),
        )
        for subscription in subscriptions:
            try:
                await self._check_one(subscription, slot_label=slot_label, now=now)
            except Exception:
                # Одна упавшая подписка не должна останавливать батч (см.
                # design report п.4: провайдеры/подписки независимы).
                logger.exception(
                    "Turkey monitoring: ошибка при проверке plate=%s (subscription_id=%s)",
                    subscription.plate, subscription.id,
                )

    async def check_now_and_notify_if_changed(
        self, subscription: TurkeyMonitoringSubscription, *, initiator: str,
    ) -> UnifiedCheckResult:
        """Публичный вход, переиспользуемый run_scheduled_batch — оставлен
        отдельным методом, чтобы его можно было вызвать и вне планового
        батча (например, из ручного теста), не дублируя save/detect/notify
        логику."""
        return await self._check_one(subscription, slot_label=initiator, now=datetime.now(timezone.utc))

    async def _check_one(
        self, subscription: TurkeyMonitoringSubscription, *, slot_label: str, now: datetime,
    ) -> UnifiedCheckResult:
        previous_by_provider = {
            provider: self._runs.get_last_successful_provider_result(subscription.plate, provider)
            for provider in _PROVIDERS
        }

        result = await self._check_service.check(subscription.plate)

        self._runs.save(
            result, telegram_user_id=subscription.telegram_user_id,
            telegram_chat_id=subscription.telegram_chat_id, initiator=f"monitoring_{slot_label}",
        )

        next_check_at = next_monitoring_slot(now)
        self._subscriptions.mark_checked(subscription.id, checked_at=now, next_check_at=next_check_at)

        changes = detect_changes(previous_by_provider, result)
        if not changes:
            logger.info("Turkey monitoring: без изменений (plate=%s)", subscription.plate)
            return result

        for change in changes:
            text = format_change_notification(subscription.plate, change)
            await self._notifier.send(telegram_chat_id=subscription.telegram_chat_id, text=text)

        return result
