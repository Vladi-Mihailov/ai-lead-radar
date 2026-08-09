"""
Тесты ArchiveFineJob — второй, независимый от FineJob режим мониторинга:
берёт задачи с archive_check_enabled=1 и next_archive_check_at <= now
(list_due_for_archive_check), проверяет их той же FineCheckService, что и
FineJob/fine check/update-all, строго последовательно.

FineMonitoringTaskRepository/DetectedFineRepository — настоящие (SQLite,
tmp_path), FineCheckService — тоже настоящий, с фейковым FineProvider вместо
сети (тот же приём, что и test_fine_job.py).
"""

import dataclasses
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import ParsedFineRecord  # noqa: E402
from reader.fines.notification_coordinator import FineNotificationCoordinator  # noqa: E402
from reader.fines.provider import FineProvider, FineProviderError  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.jobs.archive_fine_job import ArchiveFineJob  # noqa: E402
from reader.notifications.base import NotificationResult, NotificationService  # noqa: E402

_CHAT_ID = -100999
_USER_ID = 111
_TBILISI = ZoneInfo("Asia/Tbilisi")
_HOUR = 4
_INTERVAL_DAYS = 30
_DAILY_LIMIT = 200


class _FakeProvider(FineProvider):
    def __init__(self, records_by_car=None, error_for=None):
        self._records_by_car = records_by_car or {}
        self._error_for = set(error_for or ())
        self.requested_plates: list[str] = []

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        self.requested_plates.append(plate)
        if plate in self._error_for:
            raise FineProviderError(f"police.ge недоступен для {plate}")
        return self._records_by_car.get(plate, [])


class _RaisingProvider(FineProvider):
    """Бросает ИСКЛЮЧЕНИЕ, отличное от FineProviderError — имитирует баг/
    сбой, который FineCheckService.check_task() не перехватывает сам (в
    отличие от FineProviderError)."""

    def __init__(self, fail_for):
        self._fail_for = set(fail_for)
        self.requested_plates: list[str] = []

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        self.requested_plates.append(plate)
        if plate in self._fail_for:
            raise RuntimeError(f"unexpected failure for {plate}")
        return []


class _FakeNotificationService(NotificationService):
    def __init__(self, deliver_predicate=None):
        self._deliver_predicate = deliver_predicate or (lambda event: True)
        self.notify_calls: list[list] = []

    async def notify(self, events) -> NotificationResult:
        self.notify_calls.append(events)
        delivered = [e.detected_fine_id for e in events if self._deliver_predicate(e)]
        failed = [e.detected_fine_id for e in events if not self._deliver_predicate(e)]
        return NotificationResult(delivered_event_ids=delivered, failed_event_ids=failed)


def _record(
    car_number="AA001AA",
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


def _make_archive_job(
    task_repo, fine_repo, provider, notification_service,
    *, enabled=True, hour=_HOUR, interval_days=_INTERVAL_DAYS, daily_limit=_DAILY_LIMIT,
) -> ArchiveFineJob:
    check_service = FineCheckService(provider, task_repo, fine_repo)
    coordinator = FineNotificationCoordinator(fine_repo, task_repo, notification_service)
    return ArchiveFineJob(
        task_repository=task_repo,
        check_service=check_service,
        notification_coordinator=coordinator,
        enabled=enabled,
        hour=hour,
        interval_days=interval_days,
        daily_limit=daily_limit,
        tz=_TBILISI,
    )


def _create_archived_task(task_repo, car_number: str, next_check_at: datetime) -> int:
    task = task_repo.create(
        car_number=car_number, label=None,
        start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
    )
    task_repo.set_status(task.id, "completed")
    task_repo.enroll_in_archive_mode({task.id: next_check_at})
    return task.id


# ---- should_run ----


async def test_should_run_fires_at_configured_hour_in_configured_timezone():
    job = ArchiveFineJob(
        task_repository=None, check_service=None, notification_coordinator=None,
        enabled=True, hour=_HOUR, interval_days=_INTERVAL_DAYS, daily_limit=_DAILY_LIMIT,
        tz=_TBILISI,
    )

    # 04:00 в Тбилиси (UTC+4) == 00:00 UTC
    four_am_tbilisi_utc = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    assert await job.should_run(four_am_tbilisi_utc) is True


async def test_should_run_is_false_outside_configured_hour():
    job = ArchiveFineJob(
        task_repository=None, check_service=None, notification_coordinator=None,
        enabled=True, hour=_HOUR, interval_days=_INTERVAL_DAYS, daily_limit=_DAILY_LIMIT,
        tz=_TBILISI,
    )

    assert await job.should_run(datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)) is False
    assert await job.should_run(datetime(2026, 9, 1, 0, 5, tzinfo=timezone.utc)) is False


