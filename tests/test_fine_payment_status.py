"""Статусы оплаты штрафов Грузии (reader/fines/payment_status.py): сверка
по квитанции после полной проверки, ⚪ только после двух успешных полных
проверок с интервалом, неполный ответ не считается исчезновением, отметки
менеджера с журналом, распознавание текста страницы оплаты. Номера и
квитанции в тестах синтетические."""

import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.payment_status import (
    DISPLAY_GONE,
    DISPLAY_UNVERIFIED,
    MIA_PAYMENT_FAILED_MESSAGE,
    PAYMENT_NEEDS_REVIEW,
    PAYMENT_PAID,
    PAYMENT_UNPAID,
    PRESENCE_GONE,
    PRESENCE_LISTED,
    PRESENCE_MISSING,
    PRESENCE_UNVERIFIED,
    SOURCE_PAYMENT_PAGE,
    FineStatus,
    FineStatusRepository,
    FineStatusTracker,
    PaymentPageMarkers,
    PaymentPageVerdict,
    breakdown_of,
    classify_payment_page_text,
)
from reader.fines.task_repository import FineMonitoringTaskRepository

_CAR = "AA111BB"
_OTHER_CAR = "CC222DD"
_MANAGER = 5001
_T0 = datetime(2026, 11, 18, 19, 0, 0)


def _record(protocol_no: str, amount: float = 50.0, *, car_number: str = _CAR) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number, external_fine_id=protocol_no,
        penalty_date=date(2026, 11, 1), due_date=date(2026, 12, 31),
        delivered_status="Не вручено", fingerprint=f"fp-{protocol_no}",
        raw_data={"protocolNo": protocol_no}, amount=amount,
    )


@pytest.fixture()
def tracker(tmp_path):
    repository = FineStatusRepository(tmp_path / "users.db")
    yield FineStatusTracker(repository, disappearance_min_gap=timedelta(hours=12))
    repository.close()


def _status(tracker, protocol_no, car_number=_CAR):
    return tracker.statuses_by_protocol(car_number)[protocol_no]


def test_full_check_creates_unpaid_listed_statuses(tracker):
    tracker.record_full_check(_CAR, [_record("GE001", 50), _record("GE002", 100)], complete=True, at=_T0)

    statuses = tracker.statuses_by_protocol(_CAR)
    assert set(statuses) == {"GE001", "GE002"}
    assert all(s.payment_status == PAYMENT_UNPAID and s.presence == PRESENCE_LISTED for s in statuses.values())
    assert tracker.events(statuses["GE001"].id)[0]["event"] == "listed"


def test_absent_once_is_missing_and_still_counts_as_debt(tracker):
    tracker.record_full_check(_CAR, [_record("GE001", 50), _record("GE002", 100)], complete=True, at=_T0)

    tracker.record_full_check(_CAR, [_record("GE002", 100)], complete=True, at=_T0 + timedelta(hours=24))

    missing = _status(tracker, "GE001")
    assert missing.presence == PRESENCE_MISSING
    assert missing.display_status == PAYMENT_UNPAID  # прежний статус сохраняется
    breakdown = tracker.breakdown_for_cars([_CAR])[_CAR]
    assert breakdown.unpaid_amount == 150 and breakdown.unpaid_count == 2


def test_gone_only_after_two_successful_checks_with_gap(tracker):
    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0)
    tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=_T0 + timedelta(hours=1))
    # вторая проверка без штрафа слишком быстро — ещё не ⚪
    tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=_T0 + timedelta(hours=5))
    assert _status(tracker, "GE001").presence == PRESENCE_MISSING

    tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=_T0 + timedelta(hours=13))

    gone = _status(tracker, "GE001")
    assert gone.presence == PRESENCE_GONE
    assert gone.display_status == DISPLAY_GONE
    assert tracker.breakdown_for_cars([_CAR]) == {}
    assert [e["event"] for e in tracker.events(gone.id)] == ["listed", "missing", "gone"]


def test_unconfirmed_empty_answer_is_never_evidence(tracker):
    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0)

    for hours in (24, 48, 72):
        tracker.record_full_check(_CAR, [], complete=True, at=_T0 + timedelta(hours=hours))

    assert _status(tracker, "GE001").presence == PRESENCE_LISTED


