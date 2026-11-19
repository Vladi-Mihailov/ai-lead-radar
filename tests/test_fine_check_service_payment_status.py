"""FineCheckService <-> статусы оплаты: статусы меняются ТОЛЬКО после
успешной полной проверки (не после ошибки, не из кэша, не по одной
находке feed'а), неполный ответ не делает штраф исчезнувшим, 403/429
помечается provider_blocked, и одна и та же квитанция номера не порождает
второго уведомления оператору через другую задачу. Номера синтетические."""

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.payment_status import (
    PAYMENT_PAID,
    PRESENCE_GONE,
    PRESENCE_LISTED,
    PRESENCE_MISSING,
    FineStatusRepository,
    FineStatusTracker,
)
from reader.fines.provider import (
    FineProvider,
    FineProviderError,
    PoliceGeBlockedError,
    PoliceGeUnavailableError,
    ProviderSearchResult,
)
from reader.fines.task_repository import FineMonitoringTaskRepository

_CAR = "AA111BB"


async def _instant_sleep(_seconds: float) -> None:
    pass


def _record(protocol_no: str, amount: float = 50.0) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=_CAR, external_fine_id=protocol_no, penalty_date=date(2026, 11, 1),
        due_date=date(2026, 12, 31), delivered_status="Не вручено", fingerprint=f"fp-{protocol_no}",
        raw_data={"protocolNo": protocol_no}, amount=amount,
    )


class _Provider(FineProvider):
    def __init__(self):
        self.results: list = []  # ProviderSearchResult или исключение — по очереди
        self.refresh_flags: list[bool] = []
        self.available = True

    def is_available(self) -> bool:
        return self.available

    async def search_by_plate(self, plate):
        return (await self.search_by_plate_detailed(plate)).records

    async def search_by_plate_detailed(self, plate, *, refresh_page=False):
        self.refresh_flags.append(refresh_page)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class _Fixture:
    def __init__(self, tmp_path, *, min_gap=timedelta(0), dedupe=True):
        db_path = tmp_path / "users.db"
        self.tasks = FineMonitoringTaskRepository(db_path)
        self.fines = DetectedFineRepository(db_path)
        self.status_repository = FineStatusRepository(db_path)
        self.tracker = FineStatusTracker(self.status_repository, disappearance_min_gap=min_gap)
        self.provider = _Provider()
        self.service = FineCheckService(
            self.provider, self.tasks, self.fines, sleep=_instant_sleep, status_tracker=self.tracker,
            dedupe_known_receipts=dedupe,
        )

    def task(self):
        return self.tasks.create(
            car_number=_CAR, label=None, start_date=date(2026, 11, 1), end_date=date(2026, 12, 31),
            telegram_chat_id=-1, created_by_user_id=1,
        )

    def statuses(self):
        return self.tracker.statuses_by_protocol(_CAR)

    def close(self):
        self.status_repository.close()
        self.fines.close()
        self.tasks.close()


@pytest.fixture()
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


async def test_successful_check_lists_receipts_and_records_source_date(fx):
    task = fx.task()
    fx.provider.results = [ProviderSearchResult(records=[_record("GE1")], source_updated_on=date(2026, 11, 12))]

    result = await fx.service.check_task(task)

    assert result.status == "ok"
    assert fx.statuses()["GE1"].presence == PRESENCE_LISTED
    assert fx.tracker.source_updated_on()[0] == date(2026, 11, 12)


async def test_absence_in_complete_check_marks_missing(fx):
    task = fx.task()
    fx.provider.results = [ProviderSearchResult(records=[_record("GE1"), _record("GE2")])]
    await fx.service.check_task(task)

    fx.provider.results = [ProviderSearchResult(records=[_record("GE2")])]
    await fx.service.check_task(fx.tasks.get(task.id))

    assert fx.statuses()["GE1"].presence == PRESENCE_MISSING


async def test_incomplete_response_does_not_mark_missing(fx):
    task = fx.task()
    fx.provider.results = [ProviderSearchResult(records=[_record("GE1"), _record("GE2")])]
    await fx.service.check_task(task)

    fx.provider.results = [ProviderSearchResult(records=[_record("GE2")], reported_count=2)]
    await fx.service.check_task(fx.tasks.get(task.id))

    assert fx.statuses()["GE1"].presence == PRESENCE_LISTED


