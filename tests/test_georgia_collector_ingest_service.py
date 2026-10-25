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


async def test_bootstrap_result_default_mode_is_bootstrap(tmp_path):
    """Regression guard for "normal bootstrap submissions must remain
    completely unchanged": calling ingest_bootstrap_result() WITHOUT a mode=
    kwarg at all (the only way every pre-existing caller/test in this file
    already calls it) must behave exactly like mode="bootstrap"."""
    service, task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    _create_task(task_repo, "AA001AA", chat_id=-100111)
    bootstrap_repo.ensure_pending("AA001AA")

    outcome = await service.ingest_bootstrap_result("AA001AA", [])

    assert outcome.bootstrap_completed is True
    assert bootstrap_repo.is_completed("AA001AA")
    assert outcome.customer_notify_messages == [(-100111, texts.format_no_fines_found("AA001AA"))]


async def test_bootstrap_mode_zero_fines_missing_bootstrap_row_sends_no_message(tmp_path):
    """Defense in depth: the K333HY190 gap also existed on the default
    mode="bootstrap" path itself, not just a mass recheck -- a task created
    before the bootstrap system existed (so ensure_pending() was never
    called for it) going through a genuine bootstrap submission must not be
    reported as bootstrap_completed=True or trigger the zero-fine message
    either, even though mode="audit" is not involved at all here."""
    service, task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    _create_task(task_repo, "K333HY190", chat_id=-100999)
    assert bootstrap_repo.get("K333HY190") is None

    outcome = await service.ingest_bootstrap_result("K333HY190", [])

    assert outcome.bootstrap_completed is False
    assert outcome.customer_notify_messages == []
    assert bootstrap_repo.get("K333HY190") is None


async def test_unauthorized_mode_value_is_rejected(tmp_path):
    service, task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    _create_task(task_repo, "AA001AA")
    bootstrap_repo.ensure_pending("AA001AA")

    with pytest.raises(ValueError):
        await service.ingest_bootstrap_result("AA001AA", [], mode="delete_everything")

    # Rejected BEFORE any persistence/state change — same "fail closed,
    # change nothing" principle as every other guard in this module.
    assert bootstrap_repo.get("AA001AA").bootstrap_status == "pending"


# ---------------------------------------------------------------------------
# AUDIT MODE (2026-10-25) — background mass recheck. See
# reader/public_bot/collector_ingest_service.py's own module docstring for
# the full root-cause explanation (K333HY190 / the 16 bootstrap-row
# incident) that these tests pin down.
# ---------------------------------------------------------------------------

async def test_audit_zero_fines_sends_no_customer_message(tmp_path):
    service, task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    _create_task(task_repo, "AA001AA", chat_id=-100111)
    bootstrap_repo.ensure_pending("AA001AA")

    outcome = await service.ingest_bootstrap_result("AA001AA", [], mode="audit")

    assert outcome.customer_notify_messages == []


async def test_audit_zero_fines_leaves_pending_bootstrap_unchanged(tmp_path):
    service, task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    _create_task(task_repo, "AA001AA")
    bootstrap_repo.ensure_pending("AA001AA")

    outcome = await service.ingest_bootstrap_result("AA001AA", [], mode="audit")

    state = bootstrap_repo.get("AA001AA")
    assert state.bootstrap_status == "pending"
    assert state.bootstrap_completed_at is None
    assert outcome.bootstrap_completed is False


async def test_audit_zero_fines_leaves_missing_bootstrap_row_unchanged(tmp_path):
    """THE production incident this fixes (K333HY190): a fine_monitoring_task
    exists, but georgia_plate_bootstrap_state has NO row for this car_number
    at all (e.g. a task created before the bootstrap system existed). Before
    this fix, ingest_bootstrap_result() would still report
    bootstrap_completed=True (confirm_no_fines()'s own guard only checks
    `state is not None and state.is_completed`, which is False for a missing
    row too) and send the real "штрафов не найдено" message, while
    GeorgiaPlateBootstrapRepository.mark_completed()'s UPDATE silently
    no-opped against the nonexistent row -- the customer got a real message
    for a side effect that never actually happened in the database."""
    service, task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    _create_task(task_repo, "K333HY190", chat_id=-100999)
    assert bootstrap_repo.get("K333HY190") is None  # confirmed missing, not pending

    outcome = await service.ingest_bootstrap_result("K333HY190", [], mode="audit")

    assert outcome.customer_notify_messages == []
    assert outcome.bootstrap_completed is False
    assert bootstrap_repo.get("K333HY190") is None  # still no row at all


