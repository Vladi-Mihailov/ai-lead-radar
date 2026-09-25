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
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository

_TZ = ZoneInfo("UTC")
_NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


def _service():
    known_users = TurkeyBotKnownUsersRepository(":memory:")
    runs = TurkeyCheckRunRepository(":memory:")
    subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
    return TurkeyStatisticsService(known_users, runs, subscriptions), known_users, runs, subscriptions


def _service_with_garage():
    """Как _service(), плюс TurkeyUserCarsRepository — ТОЛЬКО для
    get_debt_rows() тестов ниже (см. задачу "доработать 📊 Статистика
    Turkey bot") — get_debt_rows() сознательно НЕ тянет
    TurkeyUserCarsRepository как constructor-зависимость самого
    TurkeyStatisticsService (не менять существующую сигнатуру __init__,
    у которой уже много вызывающих мест, см. reader/turkey_bot/
    statistics_service.py::get_debt_rows докстрок) — garage передаётся
    вызывающим кодом (reader/turkey_bot/conversation.py::handle_statistics)
    отдельно на каждый вызов."""
    known_users = TurkeyBotKnownUsersRepository(":memory:")
    runs = TurkeyCheckRunRepository(":memory:")
    subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
    garage = TurkeyUserCarsRepository(":memory:")
    return TurkeyStatisticsService(known_users, runs, subscriptions), known_users, runs, subscriptions, garage


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


# ---- get_debt_rows() (см. задачу "доработать 📊 Статистика Turkey bot") ----


def _debt_provider(status: ProviderStatus, amount: Decimal, *, provider: str = "gib") -> ProviderCheckResult:
    return ProviderCheckResult(
        provider=provider, status=status, debt_count=1 if status == ProviderStatus.HAS_DEBT else 0,
        principal_amount=amount if status == ProviderStatus.HAS_DEBT else Decimal(0),
        penalty_amount=Decimal(0), total_amount=amount if status == ProviderStatus.HAS_DEBT else Decimal(0),
        items=(), error_type="transport_error" if status == ProviderStatus.ERROR else None, checked_at=_NOW,
    )


def _save_debt_run(
    runs, *, telegram_user_id: int, plate: str, providers: tuple[ProviderCheckResult, ...], days_ago: int,
) -> None:
    result = UnifiedCheckResult(
        plate=plate, started_at=_NOW, finished_at=_NOW,
        overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers),
        providers=providers,
    )
    run_id = runs.save(result, telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, initiator="manual")
    finished_at = (_NOW - timedelta(days=days_ago)).isoformat()
    runs._conn.execute("UPDATE turkey_check_runs SET finished_at = ? WHERE id = ?", (finished_at, run_id))
    runs._conn.commit()


def test_debt_rows_include_latest_success_with_debt():
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    _save_debt_run(runs, telegram_user_id=1, plate="34ABC123", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(100)),), days_ago=0)

    rows = service.get_debt_rows([car])

    assert len(rows) == 1
    assert rows[0].car_number == "34ABC123"
    assert rows[0].owner_telegram_user_id == 1
    assert rows[0].total_amount == Decimal(100)
    assert rows[0].is_partial is False


def test_debt_rows_exclude_latest_success_with_zero_debt():
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    _save_debt_run(runs, telegram_user_id=1, plate="34ABC123", providers=(_debt_provider(ProviderStatus.NO_DEBT, Decimal(0)),), days_ago=0)

    rows = service.get_debt_rows([car])

    assert rows == []


def test_debt_rows_no_reliable_check_ever_is_excluded():
    service, _known, _runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    # Ни одной проверки вообще не сохранено для этой машины.

    rows = service.get_debt_rows([car])

    assert rows == []


def test_debt_rows_use_previous_success_when_latest_is_error():
    """См. задачу п.4: "ERROR не должен стирать последнее известное
    состояние" — 24.09 SUCCESS/2740, 25.09 ERROR -> статистика всё ещё
    показывает 2740 с датой/давностью именно 24.09, а не ERROR."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="M295YB196")
    _save_debt_run(runs, telegram_user_id=1, plate="M295YB196", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(2740)),), days_ago=1)
    _save_debt_run(runs, telegram_user_id=1, plate="M295YB196", providers=(_debt_provider(ProviderStatus.ERROR, Decimal(0)),), days_ago=0)

    rows = service.get_debt_rows([car])

    assert len(rows) == 1
    assert rows[0].total_amount == Decimal(2740)
    assert rows[0].checked_at == _NOW - timedelta(days=1)


def test_debt_rows_all_error_history_is_excluded():
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    _save_debt_run(runs, telegram_user_id=1, plate="34ABC123", providers=(_debt_provider(ProviderStatus.ERROR, Decimal(0)),), days_ago=0)

    rows = service.get_debt_rows([car])

    assert rows == []


def test_debt_rows_partial_included_and_marked():
    """См. задачу п.5: существующая семантика PARTIAL (см.
    derive_overall_status: хотя бы один ERROR, но не все) — сумма из
    успешно проверенных provider'ов включается, помеченная is_partial."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    providers = (
        _debt_provider(ProviderStatus.HAS_DEBT, Decimal(1250), provider="gib"),
        _debt_provider(ProviderStatus.ERROR, Decimal(0), provider="avrasya"),
    )
    _save_debt_run(runs, telegram_user_id=1, plate="34ABC123", providers=providers, days_ago=2)

    rows = service.get_debt_rows([car])

    assert len(rows) == 1
    assert rows[0].is_partial is True
    assert rows[0].total_amount == Decimal(1250)


