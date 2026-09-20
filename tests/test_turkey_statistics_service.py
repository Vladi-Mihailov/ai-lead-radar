"""
Тесты reader/turkey_bot/statistics_service.py::TurkeyStatisticsService —
ПЕРЕНЕСЕНО из reader/turkey_bot_test/statistics_service.py (см. задачу
"Перенос Unified Turkey функционала в production") — источник данных для
проверок теперь TurkeyCheckRunRepository (turkey_check_runs/
turkey_provider_results, unified-check), НЕ старый TurkeyCheckRepository
(turkey_fine_checks, только GIB) — старая версия этого теста, построенная
на 2-арг конструкторе TurkeyStatisticsService(known_users, checks),
заменена этой.
"""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.known_users_repository import (
    TurkeyBotKnownUsersRepository,
)
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.statistics_service import TurkeyStatisticsService
from reader.turkey_bot.unified.models import (
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    total_amount_for,
)
from reader.turkey_bot.unified.run_repository import (
    TurkeyCheckRunRepository,
)

_TZ = ZoneInfo("UTC")
_NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


def _service():
    known_users = TurkeyBotKnownUsersRepository(":memory:")
    runs = TurkeyCheckRunRepository(":memory:")
    subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
    return TurkeyStatisticsService(known_users, runs, subscriptions), known_users, runs, subscriptions


def _seed_user(known_users, *, telegram_user_id: int, days_ago: int, username: str | None = None) -> None:
    known_users.record_seen(
        telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, telegram_username=username,
    )
    seen_at = (_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    known_users._conn.execute(
        "UPDATE turkey_bot_known_users SET first_seen_at = ? WHERE telegram_user_id = ?",
        (seen_at, telegram_user_id),
    )
    known_users._conn.commit()


def _provider(provider: str, status: ProviderStatus, *, error_type: str | None = None) -> ProviderCheckResult:
    total = Decimal(100) if status == ProviderStatus.HAS_DEBT else Decimal(0)
    return ProviderCheckResult(
        provider=provider, status=status, debt_count=1 if status == ProviderStatus.HAS_DEBT else 0,
        principal_amount=total, penalty_amount=Decimal(0), total_amount=total,
        items=(), error_type=error_type, checked_at=_NOW,
    )


def _seed_check(
    runs, *, telegram_user_id: int, overall: str, days_ago: int, initiator: str = "manual",
) -> None:
    if overall == "has_debt":
        providers = (_provider("gib", ProviderStatus.HAS_DEBT),)
    elif overall == "error":
        providers = (_provider("gib", ProviderStatus.ERROR, error_type="transport_error"),)
    else:
        providers = (_provider("gib", ProviderStatus.NO_DEBT),)

    result = UnifiedCheckResult(
        plate="34ABC123", started_at=_NOW, finished_at=_NOW,
        overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers),
        providers=providers,
    )
    run_id = runs.save(result, telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, initiator=initiator)
    finished_at = (_NOW - timedelta(days=days_ago)).isoformat()
    runs._conn.execute("UPDATE turkey_check_runs SET finished_at = ? WHERE id = ?", (finished_at, run_id))
    runs._conn.commit()


def test_total_users_counts_everyone():
    service, known_users, _runs, _subs = _service()
    _seed_user(known_users, telegram_user_id=1, days_ago=0)
    _seed_user(known_users, telegram_user_id=2, days_ago=100)

    stats = service.get_statistics(now=_NOW, tz=_TZ)

    assert stats.total_users == 2


def test_new_users_today_7d_30d_are_rolling_inclusive_windows():
    service, known_users, _runs, _subs = _service()
    _seed_user(known_users, telegram_user_id=1, days_ago=0)   # today
    _seed_user(known_users, telegram_user_id=2, days_ago=5)   # within 7d
    _seed_user(known_users, telegram_user_id=3, days_ago=20)  # within 30d
    _seed_user(known_users, telegram_user_id=4, days_ago=60)  # outside all windows

    stats = service.get_statistics(now=_NOW, tz=_TZ)

    assert stats.new_users_today == 1
    assert stats.new_users_7d == 2
    assert stats.new_users_30d == 3
    assert stats.total_users == 4


