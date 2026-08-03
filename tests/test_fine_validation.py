"""
Тесты reader/fines/validation.py — чистые функции, без Repository/Telegram/сети.
"""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.models import FineMonitoringTask  # noqa: E402
from reader.fines.validation import (  # noqa: E402
    FineValidationError,
    normalize_car_number,
    parse_date,
    resolve_monitoring_period,
    validate_no_overlap,
)


def _task(id_, start, end) -> FineMonitoringTask:
    return FineMonitoringTask(
        id=id_,
        car_number="B957MA09",
        label=None,
        start_date=start,
        end_date=end,
        status="active",
        telegram_chat_id=-100999,
        created_by_user_id=111,
        created_at=None,
        updated_at=None,
        last_checked_at=None,
        last_check_status=None,
        last_error=None,
    )


# ---- normalize_car_number ----


def test_normalize_car_number_uppercases_and_strips():
    assert normalize_car_number("  b957ma09  ") == "B957MA09"


def test_normalize_car_number_rejects_empty():
    with pytest.raises(FineValidationError):
        normalize_car_number("   ")


def test_normalize_car_number_rejects_special_characters():
    with pytest.raises(FineValidationError):
        normalize_car_number("B957-MA09")


def test_normalize_car_number_rejects_spaces_inside():
    with pytest.raises(FineValidationError):
        normalize_car_number("B957 MA09")


# ---- parse_date ----


def test_parse_date_parses_valid_date():
    assert parse_date("03.08.2026") == date(2026, 8, 3)


def test_parse_date_rejects_invalid_format():
    with pytest.raises(FineValidationError):
        parse_date("2026-08-03")


def test_parse_date_rejects_garbage():
    with pytest.raises(FineValidationError):
        parse_date("не дата")


# ---- resolve_monitoring_period ----


def test_resolve_monitoring_period_defaults_to_today_plus_30_days():
    today = date(2026, 8, 3)

    start, end = resolve_monitoring_period(None, None, today=today)

    assert start == today
    assert end == date(2026, 9, 2)


def test_resolve_monitoring_period_parses_explicit_dates():
    start, end = resolve_monitoring_period(
        "03.08.2026", "13.08.2026", today=date(2026, 8, 1)
    )

    assert start == date(2026, 8, 3)
    assert end == date(2026, 8, 13)


def test_resolve_monitoring_period_rejects_end_before_start():
    with pytest.raises(FineValidationError):
        resolve_monitoring_period("13.08.2026", "03.08.2026", today=date(2026, 8, 1))


def test_resolve_monitoring_period_rejects_only_one_date_given():
    with pytest.raises(FineValidationError):
        resolve_monitoring_period("03.08.2026", None, today=date(2026, 8, 1))


def test_resolve_monitoring_period_accepts_same_start_and_end():
    start, end = resolve_monitoring_period(
        "03.08.2026", "03.08.2026", today=date(2026, 8, 1)
    )
    assert start == end == date(2026, 8, 3)


# ---- validate_no_overlap ----


def test_validate_no_overlap_passes_when_no_existing_tasks():
    validate_no_overlap(date(2026, 8, 3), date(2026, 8, 13), [])


def test_validate_no_overlap_passes_when_periods_do_not_touch():
    existing = [_task(1, date(2026, 7, 1), date(2026, 7, 31))]
    validate_no_overlap(date(2026, 8, 1), date(2026, 8, 31), existing)


def test_validate_no_overlap_rejects_overlapping_period():
    existing = [_task(1, date(2026, 8, 1), date(2026, 8, 31))]

    with pytest.raises(FineValidationError):
        validate_no_overlap(date(2026, 8, 15), date(2026, 9, 15), existing)


def test_validate_no_overlap_rejects_when_new_period_fully_inside_existing():
    existing = [_task(1, date(2026, 8, 1), date(2026, 8, 31))]

    with pytest.raises(FineValidationError):
        validate_no_overlap(date(2026, 8, 10), date(2026, 8, 20), existing)


def test_validate_no_overlap_rejects_touching_boundary():
    # end_date нового периода == start_date существующего — включительная
    # граница, тоже пересечение.
    existing = [_task(1, date(2026, 8, 10), date(2026, 8, 20))]

    with pytest.raises(FineValidationError):
        validate_no_overlap(date(2026, 8, 1), date(2026, 8, 10), existing)
