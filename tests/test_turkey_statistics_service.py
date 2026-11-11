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

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.known_users_repository import (
    TurkeyBotKnownUsersRepository,
)
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.statistics_service import (
    TurkeyProviderFreshness,
    TurkeyStatisticsService,
)
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
    initiator: str = "manual",
) -> None:
    result = UnifiedCheckResult(
        plate=plate, started_at=_NOW, finished_at=_NOW,
        overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers),
        providers=providers,
    )
    run_id = runs.save(result, telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, initiator=initiator)
    finished_at = (_NOW - timedelta(days=days_ago)).isoformat()
    runs._conn.execute("UPDATE turkey_check_runs SET finished_at = ? WHERE id = ?", (finished_at, run_id))
    runs._conn.commit()


def test_debt_rows_include_latest_success_with_debt():
    """KGM NO_DEBT в том же run — полная проверка: без него KGM был бы
    NO_DATA ("нет достоверных данных") и строка — is_partial (см.
    TurkeyProviderFreshness)."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    providers = (
        _debt_provider(ProviderStatus.HAS_DEBT, Decimal(100)),
        _debt_provider(ProviderStatus.NO_DEBT, Decimal(0), provider="kgm"),
    )
    _save_debt_run(runs, telegram_user_id=1, plate="34ABC123", providers=providers, days_ago=0)

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
    """См. задачу п.5: PARTIAL-run (хотя бы один ERROR, но не все) — сумма
    успешно ответившего GİB включается, строка помечена is_partial. После
    задачи "Turkey debt list: последний успешный результат отдельно по
    каждому провайдеру и госномеру" пометка — потому что по KGM нет
    достоверных данных (он ни разу не отвечал); ошибка Avrasya на сумму
    не влияет вовсе (Avrasya в сумму не входит)."""
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
    """Раньше (задача п.8) каждый владелец одного номера получал СВОЙ
    total_amount из своих проверок. См. задачу "Turkey debt list:
    последний успешный результат отдельно по каждому провайдеру и
    госномеру" — сумма теперь считается по НОМЕРУ (самый свежий успешный
    результат провайдера любого владельца), поэтому у обоих владельцев
    одинаковая, свежая сумма. Строки по-прежнему отдельные на каждого
    владельца (car_id/owner_telegram_user_id) — строки не склеиваются."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car_a = garage.add_car(telegram_user_id=111, car_number="34ABC123")
    car_b = garage.add_car(telegram_user_id=222, car_number="34ABC123")
    _save_debt_run(runs, telegram_user_id=111, plate="34ABC123", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(500)),), days_ago=1)
    _save_debt_run(runs, telegram_user_id=222, plate="34ABC123", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(900)),), days_ago=0)

    rows = service.get_debt_rows([car_a, car_b])

    by_owner = {row.owner_telegram_user_id: row.total_amount for row in rows}
    assert by_owner == {111: Decimal(900), 222: Decimal(900)}
    assert {row.car_id for row in rows} == {car_a.id, car_b.id}


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


# ---- get_debt_car_groups() (см. задачу "Turkey manager debt statistics aggregation") ----


def test_debt_car_groups_one_plate_one_owner_matches_debt_rows():
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    providers = (  # полная проверка — см. test_debt_rows_include_latest_success_with_debt
        _debt_provider(ProviderStatus.HAS_DEBT, Decimal(500)),
        _debt_provider(ProviderStatus.NO_DEBT, Decimal(0), provider="kgm"),
    )
    _save_debt_run(runs, telegram_user_id=1, plate="34ABC123", providers=providers, days_ago=0)

    groups = service.get_debt_car_groups([car])

    assert len(groups) == 1
    assert groups[0].car_number == "34ABC123"
    assert groups[0].owner_telegram_user_ids == (1,)
    assert groups[0].total_amount == Decimal(500)
    assert groups[0].is_partial is False