async def test_audit_zero_fines_leaves_completed_bootstrap_unchanged(tmp_path):
    service, task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    _create_task(task_repo, "AA001AA", chat_id=-100111)
    bootstrap_repo.ensure_pending("AA001AA")
    bootstrap_repo.mark_completed("AA001AA", completed_at=datetime(2026, 1, 1, tzinfo=timezone.utc), completed_by_user_id=None)

    outcome = await service.ingest_bootstrap_result("AA001AA", [], mode="audit")

    state = bootstrap_repo.get("AA001AA")
    assert state.bootstrap_status == "completed"
    assert state.bootstrap_completed_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert outcome.customer_notify_messages == []
    assert outcome.bootstrap_completed is True  # honest read of current status, not a new side effect


async def test_audit_existing_fine_dedupes_via_mark_seen(tmp_path):
    service, task_repo, fine_repo, bootstrap_repo = _make_service(tmp_path)
    task = _create_task(task_repo, "AA001AA")
    bootstrap_repo.mark_completed("AA001AA", completed_at=datetime.now(timezone.utc), completed_by_user_id=None)
    record = _record()
    await service.ingest_bootstrap_result("AA001AA", [record], mode="bootstrap")
    existing_rows = fine_repo.list_current_for_task(task.id, since=_EPOCH_UTC)
    assert len(existing_rows) == 1
    existing_id = existing_rows[0].id

    outcome = await service.ingest_bootstrap_result("AA001AA", [record], mode="audit")

    rows = fine_repo.list_current_for_task(task.id, since=_EPOCH_UTC)
    assert len(rows) == 1  # deduped, not duplicated
    assert rows[0].id == existing_id  # same row, re-seen (mark_seen), not recreated
    assert outcome.fines_persisted == 1


async def test_audit_new_fine_creates_row_eligible_for_normal_notification(tmp_path):
    """New fines in audit mode persist through the exact same canonical path
    (notification_sent_at=NULL) that feeds the existing NotificationFlushJob
    -- audit mode changes nothing about new-fine delivery, only suppresses
    the SEPARATE zero-fine announcement and bootstrap state transition."""
    service, task_repo, fine_repo, bootstrap_repo = _make_service(tmp_path)
    task = _create_task(task_repo, "AA001AA")
    bootstrap_repo.ensure_pending("AA001AA")

    outcome = await service.ingest_bootstrap_result("AA001AA", [_record()], mode="audit")

    rows = fine_repo.list_current_for_task(task.id, since=_EPOCH_UTC)
    assert len(rows) == 1
    assert rows[0].notification_sent_at is None  # still eligible for NotificationFlushJob
    assert outcome.fines_persisted == 1
    assert outcome.customer_notify_messages == []
    # Bootstrap state untouched even though a fine WAS found this time.
    assert bootstrap_repo.get("AA001AA").bootstrap_status == "pending"


async def test_audit_multiple_tasks_for_one_physical_plate_both_dedupe_correctly(tmp_path):
    service, task_repo, fine_repo, bootstrap_repo = _make_service(tmp_path)
    task1 = _create_task(task_repo, "AA001AA", chat_id=-100111)
    task2 = _create_task(task_repo, "AA001AA", chat_id=-100222)
    bootstrap_repo.ensure_pending("AA001AA")

    outcome = await service.ingest_bootstrap_result("AA001AA", [_record()], mode="audit")

    assert outcome.tasks_affected == 2
    assert len(fine_repo.list_current_for_task(task1.id, since=_EPOCH_UTC)) == 1
    assert len(fine_repo.list_current_for_task(task2.id, since=_EPOCH_UTC)) == 1
    assert outcome.customer_notify_messages == []
    assert bootstrap_repo.get("AA001AA").bootstrap_status == "pending"


async def test_audit_unauthorized_mode_value_is_rejected(tmp_path):
    service, task_repo, _fine_repo, bootstrap_repo = _make_service(tmp_path)
    _create_task(task_repo, "AA001AA")
    bootstrap_repo.ensure_pending("AA001AA")

    with pytest.raises(ValueError):
        await service.ingest_bootstrap_result("AA001AA", [], mode="not-a-real-mode")

    assert bootstrap_repo.get("AA001AA").bootstrap_status == "pending"


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