async def test_should_run_does_not_fire_twice_the_same_day():
    job = ArchiveFineJob(
        task_repository=None, check_service=None, notification_coordinator=None,
        enabled=True, hour=_HOUR, interval_days=_INTERVAL_DAYS, daily_limit=_DAILY_LIMIT,
        tz=_TBILISI,
    )

    four_am = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    assert await job.should_run(four_am) is True
    assert await job.should_run(four_am) is False


async def test_should_run_fires_again_next_day():
    job = ArchiveFineJob(
        task_repository=None, check_service=None, notification_coordinator=None,
        enabled=True, hour=_HOUR, interval_days=_INTERVAL_DAYS, daily_limit=_DAILY_LIMIT,
        tz=_TBILISI,
    )

    day_one = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    day_two = datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)
    assert await job.should_run(day_one) is True
    assert await job.should_run(day_two) is True


async def test_should_run_returns_false_when_disabled():
    job = ArchiveFineJob(
        task_repository=None, check_service=None, notification_coordinator=None,
        enabled=False, hour=_HOUR, interval_days=_INTERVAL_DAYS, daily_limit=_DAILY_LIMIT,
        tz=_TBILISI,
    )

    four_am = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    assert await job.should_run(four_am) is False


# ---- run(): выборка задач ----


async def test_run_checks_only_due_archive_tasks(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        due_id = _create_archived_task(task_repo, "AA001AA", now)
        not_due_id = _create_archived_task(task_repo, "BB002BB", now + timedelta(days=5))

        # Активная (обычный частый мониторинг) задача — ArchiveFineJob не
        # должен её видеть вообще.
        active_task = task_repo.create(
            car_number="CC003CC", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        provider = _FakeProvider()
        job = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService())

        await job.run(now)

        assert provider.requested_plates == ["AA001AA"]
        assert task_repo.get(not_due_id).next_archive_check_at == now + timedelta(days=5)
        assert task_repo.get(active_task.id).status == "active"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_run_uses_same_check_service_as_fine_job_and_fine_check(tmp_path):
    """Нет отдельной логики проверки/обращения к police.ge — тот же самый
    FineCheckService.check_task(), что и везде (проверяется тем же
    провайдером и той же дедупликацией по fingerprint)."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        task_id = _create_archived_task(task_repo, "AA001AA", now)

        provider = _FakeProvider({"AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")]})
        job = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService())

        await job.run(now)

        fine = fine_repo.get_by_fingerprint(task_id, "fp-1")
        assert fine is not None
    finally:
        task_repo.close()
        fine_repo.close()


async def test_run_processes_tasks_sequentially_not_concurrently(tmp_path):
    """Простая, но показательная проверка последовательности: провайдер
    фиксирует ПОРЯДОК запросов, который должен совпадать с (due, id) ASC —
    если бы обработка шла конкурентно (asyncio.gather), порядок не был бы
    гарантирован."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        first_id = _create_archived_task(task_repo, "AA001AA", now - timedelta(days=2))
        second_id = _create_archived_task(task_repo, "BB002BB", now - timedelta(days=1))
        third_id = _create_archived_task(task_repo, "CC003CC", now)
        assert first_id and second_id and third_id

        provider = _FakeProvider()
        job = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService())

        await job.run(now)

        assert provider.requested_plates == ["AA001AA", "BB002BB", "CC003CC"]
    finally:
        task_repo.close()
        fine_repo.close()


async def test_run_with_no_due_tasks_does_nothing(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        provider = _FakeProvider()
        notification_service = _FakeNotificationService()
        job = _make_archive_job(task_repo, fine_repo, provider, notification_service)

        await job.run(datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc))

        assert provider.requested_plates == []
        assert notification_service.notify_calls == []
    finally:
        task_repo.close()
        fine_repo.close()


