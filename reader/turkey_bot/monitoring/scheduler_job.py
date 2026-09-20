"""TurkeyMonitoringJob — реализация reader.jobs.base.Job (см. design
report п.4/решение "Вариант A": переиспользуем УЖЕ существующий,
bot-agnostic reader/jobs/{base.py,scheduler.py} ТОЛЬКО через импорт —
reader/jobs/* не меняется НИ строкой).

Расписание — СТРОГО calendar-based (см. задачу): 13:00 и 21:00
Europe/Istanbul, никакого "каждые N часов". Europe/Istanbul — ОТДЕЛЬНАЯ,
hardcoded константа (НЕ settings.fine_monitor.timezone — тот default
Asia/Tbilisi, Georgian-бизнес-конфиг, используемый TurkeyStatisticsService
для другой цели, см. design report)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from reader.jobs.base import Job

logger = logging.getLogger(__name__)

TURKEY_MONITORING_TZ = ZoneInfo("Europe/Istanbul")

# Строго два calendar slots в сутки (см. задачу) — (hour, minute) в
# Europe/Istanbul. Порядок важен для next_monitoring_slot() ниже.
_DAILY_SLOTS: tuple[tuple[int, int], ...] = ((13, 0), (21, 0))


def next_monitoring_slot(now: datetime) -> datetime:
    """Ближайший СЛЕДУЮЩИЙ слот (13:00 либо 21:00 Europe/Istanbul) СТРОГО
    после now — возвращается в UTC (для хранения в
    turkey_monitoring_subscriptions.next_check_at). Используется и при
    первом включении мониторинга, и после каждой плановой проверки."""
    local_now = now.astimezone(TURKEY_MONITORING_TZ)
    for hour, minute in _DAILY_SLOTS:
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > local_now:
            return candidate.astimezone(timezone.utc)
    # Оба сегодняшних слота уже прошли — первый слот завтра.
    tomorrow = local_now.date() + timedelta(days=1)
    hour, minute = _DAILY_SLOTS[0]
    candidate = datetime(
        tomorrow.year, tomorrow.month, tomorrow.day, hour, minute, tzinfo=TURKEY_MONITORING_TZ,
    )
    return candidate.astimezone(timezone.utc)


class TurkeyMonitoringJob(Job):
    """should_run(now) — True РОВНО в минуту одного из _DAILY_SLOTS
    Europe/Istanbul, не более одного раза в день на слот (in-memory dedup
    по (date, slot) — тот же приём, что и reader/jobs/fine_job.py::FineJob,
    сбрасывается при рестарте процесса, без персистентного состояния —
    достаточно для 2 фиксированных слотов в сутки)."""

    name = "turkey_monitoring"

    def __init__(self, monitoring_service) -> None:
        self._monitoring_service = monitoring_service
        self._last_run_slot: tuple[date, tuple[int, int]] | None = None

    async def should_run(self, now: datetime) -> bool:
        local = now.astimezone(TURKEY_MONITORING_TZ)
        slot = (local.hour, local.minute)
        if slot not in _DAILY_SLOTS:
            return False
        slot_key = (local.date(), slot)
        return slot_key != self._last_run_slot

    async def run(self) -> None:
        now = datetime.now(timezone.utc)
        local = now.astimezone(TURKEY_MONITORING_TZ)
        slot = (local.hour, local.minute)
        self._last_run_slot = (local.date(), slot)
        slot_label = f"{slot[0]:02d}:{slot[1]:02d}"
        logger.info("Turkey monitoring: запуск планового цикла (slot=%s Europe/Istanbul)", slot_label)
        await self._monitoring_service.run_scheduled_batch(slot_label)


class TurkeyMonitoringRetryJob(Job):
    """См. задачу "Retry orchestration Unified Turkey checks" п.3 — РОВНО
    один retry через 5 минут для providers, упавших на CAPTCHA в первом
    проходе (см. reader/turkey_bot/monitoring/monitoring_service.py::
    TurkeyMonitoringService._maybe_enqueue_retry/run_due_retries).
    ОТДЕЛЬНЫЙ Job (не часть TurkeyMonitoringJob выше) — should_run здесь
    не привязан к calendar slot 13:00/21:00, а к тому, наступил ли уже
    хотя бы один запланированный retry (тикает вместе с общим poll
    Scheduler'а раз в 30с, см. reader/jobs/scheduler.py — этого достаточно
    для 5-минутного окна)."""

    name = "turkey_monitoring_retry"

    def __init__(self, monitoring_service) -> None:
        self._monitoring_service = monitoring_service

    async def should_run(self, now: datetime) -> bool:
        return self._monitoring_service.has_due_retries(now)

    async def run(self) -> None:
        await self._monitoring_service.run_due_retries(datetime.now(timezone.utc))