def test_debt_car_groups_one_plate_multiple_owners_collapse_to_one_row():
    """См. задачу: H401HH95 у 3 owners -> ОДНА группа, не три, с суммой
    физической машины РОВНО один раз (не 3x32664.50)."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car_a = garage.add_car(telegram_user_id=111, car_number="H401HH95")
    car_b = garage.add_car(telegram_user_id=222, car_number="H401HH95")
    car_c = garage.add_car(telegram_user_id=333, car_number="H401HH95")
    for owner in (111, 222, 333):
        _save_debt_run(
            runs, telegram_user_id=owner, plate="H401HH95",
            providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal("32664.50")),), days_ago=0,
        )

    groups = service.get_debt_car_groups([car_a, car_b, car_c])

    assert len(groups) == 1
    assert groups[0].car_number == "H401HH95"
    assert set(groups[0].owner_telegram_user_ids) == {111, 222, 333}
    assert groups[0].total_amount == Decimal("32664.50")  # НЕ 97993.50 (3x)


def test_debt_car_groups_uses_freshest_owner_result_not_sum():
    """Если один из owners сделал свою собственную более свежую ручную
    проверку с ДРУГОЙ суммой — группа берёт САМЫЙ СВЕЖИЙ результат, не
    складывает старую и новую сумму."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car_a = garage.add_car(telegram_user_id=111, car_number="X773XP163")
    car_b = garage.add_car(telegram_user_id=222, car_number="X773XP163")
    _save_debt_run(runs, telegram_user_id=111, plate="X773XP163", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(108)),), days_ago=1)
    _save_debt_run(runs, telegram_user_id=222, plate="X773XP163", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(108)),), days_ago=1)
    # owner 222 позже сделал свою собственную более свежую ручную проверку —
    # его own latest reliable теперь 50, а не 108; owner 111's latest reliable
    # остаётся 108 (не перепроверял). Группа должна взять свежий (50), не
    # 108+50=158.
    _save_debt_run(runs, telegram_user_id=222, plate="X773XP163", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(50)),), days_ago=0)

    groups = service.get_debt_car_groups([car_a, car_b])

    assert len(groups) == 1
    assert groups[0].total_amount == Decimal(50)  # свежий результат owner 222, не 108+50


def test_debt_car_groups_two_different_plates_sum_normally():
    service, _known, runs, _subs, garage = _service_with_garage()
    car_a = garage.add_car(telegram_user_id=1, car_number="AAA111")
    car_b = garage.add_car(telegram_user_id=2, car_number="BBB222")
    _save_debt_run(runs, telegram_user_id=1, plate="AAA111", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(100)),), days_ago=0)
    _save_debt_run(runs, telegram_user_id=2, plate="BBB222", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(500)),), days_ago=0)

    groups = service.get_debt_car_groups([car_a, car_b])

    assert len(groups) == 2
    total = sum((g.total_amount for g in groups), Decimal(0))
    assert total == Decimal(600)


def test_debt_car_groups_sorted_by_amount_desc():
    service, _known, runs, _subs, garage = _service_with_garage()
    car_a = garage.add_car(telegram_user_id=1, car_number="AAA111")
    car_b = garage.add_car(telegram_user_id=2, car_number="BBB222")
    _save_debt_run(runs, telegram_user_id=1, plate="AAA111", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(100)),), days_ago=0)
    _save_debt_run(runs, telegram_user_id=2, plate="BBB222", providers=(_debt_provider(ProviderStatus.HAS_DEBT, Decimal(500)),), days_ago=0)

    groups = service.get_debt_car_groups([car_a, car_b])

    assert [g.car_number for g in groups] == ["BBB222", "AAA111"]


def test_debt_rows_needs_no_check_service_or_network_dependency():
    """get_debt_rows() читает ТОЛЬКО уже сохранённые repositories — сама
    сигнатура (cars, без check_service/provider) физически не может
    выполнить provider/network request (см. задачу "КРИТИЧЕСКИ ВАЖНО:
    НИКАКИХ LIVE CHECK")."""
    import inspect

    signature = inspect.signature(TurkeyStatisticsService.get_debt_rows)
    assert list(signature.parameters) == ["self", "cars"]


