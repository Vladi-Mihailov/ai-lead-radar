"""Ежедневная вечерняя перепроверка (reader/public_bot/daily_recheck.py):
окно 23:00–23:45 Asia/Tbilisi, последовательные пачки с паузами, остановка
на 403/429/открытом circuit breaker/ошибках подряд, перенос по окончанию
окна, продолжение после рестарта, защита от второго запуска того же дня,
общий BatchCheckGate с ручным "🔄 Проверить авто", отсутствие повторных
уведомлений об известных штрафах и ⚪ после двух вечерних проходов.
Часы и паузы — фейковые; номера синтетические."""

import asyncio
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.fines.batch_gate import BatchCheckGate
from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.payment_status import PRESENCE_GONE, PRESENCE_MISSING, FineStatusRepository, FineStatusTracker
from reader.fines.provider import (
    FineProvider,
    FineProviderError,
    PoliceGeBlockedError,
    PoliceGeUnavailableError,
    ProviderSearchResult,
)
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot import daily_recheck as daily_recheck_module
from reader.public_bot.daily_recheck import (
    ITEM_OK,
    RUN_COMPLETED,
    RUN_POSTPONED,
    RUN_RUNNING,
    RUN_STOPPED,
    STOP_INTERRUPTED,
    DailyRecheckRepository,
    DailyRecheckService,
)
from reader.public_bot.debt_refresh_service import DebtRefreshService, RefreshAlreadyInProgressError

_TBILISI = ZoneInfo("Asia/Tbilisi")
# 23:00:30 по Тбилиси (UTC+4)
_START = datetime(2026, 11, 18, 19, 0, 30, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, now: datetime):
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def naive(self) -> datetime:
        return self.now.astimezone(timezone.utc).replace(tzinfo=None)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


async def _instant(_seconds: float) -> None:
    pass


def _record(car: str, protocol_no: str, amount: float = 50.0) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car, external_fine_id=protocol_no, penalty_date=date(2026, 11, 1), due_date=date(2026, 12, 31),
        delivered_status="Не вручено", fingerprint=f"fp-{car}-{protocol_no}", raw_data={"protocolNo": protocol_no},
        amount=amount,
    )


class _Provider(FineProvider):
    def __init__(self):
        self.records: dict[str, list[ParsedFineRecord]] = {}
        self.errors: dict[str, Exception] = {}
        self.calls: list[tuple[str, bool]] = []

    async def search_by_plate(self, plate):
        return (await self.search_by_plate_detailed(plate)).records

    async def search_by_plate_detailed(self, plate, *, refresh_page=False):
        self.calls.append((plate, refresh_page))
        if plate in self.errors:
            raise self.errors[plate]
        return ProviderSearchResult(records=list(self.records.get(plate, [])))


