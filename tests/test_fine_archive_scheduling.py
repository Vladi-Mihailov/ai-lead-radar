"""
Тесты чистых функций reader/fines/archive_scheduling.py — распределение
next_archive_check_at по дням. Без единой SQLite-базы: только математика.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.archive_scheduling import (  # noqa: E402
    build_archive_schedule,
    distribute_evenly,
    local_time_to_utc,
)

_TBILISI = ZoneInfo("Asia/Tbilisi")


# ---- distribute_evenly ----


def test_distribute_evenly_998_over_30_days_matches_expected_pattern():
    sizes = distribute_evenly(998, 30)

    assert len(sizes) == 30
    assert sum(sizes) == 998
    assert sizes[:8] == [34] * 8
    assert sizes[8:] == [33] * 22
    assert max(sizes) - min(sizes) <= 1


def test_distribute_evenly_exact_division_gives_equal_buckets():
    sizes = distribute_evenly(900, 30)

    assert sizes == [30] * 30
    assert sum(sizes) == 900


def test_distribute_evenly_bucket_size_difference_never_exceeds_one():
    for count in (0, 1, 7, 29, 30, 31, 59, 1000, 12345):
        sizes = distribute_evenly(count, 30)
        assert sum(sizes) == count
        assert max(sizes) - min(sizes) <= 1


def test_distribute_evenly_zero_count_gives_all_empty_buckets():
    sizes = distribute_evenly(0, 30)

    assert sizes == [0] * 30


def test_distribute_evenly_rejects_non_positive_buckets():
    with pytest.raises(ValueError):
        distribute_evenly(10, 0)


def test_distribute_evenly_rejects_negative_count():
    with pytest.raises(ValueError):
        distribute_evenly(-1, 30)


# ---- local_time_to_utc ----


def test_local_time_to_utc_converts_tbilisi_04_00_correctly():
    # Тбилиси — UTC+4 круглый год (без перехода на летнее время).
    result = local_time_to_utc(date(2026, 9, 1), 4, _TBILISI)

    assert result == datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    assert result.tzinfo is not None


# ---- build_archive_schedule ----


def test_build_archive_schedule_covers_every_task_id_exactly_once():
    task_ids = list(range(144, 1142))  # 998 задач, как в production-партии
    assert len(task_ids) == 998

    schedule = build_archive_schedule(
        task_ids, start_date=date(2026, 9, 1), days=30, hour=4, tz=_TBILISI,
    )

    assert set(schedule) == set(task_ids)
    assert len(schedule) == 998


def test_build_archive_schedule_distributes_998_tasks_into_30_days_evenly():
    task_ids = list(range(144, 1142))

    schedule = build_archive_schedule(
        task_ids, start_date=date(2026, 9, 1), days=30, hour=4, tz=_TBILISI,
    )

    counts_by_day = {}
    for due_at in schedule.values():
        day = due_at.astimezone(_TBILISI).date()
        counts_by_day[day] = counts_by_day.get(day, 0) + 1

    assert len(counts_by_day) == 30
    sizes = sorted(counts_by_day.values(), reverse=True)
    assert sizes[:8] == [34] * 8
    assert sizes[8:] == [33] * 22
    assert max(counts_by_day.values()) - min(counts_by_day.values()) <= 1


def test_build_archive_schedule_assigns_lowest_ids_to_earliest_day():
    task_ids = [10, 11, 12, 13, 14]  # 5 задач на 5 дней -> по 1 в день

    schedule = build_archive_schedule(
        task_ids, start_date=date(2026, 9, 1), days=5, hour=4, tz=_TBILISI,
    )

    days = {task_id: due_at.astimezone(_TBILISI).date() for task_id, due_at in schedule.items()}
    assert days[10] == date(2026, 9, 1)
    assert days[11] == date(2026, 9, 2)
    assert days[12] == date(2026, 9, 3)
    assert days[13] == date(2026, 9, 4)
    assert days[14] == date(2026, 9, 5)


def test_build_archive_schedule_uses_configured_hour():
    schedule = build_archive_schedule(
        [1], start_date=date(2026, 9, 1), days=30, hour=4, tz=_TBILISI,
    )

    due_at = schedule[1]
    assert due_at.astimezone(_TBILISI).time().hour == 4
    assert due_at.astimezone(_TBILISI).time().minute == 0


def test_build_archive_schedule_is_deterministic_for_same_inputs():
    task_ids = list(range(1, 51))

    first = build_archive_schedule(task_ids, start_date=date(2026, 9, 1), days=30, hour=4, tz=_TBILISI)
    second = build_archive_schedule(task_ids, start_date=date(2026, 9, 1), days=30, hour=4, tz=_TBILISI)

    assert first == second


def test_build_archive_schedule_empty_task_list_gives_empty_schedule():
    schedule = build_archive_schedule([], start_date=date(2026, 9, 1), days=30, hour=4, tz=_TBILISI)

    assert schedule == {}