# ---- последний успешный результат отдельно по провайдеру и номеру (см.
# задачу "Turkey debt list: последний успешный результат отдельно по
# каждому провайдеру и госномеру" — production-находка 11.11: PARTIAL-run с
# ошибкой KGM считался "достоверным", и его неполный total обнулял/занижал
# известный долг KGM в списке менеджера) ----


def _error_provider(provider: str, error_type: str) -> ProviderCheckResult:
    return ProviderCheckResult(
        provider=provider, status=ProviderStatus.ERROR, debt_count=0,
        principal_amount=None, penalty_amount=None, total_amount=Decimal(0),
        items=(), error_type=error_type, checked_at=_NOW,
    )


def _ok(provider: str, amount: Decimal = Decimal(0)) -> ProviderCheckResult:
    """Успешный ответ провайдера: amount > 0 — HAS_DEBT, иначе NO_DEBT."""
    status = ProviderStatus.HAS_DEBT if amount > 0 else ProviderStatus.NO_DEBT
    return _debt_provider(status, amount, provider=provider)


def _full_check(*, gib=Decimal(0), avrasya=Decimal(0), kgm=Decimal(0)) -> tuple[ProviderCheckResult, ...]:
    return (_ok("gib", gib), _ok("avrasya", avrasya), _ok("kgm", kgm))


def _states(row) -> dict:
    return {state.provider: state for state in row.provider_states}


@pytest.mark.parametrize(
    "error_type", ["unexpected", "transport_error", "captcha_unavailable", "captcha_rejected", "internal_error"],
)
def test_debt_rows_kgm_error_keeps_last_known_kgm_debt(error_type):
    """Сценарий производства (долг только KGM): полная проверка нашла KGM
    3788, следующая — PARTIAL с ошибкой KGM (total_amount=0). Машина
    остаётся в списке с ПОСЛЕДНЕЙ ИЗВЕСТНОЙ суммой KGM (STALE, с датами
    успешной проверки и неответа), а не исчезает."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    _save_debt_run(runs, telegram_user_id=1, plate="34ABC123", providers=_full_check(kgm=Decimal(3788)), days_ago=6)
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123",
        providers=(_ok("gib"), _ok("avrasya"), _error_provider("kgm", error_type)), days_ago=0,
    )

    rows = service.get_debt_rows([car])

    assert len(rows) == 1
    assert rows[0].total_amount == Decimal(3788)
    assert rows[0].is_partial is True
    kgm = _states(rows[0])["kgm"]
    assert kgm.freshness == TurkeyProviderFreshness.STALE
    assert kgm.amount == Decimal(3788)
    assert kgm.last_success_at == _NOW - timedelta(days=6)
    assert kgm.last_attempt_at == _NOW
    assert kgm.last_attempt_error_type == error_type
    assert _states(rows[0])["gib"].freshness == TurkeyProviderFreshness.CURRENT
    assert rows[0].confirmed_amount == Decimal(0)
    assert rows[0].last_known_amount == Decimal(3788)


def test_debt_rows_new_gib_debt_plus_last_known_kgm_when_kgm_errors():
    """GİB ответил новой суммой, KGM упал: сумма = НОВЫЙ GİB + последний
    известный KGM (а не только GİB, как давал total PARTIAL-run'а);
    Avrasya не прибавляется ни в одном из run'ов."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123",
        providers=_full_check(gib=Decimal(13336), avrasya=Decimal(2450), kgm=Decimal("20192.50")), days_ago=7,
    )
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123",
        providers=(_ok("gib", Decimal(14000)), _ok("avrasya", Decimal(2450)), _error_provider("kgm", "unexpected")),
        days_ago=0,
    )

    rows = service.get_debt_rows([car])

    assert rows[0].total_amount == Decimal("34192.50")
    assert rows[0].confirmed_amount == Decimal(14000)
    assert rows[0].last_known_amount == Decimal("20192.50")
    assert rows[0].checked_at == _NOW  # самый свежий из использованных успешных результатов
    assert _states(rows[0])["gib"].freshness == TurkeyProviderFreshness.CURRENT
    assert _states(rows[0])["kgm"].freshness == TurkeyProviderFreshness.STALE