class _Fixture:
    def __init__(self, tmp_path, *, start=_START, **service_kwargs):
        self.db_path = tmp_path / "users.db"
        self.clock = _Clock(start)
        self.tasks = FineMonitoringTaskRepository(self.db_path)
        self.fines = DetectedFineRepository(self.db_path)
        self.status_repository = FineStatusRepository(self.db_path)
        self.tracker = FineStatusTracker(
            self.status_repository, disappearance_min_gap=timedelta(hours=12), clock=self.clock.naive,
        )
        self.provider = _Provider()
        self.provider_calls_per_plate = {}
        self.check_service = FineCheckService(
            self.provider, self.tasks, self.fines, sleep=_instant, status_tracker=self.tracker,
            dedupe_known_receipts=True,
        )
        self.gate = BatchCheckGate()
        self.repository = DailyRecheckRepository(self.db_path)
        self.service_kwargs = service_kwargs
        self.service = self.make_service()

    def make_service(self, repository=None) -> DailyRecheckService:
        kwargs = dict(
            gate=self.gate, tz=_TBILISI, start_time=time(23, 0), cutoff_time=time(23, 45),
            inter_car_delay_seconds=8, batch_size=2, batch_pause_seconds=60, max_plates=50,
            max_consecutive_errors=2, busy_wait_seconds=30, sleep=self.clock.sleep, clock=self.clock,
        )
        kwargs.update(self.service_kwargs)
        return DailyRecheckService(
            repository or self.repository, self.tasks, self.check_service, self.tracker, **kwargs,
        )

    def car_with_fines(self, car: str, *protocols: str, checked_at: str | None = None, status: str = "active"):
        """Задача номера с известными штрафами на последней проверке."""
        task = self.tasks.create(
            car_number=car, label=None, start_date=date(2026, 11, 1), end_date=date(2026, 12, 31),
            telegram_chat_id=-1, created_by_user_id=1,
        )
        records = [_record(car, p) for p in protocols]
        self.provider.records[car] = records
        for record in records:
            fine = self.fines.create(
                monitoring_task_id=task.id, car_number=car, external_fine_id=record.external_fine_id,
                fingerprint=record.fingerprint, penalty_date=None, due_date=None, delivered_status=None,
                raw_data="{}", amount=record.amount,
            )
            self.fines.mark_notification_sent(fine.id)
        self.tasks.record_successful_check(task.id, total_amount=sum(r.amount for r in records))
        if checked_at is not None:
            self.tasks._conn.execute(
                "UPDATE fine_monitoring_tasks SET last_successful_checked_at = ? WHERE id = ?", (checked_at, task.id),
            )
            self.tasks._conn.commit()
        if status != "active":
            self.tasks._conn.execute("UPDATE fine_monitoring_tasks SET status = ? WHERE id = ?", (status, task.id))
            self.tasks._conn.commit()
        self.tracker.seed_from_detected_fines()
        return task

    def close(self):
        self.repository.close()
        self.status_repository.close()
        self.fines.close()
        self.tasks.close()


@pytest.fixture()
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


def _plates_checked(provider):
    return [plate for plate, _ in provider.calls]


async def test_outside_window_does_nothing(tmp_path):
    fixture = _Fixture(tmp_path, start=datetime(2026, 11, 18, 18, 30, tzinfo=timezone.utc))  # 22:30 Тбилиси
    fixture.car_with_fines("AA111BB", "GE1")

    assert await fixture.service.tick() is None
    assert fixture.repository.latest_run() is None
    assert fixture.provider.calls == []
    fixture.close()


async def test_full_pass_is_sequential_paced_and_completes(fx):
    for index, car in enumerate(["AA111BB", "BB222CC", "CC333DD", "DD444EE", "EE555FF"]):
        fx.car_with_fines(car, f"GE{index}", checked_at=f"2026-11-1{index} 10:00:00")

    run = await fx.service.tick()

    assert run.status == RUN_COMPLETED and (run.total, run.checked, run.failed) == (5, 5, 0)
    # самые давно проверенные — первыми
    assert _plates_checked(fx.provider) == ["AA111BB", "BB222CC", "CC333DD", "DD444EE", "EE555FF"]
    # страница police.ge перечитывается только в первом запросе прохода
    assert [flag for _, flag in fx.provider.calls] == [True, False, False, False, False]
    # пачки по 2: пауза 8 с внутри пачки, 60 с между пачками
    assert fx.clock.sleeps == [8, 60, 8, 60]


async def test_second_tick_same_day_does_not_start_again(fx):
    fx.car_with_fines("AA111BB", "GE1")
    await fx.service.tick()
    calls = len(fx.provider.calls)

    fx.clock.now += timedelta(minutes=10)
    assert await fx.service.tick() is None
    assert len(fx.provider.calls) == calls


async def test_concurrent_ticks_run_once(fx):
    fx.car_with_fines("AA111BB", "GE1")
    fx.car_with_fines("BB222CC", "GE2")

    results = await asyncio.gather(fx.service.tick(), fx.service.tick())

    assert sum(result is not None for result in results) == 1
    assert sorted(_plates_checked(fx.provider)) == ["AA111BB", "BB222CC"]


