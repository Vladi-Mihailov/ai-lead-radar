"""Общая точка "доставить всё, что ещё не доставлено оператору" —
используется и FineJob (по расписанию), и FineCommand.check (по требованию
оператора, команда "fine check"), чтобы не дублировать retry-логику дважды.
Ничего не знает про Telegram — только про NotificationService (интерфейс).
"""

from typing import Protocol

from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import FineMonitoringTask, NewFineEvent
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.notifications.base import NotificationResult, NotificationService
from reader.users.models import TelegramUserInfo


class UserLookupLike(Protocol):
    """Ровно то, что нужно FineNotificationCoordinator от UserRepository
    (reader/users/repository.py) — не импортируем сам класс, чтобы не тянуть
    его целиком (sqlite и т.п.) в тесты, которым нужен только фейк. Тот же
    приём, что и InviterService.UserAccessHashUpdaterLike
    (reader/inviter/service.py)."""

    def get(self, user_id: int) -> TelegramUserInfo | None: ...


def format_user_display(user: TelegramUserInfo | None, user_id: int) -> str:
    """"@username", "Имя Фамилия (@username)", "Имя Фамилия (ID N)" или,
    если о пользователе ничего не известно (нет UserRepository, пользователь
    не найден, ни username, ни имени нет), — "ID N". Уведомление остаётся
    полезным в любом случае: id мониторинга гарантированно известен.

    Публичная функция (не только для этого модуля) — тем же форматом
    пользуется и reader/commands/fine.py (fine check), чтобы не заводить
    вторую реализацию того же самого fallback."""
    if user is None:
        return f"ID {user_id}"

    full_name = user.full_name
    username = user.username

    if full_name and username:
        return f"{full_name} (@{username})"
    if full_name:
        return f"{full_name} (ID {user_id})"
    if username:
        return f"@{username}"
    return f"ID {user_id}"


class FineNotificationCoordinator:
    def __init__(
        self,
        detected_fine_repository: DetectedFineRepository,
        task_repository: FineMonitoringTaskRepository,
        notification_service: NotificationService,
        user_repository: UserLookupLike | None = None,
    ):
        self._detected_fine_repository = detected_fine_repository
        self._task_repository = task_repository
        self._notification_service = notification_service
        # None — как и everywhere в этом проекте (см. InviterService) —
        # означает "функциональность недоступна", а не ошибку: без
        # UserRepository уведомление всё равно покажет "Telegram: ID N"
        # (created_by_user_id всегда есть на самой задаче мониторинга).
        self._user_repository = user_repository

    async def flush_pending(self) -> NotificationResult:
        """Штрафы с notification_sent_at IS NULL — свежесозданные в этом же
        проходе и оставшиеся с прошлых неудачных попыток одновременно (один
        и тот же признак, отдельной ветки для "повтора" не требуется)."""
        pending = self._detected_fine_repository.list_pending_notifications()
        if not pending:
            return NotificationResult(delivered_event_ids=[], failed_event_ids=[])

        events = [self._build_event(fine) for fine in pending]

        result = await self._notification_service.notify(events)

        for fine_id in result.delivered_event_ids:
            self._detected_fine_repository.mark_notification_sent(fine_id)
        # failed_event_ids: notification_sent_at остаётся NULL — эти же
        # штрафы снова попадут в list_pending_notifications() на следующем
        # вызове flush_pending(), без какой-либо отдельной логики повтора.

        return result

    def _build_event(self, fine) -> NewFineEvent:
        task = self._task_repository.get(fine.monitoring_task_id)
        return NewFineEvent.from_detected_fine(
            fine,
            label=task.label if task is not None else None,
            created_by_display=self._created_by_display(task) if task is not None else None,
        )

    def _created_by_display(self, task: FineMonitoringTask) -> str:
        user = (
            self._user_repository.get(task.created_by_user_id)
            if self._user_repository is not None
            else None
        )
        return format_user_display(user, task.created_by_user_id)