def test_debt_rows_gib_error_keeps_last_known_gib_debt_while_kgm_current():
    """Зеркальный случай: упал GİB, KGM ответил — GİB остаётся с последней
    известной суммой, KGM обновляет свою."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    _save_debt_run(runs, telegram_user_id=1, plate="34ABC123", providers=_full_check(gib=Decimal(500)), days_ago=3)
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123",
        providers=(_error_provider("gib", "transport_error"), _ok("avrasya"), _ok("kgm", Decimal(900))), days_ago=0,
    )

    rows = service.get_debt_rows([car])

    assert rows[0].total_amount == Decimal(1400)
    assert _states(rows[0])["gib"].freshness == TurkeyProviderFreshness.STALE
    assert _states(rows[0])["gib"].amount == Decimal(500)
    assert _states(rows[0])["kgm"].freshness == TurkeyProviderFreshness.CURRENT
    assert rows[0].confirmed_amount == Decimal(900)
    assert rows[0].last_known_amount == Decimal(500)


@pytest.mark.parametrize("avrasya_answered", [True, False])
def test_debt_rows_both_providers_error_keep_both_last_known_debts(avrasya_answered):
    """Ошибки ОБОИХ GİB и KGM (PARTIAL при ответившей Avrasya или ERROR,
    если упали все три) — обе прежние суммы остаются последними
    известными, ничего не подтверждено свежей проверкой."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123", providers=_full_check(gib=Decimal(500), kgm=Decimal(700)), days_ago=2,
    )
    avrasya = _ok("avrasya") if avrasya_answered else _error_provider("avrasya", "rate_limited")
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123",
        providers=(_error_provider("gib", "unexpected"), avrasya, _error_provider("kgm", "transport_error")),
        days_ago=0,
    )

    rows = service.get_debt_rows([car])

    assert rows[0].total_amount == Decimal(1200)
    assert rows[0].confirmed_amount == Decimal(0)
    assert rows[0].last_known_amount == Decimal(1200)
    assert {s.freshness for s in rows[0].provider_states} == {TurkeyProviderFreshness.STALE}


def test_debt_rows_confirmed_repayment_removes_car():
    """Подтверждённое погашение: полная успешная проверка без долга по GİB
    и KGM — машина уходит из списка и не считается "без данных"."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123", providers=_full_check(gib=Decimal(500), kgm=Decimal(700)), days_ago=2,
    )
    _save_debt_run(runs, telegram_user_id=1, plate="34ABC123", providers=_full_check(), days_ago=0)

    assert service.get_debt_rows([car]) == []
    assert service.count_plates_without_reliable_data([car]) == 0


def test_debt_rows_kgm_no_debt_success_replaces_old_kgm_debt():
    """Успешная проверка KGM без долга заменяет старый долг KGM; долг GİB
    (подтверждённый той же проверкой) остаётся."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123", providers=_full_check(gib=Decimal(500), kgm=Decimal(700)), days_ago=2,
    )
    _save_debt_run(runs, telegram_user_id=1, plate="34ABC123", providers=_full_check(gib=Decimal(500)), days_ago=0)

    rows = service.get_debt_rows([car])

    assert rows[0].total_amount == Decimal(500)
    assert rows[0].is_partial is False
    assert _states(rows[0])["kgm"].amount == Decimal(0)
    assert _states(rows[0])["kgm"].freshness == TurkeyProviderFreshness.CURRENT


