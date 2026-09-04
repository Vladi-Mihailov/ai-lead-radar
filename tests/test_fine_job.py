"""
Тесты FineJob — получает активные задачи, для каждой (по датам) пропускает,
проверяет через FineCheckService или завершает, затем отдельным шагом
уведомляет обо всех штрафах, ещё не доставленных оператору
(notification_sent_at IS NULL), и отмечает доставленные.

FineMonitoringTaskRepository/DetectedFineRepository — настоящие (SQLite,
tmp_path), FineCheckService — тоже настоящий (как в test_fine_check_service.py),
с фейковым FineProvider вместо сети. NotificationService — фейковый, с
управляемым исходом доставки, чтобы явно проверить retry-механику.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import NewFineEvent, ParsedFineRecord  # noqa: E402
from reader.fines.notification_coordinator import FineNotificationCoordinator  # noqa: E402
from reader.fines.provider import FineProvider  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.jobs.fine_job import FineJob  # noqa: E402
from reader.notifications.base import NotificationResult, NotificationService  # noqa: E402

_CHAT_ID = -100999
_USER_ID = 111
_TBILISI = ZoneInfo("Asia/Tbilisi")
_RUN_TIMES = [dt_time(9, 0), dt_time(15, 0), dt_time(21, 0)]
# Момент внутри периода 2026-08-01..2026-08-31, используемый по умолчанию в
# тестах run(), которым конкретная дата не важна — чтобы не зависеть от
# реального "сегодня" на машине, где выполняются тесты.
_MID_PERIOD = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)


class _FakeProvider(FineProvider):
    def __init__(self, records_by_car: dict[str, list[ParsedFineRecord]] | None = None):
        self._records_by_car = records_by_car or {}
        self.requested_plates: list[str] = []

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        self.requested_plates.append(plate)
        return self._records_by_car.get(plate, [])


class _FakeNotificationService(NotificationService):
    """deliver_predicate решает, какие detected_fine_id считать доставленными
    в конкретном вызове notify() — по умолчанию доставляется всё (успех)."""

    def __init__(self, deliver_predicate=None):
        self._deliver_predicate = deliver_predicate or (lambda event: True)
        self.notify_calls: list[list[NewFineEvent]] = []

    async def notify(self, events: list[NewFineEvent]) -> NotificationResult:
        self.notify_calls.append(events)
        delivered = [e.detected_fine_id for e in events if self._deliver_predicate(e)]
        failed = [e.detected_fine_id for e in events if not self._deliver_predicate(e)]
        return NotificationResult(delivered_event_ids=delivered, failed_event_ids=failed)


def _record(
    car_number="B957MA09",
    external_fine_id="AB123456",
    fingerprint="fp-1",
    penalty_date=date(2026, 8, 6),
    due_date=date(2026, 8, 20),
    delivered_status="Не вручено",
) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number,
        external_fine_id=external_fine_id,
        penalty_date=penalty_date,
        due_date=due_date,
        delivered_status=delivered_status,
        fingerprint=fingerprint,
        raw_data={"protocolNo": external_fine_id},
    )


def _make_task_repo(tmp_path) -> FineMonitoringTaskRepository:
    return FineMonitoringTaskRepository(tmp_path / "users.db")


def _make_fine_repo(tmp_path) -> DetectedFineRepository:
    return DetectedFineRepository(tmp_path / "users.db")


def _make_job(task_repo, fine_repo, provider, notification_service) -> FineJob:
    check_service = FineCheckService(provider, task_repo, fine_repo)
    coordinator = FineNotificationCoordinator(fine_repo, task_repo, notification_service)
    return FineJob(
        task_repository=task_repo,
        check_service=check_service,
        notification_coordinator=coordinator,
        run_times=_RUN_TIMES,
        tz=_TBILISI,
    )


def _make_job_with_archive(
    task_repo, fine_repo, provider, notification_service,
    *, archive_check_hour=4, archive_interval_days=30,
) -> FineJob:
    """Как _make_job(), но с включённым архивным режимом (см.
    reader/jobs/archive_fine_job.py) — отдельный helper, а не изменение
    _make_job()/его вызовов, чтобы все существующие тесты продолжали
    проверять поведение FineJob БЕЗ архивного режима бит в бит как раньше."""
    check_service = FineCheckService(provider, task_repo, fine_repo)
    coordinator = FineNotificationCoordinator(fine_repo, task_repo, notification_service)
    return FineJob(
        task_repository=task_repo,
        check_service=check_service,
        notification_coordinator=coordinator,
        run_times=_RUN_TIMES,
        tz=_TBILISI,
        archive_check_enabled=True,
        archive_check_hour=archive_check_hour,
        archive_interval_days=archive_interval_days,
    )


# ---- should_run ----


async def test_should_run_matches_configured_time_in_tbilisi_timezone():
    job = FineJob(
        task_repository=None,
        check_service=None,
        notification_coordinator=None,
        run_times=_RUN_TIMES,
        tz=_TBILISI,
    )

    # 09:00 в Тбилиси (UTC+4) == 05:00 UTC
    nine_am_tbilisi_utc = datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc)
    assert await job.should_run(nine_am_tbilisi_utc) is True


async def test_should_run_is_false_outside_configured_times():
    job = FineJob(
        task_repository=None,
        check_service=None,
        notification_coordinator=None,
        run_times=_RUN_TIMES,
        tz=_TBILISI,
    )

    nine_oh_five_tbilisi_utc = datetime(2026, 8, 3, 5, 5, tzinfo=timezone.utc)
    assert await job.should_run(nine_oh_five_tbilisi_utc) is False


async def test_should_run_does_not_fire_twice_for_the_same_slot():
    job = FineJob(
        task_repository=None,
        check_service=None,
        notification_coordinator=None,
        run_times=_RUN_TIMES,
        tz=_TBILISI,
    )

    nine_am = datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc)
    assert await job.should_run(nine_am) is True
    # Тот же слот (тот же час/минута того же дня) — уже отработан.
    assert await job.should_run(nine_am) is False


async def test_should_run_fires_again_for_next_configured_slot():
    job = FineJob(
        task_repository=None,
        check_service=None,
        notification_coordinator=None,
        run_times=_RUN_TIMES,
        tz=_TBILISI,
    )

    nine_am = datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc)
    three_pm = datetime(2026, 8, 3, 11, 0, tzinfo=timezone.utc)  # 15:00 Тбилиси

    assert await job.should_run(nine_am) is True
    assert await job.should_run(three_pm) is True


async def test_should_run_fires_again_after_restart_within_same_slot():
    # Рестарт процесса == новый экземпляр FineJob (_last_run_slot=None).
    # Если рестарт пришёлся ровно на минуту одного из run_times — job не
    # "помнит" о работе предыдущего инстанса и запустится снова.
    nine_am = datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc)

    first_instance = FineJob(
        task_repository=None, check_service=None,
        notification_coordinator=None, run_times=_RUN_TIMES, tz=_TBILISI,
    )
    assert await first_instance.should_run(nine_am) is True

    second_instance = FineJob(
        task_repository=None, check_service=None,
        notification_coordinator=None, run_times=_RUN_TIMES, tz=_TBILISI,
    )
    assert await second_instance.should_run(nine_am) is True


async def test_should_run_skips_permanently_if_matching_minute_was_missed():
    # Процесс не опрашивал should_run() ровно в нужную минуту (был
    # выключен/завис) — запуск просто пропускается, никакого автоматического
    # докатывания на следующем опросе нет.
    job = FineJob(
        task_repository=None, check_service=None,
        notification_coordinator=None, run_times=_RUN_TIMES, tz=_TBILISI,
    )

    nine_oh_one = datetime(2026, 8, 3, 5, 1, tzinfo=timezone.utc)  # 09:01 Тбилиси
    assert await job.should_run(nine_oh_one) is False

    nine_oh_two = datetime(2026, 8, 3, 5, 2, tzinfo=timezone.utc)  # 09:02 Тбилиси
    assert await job.should_run(nine_oh_two) is False


# ---- run(): диспетчеризация активных задач ----


async def test_fine_job_checks_multiple_tasks(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        task_repo.create(
            car_number="BB002BB", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        provider = _FakeProvider()
        job = _make_job(task_repo, fine_repo, provider, _FakeNotificationService())

        await job.run(_MID_PERIOD)

        assert sorted(provider.requested_plates) == ["AA001AA", "BB002BB"]
    finally:
        task_repo.close()
        fine_repo.close()


async def test_task_before_start_date_is_not_checked(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 10), end_date=date(2026, 8, 20),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        provider = _FakeProvider()
        job = _make_job(task_repo, fine_repo, provider, _FakeNotificationService())

        before_start = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
        await job.run(before_start)

        assert provider.requested_plates == []
        assert task_repo.get(task.id).status == "active"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_task_on_start_date_is_checked(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 10), end_date=date(2026, 8, 20),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        provider = _FakeProvider()
        job = _make_job(task_repo, fine_repo, provider, _FakeNotificationService())

        exactly_on_start = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
        await job.run(exactly_on_start)

        assert provider.requested_plates == ["AA001AA"]
    finally:
        task_repo.close()
        fine_repo.close()


async def test_task_between_dates_is_checked(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 10), end_date=date(2026, 8, 20),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        provider = _FakeProvider()
        job = _make_job(task_repo, fine_repo, provider, _FakeNotificationService())

        # today = 2026-08-15 (_MID_PERIOD) — строго между 10 и 20 августа.
        await job.run(_MID_PERIOD)

        assert provider.requested_plates == ["AA001AA"]
    finally:
        task_repo.close()
        fine_repo.close()


async def test_task_on_end_date_is_checked(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 10), end_date=date(2026, 8, 20),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        provider = _FakeProvider()
        job = _make_job(task_repo, fine_repo, provider, _FakeNotificationService())

        exactly_on_end = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
        await job.run(exactly_on_end)

        assert provider.requested_plates == ["AA001AA"]
        # Проверка была — задача остаётся active, ещё не завершена.
        assert task_repo.get(task.id).status == "active"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_task_after_end_date_is_completed_and_not_checked(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 10), end_date=date(2026, 8, 20),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        provider = _FakeProvider()
        job = _make_job(task_repo, fine_repo, provider, _FakeNotificationService())

        after_end = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)
        await job.run(after_end)

        assert provider.requested_plates == []
        assert task_repo.get(task.id).status == "completed"
    finally:
        task_repo.close()
        fine_repo.close()


# ---- run(): архивный режим (см. reader/jobs/archive_fine_job.py) ----


async def test_completion_does_not_enable_archive_mode_by_default(tmp_path):
    """archive_check_enabled НЕ передан в FineJob(...) (как во всех тестах
    выше и во всём остальном этом файле) — завершение задачи ведёт себя
    БИТ В БИТ как раньше: только set_status(..., "completed"), архивные
    поля не трогаются вообще."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 10), end_date=date(2026, 8, 20),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        provider = _FakeProvider()
        job = _make_job(task_repo, fine_repo, provider, _FakeNotificationService())

        await job.run(datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc))

        updated = task_repo.get(task.id)
        assert updated.status == "completed"
        assert updated.archive_check_enabled is False
        assert updated.next_archive_check_at is None
    finally:
        task_repo.close()
        fine_repo.close()


