"""Тесты TurkeyCheckRunRepository.get_latest_provider_results_for_plate (см.
задачу "Turkey debt list: последний успешный результат отдельно по каждому
провайдеру и госномеру") — (последняя попытка, последний успешный
результат) провайдера по НОМЕРУ: все владельцы, все initiator'ы, порядок —
finished_at run'а."""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.unified.models import (
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    total_amount_for,
)
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository

_NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)


def _result(provider: str, status: ProviderStatus, amount: Decimal = Decimal(0)) -> ProviderCheckResult:
    has_debt = status == ProviderStatus.HAS_DEBT
    return ProviderCheckResult(
        provider=provider, status=status, debt_count=1 if has_debt else 0,
        principal_amount=amount if has_debt else Decimal(0), penalty_amount=Decimal(0),
        total_amount=amount if has_debt else Decimal(0), items=(),
        error_type="unexpected" if status == ProviderStatus.ERROR else None, checked_at=_NOW,
    )


def _save(
    runs, *, plate: str, providers: tuple[ProviderCheckResult, ...], days_ago: int,
    telegram_user_id: int = 1, initiator: str = "manual",
) -> int:
    result = UnifiedCheckResult(
        plate=plate, started_at=_NOW, finished_at=_NOW,
        overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers),
        providers=providers,
    )
    run_id = runs.save(result, telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, initiator=initiator)
    runs._conn.execute(
        "UPDATE turkey_check_runs SET finished_at = ? WHERE id = ?",
        ((_NOW - timedelta(days=days_ago)).isoformat(), run_id),
    )
    runs._conn.commit()
    return run_id


def test_no_runs_means_no_attempt_and_no_success():
    runs = TurkeyCheckRunRepository(":memory:")

    assert runs.get_latest_provider_results_for_plate("34ABC123", "kgm") == (None, None)


def test_latest_attempt_failed_returns_earlier_success_separately():
    runs = TurkeyCheckRunRepository(":memory:")
    _save(runs, plate="34ABC123", providers=(_result("kgm", ProviderStatus.HAS_DEBT, Decimal(3788)),), days_ago=6)
    _save(runs, plate="34ABC123", providers=(_result("kgm", ProviderStatus.ERROR),), days_ago=0)

    last_attempt, last_success = runs.get_latest_provider_results_for_plate("34ABC123", "kgm")

    assert last_attempt.result.status == ProviderStatus.ERROR
    assert last_attempt.finished_at == _NOW
    assert last_success.result.status == ProviderStatus.HAS_DEBT
    assert last_success.result.total_amount == Decimal(3788)
    assert last_success.finished_at == _NOW - timedelta(days=6)


def test_no_debt_success_counts_as_latest_success():
    runs = TurkeyCheckRunRepository(":memory:")
    _save(runs, plate="34ABC123", providers=(_result("gib", ProviderStatus.HAS_DEBT, Decimal(500)),), days_ago=2)
    _save(runs, plate="34ABC123", providers=(_result("gib", ProviderStatus.NO_DEBT),), days_ago=0)

    last_attempt, last_success = runs.get_latest_provider_results_for_plate("34ABC123", "gib")

    assert last_attempt == last_success
    assert last_success.result.status == ProviderStatus.NO_DEBT


def test_spans_every_owner_and_initiator_of_the_plate():
    runs = TurkeyCheckRunRepository(":memory:")
    _save(runs, plate="34ABC123", providers=(_result("kgm", ProviderStatus.HAS_DEBT, Decimal(100)),), days_ago=3,
          telegram_user_id=1, initiator="manual")
    _save(runs, plate="34ABC123", providers=(_result("kgm", ProviderStatus.HAS_DEBT, Decimal(200)),), days_ago=2,
          telegram_user_id=2, initiator="manager_refresh")
    _save(runs, plate="34ABC123", providers=(_result("kgm", ProviderStatus.NO_DEBT),), days_ago=1,
          telegram_user_id=3, initiator="monitoring_21:00_retry")

    _attempt, last_success = runs.get_latest_provider_results_for_plate("34ABC123", "kgm")

    assert last_success.result.status == ProviderStatus.NO_DEBT
    assert last_success.finished_at == _NOW - timedelta(days=1)


def test_ordered_by_run_finished_at_not_insertion_order():
    runs = TurkeyCheckRunRepository(":memory:")
    _save(runs, plate="34ABC123", providers=(_result("gib", ProviderStatus.HAS_DEBT, Decimal(900)),), days_ago=0)
    _save(runs, plate="34ABC123", providers=(_result("gib", ProviderStatus.HAS_DEBT, Decimal(100)),), days_ago=5)

    last_attempt, last_success = runs.get_latest_provider_results_for_plate("34ABC123", "gib")

    assert last_attempt.result.total_amount == Decimal(900)
    assert last_success.result.total_amount == Decimal(900)


def test_other_plates_and_other_providers_are_ignored():
    runs = TurkeyCheckRunRepository(":memory:")
    _save(runs, plate="34ABC123", providers=(_result("gib", ProviderStatus.HAS_DEBT, Decimal(500)),), days_ago=1)
    _save(runs, plate="06XYZ789", providers=(_result("kgm", ProviderStatus.HAS_DEBT, Decimal(700)),), days_ago=0)

    assert runs.get_latest_provider_results_for_plate("34ABC123", "kgm") == (None, None)
    assert runs.get_latest_provider_results_for_plate("06XYZ789", "gib") == (None, None)


def test_provider_that_never_succeeded_has_attempt_but_no_success():
    runs = TurkeyCheckRunRepository(":memory:")
    _save(runs, plate="34ABC123", providers=(_result("kgm", ProviderStatus.ERROR),), days_ago=0)

    last_attempt, last_success = runs.get_latest_provider_results_for_plate("34ABC123", "kgm")

    assert last_attempt.result.status == ProviderStatus.ERROR
    assert last_success is None