def test_debt_rows_kgm_repaid_while_gib_errors_keeps_only_last_known_gib():
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123", providers=_full_check(gib=Decimal(500), kgm=Decimal(700)), days_ago=2,
    )
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123",
        providers=(_error_provider("gib", "transport_error"), _ok("avrasya"), _ok("kgm")), days_ago=0,
    )

    rows = service.get_debt_rows([car])

    assert rows[0].total_amount == Decimal(500)
    assert rows[0].last_known_amount == Decimal(500)
    assert rows[0].confirmed_amount == Decimal(0)


def test_debt_rows_later_no_debt_by_other_owner_overrides_older_debt():
    """Номер у двух владельцев: более поздняя успешная проверка без долга
    (владелец A) перекрывает более старый долг из проверки владельца B —
    долг — свойство номера, а не владельца. Не попадает ни одна строка."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car_a = garage.add_car(telegram_user_id=111, car_number="34ABC123")
    car_b = garage.add_car(telegram_user_id=222, car_number="34ABC123")
    _save_debt_run(runs, telegram_user_id=222, plate="34ABC123", providers=_full_check(gib=Decimal(900)), days_ago=3)
    _save_debt_run(runs, telegram_user_id=111, plate="34ABC123", providers=_full_check(), days_ago=0)

    assert service.get_debt_rows([car_a, car_b]) == []
    assert service.get_debt_car_groups([car_a, car_b]) == []


def test_debt_seen_by_one_owner_lists_every_owner_of_the_plate_once():
    """Долг найден проверкой владельца A — в списке менеджера номер
    показывается со ВСЕМИ его владельцами, сумма — один раз."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car_a = garage.add_car(telegram_user_id=111, car_number="34ABC123")
    car_b = garage.add_car(telegram_user_id=222, car_number="34ABC123")
    _save_debt_run(runs, telegram_user_id=111, plate="34ABC123", providers=_full_check(gib=Decimal(700)), days_ago=0)

    rows = service.get_debt_rows([car_a, car_b])
    groups = service.get_debt_car_groups([car_a, car_b])

    assert {row.owner_telegram_user_id: row.total_amount for row in rows} == {111: Decimal(700), 222: Decimal(700)}
    assert len(groups) == 1
    assert set(groups[0].owner_telegram_user_ids) == {111, 222}
    assert groups[0].total_amount == Decimal(700)