@pytest.mark.parametrize("error, reason", [
    (PoliceGeBlockedError("403", status_code=403), "blocked"),
    (PoliceGeUnavailableError("circuit open"), "unavailable"),
])
async def test_stops_immediately_on_blocking_signals(fx, error, reason):
    fx.car_with_fines("AA111BB", "GE1", checked_at="2026-11-10 10:00:00")
    fx.car_with_fines("BB222CC", "GE2", checked_at="2026-11-11 10:00:00")
    fx.car_with_fines("CC333DD", "GE3", checked_at="2026-11-12 10:00:00")
    fx.provider.errors["BB222CC"] = error

    run = await fx.service.tick()

    assert run.status == RUN_STOPPED and run.stop_reason == reason
    assert _plates_checked(fx.provider) == ["AA111BB", "BB222CC"]
    assert fx.repository.pending_plates(run.run_date) == ["CC333DD"]
    assert fx.gate.holder is None


async def test_stops_after_consecutive_errors_but_not_single_error(fx):
    for index, car in enumerate(["AA111BB", "BB222CC", "CC333DD", "DD444EE", "EE555FF"]):
        fx.car_with_fines(car, f"GE{index}", checked_at=f"2026-11-1{index} 10:00:00")
    fx.provider.errors["AA111BB"] = FineProviderError("timeout")
    fx.provider.errors["CC333DD"] = FineProviderError("timeout")
    fx.provider.errors["DD444EE"] = FineProviderError("timeout")

    run = await fx.service.tick()

    assert run.status == RUN_STOPPED and run.stop_reason == "errors"
    assert (run.checked, run.failed) == (1, 3)
    assert _plates_checked(fx.provider) == ["AA111BB", "BB222CC", "CC333DD", "DD444EE"]


async def test_errors_do_not_change_statuses(fx):
    fx.car_with_fines("AA111BB", "GE1")
    fx.provider.errors["AA111BB"] = FineProviderError("timeout")

    await fx.service.tick()

    status = fx.tracker.statuses_by_protocol("AA111BB")["GE1"]
    assert status.presence == "listed"


async def test_postponed_when_window_ends(tmp_path):
    fixture = _Fixture(tmp_path, start=datetime(2026, 11, 18, 19, 44, 0, tzinfo=timezone.utc))  # 23:44
    fixture.car_with_fines("AA111BB", "GE1", checked_at="2026-11-10 10:00:00")
    fixture.car_with_fines("BB222CC", "GE2", checked_at="2026-11-11 10:00:00")
    fixture.service_kwargs["inter_car_delay_seconds"] = 90
    service = fixture.make_service()

    run = await service.tick()

    assert run.status == RUN_POSTPONED and run.stop_reason == "cutoff"
    assert _plates_checked(fixture.provider) == ["AA111BB"]
    assert fixture.repository.pending_plates(run.run_date) == ["BB222CC"]
    fixture.close()


async def test_resumes_pending_plates_after_interruption(fx):
    fx.car_with_fines("AA111BB", "GE1", checked_at="2026-11-10 10:00:00")
    fx.car_with_fines("BB222CC", "GE2", checked_at="2026-11-11 10:00:00")
    run_date = date(2026, 11, 18)
    # прошлый процесс начал проход, проверил первый номер и упал
    started = fx.clock.now - timedelta(minutes=20)
    fx.repository.create_run(run_date, ["AA111BB", "BB222CC"], now=started)
    fx.repository.mark_item(run_date, "AA111BB", state=ITEM_OK, now=started)

    run = await fx.make_service().tick()

    assert run.status == RUN_COMPLETED and run.checked == 2
    assert _plates_checked(fx.provider) == ["BB222CC"]


async def test_fresh_running_run_is_not_taken_over(fx):
    fx.car_with_fines("AA111BB", "GE1")
    run_date = date(2026, 11, 18)
    fx.repository.create_run(run_date, ["AA111BB"], now=fx.clock.now - timedelta(seconds=30))

    assert await fx.make_service().tick() is None
    assert fx.provider.calls == []
    assert fx.repository.get_run(run_date).status == RUN_RUNNING