def test_total_checks_counts_all_recorded_runs():
    service, _known_users, runs, _subs = _service()
    _seed_check(runs, telegram_user_id=1, overall="no_debt", days_ago=0)
    _seed_check(runs, telegram_user_id=1, overall="has_debt", days_ago=0)
    _seed_check(runs, telegram_user_id=1, overall="error", days_ago=0)

    stats = service.get_statistics(now=_NOW, tz=_TZ)

    assert stats.total_checks == 3


def test_checks_today_7d_30d_are_rolling_inclusive_windows():
    service, _known_users, runs, _subs = _service()
    _seed_check(runs, telegram_user_id=1, overall="no_debt", days_ago=0)
    _seed_check(runs, telegram_user_id=1, overall="no_debt", days_ago=5)
    _seed_check(runs, telegram_user_id=1, overall="no_debt", days_ago=20)
    _seed_check(runs, telegram_user_id=1, overall="no_debt", days_ago=60)

    stats = service.get_statistics(now=_NOW, tz=_TZ)

    assert stats.checks_today == 1
    assert stats.checks_7d == 2
    assert stats.checks_30d == 3
    assert stats.total_checks == 4


def test_has_debt_no_debt_and_error_breakdown():
    service, _known_users, runs, _subs = _service()
    _seed_check(runs, telegram_user_id=1, overall="has_debt", days_ago=0)
    _seed_check(runs, telegram_user_id=1, overall="has_debt", days_ago=0)
    _seed_check(runs, telegram_user_id=1, overall="no_debt", days_ago=0)
    _seed_check(runs, telegram_user_id=1, overall="error", days_ago=0)

    stats = service.get_statistics(now=_NOW, tz=_TZ)

    assert stats.checks_has_debt == 2
    assert stats.checks_no_debt == 1
    assert stats.checks_error == 1


def test_manual_vs_scheduled_checks_split_by_initiator():
    service, _known_users, runs, _subs = _service()
    _seed_check(runs, telegram_user_id=1, overall="no_debt", days_ago=0, initiator="manual")
    _seed_check(runs, telegram_user_id=1, overall="no_debt", days_ago=0, initiator="monitoring_13:00")
    _seed_check(runs, telegram_user_id=1, overall="no_debt", days_ago=0, initiator="monitoring_21:00")

    stats = service.get_statistics(now=_NOW, tz=_TZ)

    assert stats.manual_checks == 1
    assert stats.scheduled_checks == 2


def test_active_monitoring_subscriptions_counted():
    service, _known_users, _runs, subscriptions = _service()
    subscriptions.enable(telegram_user_id=1, telegram_chat_id=1, plate="34ABC123")
    subscriptions.enable(telegram_user_id=2, telegram_chat_id=2, plate="06XYZ999")
    subscriptions.disable(telegram_user_id=2, plate="06XYZ999")

    stats = service.get_statistics(now=_NOW, tz=_TZ)

    assert stats.active_monitoring_subscriptions == 1


def test_provider_error_counts_broken_down_per_provider():
    service, _known_users, runs, _subs = _service()
    result = UnifiedCheckResult(
        plate="34ABC123", started_at=_NOW, finished_at=_NOW,
        overall_status=derive_overall_status((
            _provider("gib", ProviderStatus.ERROR, error_type="transport_error"),
            _provider("avrasya", ProviderStatus.NO_DEBT),
            _provider("kgm", ProviderStatus.NO_DEBT),
        )),
        total_amount=Decimal(0),
        providers=(
            _provider("gib", ProviderStatus.ERROR, error_type="transport_error"),
            _provider("avrasya", ProviderStatus.NO_DEBT),
            _provider("kgm", ProviderStatus.NO_DEBT),
        ),
    )
    runs.save(result, telegram_user_id=1, telegram_chat_id=1, initiator="manual")

    stats = service.get_statistics(now=_NOW, tz=_TZ)

    assert stats.provider_error_counts["gib"] == 1
    assert stats.provider_error_counts["avrasya"] == 0
    assert stats.provider_error_counts["kgm"] == 0


def test_list_known_users_delegates_to_repository_ordering():
    service, known_users, _runs, _subs = _service()
    _seed_user(known_users, telegram_user_id=2, days_ago=1, username="bob")
    _seed_user(known_users, telegram_user_id=1, days_ago=5, username="alice")

    users = service.list_known_users()

    assert users == [(1, "alice"), (2, "bob")]