def test_has_listed_receipts_and_gone_protocols(tracker):
    assert not tracker.has_listed_receipts(_CAR)
    tracker.record_full_check(_CAR, [_record("GE001"), _record("GE002")], complete=True, at=_T0)
    tracker.record_full_check(_CAR, [_record("GE002")], complete=True, at=_T0 + timedelta(hours=1))
    assert tracker.has_listed_receipts(_CAR)
    assert tracker.gone_protocols(_CAR) == set()  # "missing" — ещё не ⚪

    tracker.record_full_check(_CAR, [_record("GE002")], complete=True, at=_T0 + timedelta(hours=14))

    assert tracker.gone_protocols(_CAR) == {"GE001"}
    assert tracker.recently_gone_car_numbers(at=_T0 + timedelta(days=1)) == [_CAR]
    assert tracker.recently_gone_car_numbers(at=_T0 + timedelta(days=5)) == []
    assert tracker.car_numbers_with_statuses([_CAR, _OTHER_CAR]) == {_CAR}


def test_incomplete_response_never_counts_as_disappearance(tracker):
    tracker.record_full_check(_CAR, [_record("GE001"), _record("GE002")], complete=True, at=_T0)

    for hours in (24, 48, 72):
        tracker.record_full_check(_CAR, [_record("GE002")], complete=False, at=_T0 + timedelta(hours=hours))

    assert _status(tracker, "GE001").presence == PRESENCE_LISTED


def test_found_again_resets_missing(tracker):
    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0)
    tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=_T0 + timedelta(hours=24))
    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0 + timedelta(hours=30))
    # снова пропал — отсчёт интервала начинается заново
    tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=_T0 + timedelta(hours=36))
    tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=_T0 + timedelta(hours=40))

    status = _status(tracker, "GE001")
    assert status.presence == PRESENCE_MISSING
    assert "found_again" in [e["event"] for e in tracker.events(status.id)]


def test_reappeared_after_gone_keeps_payment_status(tracker):
    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0)
    status_id = _status(tracker, "GE001").id
    tracker.mark_paid(status_id, actor_user_id=_MANAGER, note="TBC 123")
    tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=_T0 + timedelta(hours=1))
    tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=_T0 + timedelta(hours=14))
    assert _status(tracker, "GE001").presence == PRESENCE_GONE

    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0 + timedelta(hours=30))

    status = _status(tracker, "GE001")
    assert status.presence == PRESENCE_LISTED
    assert status.payment_status == PAYMENT_PAID
    assert tracker.events(status_id)[-1]["event"] == "reappeared"


def test_matching_is_by_receipt_not_by_amount(tracker):
    tracker.record_full_check(_CAR, [_record("GE001", 50)], complete=True, at=_T0)

    # другая квитанция на ту же сумму — старая не "совпадает" по сумме
    tracker.record_full_check(_CAR, [_record("GE009", 50)], complete=True, at=_T0 + timedelta(hours=24))

    statuses = tracker.statuses_by_protocol(_CAR)
    assert statuses["GE001"].presence == PRESENCE_MISSING
    assert statuses["GE009"].presence == PRESENCE_LISTED


def test_other_car_is_not_touched(tracker):
    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0)
    tracker.record_full_check(_OTHER_CAR, [], complete=True, at=_T0 + timedelta(hours=24))

    assert _status(tracker, "GE001").presence == PRESENCE_LISTED


def test_feed_listing_does_not_mark_others_missing(tracker):
    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0)

    tracker.record_listed(_CAR, _record("GE002"), at=_T0 + timedelta(hours=24))

    statuses = tracker.statuses_by_protocol(_CAR)
    assert statuses["GE001"].presence == PRESENCE_LISTED
    assert statuses["GE002"].presence == PRESENCE_LISTED


def test_records_without_receipt_number_are_ignored(tracker):
    record = _record("X")
    record = replace(record, external_fine_id=None)

    tracker.record_full_check(_CAR, [record], complete=True, at=_T0)

    assert tracker.statuses_for_car(_CAR) == []


def _seed_unverified(tracker, tmp_path_db=None):
    """Статус 'unverified' — как после первичного заполнения истории."""
    status = tracker._repository.insert_listed(
        car_number=_CAR, protocol_no="OLD1", amount=30, at=_T0 - timedelta(days=40), source="check",
    )
    tracker._repository._conn.execute(
        "UPDATE georgia_fine_status SET presence = ? WHERE id = ?", (PRESENCE_UNVERIFIED, status.id),
    )
    tracker._repository._conn.commit()
    return status.id


