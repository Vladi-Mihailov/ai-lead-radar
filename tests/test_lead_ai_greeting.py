"""Тесты reader/lead_ai/greeting.py::tbilisi_greeting — приветствие
зависит от текущего времени в Asia/Tbilisi, а не придумывается моделью
(см. задачу)."""

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.lead_ai.greeting import tbilisi_greeting  # noqa: E402


def test_morning_hour_returns_morning_greeting():
    # 07:00 UTC = 11:00 Asia/Tbilisi (UTC+4, без перехода на летнее время).
    moment = datetime(2026, 1, 15, 7, 0, tzinfo=timezone.utc)
    assert tbilisi_greeting(moment) == "Доброе утро"


def test_midday_hour_returns_day_greeting():
    # 10:00 UTC = 14:00 Asia/Tbilisi.
    moment = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
    assert tbilisi_greeting(moment) == "Добрый день"


def test_evening_hour_returns_evening_greeting():
    # 16:00 UTC = 20:00 Asia/Tbilisi.
    moment = datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc)
    assert tbilisi_greeting(moment) == "Добрый вечер"


def test_late_night_hour_returns_evening_greeting():
    """Отдельного "ночь" нет (задача разрешает только 3 варианта) — ночь
    объединена с вечерним приветствием."""
    # 22:00 UTC = 02:00 Asia/Tbilisi (следующие сутки).
    moment = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)
    assert tbilisi_greeting(moment) == "Добрый вечер"


def test_naive_datetime_is_treated_as_utc():
    moment = datetime(2026, 1, 15, 10, 0)  # naive -> трактуется как UTC -> 14:00 Tbilisi
    assert tbilisi_greeting(moment) == "Добрый день"


def test_default_uses_real_current_time():
    """Без явного now — не должно падать и должно вернуть одно из трёх
    допустимых значений (см. задачу: только утро/день/вечер)."""
    assert tbilisi_greeting() in {"Доброе утро", "Добрый день", "Добрый вечер"}