async def test_waits_for_manual_bulk_check_and_postpones_if_window_ends(fx):
    fx.car_with_fines("AA111BB", "GE1")
    assert fx.gate.try_acquire("debt_refresh")

    run = await fx.service.tick()

    assert run.status == RUN_POSTPONED and run.stop_reason == "busy"
    assert fx.provider.calls == []
    assert set(fx.clock.sleeps) == {30}


async def test_manual_refresh_is_refused_while_recheck_runs(fx):
    fx.car_with_fines("AA111BB", "GE1")
    refresh = DebtRefreshService(fx.tasks, fx.check_service, sleep=_instant, gate=fx.gate)

    assert fx.gate.try_acquire("daily_recheck")
    with pytest.raises(RefreshAlreadyInProgressError):
        await refresh.refresh()
    assert fx.provider.calls == []
    fx.gate.release("daily_recheck")

    outcome = await refresh.refresh()
    assert outcome.checked == 1
    assert fx.gate.holder is None


async def test_known_fines_create_no_notifications_new_fine_notifies_once(fx):
    fx.car_with_fines("AA111BB", "GE1")
    assert fx.fines.list_pending_notifications() == []

    fx.provider.records["AA111BB"] = [_record("AA111BB", "GE1"), _record("AA111BB", "GE9")]
    await fx.service.tick()

    pending = fx.fines.list_pending_notifications()
    assert [fine.external_fine_id for fine in pending] == ["GE9"]


async def test_paid_fine_disappears_over_two_evenings(fx):
    fx.car_with_fines("AA111BB", "GE1", "GE2")
    status_id = fx.tracker.statuses_by_protocol("AA111BB")["GE1"].id
    fx.tracker.mark_paid(status_id, actor_user_id=1, note="TBC 1")
    fx.provider.records["AA111BB"] = [_record("AA111BB", "GE2")]  # police.ge убрал оплаченный

    await fx.service.tick()
    assert fx.tracker.get(status_id).presence == PRESENCE_MISSING

    fx.clock.now += timedelta(days=1)
    await fx.make_service().tick()
    status = fx.tracker.get(status_id)
    assert status.presence == PRESENCE_GONE and status.payment_status == "paid"


async def test_all_fines_paid_empty_answers_are_confirmed_and_followed_up(fx):
    fx.car_with_fines("AA111BB", "GE1")
    status_id = fx.tracker.statuses_by_protocol("AA111BB")["GE1"].id
    fx.tracker.mark_paid(status_id, actor_user_id=1, note="BOG 7")
    fx.provider.records["AA111BB"] = []  # police.ge убрал единственный штраф

    await fx.service.tick()
    # каждый пустой ответ подтверждён вторым запросом
    assert _plates_checked(fx.provider) == ["AA111BB", "AA111BB"]
    assert fx.tracker.get(status_id).presence == PRESENCE_MISSING

    fx.clock.now += timedelta(days=1)
    await fx.make_service().tick()
    assert fx.tracker.get(status_id).presence == PRESENCE_GONE

    # номер ещё несколько дней в перепроверке (ложное ⚪ сменилось бы на "снова найден")
    fx.clock.now += timedelta(days=1)
    assert "AA111BB" in fx.make_service().select_plates()
    fx.clock.now += timedelta(days=5)
    assert "AA111BB" not in fx.make_service().select_plates()


async def test_false_gone_returns_when_found_again(fx):
    fx.car_with_fines("AA111BB", "GE1")
    status_id = fx.tracker.statuses_by_protocol("AA111BB")["GE1"].id
    fx.provider.records["AA111BB"] = []
    await fx.service.tick()
    fx.clock.now += timedelta(days=1)
    await fx.make_service().tick()
    assert fx.tracker.get(status_id).presence == PRESENCE_GONE

    fx.provider.records["AA111BB"] = [_record("AA111BB", "GE1")]  # снова в базе полиции
    fx.clock.now += timedelta(days=1)
    await fx.make_service().tick()

    assert fx.tracker.get(status_id).presence == "listed"
    assert fx.fines.list_pending_notifications() == []  # не "новый штраф"