def test_unverified_needs_two_new_absences_and_returns_when_found(tracker):
    status_id = _seed_unverified(tracker)
    assert tracker.has_listed_receipts(_CAR)  # свидетельство долга для false-zero guard
    assert tracker.breakdown_for_cars([_CAR])[_CAR].unpaid_count == 0  # не подтверждённый долг

    tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=_T0)
    first = tracker.get(status_id)
    assert first.presence == PRESENCE_UNVERIFIED and first.missing_since == _T0
    tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=_T0 + timedelta(hours=11))
    assert tracker.get(status_id).presence == PRESENCE_UNVERIFIED

    tracker.record_full_check(_CAR, [_record("OLD1", 30)], complete=True, at=_T0 + timedelta(hours=20))

    found = tracker.get(status_id)
    assert found.presence == PRESENCE_LISTED and found.missing_since is None
    assert tracker.breakdown_for_cars([_CAR])[_CAR].unpaid_amount == 30


def test_unverified_becomes_gone_after_second_new_absence(tracker):
    status_id = _seed_unverified(tracker)
    tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=_T0)
    tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=_T0 + timedelta(hours=12))

    assert tracker.get(status_id).presence == PRESENCE_GONE
    assert [e["event"] for e in tracker.events(status_id)][-2:] == ["unverified_missing", "gone"]


def test_unverified_ignores_errors_unconfirmed_and_incomplete_answers(tracker):
    status_id = _seed_unverified(tracker)
    tracker.record_full_check(_CAR, [], complete=True, at=_T0)  # пустой, не подтверждён
    tracker.record_full_check(_CAR, [_record("OTHER")], complete=False, at=_T0 + timedelta(hours=13))

    status = tracker.get(status_id)
    assert status.presence == PRESENCE_UNVERIFIED and status.missing_since is None


# ---- отметки менеджера ----


def test_mark_paid_requires_a_reason(tracker):
    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0)
    status_id = _status(tracker, "GE001").id

    with pytest.raises(ValueError):
        tracker.mark_paid(status_id, actor_user_id=_MANAGER, note="   ")

    assert _status(tracker, "GE001").payment_status == PAYMENT_UNPAID


def test_paid_is_excluded_from_debt_and_logged(tracker):
    tracker.record_full_check(_CAR, [_record("GE001", 50), _record("GE002", 100)], complete=True, at=_T0)
    status_id = _status(tracker, "GE001").id

    updated = tracker.mark_paid(status_id, actor_user_id=_MANAGER, note="  TBC Pay  123456 ")

    assert updated.payment_status == PAYMENT_PAID
    assert updated.payment_note == "TBC Pay 123456"
    assert updated.payment_updated_by == _MANAGER
    breakdown = tracker.breakdown_for_cars([_CAR])[_CAR]
    assert (breakdown.unpaid_amount, breakdown.paid_amount) == (100, 50)
    event = tracker.events(status_id)[-1]
    assert event["event"] == "payment_paid" and event["actor_user_id"] == _MANAGER


def test_needs_review_and_clear(tracker):
    tracker.record_full_check(_CAR, [_record("GE001", 70)], complete=True, at=_T0)
    status_id = _status(tracker, "GE001").id

    tracker.mark_needs_review(status_id, actor_user_id=_MANAGER, note="отказ")
    assert tracker.breakdown_for_cars([_CAR])[_CAR].needs_review_amount == 70

    cleared = tracker.mark_unpaid(status_id, actor_user_id=_MANAGER)
    assert cleared.payment_status == PAYMENT_UNPAID
    assert [e["event"] for e in tracker.events(status_id)][-2:] == ["payment_needs_review", "payment_cleared"]


def test_unknown_status_id_returns_none(tracker):
    assert tracker.mark_needs_review(999, actor_user_id=_MANAGER, note=None) is None


# ---- текст страницы оплаты ----


def test_mia_generic_failure_is_needs_review_not_paid(tracker):
    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0)
    status_id = _status(tracker, "GE001").id

    outcome = tracker.apply_payment_page_text(status_id, "ჯარიმის გადახდა ვერ მოხერხდა!", actor_user_id=_MANAGER)

    assert outcome.verdict is PaymentPageVerdict.PAYMENT_FAILED
    assert outcome.status.payment_status == PAYMENT_NEEDS_REVIEW
    assert outcome.status.payment_source == SOURCE_PAYMENT_PAGE