# ---- run(): ошибка одной задачи не останавливает остальные ----


async def test_provider_error_on_one_task_does_not_block_others(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        failing_id = _create_archived_task(task_repo, "AA001AA", now)
        ok_id = _create_archived_task(task_repo, "BB002BB", now)

        # BB002BB намеренно без штрафов — проверяем именно "успех без новых
        # штрафов" (reschedule), не смешивая с логикой "новый штраф найден".
        provider = _FakeProvider({"BB002BB": []}, error_for={"AA001AA"})
        job = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService())

        await job.run(now)

        assert sorted(provider.requested_plates) == ["AA001AA", "BB002BB"]
        # ok_id успешно проверен и переехал на следующий срок.
        assert task_repo.get(ok_id).next_archive_check_at == now + timedelta(days=_INTERVAL_DAYS)
        # failing_id остаётся due (см. отдельный тест на "не откладывать на месяц").
        assert task_repo.get(failing_id).next_archive_check_at == now
    finally:
        task_repo.close()
        fine_repo.close()


async def test_unexpected_exception_on_one_task_does_not_block_others(tmp_path):
    """FineCheckService.check_task() сам не бросает исключение на
    FineProviderError — но если что-то другое всё же бросит (баг,
    неожиданная ошибка), ArchiveFineJob обязан изолировать её так же, как
    FineJob.run()/fine update-all."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        failing_id = _create_archived_task(task_repo, "AA001AA", now)
        ok_id = _create_archived_task(task_repo, "BB002BB", now)

        provider = _RaisingProvider(fail_for={"AA001AA"})
        job = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService())

        await job.run(now)

        assert sorted(provider.requested_plates) == ["AA001AA", "BB002BB"]
        assert task_repo.get(ok_id).next_archive_check_at == now + timedelta(days=_INTERVAL_DAYS)
        assert job.status.error_count == 1
    finally:
        task_repo.close()
        fine_repo.close()


# ---- run(): успешная проверка без новых штрафов — сдвиг без дрейфа ----


async def test_successful_check_without_new_fines_advances_by_interval_from_previous_due(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        previous_due = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        task_id = _create_archived_task(task_repo, "AA001AA", previous_due)

        # Scheduler опоздал на несколько часов — run_at позже previous_due,
        # но следующий срок должен считаться ОТ previous_due, а не от run_at.
        run_at = previous_due + timedelta(hours=3)

        provider = _FakeProvider()
        job = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService())

        await job.run(run_at)

        updated = task_repo.get(task_id)
        assert updated.next_archive_check_at == previous_due + timedelta(days=_INTERVAL_DAYS)
        assert updated.archive_check_enabled is True
        assert updated.status == "completed"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_repeated_on_time_runs_do_not_drift_schedule(tmp_path):
    """Несколько циклов подряд, каждый раз запускаемых с небольшим
    опозданием относительно "идеального" 04:00 — расписание не должно
    постепенно смещаться вперёд."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        due_1 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        task_id = _create_archived_task(task_repo, "AA001AA", due_1)

        provider = _FakeProvider()
        job = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService())

        # Цикл 1: запускается с опозданием на 20 минут.
        await job.run(due_1 + timedelta(minutes=20))
        due_2 = task_repo.get(task_id).next_archive_check_at
        assert due_2 == due_1 + timedelta(days=_INTERVAL_DAYS)

        # Цикл 2: снова с опозданием.
        await job.run(due_2 + timedelta(minutes=20))
        due_3 = task_repo.get(task_id).next_archive_check_at
        assert due_3 == due_2 + timedelta(days=_INTERVAL_DAYS)

        # Ни одного "накопленного" сдвига — интервал между стартовыми due
        # остаётся ровно interval_days, а не interval_days + опоздания.
        assert (due_3 - due_1) == timedelta(days=2 * _INTERVAL_DAYS)
    finally:
        task_repo.close()
        fine_repo.close()


