"""Тесты calendar-based расписания мониторинга (см. design report
"Перестроить UX Turkey test bot" п.8/п.6): СТРОГО 13:00 и 21:00
Europe/Istanbul, НЕ "каждые N часов", никакого третьего запуска в день.
Никаких реальных задач/сети — только TurkeyMonitoringJob.should_run() на
фиксированных datetime."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from reader.turkey_bot_test.monitoring import scheduler_job as scheduler_job_module
from reader.turkey_bot_test.monitoring.scheduler_job import (
    TURKEY_MONITORING_TZ,
    TurkeyMonitoringJob,
    next_monitoring_slot,
)


class _FixedDatetime(datetime):
    """Подменяет datetime.now() внутри scheduler_job.py на фиксированное
    значение (см. TurkeyMonitoringJob.run() — вызывает datetime.now(utc)
    самостоятельно, без параметра, т.к. это требование reader.jobs.base.Job
    интерфейса: run() не принимает аргументов)."""

    _fixed: "datetime"

    @classmethod
    def now(cls, tz=None):
        return cls._fixed if tz is None else cls._fixed.astimezone(tz)


def _freeze_run_at(monkeypatch, fixed_now: datetime) -> None:
    frozen = _FixedDatetime
    frozen._fixed = fixed_now
    monkeypatch.setattr(scheduler_job_module, "datetime", frozen)


class _UnusedMonitoringService:
    async def run_scheduled_batch(self, slot_label):
        raise AssertionError("run() не должен вызываться в тестах should_run")


class _RecordingMonitoringService:
    def __init__(self):
        self.calls: list[str] = []

    async def run_scheduled_batch(self, slot_label):
        self.calls.append(slot_label)


def _istanbul(y, m, d, h, minute) -> datetime:
    return datetime(y, m, d, h, minute, tzinfo=TURKEY_MONITORING_TZ)


async def test_should_run_true_exactly_at_13_00_istanbul():
    job = TurkeyMonitoringJob(_UnusedMonitoringService())
    now = _istanbul(2026, 9, 20, 13, 0).astimezone(timezone.utc)
    assert await job.should_run(now) is True


async def test_should_run_true_exactly_at_21_00_istanbul():
    job = TurkeyMonitoringJob(_UnusedMonitoringService())
    now = _istanbul(2026, 9, 20, 21, 0).astimezone(timezone.utc)
    assert await job.should_run(now) is True


@pytest.mark.parametrize("hour,minute", [
    (0, 0), (9, 0), (12, 59), (13, 1), (13, 30), (20, 59), (21, 1), (23, 59),
])
async def test_should_run_false_outside_slots(hour, minute):
    job = TurkeyMonitoringJob(_UnusedMonitoringService())
    now = _istanbul(2026, 9, 20, hour, minute).astimezone(timezone.utc)
    assert await job.should_run(now) is False


async def test_13_00_utc_is_not_the_same_as_13_00_istanbul():
    """Regression-guard для design report п.6 — расписание СТРОГО по
    Europe/Istanbul, а не по времени сервера/UTC. Europe/Istanbul = UTC+3
    (без летнего времени с 2016 года), поэтому 13:00 UTC = 16:00
    Istanbul — НЕ должно триггерить запуск."""
    naive_13_utc = datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc)
    job = TurkeyMonitoringJob(_UnusedMonitoringService())
    assert await job.should_run(naive_13_utc) is False


async def test_should_run_only_once_per_slot_per_day(monkeypatch):
    """Один и тот же слот (13:00) не должен триггерить run() дважды в
    один день — даже если Scheduler тикнет несколько раз подряд в ту же
    минуту (см. reader/jobs/scheduler.py — poll каждые 30с)."""
    job = TurkeyMonitoringJob(_RecordingMonitoringService())
    now = _istanbul(2026, 9, 20, 13, 0).astimezone(timezone.utc)
    _freeze_run_at(monkeypatch, now)

    assert await job.should_run(now) is True
    await job.run()
    assert await job.should_run(now) is False

    later_same_minute = now
    assert await job.should_run(later_same_minute) is False


async def test_should_run_again_next_day_same_slot(monkeypatch):
    job = TurkeyMonitoringJob(_RecordingMonitoringService())
    day1 = _istanbul(2026, 9, 20, 13, 0).astimezone(timezone.utc)
    day2 = _istanbul(2026, 9, 21, 13, 0).astimezone(timezone.utc)

    _freeze_run_at(monkeypatch, day1)
    assert await job.should_run(day1) is True
    await job.run()
    assert await job.should_run(day2) is True


async def test_no_third_slot_exists_in_a_day():
    """Ровно 2 calendar slots в сутки — не больше (см. design report:
    "строго два calendar slots в сутки")."""
    triggered_hours = []
    for hour in range(24):
        for minute in (0,):
            now = _istanbul(2026, 9, 20, hour, minute).astimezone(timezone.utc)
            fresh_job = TurkeyMonitoringJob(_UnusedMonitoringService())
            if await fresh_job.should_run(now):
                triggered_hours.append(hour)
    assert triggered_hours == [13, 21]


def test_next_monitoring_slot_from_morning_returns_13_00_today():
    now = _istanbul(2026, 9, 20, 8, 0).astimezone(timezone.utc)
    next_slot = next_monitoring_slot(now)
    local = next_slot.astimezone(TURKEY_MONITORING_TZ)
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2026, 9, 20, 13, 0)


def test_next_monitoring_slot_from_afternoon_returns_21_00_today():
    now = _istanbul(2026, 9, 20, 14, 0).astimezone(timezone.utc)
    next_slot = next_monitoring_slot(now)
    local = next_slot.astimezone(TURKEY_MONITORING_TZ)
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2026, 9, 20, 21, 0)


def test_next_monitoring_slot_after_last_slot_rolls_over_to_tomorrow():
    now = _istanbul(2026, 9, 20, 22, 0).astimezone(timezone.utc)
    next_slot = next_monitoring_slot(now)
    local = next_slot.astimezone(TURKEY_MONITORING_TZ)
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2026, 9, 21, 13, 0)


def test_turkey_monitoring_tz_is_europe_istanbul_not_business_tz():
    """См. design report решение п.6 — ОТДЕЛЬНАЯ константа, НЕ
    settings.fine_monitor.timezone (default Asia/Tbilisi)."""
    assert TURKEY_MONITORING_TZ == ZoneInfo("Europe/Istanbul")
    assert str(TURKEY_MONITORING_TZ) != "Asia/Tbilisi"
