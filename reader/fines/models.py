from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

FineTaskStatus = Literal["active", "completed", "stopped"]


@dataclass(frozen=True)
class FineMonitoringTask:
    id: int
    car_number: str
    label: str | None
    start_date: date
    end_date: date
    status: FineTaskStatus
    telegram_chat_id: int
    created_by_user_id: int
    created_at: datetime
    updated_at: datetime
    last_checked_at: datetime | None
    last_check_status: str | None
    last_error: str | None
    # Архивный режим (см. reader/jobs/archive_fine_job.py) — независим от
    # status: задача остаётся 'completed' (не попадает в list_active()/
    # обычные 3 проверки в день), пока archive_check_enabled=1 не выставлен
    # явно (FineJob при завершении периода — для новых задач, либо разовым
    # enrollment — для существующих, см. reader/fines/archive_enrollment.py).
    # Default'ы сохраняют старое поведение для существующих вызовов
    # FineMonitoringTask(...) (см. tests/test_fine_validation.py) без правки.
    archive_check_enabled: bool = False
    next_archive_check_at: datetime | None = None


@dataclass(frozen=True)
class DetectedFine:
    id: int
    monitoring_task_id: int
    car_number: str
    external_fine_id: str | None
    fingerprint: str
    penalty_date: date | None
    due_date: date | None
    delivered_status: str | None
    raw_data: str
    first_detected_at: datetime
    last_seen_at: datetime
    notification_sent_at: datetime | None


@dataclass(frozen=True)
class CarFineStats:
    """Одна строка статистики fine stats — сколько штрафов опубликовано
    по конкретному автомобилю (detected_fines, сгруппированные по
    car_number)."""

    car_number: str
    fine_count: int


@dataclass(frozen=True)
class ParsedFineRecord:
    """Одна запись из ответа police.ge (или другого будущего FineProvider),
    уже приведённая к доменному виду — независимо от того, как именно
    источник называет свои поля."""

    car_number: str
    external_fine_id: str | None
    penalty_date: date | None
    due_date: date | None
    delivered_status: str
    fingerprint: str
    raw_data: dict[str, Any]


@dataclass(frozen=True)
class NewFineEvent:
    """Штраф, требующий уведомления оператора — либо только что обнаруженный
    FineCheckService, либо ожидающий повторной отправки после прошлой
    неудачи (см. DetectedFineRepository.list_pending_notifications()).
    Никакой Telegram-специфики здесь нет.

    detected_fine_id — id строки в detected_fines: по нему NotificationService
    сообщает, какие события доставлены, а FineNotificationCoordinator
    отмечает notification_sent_at только для реально доставленных.
    """

    detected_fine_id: int
    task_id: int
    car_number: str
    label: str | None
    external_fine_id: str | None
    penalty_date: date | None
    due_date: date | None
    delivered_status: str | None
    # Готовая для показа строка вида "@ivan_petrov"/"Иван Петров (@ivan_petrov)"/
    # "не найден"/"@ivan_petrov, @another_user" (несколько владельцев одного
    # car_number — валидное состояние, см. format_car_owner_display) —
    # Telegram-ВЛАДЕЛЕЦ(Ы) автомобиля, определяемый по car_number -> users.car_numbers ->
    # UserRepository.find_by_car_number() (см. FineNotificationCoordinator).
    # Это НЕ fine_monitoring_tasks.created_by_user_id (тот, кто создал
    # задачу мониторинга) — раньше поле ошибочно показывало именно его,
    # см. задачу про production-баг. None, только если задачу мониторинга
    # вообще не удалось найти. Default сохраняет старое поведение для
    # существующих вызовов (например, FineCheckService.check_task(),
    # которому эта информация не нужна).
    car_owner_display: str | None = None

    @classmethod
    def from_detected_fine(
        cls, fine: DetectedFine, *, label: str | None, car_owner_display: str | None = None,
    ) -> "NewFineEvent":
        return cls(
            detected_fine_id=fine.id,
            task_id=fine.monitoring_task_id,
            car_number=fine.car_number,
            label=label,
            external_fine_id=fine.external_fine_id,
            penalty_date=fine.penalty_date,
            due_date=fine.due_date,
            delivered_status=fine.delivered_status,
            car_owner_display=car_owner_display,
        )


CheckStatus = Literal["ok", "error"]


@dataclass(frozen=True)
class CheckResult:
    status: CheckStatus
    new_fines: list[NewFineEvent]
    error_message: str | None
    total_fines_found: int
    duration_ms: int
