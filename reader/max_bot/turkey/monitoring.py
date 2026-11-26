"""Плановый мониторинг MAX-подписчиков Турции — раз в сутки с 22:30
Europe/Istanbul, ПОСЛЕ планового прогона Telegram-бота (21:00).

Без второго сбора данных: номер = одна машина, поэтому сначала берётся
последний успешный результат каждого провайдера по номеру из общей истории
проверок (turkey_check_runs); если он получен после сегодняшнего слота 21:00
(Telegram-мониторинг уже проверил номер), повторного запроса к GİB/Avrasya/
KGM нет. Только провайдеры без свежего результата проверяются — ОДНОЙ
проверкой на номер (не на подписчика), строго по очереди номеров; результат
сохраняется в общую историю (initiator max_monitoring).

Уведомления — по подписчику: detect_changes(что он уже видел, текущее) с
теми же правилами и текстами, что у Telegram-мониторинга (ERROR не
сравнивается, GİB/Avrasya/KGM — отдельными сообщениями). Снимок провайдера
обновляется только после успешной отправки его изменения: недоставленное
уведомление будет найдено снова при следующем прогоне. Провайдер, для
которого у подписчика ещё нет снимка, получает его молча — первоначальный
снимок задолженности не рассылается.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from collections.abc import Callable
from typing import Protocol

from reader.jobs.base import Job
from reader.max_bot.api import MaxApiError
from reader.max_bot.turkey.repository import MaxTrMonitoring, MaxTurkeyRepository
from reader.max_bot.turkey.service import (
    MAX_RUN_OWNER,
    PROVIDERS,
    _as_utc,
    last_telegram_slot_start,
)
from reader.turkey_bot.monitoring.change_detector import detect_changes
from reader.turkey_bot.monitoring.notification_texts import format_change_notification
from reader.turkey_bot.monitoring.scheduler_job import TURKEY_MONITORING_TZ
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService
from reader.turkey_bot.unified.models import (
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    total_amount_for,
)
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository

logger = logging.getLogger(__name__)

INITIATOR_MONITORING = "max_monitoring"
MAX_MONITORING_SLOT = (22, 30)
TELEGRAM_QUIET_PERIOD = timedelta(minutes=10)
_NO_WAIT_AFTER = (23, 30)
# Тот же лимит попыток на провайдера, что у планового Telegram-мониторинга.
_SCHEDULED_MAX_ATTEMPTS = 25


class MaxSenderLike(Protocol):
    async def send_notification(self, max_user_id: int, text: str, keyboard) -> None: ...

    def mark_blocked(self, max_user_id: int) -> None: ...


@dataclass(frozen=True)
class MonitoringBatchResult:
    plates: int
    checks: int
    notified: int
    failed: int


class MaxTurkeyMonitoring:
    def __init__(
        self,
        repository: MaxTurkeyRepository,
        run_repository: TurkeyCheckRunRepository,
        check_service: UnifiedTurkeyCheckService,
        sender: MaxSenderLike,
    ):
        self._repository = repository
        self._runs = run_repository
        self._check_service = check_service
        self._sender = sender

    async def run_batch(self, *, now: datetime | None = None) -> MonitoringBatchResult:
        now = now or datetime.now(timezone.utc)
        slot_start = last_telegram_slot_start(now)
        plates = self._repository.active_plates()
        checks = notified = failed = 0
        for plate in plates:
            try:
                checked, sent, not_sent = await self._process_plate(plate, slot_start=slot_start, now=now)
            except Exception:
                logger.exception("MAX TR monitoring: ошибка обработки номера (остальные продолжаются)")
                continue
            checks += checked
            notified += sent
            failed += not_sent
        logger.info(
            "MAX TR monitoring: номеров %d, проверок %d, уведомлений %d, не доставлено %d",
            len(plates), checks, notified, failed,
        )
        return MonitoringBatchResult(plates=len(plates), checks=checks, notified=notified, failed=failed)

    async def _current_state(
        self, plate: str, *, slot_start: datetime, now: datetime,
    ) -> tuple[UnifiedCheckResult, int]:
        current: dict[str, ProviderCheckResult] = {}
        missing: list[str] = []
        for provider in PROVIDERS:
            last_attempt, last_success = self._runs.get_latest_provider_results_for_plate(plate, provider)
            if last_success is not None and _as_utc(last_success.finished_at) >= slot_start:
                current[provider] = last_success.result
            elif last_attempt is not None and _as_utc(last_attempt.finished_at) >= slot_start:
                # Telegram-прогон уже пытался сегодня и получил ERROR — второй
                # сессии к сбоящему провайдеру нет; ERROR и так не сравнивается.
                continue
            else:
                missing.append(provider)
        checks = 0
        started_at = now
        if missing:
            result = await self._check_service.check(
                plate, max_attempts=_SCHEDULED_MAX_ATTEMPTS, mode="scheduled", providers=tuple(missing),
            )
            self._runs.save(result, telegram_user_id=MAX_RUN_OWNER, telegram_chat_id=MAX_RUN_OWNER,
                            initiator=INITIATOR_MONITORING)
            checks = 1
            started_at = result.started_at
            for provider_result in result.providers:
                current[provider_result.provider] = provider_result
        providers = tuple(current[provider] for provider in PROVIDERS if provider in current)
        unified = UnifiedCheckResult(
            plate=plate, started_at=started_at, finished_at=now, overall_status=derive_overall_status(providers),
            total_amount=total_amount_for(providers), providers=providers,
        )
        return unified, checks

    async def _process_plate(self, plate: str, *, slot_start: datetime, now: datetime) -> tuple[int, int, int]:
        subscriptions = self._repository.active_for_plate(plate)
        if not subscriptions:
            return 0, 0, 0
        unified, checks = await self._current_state(plate, slot_start=slot_start, now=now)
        sent = not_sent = 0
        for subscription in subscriptions:
            ok, failed = await self._notify_subscriber(subscription, unified)
            sent += ok
            not_sent += failed
        return checks, sent, not_sent

    async def _notify_subscriber(self, subscription: MaxTrMonitoring, current: UnifiedCheckResult) -> tuple[int, int]:
        seen = self._repository.get_seen(subscription.id)
        comparable = []
        for provider_result in current.providers:
            if provider_result.status == ProviderStatus.ERROR:
                continue
            if provider_result.provider in seen:
                comparable.append(provider_result)
            else:
                # Первая успешная проверка провайдера для подписчика — базовая
                # точка без уведомления.
                self._repository.set_seen(subscription.id, provider_result)
        if not comparable:
            return 0, 0
        changes = detect_changes(
            seen, UnifiedCheckResult(
                plate=current.plate, started_at=current.started_at, finished_at=current.finished_at,
                overall_status=current.overall_status, total_amount=current.total_amount, providers=tuple(comparable),
            ),
        )
        changed_providers = {change.provider for change in changes}
        for provider_result in comparable:
            if provider_result.provider not in changed_providers:
                self._repository.set_seen(subscription.id, provider_result)

        sent = failed = 0
        by_provider = {provider_result.provider: provider_result for provider_result in comparable}
        for change in changes:
            text = format_change_notification(current.plate, change)
            try:
                await self._sender.send_notification(subscription.max_user_id, text, None)
            except MaxApiError as exc:
                logger.warning(
                    "MAX TR monitoring: уведомление подписке id=%s не доставлено: %s", subscription.id, exc.code,
                )
                if exc.recipient_unavailable:
                    # Пользователь заблокировал бота — остальные изменения ему
                    # тоже не уйдут; снимки не обновляются.
                    self._sender.mark_blocked(subscription.max_user_id)
                    failed = len(changes) - sent
                    break
                failed += 1
                continue
            except Exception:
                failed += 1
                logger.exception("MAX TR monitoring: уведомление подписке id=%s не доставлено", subscription.id)
                continue
            self._repository.set_seen(subscription.id, by_provider[change.provider])
            sent += 1
        return sent, failed


class MaxTurkeyMonitoringJob(Job):
    """Раз в сутки, в окне 22:30–23:59 Europe/Istanbul (дедуп по дате в
    памяти — тот же приём, что у TurkeyMonitoringJob). Пока плановый прогон
    Telegram ещё пишет результаты (последний — меньше TELEGRAM_QUIET_PERIOD
    назад), запуск откладывается: проверки двух процессов не пересекаются, а
    номера, которые Telegram ещё не проверил, не проверяются дважды. С 23:30
    — без ожидания."""

    name = "max_turkey_monitoring"

    def __init__(
        self, monitoring: MaxTurkeyMonitoring, *,
        telegram_last_finished_at: Callable[[], datetime | None] = lambda: None,
    ) -> None:
        self._monitoring = monitoring
        self._telegram_last_finished_at = telegram_last_finished_at
        self._last_run_date: date | None = None

    async def should_run(self, now: datetime) -> bool:
        local = now.astimezone(TURKEY_MONITORING_TZ)
        if local.date() == self._last_run_date or (local.hour, local.minute) < MAX_MONITORING_SLOT:
            return False
        if (local.hour, local.minute) >= _NO_WAIT_AFTER:
            return True
        last = self._telegram_last_finished_at()
        return last is None or now - last >= TELEGRAM_QUIET_PERIOD

    async def run(self) -> None:
        self._last_run_date = datetime.now(timezone.utc).astimezone(TURKEY_MONITORING_TZ).date()
        await self._monitoring.run_batch()
