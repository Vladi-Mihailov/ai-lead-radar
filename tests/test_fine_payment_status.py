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
    MIA_PAYMENT_FAILED_MESSAGE,
    PAYMENT_NEEDS_REVIEW,
    PAYMENT_PAID,
    PAYMENT_UNPAID,
    PRESENCE_GONE,
    PRESENCE_LISTED,
    PRESENCE_MISSING,
    SOURCE_PAYMENT_PAGE,
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
    tracker.record_full_check(_CAR, [], complete=True, at=_T0 + timedelta(hours=1))
    # вторая проверка без штрафа слишком быстро — ещё не ⚪
    tracker.record_full_check(_CAR, [], complete=True, at=_T0 + timedelta(hours=5))
    assert _status(tracker, "GE001").presence == PRESENCE_MISSING

    tracker.record_full_check(_CAR, [], complete=True, at=_T0 + timedelta(hours=13))

    gone = _status(tracker, "GE001")
    assert gone.presence == PRESENCE_GONE
    assert gone.display_status == DISPLAY_GONE
    assert tracker.breakdown_for_cars([_CAR]) == {}
    assert [e["event"] for e in tracker.events(gone.id)] == ["listed", "missing", "gone"]


def test_incomplete_response_never_counts_as_disappearance(tracker):
    tracker.record_full_check(_CAR, [_record("GE001"), _record("GE002")], complete=True, at=_T0)

    for hours in (24, 48, 72):
        tracker.record_full_check(_CAR, [_record("GE002")], complete=False, at=_T0 + timedelta(hours=hours))

    assert _status(tracker, "GE001").presence == PRESENCE_LISTED


def test_found_again_resets_missing(tracker):
    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0)
    tracker.record_full_check(_CAR, [], complete=True, at=_T0 + timedelta(hours=24))
    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0 + timedelta(hours=30))
    # снова пропал — отсчёт интервала начинается заново
    tracker.record_full_check(_CAR, [], complete=True, at=_T0 + timedelta(hours=36))
    tracker.record_full_check(_CAR, [], complete=True, at=_T0 + timedelta(hours=40))

    status = _status(tracker, "GE001")
    assert status.presence == PRESENCE_MISSING
    assert "found_again" in [e["event"] for e in tracker.events(status.id)]


def test_reappeared_after_gone_keeps_payment_status(tracker):
    tracker.record_full_check(_CAR, [_record("GE001")], complete=True, at=_T0)
    status_id = _status(tracker, "GE001").id
    tracker.mark_paid(status_id, actor_user_id=_MANAGER, note="TBC 123")
    tracker.record_full_check(_CAR, [], complete=True, at=_T0 + timedelta(hours=1))
    tracker.record_full_check(_CAR, [], complete=True, at=_T0 + timedelta(hours=14))
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


def test_breakdown_ignores_gone():
    class _S:
        def __init__(self, payment_status, presence, amount):
            self.payment_status = payment_status
            self.presence = presence
            self.amount = amount

        @property
        def counts_as_listed(self):
            return self.presence != PRESENCE_GONE

    breakdown = breakdown_of([
        _S(PAYMENT_UNPAID, PRESENCE_LISTED, 10), _S(PAYMENT_PAID, PRESENCE_MISSING, 20),
        _S(PAYMENT_NEEDS_REVIEW, PRESENCE_LISTED, 30), _S(PAYMENT_UNPAID, PRESENCE_GONE, 1000),
    ])
    assert (breakdown.unpaid_amount, breakdown.paid_amount, breakdown.needs_review_amount) == (10, 20, 30)


def test_source_updated_on_round_trip(tracker):
    assert tracker.source_updated_on() is None
    tracker.record_source_updated_on(date(2026, 11, 12), at=_T0)
    tracker.record_source_updated_on(date(2026, 11, 18), at=_T0 + timedelta(hours=1))

    updated_on, observed_at = tracker.source_updated_on()
    assert updated_on == date(2026, 11, 18)
    assert observed_at == _T0 + timedelta(hours=1)


# ---- первичное заполнение из detected_fines ----


def test_seed_takes_only_receipts_listed_at_last_snapshot(tmp_path):
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
    repository = FineStatusRepository(db_path)
    tracker = FineStatusTracker(repository)

    assert tracker.seed_from_detected_fines() == 1
    assert tracker.seed_from_detected_fines() == 0  # идемпотентно

    statuses = tracker.statuses_by_protocol(_CAR)
    assert set(statuses) == {"CUR1"}
    assert statuses["CUR1"].amount == 60
    repository.close()
    fines.close()
    tasks.close()