async def test_missing_previous_due_falls_back_to_run_at(tmp_path):
    """Защитный fallback: next_archive_check_at пуст к моменту расчёта
    следующего срока (не должно случаться при нормальной выборке из
    list_due_for_archive_check, но код не должен упасть/зациклиться)."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        task_id = _create_archived_task(task_repo, "AA001AA", now)

        provider = _FakeProvider()
        job = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService())

        task_without_previous_due = dataclasses.replace(
            task_repo.get(task_id), next_archive_check_at=None
        )

        result = job._compute_next_check_at(task_without_previous_due, now)

        assert result == now + timedelta(days=_INTERVAL_DAYS)
    finally:
        task_repo.close()
        fine_repo.close()


# ---- run(): технический сбой не откладывает проверку на месяц ----


async def test_technical_error_leaves_task_due_instead_of_deferring_a_month(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        due_at = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        task_id = _create_archived_task(task_repo, "AA001AA", due_at)

        provider = _FakeProvider(error_for={"AA001AA"})
        job = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService())

        await job.run(due_at)

        updated = task_repo.get(task_id)
        # НЕ +30 дней — next_archive_check_at не тронут вообще, задача
        # остаётся due и попадёт в следующий (завтрашний) запуск.
        assert updated.next_archive_check_at == due_at
        assert updated.archive_check_enabled is True

        # Повторный запуск (как "следующий день") снова видит эту задачу
        # как due — без искусственной задержки на месяц.
        provider_next_day = _FakeProvider()
        job_next_day = _make_archive_job(task_repo, fine_repo, provider_next_day, _FakeNotificationService())
        await job_next_day.run(due_at + timedelta(days=1))

        assert provider_next_day.requested_plates == ["AA001AA"]
    finally:
        task_repo.close()
        fine_repo.close()


# ---- run(): новый штраф возвращает задачу в частый мониторинг ----


async def test_new_fine_returns_task_to_active_monitoring_for_30_days(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        task_id = _create_archived_task(task_repo, "AA001AA", now)

        provider = _FakeProvider({"AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")]})
        job = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService())

        await job.run(now)

        updated = task_repo.get(task_id)
        assert updated.status == "active"
        assert updated.start_date == now.astimezone(_TBILISI).date()
        assert updated.end_date == now.astimezone(_TBILISI).date() + timedelta(days=_INTERVAL_DAYS)
        assert updated.archive_check_enabled is False
        assert updated.next_archive_check_at is None

        # Снова попадает в обычный частый мониторинг.
        assert [t.id for t in task_repo.list_active()] == [task_id]
    finally:
        task_repo.close()
        fine_repo.close()


async def test_already_known_fine_does_not_return_task_to_active_mode(tmp_path):
    """Штраф, уже виденный для этой задачи раньше (тот же fingerprint),
    попавшийся снова в ответе police.ge — НЕ считается new_fines (см.
    FineCheckService.check_task dedup по (monitoring_task_id, fingerprint)),
    поэтому не должен возвращать задачу в частый мониторинг."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        task_id = _create_archived_task(task_repo, "AA001AA", now)

        provider = _FakeProvider({"AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")]})

        # Первый архивный проход — штраф действительно новый, задача
        # возвращается в частый мониторинг (см. тест выше). Симулируем, что
        # он уже был замечен РАНЬШЕ (например, во время предыдущего частого
        # мониторинга) — второй раз тот же fingerprint для той же задачи не
        # должен считаться new_fines.
        await job_first_run_marks_seen(task_repo, fine_repo, task_id)

        job = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService())
        await job.run(now)

        updated = task_repo.get(task_id)
        # Остаётся в архивном режиме — не вернулась в active.
        assert updated.status == "completed"
        assert updated.archive_check_enabled is True
        assert updated.next_archive_check_at == now + timedelta(days=_INTERVAL_DAYS)
    finally:
        task_repo.close()
        fine_repo.close()


async def job_first_run_marks_seen(task_repo, fine_repo, task_id) -> None:
    """Вставляет detected_fines-запись с тем же fingerprint напрямую через
    репозиторий — имитирует "штраф уже был обнаружен раньше", без
    выполнения полноценного check_task()."""
    fine_repo.create(
        monitoring_task_id=task_id,
        car_number="AA001AA",
        external_fine_id="AB123456",
        fingerprint="fp-1",
        penalty_date=date(2026, 8, 6),
        due_date=date(2026, 8, 20),
        delivered_status="Не вручено",
        raw_data="{}",
    )


# ---- run(): notification flow — тот же существующий механизм ----