def test_unrecognized_text_is_needs_review(tracker):
    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0)
    status_id = _status(tracker, "GE001").id

    outcome = tracker.apply_payment_page_text(status_id, "Что-то пошло не так", actor_user_id=_MANAGER)

    assert outcome.verdict is PaymentPageVerdict.UNRECOGNIZED
    assert outcome.status.payment_status == PAYMENT_NEEDS_REVIEW


def test_already_paid_only_with_configured_marker(tmp_path):
    repository = FineStatusRepository(tmp_path / "users.db")
    tracker = FineStatusTracker(
        repository, markers=PaymentPageMarkers(already_paid=("ქვითარი უკვე გადახდილია",)),
    )
    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0)
    status_id = _status(tracker, "GE001").id

    outcome = tracker.apply_payment_page_text(
        status_id, "შეცდომა: ქვითარი უკვე გადახდილია.", actor_user_id=_MANAGER,
    )

    assert outcome.verdict is PaymentPageVerdict.ALREADY_PAID
    assert outcome.status.payment_status == PAYMENT_PAID
    repository.close()


@pytest.mark.parametrize("markers", [
    PaymentPageMarkers(already_paid=("paid",)),                      # слишком короткий
    PaymentPageMarkers(already_paid=("ვერ მოხერხდა",)),              # часть общего отказа МВД
    PaymentPageMarkers(already_paid=(MIA_PAYMENT_FAILED_MESSAGE,)),
    PaymentPageMarkers(technical_error=("ჯარიმის გადახდა",)),
])
def test_dangerous_markers_are_rejected(tmp_path, markers):
    repository = FineStatusRepository(tmp_path / "users.db")
    with pytest.raises(ValueError):
        FineStatusTracker(repository, markers=markers)
    repository.close()


def test_default_markers_never_confirm_payment():
    markers = PaymentPageMarkers()
    assert markers.already_paid == ()
    for text in ("ჯარიმის გადახდა ვერ მოხერხდა!", "Payment declined", "already paid", ""):
        assert classify_payment_page_text(text, markers) is not PaymentPageVerdict.ALREADY_PAID


def test_technical_error_changes_nothing_but_is_logged(tmp_path):
    repository = FineStatusRepository(tmp_path / "users.db")
    tracker = FineStatusTracker(repository, markers=PaymentPageMarkers(technical_error=("technical error",)))
    tracker.record_full_check(_CAR, [_record("GE001", 40)], complete=True, at=_T0)
    status_id = _status(tracker, "GE001").id

    outcome = tracker.apply_payment_page_text(status_id, "Technical  ERROR, try later", actor_user_id=_MANAGER)

    assert outcome.verdict is PaymentPageVerdict.TECHNICAL_ERROR
    assert outcome.changed is False
    status = _status(tracker, "GE001")
    assert status.payment_status == PAYMENT_UNPAID
    assert tracker.breakdown_for_cars([_CAR])[_CAR].unpaid_amount == 40
    assert tracker.events(status_id)[-1]["event"] == "payment_page_technical_error"
    repository.close()


def test_classifier_priority_and_normalization():
    markers = PaymentPageMarkers(already_paid=("already paid",), technical_error=("timeout",))
    assert classify_payment_page_text("  ALREADY   paid! ", markers) is PaymentPageVerdict.ALREADY_PAID
    assert classify_payment_page_text(
        f"{MIA_PAYMENT_FAILED_MESSAGE}! already paid", markers,
    ) is PaymentPageVerdict.ALREADY_PAID
    assert classify_payment_page_text(f"{MIA_PAYMENT_FAILED_MESSAGE} timeout", markers) is (
        PaymentPageVerdict.TECHNICAL_ERROR
    )
    assert classify_payment_page_text(None, markers) is PaymentPageVerdict.UNRECOGNIZED


class _S:
    """Только поля, от которых зависит display_status (логика — настоящая)."""

    display_status = FineStatus.display_status

    def __init__(self, payment_status, presence, amount):
        self.payment_status = payment_status
        self.presence = presence
        self.amount = amount


