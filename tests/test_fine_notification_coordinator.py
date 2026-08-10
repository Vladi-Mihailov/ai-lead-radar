"""
Тесты FineNotificationCoordinator — общей точки "доставить всё, что ещё не
доставлено", используемой и FineJob, и FineCommand.check. Repository —
настоящие (SQLite/tmp_path), NotificationService — фейковый.
"""

import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import DetectedFine, NewFineEvent  # noqa: E402
from reader.fines.notification_coordinator import FineNotificationCoordinator  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.notifications.base import NotificationResult, NotificationService  # noqa: E402
from reader.users.models import TelegramUserInfo  # noqa: E402

_CHAT_ID = -100999
_USER_ID = 111


class _FakeUserRepository:
    """Реализует ровно UserLookupLike (см. notification_coordinator.py) —
    без единой строчки SQL, только то, что нужно координатору."""

    def __init__(self, users_by_id: dict[int, TelegramUserInfo] | None = None):
        self._users_by_id = users_by_id or {}

    def get(self, user_id: int) -> TelegramUserInfo | None:
        return self._users_by_id.get(user_id)


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


# ---- created_by_display: fine_monitoring_tasks.created_by_user_id -> users ----


def _create_task_and_fine(task_repo, fine_repo, *, created_by_user_id, fingerprint="fp-1"):
    task = task_repo.create(
        car_number="AA001AA", label=None,
        start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=created_by_user_id,
    )
    fine = fine_repo.create(
        monitoring_task_id=task.id, car_number="AA001AA",
        external_fine_id="A1", fingerprint=fingerprint,
        penalty_date=None, due_date=None, delivered_status="Не вручено",
        raw_data="{}",
    )
    return task, fine


