"""Чистые функции валидации команды "fine add" — без Telegram, без
Repository, без сети. Полностью детерминированы, тестируются напрямую.
"""

from datetime import date, datetime, timedelta
import re

from reader.fines.models import FineMonitoringTask

_CAR_NUMBER_RE = re.compile(r"^[A-Z0-9]+$")
_DATE_FORMAT = "%d.%m.%Y"
_DATE_FORMAT_HINT = "DD.MM.YYYY"
_DEFAULT_PERIOD_DAYS = 30


class FineValidationError(Exception):
    """Ожидаемая ошибка валидации команды — её сообщение показывается
    оператору как есть (см. reader/commands/fine.py)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def normalize_car_number(raw: str) -> str:
    normalized = raw.strip().upper()

    if not normalized:
        raise FineValidationError("Номер автомобиля не может быть пустым")

    if not _CAR_NUMBER_RE.match(normalized):
        raise FineValidationError(
            f'Неверный формат номера: "{raw}" — разрешены только буквы и цифры'
        )

    return normalized


def parse_date(raw: str) -> date:
    try:
        return datetime.strptime(raw.strip(), _DATE_FORMAT).date()
    except ValueError:
        raise FineValidationError(
            f'Неверный формат даты: "{raw}" — используйте {_DATE_FORMAT_HINT}'
        ) from None


def resolve_monitoring_period(
    start_raw: str | None,
    end_raw: str | None,
    *,
    today: date,
) -> tuple[date, date]:
    """Обе даты не заданы — период по умолчанию (today..today+30). Заданы
    обе — парсим и проверяем, что end не раньше start. Задана только одна —
    ошибка (нет смысла молча достраивать вторую)."""
    if start_raw is None and end_raw is None:
        return today, today + timedelta(days=_DEFAULT_PERIOD_DAYS)

    if start_raw is None or end_raw is None:
        raise FineValidationError("Укажите обе даты (START_DATE и END_DATE) или ни одной")

    start_date = parse_date(start_raw)
    end_date = parse_date(end_raw)

    if end_date < start_date:
        raise FineValidationError(
            f"END_DATE ({end_raw}) не может быть раньше START_DATE ({start_raw})"
        )

    return start_date, end_date


def find_overlapping_task(
    start_date: date,
    end_date: date,
    existing_tasks: list[FineMonitoringTask],
) -> FineMonitoringTask | None:
    """Первая (в порядке существующего списка) активная задача из
    existing_tasks, чей период пересекается с [start_date, end_date], либо
    None, если пересечений нет. existing_tasks — уже отфильтрованные по
    номеру и статусу 'active' задачи (см.
    FineMonitoringTaskRepository.get_active_by_car_number()).

    Общая, не бросающая исключение проверка — используется и
    validate_no_overlap() ниже (оператору нужна ошибка), и
    reader/commands/fine.py (сценарий "номер уже на мониторинге, обогатить
    существующую запись владельцем" — там нужен сам факт пересечения, а не
    исключение, см. задачу)."""
    for task in existing_tasks:
        if start_date <= task.end_date and task.start_date <= end_date:
            return task
    return None


def validate_no_overlap(
    start_date: date,
    end_date: date,
    existing_tasks: list[FineMonitoringTask],
) -> None:
    """existing_tasks — уже отфильтрованные по номеру и статусу 'active'
    задачи (см. FineMonitoringTaskRepository.get_active_by_car_number)."""
    task = find_overlapping_task(start_date, end_date, existing_tasks)
    if task is not None:
        raise FineValidationError(
            f"Для этого номера уже есть активная задача (ID {task.id}), "
            f"период {task.start_date.strftime(_DATE_FORMAT)}"
            f"–{task.end_date.strftime(_DATE_FORMAT)}, пересекающийся с указанным"
        )