def test_breakdown_ignores_gone():
    breakdown = breakdown_of([
        _S(PAYMENT_UNPAID, PRESENCE_LISTED, 10), _S(PAYMENT_PAID, PRESENCE_MISSING, 20),
        _S(PAYMENT_NEEDS_REVIEW, PRESENCE_LISTED, 30), _S(PAYMENT_UNPAID, PRESENCE_GONE, 1000),
    ])
    assert (breakdown.unpaid_amount, breakdown.paid_amount, breakdown.needs_review_amount) == (10, 20, 30)


def test_breakdown_counts_marked_unverified_receipts_by_their_mark():
    """⏳ — только неоплаченная история; 🟡 на квитанции ⏳ — долг, 🟢 — оплачено."""
    breakdown = breakdown_of([
        _S(PAYMENT_UNPAID, PRESENCE_UNVERIFIED, 5),
        _S(PAYMENT_NEEDS_REVIEW, PRESENCE_UNVERIFIED, 40),
        _S(PAYMENT_PAID, PRESENCE_UNVERIFIED, 7),
    ])
    assert (breakdown.unverified_amount, breakdown.unverified_count) == (5, 1)
    assert (breakdown.needs_review_amount, breakdown.needs_review_count) == (40, 1)
    assert (breakdown.paid_amount, breakdown.paid_count) == (7, 1)
    assert breakdown.unpaid_amount == 0


def test_source_updated_on_round_trip(tracker):
    assert tracker.source_updated_on() is None
    tracker.record_source_updated_on(date(2026, 11, 12), at=_T0)
    tracker.record_source_updated_on(date(2026, 11, 18), at=_T0 + timedelta(hours=1))

    updated_on, observed_at = tracker.source_updated_on()
    assert updated_on == date(2026, 11, 18)
    assert observed_at == _T0 + timedelta(hours=1)


# ---- первичное заполнение из detected_fines ----


def test_seed_marks_historical_receipts_unverified_and_keys_by_task_plate(tmp_path):
    db_path = tmp_path / "users.db"
    tasks = FineMonitoringTaskRepository(db_path)
    fines = DetectedFineRepository(db_path)
    task = tasks.create(
        car_number=_CAR, label=None, start_date=date(2026, 11, 1), end_date=date(2026, 12, 31),
        telegram_chat_id=-1, created_by_user_id=1,
    )
    old = fines.create(monitoring_task_id=task.id, car_number=_CAR, external_fine_id="OLD1", fingerprint="f-old",
                       penalty_date=None, due_date=None, delivered_status=None, raw_data="{}", amount=10)
    tasks.record_successful_check(task.id, total_amount=60)
    # "OLD1" остался со старым last_seen_at — на последней проверке его уже не было
    fines._conn.execute("UPDATE detected_fines SET last_seen_at = '2026-01-01 00:00:00' WHERE id = ?", (old.id,))
    fines._conn.commit()
    fines.create(monitoring_task_id=task.id, car_number=_CAR, external_fine_id="CUR1", fingerprint="f-cur",
                 penalty_date=None, due_date=None, delivered_status=None, raw_data="{}", amount=60)
    # строка, у которой protocolAuto записан иначе, чем номер задачи
    fines.create(monitoring_task_id=task.id, car_number="aa 111 bb", external_fine_id="CUR2", fingerprint="f-c2",
                 penalty_date=None, due_date=None, delivered_status=None, raw_data="{}", amount=5)
    repository = FineStatusRepository(db_path)
    tracker = FineStatusTracker(repository)

    assert tracker.seed_from_detected_fines() == 3
    assert tracker.seed_from_detected_fines() == 0  # идемпотентно

    statuses = tracker.statuses_by_protocol(_CAR)
    assert set(statuses) == {"OLD1", "CUR1", "CUR2"}
    assert statuses["CUR1"].presence == PRESENCE_LISTED and statuses["CUR1"].amount == 60
    assert statuses["CUR2"].presence == PRESENCE_LISTED
    # на последней (старой) проверке его не было — но это не подтверждение ⚪
    assert statuses["OLD1"].presence == PRESENCE_UNVERIFIED
    assert statuses["OLD1"].display_status == DISPLAY_UNVERIFIED
    assert tracker.gone_protocols(_CAR) == set()
    breakdown = tracker.breakdown_for_cars([_CAR])[_CAR]
    assert (breakdown.unpaid_amount, breakdown.unverified_amount) == (65, 10)
    repository.close()
    fines.close()
    tasks.close()