async def test_created_by_display_shows_username_when_available(tmp_path):
    task_repo, fine_repo = _make_repos(tmp_path)
    try:
        _create_task_and_fine(task_repo, fine_repo, created_by_user_id=42)

        user_repository = _FakeUserRepository(
            {42: TelegramUserInfo(user_id=42, username="ivan_petrov", first_name=None, last_name=None)}
        )
        notification_service = _FakeNotificationService()
        coordinator = FineNotificationCoordinator(
            fine_repo, task_repo, notification_service, user_repository,
        )

        await coordinator.flush_pending()

        sent_event = notification_service.notify_calls[0][0]
        assert sent_event.created_by_display == "@ivan_petrov"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_created_by_display_falls_back_to_user_id_when_username_missing(tmp_path):
    task_repo, fine_repo = _make_repos(tmp_path)
    try:
        _create_task_and_fine(task_repo, fine_repo, created_by_user_id=123456789)

        # UserRepository знает пользователя, но у него нет ни username, ни имени.
        user_repository = _FakeUserRepository(
            {123456789: TelegramUserInfo(user_id=123456789, username=None, first_name=None, last_name=None)}
        )
        notification_service = _FakeNotificationService()
        coordinator = FineNotificationCoordinator(
            fine_repo, task_repo, notification_service, user_repository,
        )

        await coordinator.flush_pending()

        sent_event = notification_service.notify_calls[0][0]
        assert sent_event.created_by_display == "ID 123456789"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_created_by_display_falls_back_to_user_id_when_user_repository_is_none(tmp_path):
    """Без UserRepository вообще (например, старое подключение) — уведомление
    остаётся полезным: показывает хотя бы created_by_user_id."""
    task_repo, fine_repo = _make_repos(tmp_path)
    try:
        _create_task_and_fine(task_repo, fine_repo, created_by_user_id=999)

        notification_service = _FakeNotificationService()
        coordinator = FineNotificationCoordinator(fine_repo, task_repo, notification_service)

        await coordinator.flush_pending()

        sent_event = notification_service.notify_calls[0][0]
        assert sent_event.created_by_display == "ID 999"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_created_by_display_falls_back_to_user_id_when_user_not_found_in_repository(tmp_path):
    task_repo, fine_repo = _make_repos(tmp_path)
    try:
        _create_task_and_fine(task_repo, fine_repo, created_by_user_id=555)

        user_repository = _FakeUserRepository({})  # пользователь не найден
        notification_service = _FakeNotificationService()
        coordinator = FineNotificationCoordinator(
            fine_repo, task_repo, notification_service, user_repository,
        )

        await coordinator.flush_pending()

        sent_event = notification_service.notify_calls[0][0]
        assert sent_event.created_by_display == "ID 555"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_created_by_display_combines_full_name_and_username(tmp_path):
    task_repo, fine_repo = _make_repos(tmp_path)
    try:
        _create_task_and_fine(task_repo, fine_repo, created_by_user_id=42)

        user_repository = _FakeUserRepository(
            {42: TelegramUserInfo(
                user_id=42, username="ivan_petrov", first_name="Иван", last_name="Петров",
            )}
        )
        notification_service = _FakeNotificationService()
        coordinator = FineNotificationCoordinator(
            fine_repo, task_repo, notification_service, user_repository,
        )

        await coordinator.flush_pending()

        sent_event = notification_service.notify_calls[0][0]
        assert sent_event.created_by_display == "Иван Петров (@ivan_petrov)"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_created_by_display_shows_full_name_with_id_when_username_missing(tmp_path):
    task_repo, fine_repo = _make_repos(tmp_path)
    try:
        _create_task_and_fine(task_repo, fine_repo, created_by_user_id=42)

        user_repository = _FakeUserRepository(
            {42: TelegramUserInfo(user_id=42, username=None, first_name="Иван", last_name="Петров")}
        )
        notification_service = _FakeNotificationService()
        coordinator = FineNotificationCoordinator(
            fine_repo, task_repo, notification_service, user_repository,
        )

        await coordinator.flush_pending()

        sent_event = notification_service.notify_calls[0][0]
        assert sent_event.created_by_display == "Иван Петров (ID 42)"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_created_by_display_is_looked_up_once_per_task_for_multiple_fines_same_car(tmp_path):
    """Несколько новых штрафов по одной и той же задаче мониторинга — у
    всех событий должен быть одинаковый created_by_display (сам рендеринг
    "один раз на автомобиль" — забота TelegramNotificationService, но
    источник данных для этого — координатор, и он не должен путать
    пользователей между разными штрафами одной задачи)."""
    task_repo, fine_repo = _make_repos(tmp_path)
    try:
        task = task_repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=42,
        )
        fine_repo.create(
            monitoring_task_id=task.id, car_number="AA001AA",
            external_fine_id="A1", fingerprint="fp-1",
            penalty_date=None, due_date=None, delivered_status="Не вручено",
            raw_data="{}",
        )
        fine_repo.create(
            monitoring_task_id=task.id, car_number="AA001AA",
            external_fine_id="A2", fingerprint="fp-2",
            penalty_date=None, due_date=None, delivered_status="Не вручено",
            raw_data="{}",
        )

        user_repository = _FakeUserRepository(
            {42: TelegramUserInfo(user_id=42, username="ivan_petrov", first_name=None, last_name=None)}
        )
        notification_service = _FakeNotificationService()
        coordinator = FineNotificationCoordinator(
            fine_repo, task_repo, notification_service, user_repository,
        )

        await coordinator.flush_pending()

        sent_events = notification_service.notify_calls[0]
        assert len(sent_events) == 2
        assert all(e.created_by_display == "@ivan_petrov" for e in sent_events)
    finally:
        task_repo.close()
        fine_repo.close()


async def test_created_by_display_is_none_when_monitoring_task_not_found(tmp_path):
    """Задача мониторинга не найдена (не должно случаться в норме — есть
    FOREIGN KEY на fine_monitoring_tasks(id), но репозиторий уже терпим к
    этому для label, см. _build_event) — created_by_display той же логике
    следует (None), а не падает с исключением."""
    task_repo, fine_repo = _make_repos(tmp_path)
    try:
        user_repository = _FakeUserRepository(
            {42: TelegramUserInfo(user_id=42, username="ivan_petrov", first_name=None, last_name=None)}
        )
        coordinator = FineNotificationCoordinator(
            fine_repo, task_repo, _FakeNotificationService(), user_repository,
        )

        orphan_fine = DetectedFine(
            id=1, monitoring_task_id=999999, car_number="AA001AA",
            external_fine_id="A1", fingerprint="fp-1",
            penalty_date=None, due_date=None, delivered_status="Не вручено",
            raw_data="{}", first_detected_at=datetime.now(), last_seen_at=datetime.now(),
            notification_sent_at=None,
        )

        event = coordinator._build_event(orphan_fine)

        assert event.label is None
        assert event.created_by_display is None
    finally:
        task_repo.close()
        fine_repo.close()
