"""
Тесты reader/time_display.py — единственное место конвертации UTC ->
Asia/Tbilisi для человекочитаемого отображения (см. задачу про перевод
отображения времени операторам на время Тбилиси). Внутреннее хранение/
сравнение datetime нигде не меняется — эти тесты проверяют только сам
helper.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.time_display import TBILISI_TZ, format_tbilisi, to_tbilisi  # noqa: E402


def test_to_tbilisi_converts_utc_to_utc_plus_4():
    utc_value = datetime(2026, 8, 14, 11, 44, tzinfo=timezone.utc)

    local = to_tbilisi(utc_value)

    assert local.hour == 15
    assert local.minute == 44
    assert local.utcoffset() == timedelta(hours=4)
    # Тот же момент времени — не сдвиг данных, только представление.
    assert local == utc_value


def test_to_tbilisi_crosses_midnight_forward():
    """21:00 UTC -> 01:00 следующего дня по Тбилиси (UTC+4) — переход
    через полночь должен сдвинуть и дату, не только время."""
    utc_value = datetime(2026, 8, 13, 21, 0, tzinfo=timezone.utc)

    local = to_tbilisi(utc_value)

    assert local.date() == datetime(2026, 8, 14).date()
    assert local.hour == 1
    assert local.minute == 0


def test_to_tbilisi_treats_naive_datetime_as_utc_not_local():
    """Naive datetime (без tzinfo) — как приходит, например,
    FineMonitoringTask.last_checked_at из SQLite CURRENT_TIMESTAMP —
    трактуется как UTC явно, а не как уже локальное время (см. задачу:
    "naive datetime не трактовать молча как локальное время")."""
    naive_value = datetime(2026, 8, 14, 11, 44)

    local = to_tbilisi(naive_value)

    assert local.hour == 15
    assert local.minute == 44
    assert local.tzinfo is not None


def test_to_tbilisi_is_idempotent_representation_of_same_instant():
    utc_value = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    local = to_tbilisi(utc_value)

    assert local.astimezone(timezone.utc) == utc_value


def test_format_tbilisi_default_format_and_suffix():
    utc_value = datetime(2026, 8, 14, 11, 44, tzinfo=timezone.utc)

    assert format_tbilisi(utc_value) == "2026-08-14 15:44 по Тбилиси"


def test_format_tbilisi_custom_format_without_suffix():
    utc_value = datetime(2026, 8, 14, 11, 44, tzinfo=timezone.utc)

    assert format_tbilisi(utc_value, fmt="%d.%m.%Y %H:%M", suffix=None) == "14.08.2026 15:44"


def test_tbilisi_tz_has_no_dst_offset_variation():
    """Asia/Tbilisi не переходит на летнее/зимнее время (задача явно
    запрещает вручную реализовывать DST) — смещение должно быть
    одинаковым и зимой, и летом."""
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    assert to_tbilisi(winter).utcoffset() == timedelta(hours=4)
    assert to_tbilisi(summer).utcoffset() == timedelta(hours=4)
    assert to_tbilisi(winter).utcoffset() == to_tbilisi(summer).utcoffset()


def test_tbilisi_tz_constant_is_asia_tbilisi():
    assert str(TBILISI_TZ) == "Asia/Tbilisi"
