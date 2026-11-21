"""Настройки суммы к оплате (fine_monitor.payable_amount_*, reader/settings.py):
всё выключено по умолчанию, без merch_id/page_id сессий нет, опечатки
останавливают старт; сборка PayableAmountService в reader/public_bot/main.py.
Сети нет."""

import asyncio
import sys
from datetime import time, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
import yaml  # noqa: E402

from reader.fines.payable_amount import PayableAmountRepository  # noqa: E402
from reader.fines.payment_status import FineStatusRepository, FineStatusTracker  # noqa: E402
from reader.public_bot.main import build_payable_amount_service  # noqa: E402
from reader.public_bot.payable_amount_service import STATUS_NOT_CONFIGURED, ReceiptRef  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.settings import ConfigError, load_settings  # noqa: E402


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


def test_everything_is_off_by_default(tmp_path, monkeypatch):
    fine_monitor = _load(tmp_path, monkeypatch, {})
    assert fine_monitor.payable_amount_enabled is False
    assert fine_monitor.payable_amount_auto_checks_enabled is False
    assert fine_monitor.payable_amount_gc_merchant_id == "" and fine_monitor.payable_amount_gc_page_id == ""
    assert fine_monitor.payable_amount_gc_base_url == "https://mpi.gc.ge"
    assert fine_monitor.payable_amount_gc_amount_unit == "major"
    assert fine_monitor.payable_amount_auto_time == time(13, 0)
    assert fine_monitor.payable_amount_auto_cutoff_time == time(14, 0)
    assert fine_monitor.payable_amount_max_sessions_per_day == 40
    assert fine_monitor.payable_amount_max_consecutive_errors == 2
    assert fine_monitor.payable_amount_manual_min_interval_hours == 12.0
    assert fine_monitor.payable_amount_manual_max_sessions_per_user_per_day == 5
    assert fine_monitor.payable_amount_blocked_suspension_hours == 24.0


def test_overrides_are_parsed(tmp_path, monkeypatch):
    fine_monitor = _load(tmp_path, monkeypatch, {
        "payable_amount_enabled": True,
        "payable_amount_auto_checks_enabled": True,
        "payable_amount_gc_merchant_id": " M1 ",
        "payable_amount_gc_page_id": "P1",
        "payable_amount_gc_amount_unit": "minor",
        "payable_amount_auto_time": "12:30",
        "payable_amount_auto_cutoff_time": "13:15",
        "payable_amount_max_sessions_per_day": 10,
    })
    assert fine_monitor.payable_amount_enabled and fine_monitor.payable_amount_auto_checks_enabled
    assert fine_monitor.payable_amount_gc_merchant_id == "M1"
    assert fine_monitor.payable_amount_gc_amount_unit == "minor"
    assert fine_monitor.payable_amount_auto_time == time(12, 30)
    assert fine_monitor.payable_amount_max_sessions_per_day == 10


@pytest.mark.parametrize("overrides", [
    {"payable_amount_gc_amount_unit": "tetri"},
    {"payable_amount_gc_base_url": "https://police.ge"},
    {"payable_amount_gc_base_url": "http://mpi.gc.ge"},
    {"payable_amount_request_timeout_seconds": 61},
    {"payable_amount_request_timeout_seconds": 0},
    {"payable_amount_max_sessions_per_day": 0},
    {"payable_amount_max_consecutive_errors": 0},
])
def test_typos_stop_the_start(tmp_path, monkeypatch, overrides):
    with pytest.raises(ConfigError):
        _load(tmp_path, monkeypatch, overrides)


def test_service_built_from_settings_opens_no_session_without_merchant_values(tmp_path, monkeypatch):
    fine_monitor = _load(tmp_path, monkeypatch, {
        "payable_amount_enabled": True, "payable_amount_max_sessions_per_day": 7,
        "payable_amount_manual_min_interval_hours": 6,
    })
    db_path = tmp_path / "users.db"
    amounts = PayableAmountRepository(db_path)
    statuses = FineStatusRepository(db_path)
    subscriptions = FineSubscriptionRepository(db_path)
    try:
        service = build_payable_amount_service(
            fine_monitor, amounts, FineStatusTracker(statuses), subscriptions, trusted_user_ids=[1],
        )
        assert service.auto_enabled is False
        assert service._limits.max_sessions_per_day == 7
        assert service._limits.manual_min_interval == timedelta(hours=6)
        result = asyncio.run(service.refresh(ReceiptRef("AA111BB", "R1", 50.0), trigger="manual"))
        assert result.status == STATUS_NOT_CONFIGURED
        assert amounts.checks_for_receipt("AA111BB", "R1") == []
    finally:
        amounts.close()
        statuses.close()
        subscriptions.close()
