"""
Тесты FineNotificationCoordinator — общей точки "доставить всё, что ещё не
доставлено", используемой и FineJob, и FineCommand.check. Repository —
настоящие (SQLite/tmp_path), NotificationService — фейковый.
"""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import NewFineEvent  # noqa: E402
from reader.fines.notification_coordinator import FineNotificationCoordinator  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.notifications.base import NotificationResult, NotificationService  # noqa: E402

_CHAT_ID = -100999
_USER_ID = 111


class _FakeNotificationService(NotificationService):
    def __init__(self, deliver_predicate=None):
        self._deliver_predicate = deliver_predicate or (lambda event: True)
        self.notify_calls: list[list[NewFineEvent]] = []

    async def notify(self, events: list[NewFineEvent]) -> NotificationResult:
        self.notify_calls.append(events)
        delivered = [e.detected_fine_id for e in events if self._deliver_predicate(e)]
        failed = [e.detected_fine_id for e in events if not self._deliver_predicate(e)]
        return NotificationResult(delivered_event_ids=delivered, failed_event_ids=failed)


def _make_repos(tmp_path):
    db_path = tmp_path / "users.db"
    return FineMonitoringTaskRepository(db_path), DetectedFineRepository(db_path)


async def test_flush_pending_with_no_pending_fines_does_not_call_notify(tmp_path):
    task_repo, fine_repo = _make_repos(tmp_path)
    try:
        notification_service = _FakeNotificationService()
        coordinator = FineNotificationCoordinator(fine_repo, task_repo, notification_service)

        result = await coordinator.flush_pending()

        assert notification_service.notify_calls == []
        assert result.delivered_event_ids == []
        assert result.failed_event_ids == []
    finally:
        task_repo.close()
        fine_repo.close()


async def test_flush_pending_delivers_and_marks_notification_sent(tmp_path):
    task_repo, fine_repo = _make_repos(tmp_path)
    try:
        task = task_repo.create(
            car_number="AA001AA", label="Моя машина",
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        fine = fine_repo.create(
            monitoring_task_id=task.id, car_number="AA001AA",
            external_fine_id="A1", fingerprint="fp-1",
            penalty_date=None, due_date=None, delivered_status="Не вручено",
            raw_data="{}",
        )

        notification_service = _FakeNotificationService()
        coordinator = FineNotificationCoordinator(fine_repo, task_repo, notification_service)

        result = await coordinator.flush_pending()

        assert len(notification_service.notify_calls) == 1
        sent_event = notification_service.notify_calls[0][0]
        assert sent_event.detected_fine_id == fine.id
        assert sent_event.label == "Моя машина"

        assert result.delivered_event_ids == [fine.id]

        updated = fine_repo.get_by_fingerprint(task.id, "fp-1")
        assert updated.notification_sent_at is not None
    finally:
        task_repo.close()
        fine_repo.close()


async def test_flush_pending_leaves_failed_fines_pending_for_next_call(tmp_path):
    task_repo, fine_repo = _make_repos(tmp_path)
    try:
        task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        fine_repo.create(
            monitoring_task_id=task.id, car_number="AA001AA",
            external_fine_id="A1", fingerprint="fp-1",
            penalty_date=None, due_date=None, delivered_status="Не вручено",
            raw_data="{}",
        )

        failing_service = _FakeNotificationService(deliver_predicate=lambda e: False)
        coordinator = FineNotificationCoordinator(fine_repo, task_repo, failing_service)

        result = await coordinator.flush_pending()
        assert result.failed_event_ids != []

        updated = fine_repo.get_by_fingerprint(task.id, "fp-1")
        assert updated.notification_sent_at is None

        # Тот же координатор, следующий вызов — штраф снова попадает в pending.
        succeeding_service = _FakeNotificationService()
        coordinator_2 = FineNotificationCoordinator(fine_repo, task_repo, succeeding_service)
        await coordinator_2.flush_pending()

        assert len(succeeding_service.notify_calls) == 1
        updated_again = fine_repo.get_by_fingerprint(task.id, "fp-1")
        assert updated_again.notification_sent_at is not None
    finally:
        task_repo.close()
        fine_repo.close()