@pytest.mark.parametrize("error", [
    FineProviderError("boom"), PoliceGeUnavailableError("open"), PoliceGeBlockedError("403", status_code=403),
])
async def test_errors_never_touch_statuses(fx, error):
    task = fx.task()
    fx.provider.results = [ProviderSearchResult(records=[_record("GE1")])]
    await fx.service.check_task(task)

    fx.provider.results = [error]
    result = await fx.service.check_task(fx.tasks.get(task.id))

    assert result.status == "error"
    assert result.provider_blocked is isinstance(error, PoliceGeBlockedError)
    assert fx.statuses()["GE1"].presence == PRESENCE_LISTED


async def test_cached_read_never_touches_statuses(fx):
    task = fx.task()
    fx.provider.results = [ProviderSearchResult(records=[_record("GE1")])]
    await fx.service.check_task(task)
    fx.provider.available = False  # circuit breaker открыт -> показ из БД

    result = await fx.service.check_task_or_read_cached(fx.tasks.get(task.id))

    assert result.from_cache
    assert fx.statuses()["GE1"].presence == PRESENCE_LISTED


async def test_false_empty_is_confirmed_and_counts_once(tmp_path):
    fixture = _Fixture(tmp_path, min_gap=timedelta(hours=12))
    task = fixture.task()
    fixture.provider.results = [ProviderSearchResult(records=[_record("GE1")])]
    await fixture.service.check_task(task)

    # пустой ответ -> повторный запрос (false-zero guard) -> всё ещё пусто
    fixture.provider.results = [ProviderSearchResult(records=[]), ProviderSearchResult(records=[])]
    await fixture.service.check_task(fixture.tasks.get(task.id))

    assert fixture.provider.refresh_flags.count(False) == 3
    assert fixture.statuses()["GE1"].presence == PRESENCE_MISSING  # ещё не ⚪
    fixture.close()


async def test_feed_found_receipt_forces_confirmation_even_with_zero_snapshot(fx):
    """Снимок задачи 0, штраф найден feed'ом (снимок он не обновляет): пустой
    ответ всё равно перепроверяется, и неподтверждённым он не засчитывается."""
    task = fx.task()
    fx.provider.results = [ProviderSearchResult(records=[])]
    await fx.service.check_task(task)  # снимок = 0
    await fx.service.persist_feed_match(fx.tasks.get(task.id), _record("GE1"))
    fx.provider.refresh_flags.clear()

    # пустой ответ, а повторный запрос штраф находит — ложный ноль пойман
    fx.provider.results = [ProviderSearchResult(records=[]), ProviderSearchResult(records=[_record("GE1")])]
    await fx.service.check_task(fx.tasks.get(task.id))

    assert len(fx.provider.refresh_flags) == 2
    assert fx.statuses()["GE1"].presence == PRESENCE_LISTED


async def test_unverified_history_also_forces_confirmation(fx):
    task = fx.task()
    fx.provider.results = [ProviderSearchResult(records=[])]
    await fx.service.check_task(task)  # снимок 0
    row = fx.status_repository.insert_listed(
        car_number=_CAR, protocol_no="OLD1", amount=30, at=datetime(2026, 10, 1, 10, 0), source="check",
    )
    fx.status_repository._conn.execute("UPDATE georgia_fine_status SET presence = 'unverified' WHERE id = ?", (row.id,))
    fx.status_repository._conn.commit()
    fx.provider.refresh_flags.clear()

    fx.provider.results = [ProviderSearchResult(records=[]), ProviderSearchResult(records=[])]
    await fx.service.check_task(fx.tasks.get(task.id))

    assert len(fx.provider.refresh_flags) == 2


async def test_second_empty_answer_is_still_double_checked(tmp_path):
    fixture = _Fixture(tmp_path, min_gap=timedelta(0))
    task = fixture.task()
    fixture.provider.results = [ProviderSearchResult(records=[_record("GE1")])]
    await fixture.service.check_task(task)

    for _ in range(2):
        fixture.provider.refresh_flags.clear()
        fixture.provider.results = [ProviderSearchResult(records=[]), ProviderSearchResult(records=[])]
        await fixture.service.check_task(fixture.tasks.get(task.id))
        # снимок уже 0, но квитанция ещё не ⚪ — без второго запроса нельзя
        assert len(fixture.provider.refresh_flags) == 2

    assert fixture.statuses()["GE1"].presence == "gone"
    fixture.close()


async def test_feed_match_lists_receipt_without_touching_others(fx):
    task = fx.task()
    fx.provider.results = [ProviderSearchResult(records=[_record("GE1")])]
    await fx.service.check_task(task)

    await fx.service.persist_feed_match(fx.tasks.get(task.id), _record("GE2"))

    statuses = fx.statuses()
    assert statuses["GE1"].presence == PRESENCE_LISTED
    assert statuses["GE2"].presence == PRESENCE_LISTED


