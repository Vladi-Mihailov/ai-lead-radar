"""Новые настройки fine_monitor для статусов оплаты и вечерней перепроверки
(reader/settings.py): безопасные значения по умолчанию (перепроверка
выключена, маркер "уже оплачено" пуст) и разбор переопределений из
config.yaml; сборка PaymentPageMarkers в reader/public_bot/main.py."""

import sys
from datetime import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml

from reader.fines.payment_status import MIA_PAYMENT_FAILED_MESSAGE
from reader.public_bot.main import build_payment_page_markers
from reader.settings import load_settings


def _load(tmp_path, monkeypatch, fine_monitor_overrides: dict):
    for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_PHONE"):
        monkeypatch.setenv(name, "1")
    raw = yaml.safe_load((PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8")) or {}
    raw.setdefault("fine_monitor", {}).update(fine_monitor_overrides)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return load_settings(path).fine_monitor


def test_defaults_are_safe(tmp_path, monkeypatch):
    fine_monitor = _load(tmp_path, monkeypatch, {})

    assert fine_monitor.daily_recheck_enabled is False
    assert fine_monitor.daily_recheck_time == time(23, 0)
    assert fine_monitor.daily_recheck_cutoff_time == time(23, 45)
    assert fine_monitor.daily_recheck_inter_car_delay_seconds == 8.0
    assert fine_monitor.daily_recheck_batch_size == 10
    assert fine_monitor.daily_recheck_batch_pause_seconds == 60.0
    assert fine_monitor.daily_recheck_max_plates == 200
    assert fine_monitor.daily_recheck_max_consecutive_errors == 2
    assert fine_monitor.fine_disappearance_min_gap_hours == 12.0
    markers = build_payment_page_markers(fine_monitor)
    assert markers.already_paid == ()
    assert markers.payment_failed == (MIA_PAYMENT_FAILED_MESSAGE,)
    assert markers.technical_error == ()


def test_overrides_are_parsed(tmp_path, monkeypatch):
    fine_monitor = _load(tmp_path, monkeypatch, {
        "daily_recheck_enabled": True,
        "daily_recheck_time": "22:30",
        "daily_recheck_cutoff_time": "23:40",
        "daily_recheck_inter_car_delay_seconds": 12,
        "daily_recheck_batch_size": 5,
        "daily_recheck_batch_pause_seconds": 90,
        "daily_recheck_max_plates": 80,
        "daily_recheck_max_consecutive_errors": 3,
        "fine_disappearance_min_gap_hours": 20,
        "payment_page_already_paid_markers": ["ქვითარი უკვე გადახდილია"],
        "payment_page_failed_markers": ["payment failed"],
        "payment_page_technical_error_markers": ["technical error"],
    })

    assert fine_monitor.daily_recheck_enabled is True
    assert (fine_monitor.daily_recheck_time, fine_monitor.daily_recheck_cutoff_time) == (time(22, 30), time(23, 40))
    assert fine_monitor.daily_recheck_inter_car_delay_seconds == 12.0
    assert fine_monitor.daily_recheck_batch_size == 5
    assert fine_monitor.daily_recheck_max_plates == 80
    assert fine_monitor.fine_disappearance_min_gap_hours == 20.0
    markers = build_payment_page_markers(fine_monitor)
    assert markers.already_paid == ("ქვითარი უკვე გადახდილია",)
    assert markers.payment_failed == ("payment failed",)
    assert markers.technical_error == ("technical error",)
