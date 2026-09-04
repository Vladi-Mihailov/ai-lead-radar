"""
Тесты NotificationFlushJob — единственная задача которого доставлять
оператору уже накопленные, но ещё не отправленные штрафы на каждом тике
Scheduler'а (см. design report Stage 4, раздел "Immediate-check race
handling"). Repository — настоящие (SQLite, tmp_path), NotificationService —
фейковый.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.notification_coordinator import FineNotificationCoordinator  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.jobs.notification_flush_job import NotificationFlushJob  # noqa: E402
from reader.notifications.base import NotificationResult, NotificationService  # noqa: E402

_CHAT_ID = -100999
_USER_ID = 111


class _FakeNotificationService(NotificationService):
    def __init__(self):
        self.notify_calls = []

    async def notify(self, events):
        self.notify_calls.append(events)
        return NotificationResult(
            delivered_event_ids=[e.detected_fine_id for e in events], failed_event_ids=[],
        )


async def test_should_run_is_always_true():
    job = NotificationFlushJob(notification_coordinator=None)
    assert await job.should_run(datetime.now(timezone.utc)) is True


async def test_run_flushes_pending_notification(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = task_repo.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        fine = fine_repo.create(
            monitoring_task_id=task.id, car_number="AA001AA",
            external_fine_id="AB1", fingerprint="fp-1",
            penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
            delivered_status=None, raw_data="{}",
        )
        notification_service = _FakeNotificationService()
        coordinator = FineNotificationCoordinator(fine_repo, task_repo, notification_service)
        job = NotificationFlushJob(coordinator)

        await job.run()

        assert len(notification_service.notify_calls) == 1
        assert fine_repo.get_by_fingerprint(task.id, "fp-1").notification_sent_at is not None
    finally:
        task_repo.close()
        fine_repo.close()


async def test_run_is_cheap_noop_when_nothing_pending(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        notification_service = _FakeNotificationService()
        coordinator = FineNotificationCoordinator(fine_repo, task_repo, notification_service)
        job = NotificationFlushJob(coordinator)

        await job.run()

        assert notification_service.notify_calls == []
    finally:
        task_repo.close()
        fine_repo.close()
