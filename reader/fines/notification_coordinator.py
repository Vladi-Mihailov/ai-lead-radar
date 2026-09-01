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
    (reader/inviter/service.py).

    find_by_car_number(), а НЕ get(user_id) — Telegram-владелец автомобиля
    определяется по car_number -> users.car_numbers, а не по
    fine_monitoring_tasks.created_by_user_id (это другой человек — тот, кто
    создал задачу мониторинга, см. докстрок _car_owner_display)."""

    def find_by_car_number(self, car_number: str) -> list[TelegramUserInfo]: ...


def format_car_owner_display(users: list[TelegramUserInfo]) -> str:
    """"@username", "Имя Фамилия (@username)", "Имя Фамилия (ID N)" или
    "ID N" для одного владельца; для нескольких — то же самое для каждого,
    через ", " (см. задачу: один car_number может быть валидно связан
    сразу с несколькими Telegram-пользователями — это НЕ конфликт и не
    повод скрывать их имена за общей фразой).

    users пуст (car_number не встречался ни у одного пользователя, либо
    UserRepository вообще не передан) -> "не найден". Никогда не
    подставляем сюда чей-либо user_id "на всякий случай" — это была бы
    ложная информация о владельце (см. задачу про production-баг с
    created_by_user_id)."""
    if not users:
        return "не найден"
    return ", ".join(_format_single_owner(user) for user in users)


def _format_single_owner(user: TelegramUserInfo) -> str:
    full_name = user.full_name
    username = user.username

    if full_name and username:
        return f"{full_name} (@{username})"
    if full_name:
        return f"{full_name} (ID {user.user_id})"
    if username:
        return f"@{username}"
    return f"ID {user.user_id}"


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
        # UserRepository уведомление покажет "Telegram: не найден", а не
        # какой-либо (заведомо неверный) id.
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
            car_owner_display=self._car_owner_display(task.car_number) if task is not None else None,
        )

    def _car_owner_display(self, car_number: str) -> str:
        """Один car_number может быть валидно связан сразу с несколькими
        Telegram-пользователями (см. format_car_owner_display) — это
        обычное состояние, не повод для WARNING в логе."""
        if self._user_repository is None:
            return format_car_owner_display([])

        return format_car_owner_display(self._user_repository.find_by_car_number(car_number))