async def test_completion_schedules_first_archive_check_when_archive_enabled(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 10), end_date=date(2026, 8, 20),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        provider = _FakeProvider()
        job = _make_job_with_archive(
            task_repo, fine_repo, provider, _FakeNotificationService(),
            archive_check_hour=4, archive_interval_days=30,
        )

        after_end = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)
        await job.run(after_end)

        updated = task_repo.get(task.id)
        assert updated.status == "completed"
        assert updated.archive_check_enabled is True
        assert updated.next_archive_check_at is not None

        # ~30 дней от даты завершения (2026-08-21 по Тбилиси), в 04:00 по
        # Тбилиси (UTC+4 -> 00:00 UTC).
        expected_day = date(2026, 8, 21) + timedelta(days=30)
        assert updated.next_archive_check_at == datetime(
            expected_day.year, expected_day.month, expected_day.day, 0, 0, tzinfo=timezone.utc
        )
    finally:
        task_repo.close()
        fine_repo.close()


async def test_archive_enrollment_on_completion_does_not_affect_other_active_tasks(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        overdue_task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        active_task = task_repo.create(
            car_number="BB002BB", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        provider = _FakeProvider()
        job = _make_job_with_archive(task_repo, fine_repo, provider, _FakeNotificationService())

        await job.run(_MID_PERIOD)

        assert task_repo.get(overdue_task.id).archive_check_enabled is True
        # Всё ещё активная задача не должна получить архивные поля.
        still_active = task_repo.get(active_task.id)
        assert still_active.status == "active"
        assert still_active.archive_check_enabled is False
        assert still_active.next_archive_check_at is None
    finally:
        task_repo.close()
        fine_repo.close()


async def test_completed_and_stopped_tasks_are_not_processed(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        completed_task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        task_repo.set_status(completed_task.id, "completed")

        stopped_task = task_repo.create(
            car_number="BB002BB", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        task_repo.set_status(stopped_task.id, "stopped")

        provider = _FakeProvider()
        job = _make_job(task_repo, fine_repo, provider, _FakeNotificationService())

        await job.run(_MID_PERIOD)

        assert provider.requested_plates == []
    finally:
        task_repo.close()
        fine_repo.close()


async def test_completing_one_overdue_task_does_not_prevent_checking_others(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        overdue_task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        active_task = task_repo.create(
            car_number="BB002BB", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        provider = _FakeProvider({"BB002BB": [_record(car_number="BB002BB", fingerprint="fp-bb")]})
        notification_service = _FakeNotificationService()
        job = _make_job(task_repo, fine_repo, provider, notification_service)

        # today = 2026-08-15: overdue_task уже после end_date (2026-07-31),
        # active_task — в середине своего периода.
        await job.run(_MID_PERIOD)

        assert task_repo.get(overdue_task.id).status == "completed"
        assert task_repo.get(active_task.id).status == "active"
        assert provider.requested_plates == ["BB002BB"]
        assert len(notification_service.notify_calls) == 1
        assert notification_service.notify_calls[0][0].car_number == "BB002BB"
    finally:
        task_repo.close()
        fine_repo.close()


# ---- run(): надёжность уведомлений (notification_sent_at, retry) ----


async def test_successful_delivery_marks_notification_sent(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        provider = _FakeProvider({"AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")]})
        notification_service = _FakeNotificationService()  # доставляет всё
        job = _make_job(task_repo, fine_repo, provider, notification_service)

        await job.run(_MID_PERIOD)

        fine = fine_repo.get_by_fingerprint(
            task_repo.get_active_by_car_number("AA001AA")[0].id, "fp-1"
        )
        assert fine.notification_sent_at is not None
        assert len(notification_service.notify_calls) == 1
    finally:
        task_repo.close()
        fine_repo.close()


async def test_failed_delivery_leaves_notification_sent_at_null_and_is_retried(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        provider = _FakeProvider({"AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")]})
        failing_notification_service = _FakeNotificationService(deliver_predicate=lambda e: False)
        job = _make_job(task_repo, fine_repo, provider, failing_notification_service)

        await job.run(_MID_PERIOD)

        fine = fine_repo.get_by_fingerprint(task.id, "fp-1")
        assert fine.notification_sent_at is None
        assert len(failing_notification_service.notify_calls) == 1

        # Следующий проход: провайдер снова находит тот же штраф (fingerprint
        # не меняется), поэтому check_task() не создаёт дубль — но
        # неотправленный штраф всё равно должен уйти в notify() повторно.
        retry_notification_service = _FakeNotificationService(deliver_predicate=lambda e: True)
        second_job = _make_job(task_repo, fine_repo, provider, retry_notification_service)
        await second_job.run(_MID_PERIOD)

        assert len(retry_notification_service.notify_calls) == 1
        retried_event = retry_notification_service.notify_calls[0][0]
        assert retried_event.detected_fine_id == fine.id

        updated_fine = fine_repo.get_by_fingerprint(task.id, "fp-1")
        assert updated_fine.notification_sent_at is not None
    finally:
        task_repo.close()
        fine_repo.close()


async def test_successfully_retried_fine_is_not_notified_again(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        provider = _FakeProvider({"AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")]})

        # Первый проход — доставка успешна.
        first_notification_service = _FakeNotificationService()
        await _make_job(task_repo, fine_repo, provider, first_notification_service).run(_MID_PERIOD)
        assert len(first_notification_service.notify_calls) == 1

        # Второй проход — тот же штраф уже отмечен, notify() не должен
        # вызываться для него снова.
        second_notification_service = _FakeNotificationService()
        await _make_job(task_repo, fine_repo, provider, second_notification_service).run(_MID_PERIOD)

        assert second_notification_service.notify_calls == []
    finally:
        task_repo.close()
        fine_repo.close()


async def test_grouped_message_marks_all_events_in_group_as_delivered(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        provider = _FakeProvider(
            {
                "AA001AA": [
                    _record(car_number="AA001AA", fingerprint="fp-1", external_fine_id="A1"),
                    _record(car_number="AA001AA", fingerprint="fp-2", external_fine_id="A2"),
                ]
            }
        )
        notification_service = _FakeNotificationService()
        job = _make_job(task_repo, fine_repo, provider, notification_service)

        await job.run(_MID_PERIOD)

        assert len(notification_service.notify_calls) == 1
        assert len(notification_service.notify_calls[0]) == 2

        fine_1 = fine_repo.get_by_fingerprint(task.id, "fp-1")
        fine_2 = fine_repo.get_by_fingerprint(task.id, "fp-2")
        assert fine_1.notification_sent_at is not None
        assert fine_2.notification_sent_at is not None
    finally:
        task_repo.close()
        fine_repo.close()


# ---- scope/name/pre_complete_hook (см. design report Stage 4, раздел
# "как исключается двойная проверка одной машины") ----


def test_default_name_is_unchanged_when_not_overridden():
    job = FineJob(
        task_repository=None, check_service=None, notification_coordinator=None,
        run_times=_RUN_TIMES, tz=_TBILISI,
    )
    assert job.name == "fine_monitoring"


def test_custom_name_overrides_class_default():
    job = FineJob(
        task_repository=None, check_service=None, notification_coordinator=None,
        run_times=_RUN_TIMES, tz=_TBILISI, name="fine_monitoring_client_bot",
    )
    assert job.name == "fine_monitoring_client_bot"


async def test_scope_none_uses_list_active_unfiltered(tmp_path):
    """scope=None (по умолчанию) — БИТ В БИТ то же поведение, что и раньше:
    list_active(), без учёта monitoring_scope."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task_repo.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID, monitoring_scope="operator",
        )
        task_repo.create(
            car_number="BB002BB", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID, monitoring_scope="client_bot",
        )

        provider = _FakeProvider()
        job = _make_job(task_repo, fine_repo, provider, _FakeNotificationService())

        await job.run(_MID_PERIOD)

        assert sorted(provider.requested_plates) == ["AA001AA", "BB002BB"]
    finally:
        task_repo.close()
        fine_repo.close()


async def test_scope_operator_only_checks_operator_tasks(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task_repo.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID, monitoring_scope="operator",
        )
        task_repo.create(
            car_number="BB002BB", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID, monitoring_scope="client_bot",
        )

        provider = _FakeProvider()
        check_service = FineCheckService(provider, task_repo, fine_repo)
        coordinator = FineNotificationCoordinator(fine_repo, task_repo, _FakeNotificationService())
        job = FineJob(
            task_repository=task_repo, check_service=check_service, notification_coordinator=coordinator,
            run_times=_RUN_TIMES, tz=_TBILISI, scope="operator",
        )

        await job.run(_MID_PERIOD)

        assert provider.requested_plates == ["AA001AA"]
    finally:
        task_repo.close()
        fine_repo.close()


async def test_scope_client_bot_only_checks_client_bot_tasks(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task_repo.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID, monitoring_scope="operator",
        )
        task_repo.create(
            car_number="BB002BB", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID, monitoring_scope="client_bot",
        )

        provider = _FakeProvider()
        check_service = FineCheckService(provider, task_repo, fine_repo)
        coordinator = FineNotificationCoordinator(fine_repo, task_repo, _FakeNotificationService())
        job = FineJob(
            task_repository=task_repo, check_service=check_service, notification_coordinator=coordinator,
            run_times=_RUN_TIMES, tz=_TBILISI, scope="client_bot",
        )

        await job.run(_MID_PERIOD)

        assert provider.requested_plates == ["BB002BB"]
    finally:
        task_repo.close()
        fine_repo.close()


async def test_operator_and_client_bot_scoped_jobs_never_double_check_same_task(tmp_path):
    """Интеграционная проверка "двойной проверки не бывает" — ДВА
    FineJob-инстанса (операторский и client_bot) в одном "тике" ни разу не
    запрашивают один и тот же номер (см. design report Stage 4)."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task_repo.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID, monitoring_scope="operator",
        )
        task_repo.create(
            car_number="BB002BB", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID, monitoring_scope="client_bot",
        )
        # "Общая" машина оператора+клиента — должна остаться operator scope
        # (см. Stage 1: monitoring_scope только upgrade, никогда downgrade)
        # и попасть РОВНО в одну из двух проверок.
        shared_task = task_repo.create(
            car_number="CC003CC", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID, monitoring_scope="operator",
        )

        provider = _FakeProvider()
        check_service = FineCheckService(provider, task_repo, fine_repo)
        coordinator = FineNotificationCoordinator(fine_repo, task_repo, _FakeNotificationService())
        operator_job = FineJob(
            task_repository=task_repo, check_service=check_service, notification_coordinator=coordinator,
            run_times=_RUN_TIMES, tz=_TBILISI, scope="operator",
        )
        client_job = FineJob(
            task_repository=task_repo, check_service=check_service, notification_coordinator=coordinator,
            run_times=_RUN_TIMES, tz=_TBILISI, scope="client_bot",
        )

        await operator_job.run(_MID_PERIOD)
        await client_job.run(_MID_PERIOD)

        # Каждый номер запрошен РОВНО один раз суммарно между двумя job'ами.
        assert sorted(provider.requested_plates) == ["AA001AA", "BB002BB", "CC003CC"]
        assert task_repo.get(shared_task.id).monitoring_scope == "operator"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_pre_complete_hook_extends_task_and_checks_it_in_same_run(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 20),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID, monitoring_scope="client_bot",
        )

        async def _extend_hook(task, today):
            return task_repo.extend_period_if_shorter(task.id, date(2026, 9, 30))

        provider = _FakeProvider()
        check_service = FineCheckService(provider, task_repo, fine_repo)
        coordinator = FineNotificationCoordinator(fine_repo, task_repo, _FakeNotificationService())
        job = FineJob(
            task_repository=task_repo, check_service=check_service, notification_coordinator=coordinator,
            run_times=_RUN_TIMES, tz=_TBILISI, scope="client_bot", pre_complete_hook=_extend_hook,
        )

        after_end = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)
        await job.run(after_end)

        # Задача продлена и ПРОВЕРЕНА в этом же проходе — не завершена.
        updated = task_repo.get(task.id)
        assert updated.status == "active"
        assert updated.end_date == date(2026, 9, 30)
        assert provider.requested_plates == ["AA001AA"]
    finally:
        task_repo.close()
        fine_repo.close()


async def test_pre_complete_hook_returning_unchanged_task_completes_as_usual(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 20),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID, monitoring_scope="client_bot",
        )

        async def _noop_hook(task, today):
            return task  # ничего не продлевает — как будто подписчиков не осталось

        provider = _FakeProvider()
        check_service = FineCheckService(provider, task_repo, fine_repo)
        coordinator = FineNotificationCoordinator(fine_repo, task_repo, _FakeNotificationService())
        job = FineJob(
            task_repository=task_repo, check_service=check_service, notification_coordinator=coordinator,
            run_times=_RUN_TIMES, tz=_TBILISI, scope="client_bot", pre_complete_hook=_noop_hook,
        )

        after_end = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)
        await job.run(after_end)

        updated = task_repo.get(task.id)
        assert updated.status == "completed"
        assert provider.requested_plates == []
    finally:
        task_repo.close()
        fine_repo.close()


async def test_pre_complete_hook_not_called_when_none(tmp_path):
    """Регресс: hook=None (по умолчанию, единственный существующий вызов
    в reader/main.py для операторского FineJob) — завершение задачи ведёт
    себя БИТ В БИТ как раньше."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 20),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        provider = _FakeProvider()
        job = _make_job(task_repo, fine_repo, provider, _FakeNotificationService())

        await job.run(datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc))

        assert task_repo.get(task.id).status == "completed"
        assert provider.requested_plates == []
    finally:
        task_repo.close()
        fine_repo.close()