async def test_new_fine_notification_goes_through_shared_coordinator(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        task_id = _create_archived_task(task_repo, "AA001AA", now)

        provider = _FakeProvider({"AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")]})
        notification_service = _FakeNotificationService()
        job = _make_archive_job(task_repo, fine_repo, provider, notification_service)

        await job.run(now)

        assert len(notification_service.notify_calls) == 1
        fine = fine_repo.get_by_fingerprint(task_id, "fp-1")
        assert fine.notification_sent_at is not None
    finally:
        task_repo.close()
        fine_repo.close()


async def test_failed_notification_delivery_is_retried_next_run(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        _create_archived_task(task_repo, "AA001AA", now)

        provider = _FakeProvider({"AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")]})
        failing_notification_service = _FakeNotificationService(deliver_predicate=lambda e: False)
        job = _make_archive_job(task_repo, fine_repo, provider, failing_notification_service)

        await job.run(now)

        assert len(failing_notification_service.notify_calls) == 1
        assert job.status.error_count == 0  # доставка сама по себе не ошибка check_task

        # Ретрай происходит через тот же общий механизм (notification_sent_at
        # IS NULL) — например, следующим fine check/update-all/FineJob, не
        # обязательно снова ArchiveFineJob (задача уже вернулась в active).
        pending = fine_repo.list_pending_notifications()
        assert len(pending) == 1
    finally:
        task_repo.close()
        fine_repo.close()


# ---- run(): backlog / daily_limit ----


async def test_daily_limit_caps_tasks_processed_per_run(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        ids = [_create_archived_task(task_repo, f"AA{i:03d}AA", now) for i in range(5)]

        provider = _FakeProvider()
        job = _make_archive_job(
            task_repo, fine_repo, provider, _FakeNotificationService(), daily_limit=2,
        )

        await job.run(now)

        assert len(provider.requested_plates) == 2
        processed = sorted(
            task_id for task_id in ids
            if task_repo.get(task_id).next_archive_check_at != now
        )
        untouched = sorted(
            task_id for task_id in ids
            if task_repo.get(task_id).next_archive_check_at == now
        )
        assert len(processed) == 2
        assert len(untouched) == 3
        # Ничего не потеряно — оставшиеся всё ещё due (next_archive_check_at
        # не сдвинут), просто не обработаны в ЭТОМ прогоне.
        assert task_repo.list_due_for_archive_check(now, limit=100) == [
            task_repo.get(task_id) for task_id in untouched
        ]
    finally:
        task_repo.close()
        fine_repo.close()


async def test_backlog_beyond_limit_is_processed_over_subsequent_runs(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        for i in range(5):
            _create_archived_task(task_repo, f"AA{i:03d}AA", now)

        provider = _FakeProvider()

        # Запуск 1: обрабатывает 2 из 5.
        job_1 = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService(), daily_limit=2)
        await job_1.run(now)
        assert len(provider.requested_plates) == 2

        # Запуск 2 (следующий день) с тем же лимитом — добирает оставшийся
        # backlog постепенно, ничего не теряя.
        job_2 = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService(), daily_limit=2)
        await job_2.run(now + timedelta(days=1))
        assert len(provider.requested_plates) == 4

        job_3 = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService(), daily_limit=2)
        await job_3.run(now + timedelta(days=2))
        assert len(provider.requested_plates) == 5  # последняя (5-я) задача добита
    finally:
        task_repo.close()
        fine_repo.close()


# ---- наблюдаемость ----


async def test_run_logs_started_and_finished_summary(tmp_path, caplog):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        _create_archived_task(task_repo, "AA001AA", now)

        provider = _FakeProvider({"AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")]})
        job = _make_archive_job(task_repo, fine_repo, provider, _FakeNotificationService())

        with caplog.at_level("INFO", logger="reader.jobs.archive_fine_job"):
            await job.run(now)

        messages = [record.getMessage() for record in caplog.records]
        assert any(m.startswith("ArchiveFineJob started: due=1") for m in messages)
        assert any(m.startswith("ArchiveFineJob finished:") for m in messages)
        assert any("checked=1" in m and "new_fines=1" in m and "errors=0" in m for m in messages)
    finally:
        task_repo.close()
        fine_repo.close()