async def test_check_plate_for_tasks_passes_refresh_flag_and_tracks_once(fx):
    first, second = fx.task(), fx.task()
    fx.provider.results = [ProviderSearchResult(records=[_record("GE1")])]

    results = await fx.service.check_plate_for_tasks(_CAR, [first, second], refresh_page=True)

    assert {r.status for r in results.values()} == {"ok"}
    assert fx.provider.refresh_flags == [True]
    status = fx.statuses()["GE1"]
    assert [e["event"] for e in fx.tracker.events(status.id)] == ["listed"]


async def test_snapshot_only_tasks_get_total_but_no_rows(fx):
    main_task, other = fx.task(), fx.task()
    fx.provider.results = [ProviderSearchResult(records=[_record("GE1", 40)])]

    results = await fx.service.check_plate_for_tasks(_CAR, [main_task], snapshot_only_tasks=[other])

    assert list(results) == [main_task.id]
    assert fx.tasks.get(other.id).last_successful_total_amount == 40
    assert [fine.monitoring_task_id for fine in fx.fines.list_by_car_number(_CAR)] == [main_task.id]


async def test_tracker_failure_does_not_fail_the_check(fx, caplog):
    class _Broken:
        def record_full_check(self, *args, **kwargs):
            raise RuntimeError("db locked")

        def record_listed(self, *args, **kwargs):
            raise RuntimeError("db locked")

        def record_source_updated_on(self, *args, **kwargs):
            raise RuntimeError("db locked")

    service = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant_sleep, status_tracker=_Broken())
    fx.provider.results = [ProviderSearchResult(records=[_record("GE1")])]

    result = await service.check_task(fx.task())

    assert result.status == "ok"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings  # сбой статусов виден в журнале...
    assert all(_CAR not in r.getMessage() for r in warnings)  # ...но без номера


# ---- уже известная квитанция номера — без повторного уведомления оператору ----


async def test_receipt_already_notified_via_another_task_is_born_notified(fx):
    first = fx.task()
    fx.provider.results = [ProviderSearchResult(records=[_record("GE1")])]
    await fx.service.check_task(first)
    for fine in fx.fines.list_pending_notifications():
        fx.fines.mark_notification_sent(fine.id)  # оператор уже уведомлён

    second = fx.task()
    result = await fx.service.check_task(second)

    assert len(result.new_fines) == 1  # строка для второй задачи создана
    assert fx.fines.list_pending_notifications() == []


async def test_receipt_still_pending_for_another_task_keeps_per_task_semantics(fx):
    first = fx.task()
    fx.provider.results = [ProviderSearchResult(records=[_record("GE1")])]
    await fx.service.check_task(first)

    second = fx.task()
    await fx.service.check_task(second)

    pending = fx.fines.list_pending_notifications()
    assert sorted(fine.monitoring_task_id for fine in pending) == sorted([first.id, second.id])


async def test_operator_process_without_flag_keeps_old_behaviour(tmp_path):
    fixture = _Fixture(tmp_path, dedupe=False)
    first = fixture.task()
    fixture.provider.results = [ProviderSearchResult(records=[_record("GE1")])]
    await fixture.service.check_task(first)
    for fine in fixture.fines.list_pending_notifications():
        fixture.fines.mark_notification_sent(fine.id)

    await fixture.service.check_task(fixture.task())

    assert len(fixture.fines.list_pending_notifications()) == 1
    fixture.close()


async def test_new_receipt_for_two_tasks_keeps_per_task_notifications(fx):
    first, second = fx.task(), fx.task()
    fx.provider.results = [ProviderSearchResult(records=[_record("GE7")])]

    await fx.service.check_plate_for_tasks(_CAR, [first, second])

    assert len(fx.fines.list_by_car_number(_CAR)) == 2
    assert len(fx.fines.list_pending_notifications()) == 2


async def test_recheck_of_known_receipts_creates_no_new_notifications(fx):
    task = fx.task()
    fx.provider.results = [ProviderSearchResult(records=[_record("GE1"), _record("GE2")])]
    await fx.service.check_task(task)
    for fine in fx.fines.list_pending_notifications():
        fx.fines.mark_notification_sent(fine.id)

    for _ in range(3):
        result = await fx.service.check_task(fx.tasks.get(task.id))
        assert result.new_fines == []

    assert fx.fines.list_pending_notifications() == []


# ---- 🟢 оплачено: повторное обнаружение — не новый штраф, проверки идут до ⚪ ----


