"""Общая точка "доставить всё, что ещё не доставлено оператору" —
используется и FineJob (по расписанию), и FineCommand.check (по требованию
оператора, команда "fine check"), чтобы не дублировать retry-логику дважды.
Ничего не знает про Telegram — только про NotificationService (интерфейс).
"""

from datetime import datetime, timezone
from typing import Protocol

from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import FineMonitoringTask, NewFineEvent
from reader.fines.notification_control import EXCLUSION_KNOWN_BEFORE_BOUNDARY, NotificationControlState
from reader.fines.suppression import is_fine_output_suppressed
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


class NotificationControlLike(Protocol):
    """Ровно то, что нужно отсюда от GeorgiaNotificationControl (см.
    reader/fines/notification_control.py)."""

    def state(self) -> NotificationControlState: ...

    def known_before(
        self, boundary_at: datetime, *, car_number: str, protocol_no: str | None,
        row_first_detected_at: datetime | None,
    ) -> bool: ...


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
        notification_control: NotificationControlLike | None = None,
    ):
        # None — пауза и граница уведомлений Грузии не ведутся (прежнее
        # поведение, см. reader/fines/notification_control.py).
        self._notification_control = notification_control
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
        control = self._notification_control.state() if self._notification_control is not None else None
        if control is not None and control.paused:
            # Пауза уведомлений Грузии: ничего не отправляется и ничего не
            # отмечается — после паузы накопленное явно исключается.
            return NotificationResult(delivered_event_ids=[], failed_event_ids=[])
        pending = self._detected_fine_repository.list_pending_notifications()
        # Denylisted car_number (см. reader/fines/suppression.py) —
        # исключён здесь на случай, если для него уже существует
        # detected_fines-строка с notification_sent_at IS NULL, созданная
        # ДО появления denylist'а (FineCheckService больше не создаёт новых
        # таких строк вовсе, см. check_service.py, но старые не удаляются,
        # см. задачу "не изменять historical records"). Такая строка
        # остаётся pending навсегда — безвредно, просто никогда не
        # отправляется ни одному оператору.
        pending = [fine for fine in pending if not is_fine_output_suppressed(fine.car_number)]
        if control is not None and control.boundary_at is not None:
            # Квитанция, известная с до границы включения уведомлений, — не
            # новый штраф (повторное обнаружение, другая задача номера,
            # подтверждённая позже ⏳): явно исключается, не отправляется.
            known = [fine for fine in pending if self._known_before(fine, control.boundary_at)]
            if known:
                self._detected_fine_repository.exclude_from_operator_notifications(
                    [fine.id for fine in known], reason=EXCLUSION_KNOWN_BEFORE_BOUNDARY,
                )
                known_ids = {fine.id for fine in known}
                pending = [fine for fine in pending if fine.id not in known_ids]
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

    def exclude_pending(self, *, reason: str, dry_run: bool = False) -> int:
        """Явно исключает из операторской рассылки всё, что сейчас ждёт
        отправки (см. scripts/georgia_notifications.py resume). Строки
        скрытых номеров (denylist) не трогает — их и так никогда не
        отправляют. dry_run — только подсчёт."""
        pending = [
            fine for fine in self._detected_fine_repository.list_pending_notifications()
            if not is_fine_output_suppressed(fine.car_number)
        ]
        if dry_run:
            return len(pending)
        return self._detected_fine_repository.exclude_from_operator_notifications(
            [fine.id for fine in pending], reason=reason,
        )

    def _known_before(self, fine, boundary_at: datetime) -> bool:
        first_detected_at = fine.first_detected_at
        if first_detected_at is not None and first_detected_at.tzinfo is not None:
            first_detected_at = first_detected_at.astimezone(timezone.utc).replace(tzinfo=None)
        return self._notification_control.known_before(
            boundary_at, car_number=fine.car_number, protocol_no=fine.external_fine_id,
            row_first_detected_at=first_detected_at,
        )

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
