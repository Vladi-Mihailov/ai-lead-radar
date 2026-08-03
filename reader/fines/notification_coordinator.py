"""Общая точка "доставить всё, что ещё не доставлено оператору" —
используется и FineJob (по расписанию), и FineCommand.check (по требованию
оператора, команда "fine check"), чтобы не дублировать retry-логику дважды.
Ничего не знает про Telegram — только про NotificationService (интерфейс).
"""

from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import NewFineEvent
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.notifications.base import NotificationResult, NotificationService


class FineNotificationCoordinator:
    def __init__(
        self,
        detected_fine_repository: DetectedFineRepository,
        task_repository: FineMonitoringTaskRepository,
        notification_service: NotificationService,
    ):
        self._detected_fine_repository = detected_fine_repository
        self._task_repository = task_repository
        self._notification_service = notification_service

    async def flush_pending(self) -> NotificationResult:
        """Штрафы с notification_sent_at IS NULL — свежесозданные в этом же
        проходе и оставшиеся с прошлых неудачных попыток одновременно (один
        и тот же признак, отдельной ветки для "повтора" не требуется)."""
        pending = self._detected_fine_repository.list_pending_notifications()
        if not pending:
            return NotificationResult(delivered_event_ids=[], failed_event_ids=[])

        events = [
            NewFineEvent.from_detected_fine(fine, label=self._task_label(fine.monitoring_task_id))
            for fine in pending
        ]

        result = await self._notification_service.notify(events)

        for fine_id in result.delivered_event_ids:
            self._detected_fine_repository.mark_notification_sent(fine_id)
        # failed_event_ids: notification_sent_at остаётся NULL — эти же
        # штрафы снова попадут в list_pending_notifications() на следующем
        # вызове flush_pending(), без какой-либо отдельной логики повтора.

        return result

    def _task_label(self, task_id: int) -> str | None:
        task = self._task_repository.get(task_id)
        return task.label if task is not None else None
