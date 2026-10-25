"""Tests for CollectorIngestService (reader/public_bot/
collector_ingest_service.py) — the pure persistence/completion logic behind
the Georgia remote collector's internal API, see "Georgia remote collector
architecture (REDNET)". HTTP/HMAC transport is tested separately in
tests/test_georgia_collector_api.py; this file exercises the canonical
persistence path directly."""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.parser import compute_fingerprint
from reader.fines.plate_bootstrap_repository import (
    GeorgiaPlateBootstrapRepository,
)
from reader.fines.provider import FineProvider
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot import texts
from reader.public_bot.collector_ingest_service import (
    CollectorIngestService,
)
from reader.public_bot.manual_bootstrap_service import (
    ManualBootstrapService,
)

pytestmark = pytest.mark.asyncio

_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)


class _AlwaysAvailableProvider(FineProvider):
    def is_available(self) -> bool:
        return True

    async def search_by_plate(self, plate: str):
        raise AssertionError("collector ingest path must never call search_by_plate()")


def _make_service(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    bootstrap_repo = GeorgiaPlateBootstrapRepository(db_path)
    check_service = FineCheckService(_AlwaysAvailableProvider(), task_repo, fine_repo)
    manual_bootstrap = ManualBootstrapService(bootstrap_repo, task_repo, fine_repo, check_service)
    service = CollectorIngestService(task_repo, fine_repo, check_service, bootstrap_repo, manual_bootstrap)
    return service, task_repo, fine_repo, bootstrap_repo


def _create_task(task_repo, car_number, *, chat_id=-100111, created_by=1):
    return task_repo.create(
        car_number=car_number, label=None,
        start_date=date(2026, 10, 1), end_date=date(2026, 11, 1),
        telegram_chat_id=chat_id, created_by_user_id=created_by,
    )


def _record(car_number="AA001AA", protocol_no="P-0001", amount=50, violation_date=date(2026, 10, 10)):
    fingerprint = compute_fingerprint(external_fine_id=protocol_no, violation_date=violation_date, amount=amount)
    return ParsedFineRecord(
        car_number=car_number, external_fine_id=protocol_no,
        penalty_date=violation_date, due_date=None, delivered_status="Вручено",
        fingerprint=fingerprint, raw_data={"source": "collector", "protocolAmount": amount},
        violation_date=violation_date, amount=float(amount), place=None, violation_description=None,
    )


async def test_active_plates_returns_normalized_deduplicated_set(tmp_path):
    service, task_repo, _fine_repo, _bootstrap = _make_service(tmp_path)
    _create_task(task_repo, "AA001AA")
    _create_task(task_repo, "AA001AA")  # второй taск того же физического номера (O528HY702-кейс)
    _create_task(task_repo, "BB002BB")

    assert service.active_plates() == ["AA001AA", "BB002BB"]


async def test_pending_bootstrap_plates_delegates_to_repository(tmp_path):
    service, _task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    bootstrap_repo.ensure_pending("AA001AA")

    assert service.pending_bootstrap_plates() == ["AA001AA"]


async def test_feed_match_persists_canonical_detected_fine(tmp_path):
    service, task_repo, fine_repo, _bootstrap = _make_service(tmp_path)
    task = _create_task(task_repo, "AA001AA")

    result = await service.ingest_feed_match("AA001AA", _record())

    assert result.tasks_matched == 1
    rows = fine_repo.list_current_for_task(task.id, since=_EPOCH_UTC)
    assert len(rows) == 1
    assert rows[0].notification_sent_at is None


async def test_feed_match_no_active_tasks_is_not_an_error(tmp_path):
    service, _task_repo, _fine_repo, _bootstrap = _make_service(tmp_path)

    result = await service.ingest_feed_match("ZZ999ZZ", _record(car_number="ZZ999ZZ"))

    assert result.tasks_matched == 0


async def test_duplicate_feed_match_submission_creates_exactly_one_row(tmp_path):
    service, task_repo, fine_repo, _bootstrap = _make_service(tmp_path)
    task = _create_task(task_repo, "AA001AA")
    record = _record()

    await service.ingest_feed_match("AA001AA", record)
    await service.ingest_feed_match("AA001AA", record)

    rows = fine_repo.list_current_for_task(task.id, since=_EPOCH_UTC)
    assert len(rows) == 1


async def test_feed_match_fan_out_across_multiple_tasks_same_plate(tmp_path):
    service, task_repo, fine_repo, _bootstrap = _make_service(tmp_path)
    task1 = _create_task(task_repo, "AA001AA", chat_id=-100111)
    task2 = _create_task(task_repo, "AA001AA", chat_id=-100222)

    result = await service.ingest_feed_match("AA001AA", _record())

    assert result.tasks_matched == 2
    assert len(fine_repo.list_current_for_task(task1.id, since=_EPOCH_UTC)) == 1
    assert len(fine_repo.list_current_for_task(task2.id, since=_EPOCH_UTC)) == 1


async def test_bootstrap_result_zero_fines_completes_and_sends_canonical_customer_message(tmp_path):
    service, task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    _create_task(task_repo, "AA001AA", chat_id=-100111)
    bootstrap_repo.ensure_pending("AA001AA")

    outcome = await service.ingest_bootstrap_result("AA001AA", [])

    assert outcome.bootstrap_completed is True
    assert outcome.fines_persisted == 0
    assert bootstrap_repo.is_completed("AA001AA")
    assert outcome.customer_notify_messages == [(-100111, texts.format_no_fines_found("AA001AA"))]


async def test_bootstrap_result_with_fines_completes_without_zero_fine_message(tmp_path):
    service, task_repo, fine_repo, bootstrap_repo = _make_service(tmp_path)
    task = _create_task(task_repo, "AA001AA")
    bootstrap_repo.ensure_pending("AA001AA")

    outcome = await service.ingest_bootstrap_result("AA001AA", [_record()])

    assert outcome.bootstrap_completed is True
    assert outcome.fines_persisted == 1
    assert outcome.customer_notify_messages == []
    assert len(fine_repo.list_current_for_task(task.id, since=_EPOCH_UTC)) == 1


async def test_bootstrap_result_no_active_tasks_does_not_complete(tmp_path):
    service, _task_repo, _fine_repo, _bootstrap_repo = _make_service(tmp_path)

    outcome = await service.ingest_bootstrap_result("ZZ999ZZ", [])

    assert outcome.bootstrap_completed is False
    assert outcome.tasks_affected == 0


async def test_bootstrap_result_zero_fines_missing_bootstrap_row_does_not_report_completed(tmp_path):
    """Регрессия для production-инцидента K333HY190 (см. задачу "fix the
    remaining issues discovered during live testing"): плита с активной
    задачей, но БЕЗ единой строки в georgia_plate_bootstrap_state вообще
    (ensure_pending() ни разу не вызывался — реальный случай: задача создана
    до появления bootstrap-tracking) — confirm_no_fines() само по себе
    вернуло бы True (mark_completed() молча no-op'ает на несуществующую
    строку), но ЭТО больше не должно репортиться наружу как
    bootstrap_completed=True, и customer_notify_messages должен остаться
    пустым (иначе клиент получил бы необоснованное 'штрафов не найдено')."""
    service, task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    _create_task(task_repo, "AA001AA", chat_id=-100111)
    assert bootstrap_repo.get("AA001AA") is None  # никогда не было строки

    outcome = await service.ingest_bootstrap_result("AA001AA", [])

    assert outcome.bootstrap_completed is False  # ИСПРАВЛЕНО — раньше было True
    assert outcome.fines_persisted == 0
    assert outcome.customer_notify_messages == []  # никакого необоснованного "штрафов нет"
    assert bootstrap_repo.get("AA001AA") is None  # по-прежнему не создана


async def test_bootstrap_result_zero_fines_already_completed_plate_reports_not_completed(tmp_path):
    """already-completed остаётся безопасным/идемпотентным (не регрессия от
    Issue 3's фикса — bootstrap_completed уже было False в этом случае и
    раньше, т.к. confirm_no_fines()'s собственный idempotency guard уже
    покрывал именно этот кейс)."""
    service, task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    _create_task(task_repo, "AA001AA", chat_id=-100111)
    bootstrap_repo.mark_completed("AA001AA", completed_at=datetime.now(timezone.utc), completed_by_user_id=None)

    outcome = await service.ingest_bootstrap_result("AA001AA", [])

    assert outcome.bootstrap_completed is False
    assert outcome.customer_notify_messages == []


async def test_bootstrap_result_zero_fines_pending_plate_still_completes_and_notifies(tmp_path):
    """Regression — genuine pending->completed transition (Issue 3's fix
    must not affect this, already-correct case)."""
    service, task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    _create_task(task_repo, "AA001AA", chat_id=-100111)
    bootstrap_repo.ensure_pending("AA001AA")

    outcome = await service.ingest_bootstrap_result("AA001AA", [])

    assert outcome.bootstrap_completed is True
    assert outcome.customer_notify_messages == [(-100111, texts.format_no_fines_found("AA001AA"))]


# ---- mode="audit" (см. задачу "fix the remaining issues discovered during
# live testing" — production 515-plate audit needed an ad-hoc skip-list
# workaround precisely because this mode didn't exist yet) ----


@pytest.mark.parametrize("starting_state", ["missing", "pending", "completed"])
async def test_audit_mode_zero_fines_never_touches_bootstrap_or_notifies(tmp_path, starting_state):
    service, task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    _create_task(task_repo, "AA001AA", chat_id=-100111)
    if starting_state in ("pending", "completed"):
        bootstrap_repo.ensure_pending("AA001AA")  # mark_completed() alone never creates a row
    if starting_state == "completed":
        bootstrap_repo.mark_completed("AA001AA", completed_at=datetime.now(timezone.utc), completed_by_user_id=None)

    before = bootstrap_repo.get("AA001AA")

    outcome = await service.ingest_bootstrap_result("AA001AA", [], mode="audit")

    assert outcome.bootstrap_completed is False
    assert outcome.fines_persisted == 0
    assert outcome.customer_notify_messages == []  # NEVER notifies in audit mode
    after = bootstrap_repo.get("AA001AA")
    if starting_state == "missing":
        assert before is None
        assert after is None  # still no row created
    else:
        assert after.bootstrap_status == before.bootstrap_status  # completely unchanged


@pytest.mark.parametrize("starting_state", ["missing", "pending", "completed"])
async def test_audit_mode_new_fines_persist_and_notify_normally_without_touching_bootstrap(
    tmp_path, starting_state,
):
    """New fines found during an audit mode pass must still go through the
    normal canonical persistence/notification path -- only bootstrap state
    is exempt from being touched."""
    service, task_repo, fine_repo, bootstrap_repo = _make_service(tmp_path)
    task = _create_task(task_repo, "AA001AA", chat_id=-100111)
    if starting_state in ("pending", "completed"):
        bootstrap_repo.ensure_pending("AA001AA")  # mark_completed() alone never creates a row
    if starting_state == "completed":
        bootstrap_repo.mark_completed("AA001AA", completed_at=datetime.now(timezone.utc), completed_by_user_id=None)
    before = bootstrap_repo.get("AA001AA")

    outcome = await service.ingest_bootstrap_result("AA001AA", [_record()], mode="audit")

    assert outcome.fines_persisted == 1
    assert outcome.bootstrap_completed is False
    assert outcome.customer_notify_messages == []
    rows = fine_repo.list_current_for_task(task.id, since=_EPOCH_UTC)
    assert len(rows) == 1
    assert rows[0].notification_sent_at is None  # pending normal NotificationFlushJob pickup
    after = bootstrap_repo.get("AA001AA")
    if starting_state == "missing":
        assert before is None and after is None
    else:
        assert after.bootstrap_status == before.bootstrap_status


async def test_audit_mode_duplicate_submission_still_dedupes(tmp_path):
    service, task_repo, fine_repo, _bootstrap = _make_service(tmp_path)
    task = _create_task(task_repo, "AA001AA")
    record = _record()

    await service.ingest_bootstrap_result("AA001AA", [record], mode="audit")
    await service.ingest_bootstrap_result("AA001AA", [record], mode="audit")

    assert len(fine_repo.list_current_for_task(task.id, since=_EPOCH_UTC)) == 1


async def test_bootstrap_mode_explicit_matches_default_behavior(tmp_path):
    """mode="bootstrap" передан явно — байт-в-байт то же самое поведение,
    что и default/omitted (regression для существующих вызовов, см. задачу:
    "Default must preserve current bootstrap behavior for backward
    compatibility")."""
    service, task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    _create_task(task_repo, "AA001AA", chat_id=-100111)
    bootstrap_repo.ensure_pending("AA001AA")

    outcome = await service.ingest_bootstrap_result("AA001AA", [], mode="bootstrap")

    assert outcome.bootstrap_completed is True
    assert outcome.customer_notify_messages == [(-100111, texts.format_no_fines_found("AA001AA"))]


async def test_collector_bootstrap_completion_is_idempotent_against_already_completed_plate(tmp_path):
    """Race с manual manager bootstrap (см. задачу п. "Collector/manager
    bootstrap race") — менеджер уже завершил bootstrap вручную ДО того, как
    коллекторский bootstrap-result дошёл; коллектор не должен ни создать
    дубликат, ни откатить/перезаписать completed-состояние."""
    service, task_repo, fine_repo, bootstrap_repo = _make_service(tmp_path)
    task = _create_task(task_repo, "AA001AA")
    bootstrap_repo.ensure_pending("AA001AA")

    # Менеджер уже подтвердил "штрафов нет" вручную.
    bootstrap_repo.mark_completed("AA001AA", completed_at=datetime.now(timezone.utc), completed_by_user_id=777)

    outcome = await service.ingest_bootstrap_result("AA001AA", [_record()])

    # confirm_no_fines() внутри вернул False (уже completed) — но fines всё
    # равно уже были персистированы ДО этой проверки (см. модульный
    # докстрок ingest_bootstrap_result: dedup делает это безопасным, а не
    # "потерянным" штрафом).
    assert outcome.bootstrap_completed is False
    assert len(fine_repo.list_current_for_task(task.id, since=_EPOCH_UTC)) == 1
    state = bootstrap_repo.get("AA001AA")
    assert state.bootstrap_completed_by_user_id == 777  # менеджерская audit-запись не перезаписана