def test_debt_rows_monitoring_retry_subset_run_does_not_zero_gib_debt():
    """Monitoring retry проверяет ТОЛЬКО упавших на капче провайдеров: run
    из одного KGM NO_DEBT имеет total_amount=0, но долг GİB из первого
    прохода никуда не девается."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123",
        providers=(_ok("gib", Decimal(500)), _ok("avrasya"), _error_provider("kgm", "captcha_unavailable")),
        days_ago=1, initiator="monitoring_21:00",
    )
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123", providers=(_ok("kgm"),), days_ago=0,
        initiator="monitoring_21:00_retry",
    )

    rows = service.get_debt_rows([car])

    assert rows[0].total_amount == Decimal(500)
    assert rows[0].is_partial is False
    assert _states(rows[0])["gib"].freshness == TurkeyProviderFreshness.CURRENT
    assert _states(rows[0])["kgm"].freshness == TurkeyProviderFreshness.CURRENT


def test_debt_rows_kgm_never_succeeded_with_gib_debt_is_listed_with_kgm_no_data():
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123",
        providers=(_ok("gib", Decimal(18000)), _ok("avrasya"), _error_provider("kgm", "transport_error")), days_ago=0,
    )

    rows = service.get_debt_rows([car])

    assert rows[0].total_amount == Decimal(18000)
    assert rows[0].is_partial is True
    kgm = _states(rows[0])["kgm"]
    assert kgm.freshness == TurkeyProviderFreshness.NO_DATA
    assert kgm.last_success_at is None
    assert kgm.last_attempt_at == _NOW
    assert rows[0].confirmed_amount == Decimal(18000)
    assert rows[0].last_known_amount == Decimal(0)


def test_plates_without_reliable_data_counted_once_and_not_listed():
    """GİB без долга + KGM ни разу не ответил, и номер, который не
    проверялся вовсе, — не в списке задолженности, но посчитаны как "нет
    достоверных данных" (по одному разу на номер, даже при двух
    владельцах). Полностью проверенные номера (с долгом и без) — нет."""
    service, _known, runs, _subs, garage = _service_with_garage()
    unknown_a = garage.add_car(telegram_user_id=1, car_number="06AAA001")
    unknown_b = garage.add_car(telegram_user_id=2, car_number="06AAA001")
    never_checked = garage.add_car(telegram_user_id=3, car_number="06BBB002")
    clean = garage.add_car(telegram_user_id=4, car_number="06CCC003")
    with_debt = garage.add_car(telegram_user_id=5, car_number="06DDD004")
    _save_debt_run(
        runs, telegram_user_id=1, plate="06AAA001",
        providers=(_ok("gib"), _ok("avrasya"), _error_provider("kgm", "unexpected")), days_ago=0,
    )
    _save_debt_run(runs, telegram_user_id=4, plate="06CCC003", providers=_full_check(), days_ago=0)
    _save_debt_run(runs, telegram_user_id=5, plate="06DDD004", providers=_full_check(gib=Decimal(100)), days_ago=0)
    cars = [unknown_a, unknown_b, never_checked, clean, with_debt]

    assert [row.car_number for row in service.get_debt_rows(cars)] == ["06DDD004"]
    assert service.count_plates_without_reliable_data(cars) == 2


def test_all_error_history_counts_as_no_reliable_data():
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123",
        providers=(_error_provider("gib", "transport_error"), _error_provider("kgm", "unexpected")), days_ago=0,
    )

    assert service.get_debt_rows([car]) == []
    assert service.count_plates_without_reliable_data([car]) == 1


def test_avrasya_debt_never_added_even_when_kgm_errors():
    """Avrasya не прибавляется и не подменяет упавший KGM: сумма —
    последний известный KGM (615, он уже включает Avrasya), не 945; номер
    с Avrasya-долгом, но без единого успешного KGM, в список не попадает
    (и считается "без данных")."""
    service, _known, runs, _subs, garage = _service_with_garage()
    car = garage.add_car(telegram_user_id=1, car_number="34ABC123")
    no_kgm_car = garage.add_car(telegram_user_id=2, car_number="35XYZ789")
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123", providers=_full_check(avrasya=Decimal(330), kgm=Decimal(615)),
        days_ago=2,
    )
    _save_debt_run(
        runs, telegram_user_id=1, plate="34ABC123",
        providers=(_ok("gib"), _ok("avrasya", Decimal(330)), _error_provider("kgm", "unexpected")), days_ago=0,
    )
    _save_debt_run(
        runs, telegram_user_id=2, plate="35XYZ789",
        providers=(_ok("gib"), _ok("avrasya", Decimal(330)), _error_provider("kgm", "unexpected")), days_ago=0,
    )

    rows = service.get_debt_rows([car, no_kgm_car])

    assert [(row.car_number, row.total_amount) for row in rows] == [("34ABC123", Decimal(615))]
    assert service.count_plates_without_reliable_data([car, no_kgm_car]) == 1


def test_debt_rows_sorted_by_total_including_last_known_amounts():
    service, _known, runs, _subs, garage = _service_with_garage()
    current = garage.add_car(telegram_user_id=1, car_number="AAA111")
    stale = garage.add_car(telegram_user_id=2, car_number="BBB222")
    _save_debt_run(runs, telegram_user_id=1, plate="AAA111", providers=_full_check(gib=Decimal(1000)), days_ago=0)
    _save_debt_run(runs, telegram_user_id=2, plate="BBB222", providers=_full_check(kgm=Decimal(5000)), days_ago=5)
    _save_debt_run(
        runs, telegram_user_id=2, plate="BBB222",
        providers=(_ok("gib"), _ok("avrasya"), _error_provider("kgm", "unexpected")), days_ago=0,
    )

    rows = service.get_debt_rows([current, stale])

    assert [row.car_number for row in rows] == ["BBB222", "AAA111"]