async def test_yesterdays_failures_go_to_the_end_of_the_queue(fx):
    fx.car_with_fines("AA111BB", "GE1", checked_at="2026-11-10 10:00:00")
    fx.car_with_fines("BB222CC", "GE2", checked_at="2026-11-11 10:00:00")
    fx.car_with_fines("CC333DD", "GE3", checked_at="2026-11-12 10:00:00")
    fx.provider.errors["AA111BB"] = FineProviderError("timeout")
    fx.provider.errors["BB222CC"] = FineProviderError("timeout")

    first = await fx.service.tick()
    assert first.status == RUN_STOPPED and first.stop_reason == "errors"
    assert "CC333DD" not in _plates_checked(fx.provider)

    fx.provider.calls.clear()
    fx.clock.now += timedelta(days=1)
    second = await fx.make_service().tick()

    # вчерашние "вечно падающие" номера — в конце, остальные проверены
    assert _plates_checked(fx.provider)[0] == "CC333DD"
    assert second.checked == 1


async def test_inactive_task_snapshot_is_refreshed_so_gone_car_leaves_statistics(fx):
    task = fx.car_with_fines("AA111BB", "GE1", status="completed")
    active = fx.tasks.create(
        car_number="AA111BB", label=None, start_date=date(2026, 11, 1), end_date=date(2026, 12, 31),
        telegram_chat_id=-1, created_by_user_id=1,
    )

    assert {t.id for t in fx.service._tasks_for_plate("AA111BB")} == {task.id, active.id}


async def test_stale_running_run_is_closed_outside_window(tmp_path):
    fixture = _Fixture(tmp_path, start=datetime(2026, 11, 18, 20, 30, tzinfo=timezone.utc))  # 00:30 Тбилиси
    fixture.repository.create_run(date(2026, 11, 18), ["AA111BB"], now=datetime(2026, 11, 18, 19, 40, tzinfo=timezone.utc))

    assert await fixture.service.tick() is None

    run = fixture.repository.get_run(date(2026, 11, 18))
    assert run.status == RUN_POSTPONED and run.stop_reason == STOP_INTERRUPTED
    fixture.close()


async def test_selection_includes_feed_found_cars_and_skips_suppressed(fx, monkeypatch):
    fx.car_with_fines("AA111BB", "GE1")
    hidden = fx.car_with_fines("ZZ999ZZ", "GE5")
    # номер с найденным feed'ом штрафом, но без положительного снимка долга
    feed_task = fx.tasks.create(
        car_number="FF777GG", label=None, start_date=date(2026, 11, 1), end_date=date(2026, 12, 31),
        telegram_chat_id=-1, created_by_user_id=1,
    )
    await fx.check_service.persist_feed_match(feed_task, _record("FF777GG", "GE7"))
    monkeypatch.setattr(daily_recheck_module, "is_fine_output_suppressed", lambda car: car == hidden.car_number)

    plates = fx.service.select_plates()

    assert set(plates) == {"AA111BB", "FF777GG"}


async def test_max_plates_cap(tmp_path):
    fixture = _Fixture(tmp_path, max_plates=2)
    for index, car in enumerate(["AA111BB", "BB222CC", "CC333DD"]):
        fixture.car_with_fines(car, f"GE{index}", checked_at=f"2026-11-1{index} 10:00:00")

    assert fixture.service.select_plates() == ["AA111BB", "BB222CC"]
    fixture.close()


async def test_inactive_car_is_checked_through_its_freshest_task(fx):
    task = fx.car_with_fines("AA111BB", "GE1", status="completed")

    tasks = fx.service._tasks_for_plate("AA111BB")

    assert [t.id for t in tasks] == [task.id]


async def test_invalid_window_is_rejected(fx):
    with pytest.raises(ValueError):
        DailyRecheckService(
            fx.repository, fx.tasks, fx.check_service, fx.tracker, gate=fx.gate, tz=_TBILISI,
            start_time=time(23, 0), cutoff_time=time(22, 0),
        )
