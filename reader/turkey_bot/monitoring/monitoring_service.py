"""TurkeyMonitoringService — плановый (13:00/21:00 Europe/Istanbul) цикл
проверки всех активных подписок (см. design report п.8/п.12 задачи
"Перестроить UX Turkey test bot"): использует ТОТ ЖЕ
UnifiedTurkeyCheckService, что и ручная "🔎 Проверить сейчас"
(reader/turkey_bot/conversation.py) — разница только в инициаторе
(initiator="monitoring_<slot>") и notification policy (см. ChangeDetector
ниже — ручная проверка её не запускает вообще, всегда просто показывает
live-результат).

RETRY ORCHESTRATION (см. задачу "Retry orchestration Unified Turkey
checks" п.2/п.3/п.5) — ДВА уровня:
  1. Внутри ОДНОГО check() — до 25 попыток НА ПРОВАЙДЕРА при
     captcha_unavailable/captcha_rejected (см. reader/turkey_bot/unified/
     check_service.py — вся эта логика живёт там, здесь только
     max_attempts=_SCHEDULED_MAX_ATTEMPTS).
  2. Если ПОСЛЕ этих 25 попыток провайдер всё ещё ERROR именно из-за
     captcha (не transport_error/rate_limited/unexpected — см.
     _CAPTCHA_RETRY_ERROR_TYPES), планируется ОДИН retry ЧЕРЕЗ 5 минут
     (см. _maybe_enqueue_retry/_PendingRetry/run_due_retries) — ТОЛЬКО
     для этих конкретных провайдеров этой конкретной подписки (provider-
     specific state, см. задачу п.5), успешные providers первого прохода
     НЕ трогаются вообще (ни HTTP-запрос, ни новая строка в
     UnifiedCheckResult.providers, см. check_service.py::check(providers=...)).
     После этого retry — НИКАКОГО третьего прохода, независимо от
     результата (см. _run_retry — никогда не вызывает
     _maybe_enqueue_retry повторно)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
from reader.turkey_bot.unified.models import ProviderStatus, UnifiedCheckResult
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository

logger = logging.getLogger(__name__)

_PROVIDERS = ("gib", "avrasya", "kgm")

# См. задачу п.2 — 25 попыток на провайдера для планового мониторинга (35
# для ручной проверки, см. reader/turkey_bot/conversation.py::
# _MANUAL_MAX_ATTEMPTS — сознательно РАЗНЫЙ лимит для другого режима).
_SCHEDULED_MAX_ATTEMPTS = 25

# См. задачу п.3 — РОВНО один retry, РОВНО через 5 минут, только для
# providers, упавших именно на CAPTCHA (не на transport_error/
# rate_limited/unexpected — см. _CAPTCHA_RETRY_ERROR_TYPES ниже, тот же
# принцип, что и retry-цикл внутри check_service.py).
_RETRY_DELAY = timedelta(minutes=5)
_CAPTCHA_RETRY_ERROR_TYPES = frozenset({"captcha_unavailable", "captcha_rejected"})


class NotifierLike(Protocol):
    async def send(self, *, telegram_chat_id: int, text: str) -> None: ...


@dataclass(frozen=True)
class _PendingRetry:
    """Provider-specific retry state (см. задачу п.5) — ОДНА запись на
    (subscription, набор упавших providers) одного планового цикла.
    Полностью in-memory (см. reader/jobs/scheduler.py — тот же приём, что
    и у TurkeyMonitoringJob._last_run_slot: сбрасывается при рестарте
    процесса, без персистентного состояния — 5-минутное окно короче
    любого разумного интервала обслуживания)."""

    subscription_id: int
    telegram_user_id: int
    telegram_chat_id: int
    plate: str
    providers: tuple[str, ...]
    slot_label: str
    retry_at: datetime


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
        self._pending_retries: list[_PendingRetry] = []

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

    def has_due_retries(self, now: datetime) -> bool:
        """См. reader/turkey_bot/monitoring/scheduler_job.py::
        TurkeyMonitoringRetryJob.should_run — True, если хотя бы один
        запланированный retry уже наступил."""
        return any(retry.retry_at <= now for retry in self._pending_retries)

    async def run_due_retries(self, now: datetime) -> None:
        """См. задачу п.3 — обрабатывает ВСЕ наступившие retry (обычно
        один, но батч мог поставить несколько подписок в очередь на один
        и тот же +5 минут слот). Каждый retry удаляется из очереди СРАЗУ
        (до запуска), чтобы упавший retry не застрял и не повторялся
        бесконечно (см. задачу: "Никаких бесконечных retries")."""
        due = [retry for retry in self._pending_retries if retry.retry_at <= now]
        if not due:
            return
        self._pending_retries = [retry for retry in self._pending_retries if retry.retry_at > now]
        for retry in due:
            try:
                await self._run_retry(retry)
            except Exception:
                logger.exception(
                    "Turkey monitoring retry: ошибка при повторной проверке plate=%s providers=%s",
                    retry.plate, retry.providers,
                )

    async def _check_one(
        self, subscription: TurkeyMonitoringSubscription, *, slot_label: str, now: datetime,
    ) -> UnifiedCheckResult:
        previous_by_provider = {
            provider: self._runs.get_last_successful_provider_result(subscription.plate, provider)
            for provider in _PROVIDERS
        }

        result = await self._check_service.check(
            subscription.plate, max_attempts=_SCHEDULED_MAX_ATTEMPTS, mode="scheduled",
        )

        self._runs.save(
            result, telegram_user_id=subscription.telegram_user_id,
            telegram_chat_id=subscription.telegram_chat_id, initiator=f"monitoring_{slot_label}",
        )

        next_check_at = next_monitoring_slot(now)
        self._subscriptions.mark_checked(subscription.id, checked_at=now, next_check_at=next_check_at)

        await self._notify_changes(
            plate=subscription.plate, telegram_chat_id=subscription.telegram_chat_id,
            previous_by_provider=previous_by_provider, result=result,
        )
        self._maybe_enqueue_retry(subscription, result, slot_label=slot_label, now=now)

        return result

    async def _run_retry(self, retry: _PendingRetry) -> None:
        """См. задачу п.3/п.5 — проверяет ТОЛЬКО retry.providers (успешные
        providers первого прохода НЕ повторяются вообще, ни HTTP-запросом,
        ни отдельной строкой в UnifiedCheckResult, см.
        reader/turkey_bot/unified/check_service.py::check(providers=...)).
        Отдельная строка в turkey_check_runs/turkey_provider_results (
        initiator="monitoring_<slot>_retry") — НЕ обновление/слияние с
        первым проходом: это делает сравнение change-detection чистым
        (previous_by_provider здесь строится ТОЛЬКО для повторяемых
        providers, поэтому providers, уже сравненные и, если нужно,
        уведомлённые в первом проходе, здесь вообще не участвуют — см.
        задачу: "не создавать duplicate user notifications")."""
        previous_by_provider = {
            provider: self._runs.get_last_successful_provider_result(retry.plate, provider)
            for provider in retry.providers
        }

        result = await self._check_service.check(
            retry.plate, max_attempts=_SCHEDULED_MAX_ATTEMPTS, mode="scheduled_retry",
            providers=retry.providers,
        )

        self._runs.save(
            result, telegram_user_id=retry.telegram_user_id,
            telegram_chat_id=retry.telegram_chat_id, initiator=f"monitoring_{retry.slot_label}_retry",
        )

        await self._notify_changes(
            plate=retry.plate, telegram_chat_id=retry.telegram_chat_id,
            previous_by_provider=previous_by_provider, result=result,
        )
        logger.info(
            "Turkey monitoring retry finished: plate=%s providers=%s slot=%s",
            retry.plate, retry.providers, retry.slot_label,
        )
        # НИКОГДА не вызывает _maybe_enqueue_retry снова здесь (см. задачу:
        # "После +5 минут третьего retry нет" / "Никаких бесконечных
        # retries") — если провайдер всё ещё ERROR, он остаётся ERROR до
        # следующего обычного слота (13:00/21:00), next_check_at на это
        # никак не влияет (уже выставлен в первом проходе).

    async def _notify_changes(
        self, *, plate: str, telegram_chat_id: int, previous_by_provider, result: UnifiedCheckResult,
    ) -> None:
        """Общий хвост для первого прохода И retry — ERROR-провайдеры
        НИКОГДА не сравниваются (см. reader/turkey_bot/monitoring/
        change_detector.py::detect_changes — не меняется этой задачей,
        безопасная семантика уже гарантирована там)."""
        changes = detect_changes(previous_by_provider, result)
        if not changes:
            logger.info("Turkey monitoring: без изменений (plate=%s)", plate)
            return
        for change in changes:
            text = format_change_notification(plate, change)
            await self._notifier.send(telegram_chat_id=telegram_chat_id, text=text)

    def _maybe_enqueue_retry(
        self, subscription: TurkeyMonitoringSubscription, result: UnifiedCheckResult,
        *, slot_label: str, now: datetime,
    ) -> None:
        """См. задачу п.3 — retry через 5 минут ТОЛЬКО для providers,
        упавших именно на CAPTCHA (captcha_unavailable/captcha_rejected).
        transport_error/rate_limited/unexpected НЕ считаются "неполными
        из-за CAPTCHA" — retry для них не создаётся (тот же принцип, что
        и внутри check_service.py — retry существует ИСКЛЮЧИТЕЛЬНО для
        captcha-специфичных исходов, не общий "повторить при любой
        ошибке")."""
        failed_providers = tuple(
            p.provider for p in result.providers
            if p.status == ProviderStatus.ERROR and p.error_type in _CAPTCHA_RETRY_ERROR_TYPES
        )
        if not failed_providers:
            return
        self._pending_retries.append(_PendingRetry(
            subscription_id=subscription.id,
            telegram_user_id=subscription.telegram_user_id,
            telegram_chat_id=subscription.telegram_chat_id,
            plate=subscription.plate,
            providers=failed_providers,
            slot_label=slot_label,
            retry_at=now + _RETRY_DELAY,
        ))
        logger.info(
            "Turkey monitoring: запланирован retry через 5 минут plate=%s providers=%s slot=%s",
            subscription.plate, failed_providers, slot_label,
        )
