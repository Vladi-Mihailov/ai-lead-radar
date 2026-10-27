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

# Визуальное соответствие кириллических букв на российских гос. номерах их
# латинским аналогам (см. design report "исправить Georgian bot: госномер
# кириллицей не проходит validation") — ТА ЖЕ таблица, что и в
# reader/turkey_bot/validation.py::normalize_plate, продублирована здесь
# намеренно, а не импортирована оттуда: Turkey-бот explicitly не должен
# импортироваться из reader/fines/* (см. reader/turkey_bot/main.py про
# независимость процессов), а reader/fines/* — общий, используемый и
# reader/commands/fine.py, и reader/public_bot/*, и reader/users/*, не
# должен зависеть от конкретного отдельного бота. Только ЭТИ 12 букв — не
# произвольная кириллица (тот же принцип, что и в Turkey-реализации).
_CYRILLIC_TO_LATIN = str.maketrans(
    {
        "А": "A",
        "В": "B",
        "Е": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "У": "Y",
        "Х": "X",
    }
)


class FineValidationError(Exception):
    """Ожидаемая ошибка валидации команды — её сообщение показывается
    оператору как есть (см. reader/commands/fine.py)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _normalize_candidate(raw: str) -> str:
    """Общий первый шаг и для normalize_car_number(), и для
    looks_like_car_number() ниже — ЧИСТАЯ нормализация, без решения о
    валидности: uppercase, удалить пробелы/дефисы, перевести
    поддерживаемую кириллицу в латиницу (см. _CYRILLIC_TO_LATIN выше).
    Буква, отсутствующая среди поддерживаемых 12, остаётся кириллической
    после .translate() — вызывающий код сам решает, что с этим делать
    (normalize_car_number — отклоняет, looks_like_car_number — тоже
    отклоняет implicitly, не матчится _CAR_NUMBER_RE)."""
    normalized = raw.strip().upper().replace(" ", "").replace("-", "")
    return normalized.translate(_CYRILLIC_TO_LATIN)


# "Выглядит как номер" (см. задачу "Improve the /start UX... Allow direct
# plate input after /start") — НАМЕРЕННО строже голого _CAR_NUMBER_RE
# (который принял бы и "HELLO", и "12345" — это ВАЛИДНЫЙ формат номера по
# принятым правилам, но явно НЕ похоже на реальный номер и hijack'нуло бы
# обычный текст/слова). Реальные номера (см. тесты/примеры по всему
# проекту: O127PX62, M295YB196, AA001AA, E911EE95) ВСЕГДА смешивают буквы
# и цифры и имеют длину примерно 5–10 символов — этого достаточно, чтобы
# отличить "похоже, попытка ввести номер" от "обычное сообщение", не
# будучи второй, отдельной реализацией валидации формата (та остаётся
# ТОЛЬКО в normalize_car_number/_CAR_NUMBER_RE — эта функция только решает
# ВЫЗЫВАТЬ ли её, никогда не заменяет её результат)."""
_PLATE_LIKE_RE = re.compile(r"^(?=.{5,10}$)(?=.*[A-Z])(?=.*[0-9])[A-Z0-9]+$")


def looks_like_car_number(raw: str) -> bool:
    """True — похоже на попытку ввести номер (см. _PLATE_LIKE_RE докстрок).
    НЕ гарантирует, что normalize_car_number(raw) не бросит
    FineValidationError дальше (например, при длине на границе диапазона
    с недопустимым символом) — вызывающий код (reader/public_bot/
    conversation.py::handle_text()) всё равно обязан вызвать
    normalize_car_number()/обработать её ошибку как обычно, эта функция
    только РЕШАЕТ, стоит ли вообще пробовать."""
    return bool(_PLATE_LIKE_RE.match(_normalize_candidate(raw)))


def normalize_car_number(raw: str) -> str:
    """См. _normalize_candidate() про сам порядок нормализации."""
    normalized = _normalize_candidate(raw)

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