def _changed_card(protocol_no: str, amount: float = 50.0) -> ParsedFineRecord:
    """Та же квитанция, но её карточка на police.ge изменилась — другой fingerprint."""
    return ParsedFineRecord(
        car_number=_CAR, external_fine_id=protocol_no, penalty_date=date(2026, 11, 1),
        due_date=date(2026, 12, 31), delivered_status="Вручено", fingerprint=f"fp-{protocol_no}-changed",
        raw_data={"protocolNo": protocol_no}, amount=amount,
    )


async def _listed_and_paid(fx):
    task = fx.task()
    fx.provider.results = [ProviderSearchResult(records=[_record("GE1")])]
    await fx.service.check_task(task)
    fx.tracker.mark_paid(fx.statuses()["GE1"].id, actor_user_id=1, note="TBC 1")
    return fx.tasks.get(task.id)


def _changed_rows(fx):
    return [row for row in fx.fines.list_by_car_number(_CAR) if row.fingerprint == "fp-GE1-changed"]


async def test_paid_receipt_found_again_with_a_changed_card_is_not_a_new_fine(fx):
    task = await _listed_and_paid(fx)
    fx.provider.results = [ProviderSearchResult(records=[_changed_card("GE1")])]

    result = await fx.service.check_task(task)

    assert result.new_fines == []
    assert [fine.external_fine_id for fine in result.current_fines] == ["GE1"]
    rows = _changed_rows(fx)
    assert len(rows) == 1 and rows[0].notification_sent_at is not None  # оператору не уйдёт
    assert fx.statuses()["GE1"].payment_status == PAYMENT_PAID


async def test_feed_match_of_a_paid_receipt_is_not_a_new_fine(fx):
    task = await _listed_and_paid(fx)

    result = await fx.service.persist_feed_match(task, _changed_card("GE1"))

    assert result.new_fines == []
    rows = _changed_rows(fx)
    assert len(rows) == 1 and rows[0].notification_sent_at is not None
    assert fx.statuses()["GE1"].payment_status == PAYMENT_PAID


async def test_paid_receipt_keeps_being_checked_until_gone_and_stays_paid_when_back(fx):
    """🟢 остаётся в истории и в перепроверке, пока police.ge его показывает;
    исчез дважды — ⚪ (и ещё 3 дня в follow-up); вернулся — снова в базе,
    по-прежнему 🟢 и не новый штраф."""
    task = await _listed_and_paid(fx)
    assert _CAR in fx.tracker.listed_car_numbers()

    fx.provider.results = [ProviderSearchResult(records=[])]
    await fx.service.check_task(task)
    assert fx.statuses()["GE1"].presence == PRESENCE_MISSING
    assert _CAR in fx.tracker.listed_car_numbers()
    await fx.service.check_task(fx.tasks.get(task.id))
    assert fx.statuses()["GE1"].presence == PRESENCE_GONE
    assert _CAR not in fx.tracker.listed_car_numbers() and _CAR in fx.tracker.recently_gone_car_numbers()

    fx.provider.results = [ProviderSearchResult(records=[_changed_card("GE1")])]
    result = await fx.service.check_task(fx.tasks.get(task.id))

    status = fx.statuses()["GE1"]
    assert status.presence == PRESENCE_LISTED and status.payment_status == PAYMENT_PAID
    assert result.new_fines == []
    assert all(row.notification_sent_at is not None for row in _changed_rows(fx))


async def test_unreadable_payment_status_still_announces_a_new_receipt(fx, caplog):
    task = await _listed_and_paid(fx)

    def unreadable(*_args):
        raise RuntimeError("db locked")

    fx.tracker.is_paid = unreadable
    fx.provider.results = [ProviderSearchResult(records=[_record("GE1"), _record("GE2")])]

    result = await fx.service.check_task(task)

    assert [fine.external_fine_id for fine in result.new_fines] == ["GE2"]
    assert all(_CAR not in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


async def test_a_new_receipt_next_to_a_paid_one_is_a_new_fine(fx):
    """Номер с 🟢 квитанцией: новая квитанция — по-прежнему новый штраф
    (ждёт уведомления), а 🟢 с изменившейся карточкой — нет."""
    task = await _listed_and_paid(fx)
    fx.provider.results = [ProviderSearchResult(records=[_changed_card("GE1"), _record("GE2")])]

    result = await fx.service.check_task(task)

    assert [fine.external_fine_id for fine in result.new_fines] == ["GE2"]
    new_row = [row for row in fx.fines.list_by_car_number(_CAR) if row.external_fine_id == "GE2"]
    assert len(new_row) == 1 and new_row[0].notification_sent_at is None
    assert all(row.notification_sent_at is not None for row in _changed_rows(fx))