def test_debt_rows_reuse_stored_total_amount_avrasya_not_double_counted():
    """См. задачу п.6: "не суммируй GİB + Avrasya + KGM самостоятельно" —
    сохранённый total_amount_for() уже исключает Avrasya (её долг учтён
    внутри KGM) — 80 + 2580 + 2660 должно остаться 2740, а не 5320."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="M295YB196")
    providers = (
        ProviderCheckResult(provider="gib", status=ProviderStatus.HAS_DEBT, debt_count=1,
                             principal_amount=Decimal(80), penalty_amount=Decimal(0), total_amount=Decimal(80),
                             items=(), error_type=None, checked_at=_NOW),
        ProviderCheckResult(provider="avrasya", status=ProviderStatus.HAS_DEBT, debt_count=1,
                             principal_amount=Decimal(2580), penalty_amount=Decimal(0), total_amount=Decimal(2580),
                             items=(), error_type=None, checked_at=_NOW),
        ProviderCheckResult(provider="kgm", status=ProviderStatus.HAS_DEBT, debt_count=1,
                             principal_amount=Decimal(2660), penalty_amount=Decimal(0), total_amount=Decimal(2660),
                             items=(), error_type=None, checked_at=_NOW),
    )
    _save_debt_run(runs, telegram_user_id=1, plate="M295YB196", providers=providers, days_ago=0)

    rows = service.get_debt_rows([car])

    assert len(rows) == 1
    assert rows[0].total_amount == Decimal(2740)


def test_debt_rows_duplicate_plate_different_owners_attributed_correctly():
    """См. задачу п.8: "не склеивай разных владельцев только по номеру" —
    один и тот же plate у двух owner'ов, каждый получает СВОЙ total_amount."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car_a = garage.add_car(telegram_user_id=111, car_number="34ABC123")
    car_b = garage.add_car(telegram_user_id=222, car_number="34ABC123")
    _save_debt_run(runs, telegram_user_id=111, plate="34ABC123", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(500)),), days_ago=0)
    _save_debt_run(runs, telegram_user_id=222, plate="34ABC123", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(900)),), days_ago=0)

    rows = service.get_debt_rows([car_a, car_b])

    by_owner = {row.owner_telegram_user_id: row.total_amount for row in rows}
    assert by_owner == {111: Decimal(500), 222: Decimal(900)}


def test_debt_rows_sorted_by_amount_desc_then_recency():
    service, _known, runs, _subs, garage = _service_with_garage()
    car_a = garage.add_car(telegram_user_id=1, car_number="AAA111")
    car_b = garage.add_car(telegram_user_id=2, car_number="BBB222")
    car_c = garage.add_car(telegram_user_id=3, car_number="CCC333")
    _save_debt_run(runs, telegram_user_id=1, plate="AAA111", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(100)),), days_ago=5)
    _save_debt_run(runs, telegram_user_id=2, plate="BBB222", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(500)),), days_ago=1)
    _save_debt_run(runs, telegram_user_id=3, plate="CCC333", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(100)),), days_ago=1)

    rows = service.get_debt_rows([car_a, car_b, car_c])

    assert [r.car_number for r in rows] == ["BBB222", "CCC333", "AAA111"]


def test_debt_rows_needs_no_check_service_or_network_dependency():
    """get_debt_rows() читает ТОЛЬКО уже сохранённые repositories — сама
    сигнатура (cars, без check_service/provider) физически не может
    выполнить provider/network request (см. задачу "КРИТИЧЕСКИ ВАЖНО:
    НИКАКИХ LIVE CHECK")."""
    import inspect

    signature = inspect.signature(TurkeyStatisticsService.get_debt_rows)
    assert list(signature.parameters) == ["self", "cars"]
