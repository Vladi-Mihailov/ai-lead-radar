"""Единая точка конвертации/форматирования datetime для показа человеку
(операторские Telegram-сообщения, CLI-вывод) — целевая timezone Asia/Tbilisi
(см. задачу). Внутреннее хранение и сравнение datetime (БД, blocked_until,
любая timezone-aware бизнес-логика) НИГДЕ не меняется — это остаётся в UTC
везде, как и раньше; здесь только текст, который видит оператор.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TBILISI_TZ = ZoneInfo("Asia/Tbilisi")

_DEFAULT_FORMAT = "%Y-%m-%d %H:%M"
_DEFAULT_SUFFIX = "по Тбилиси"


def to_tbilisi(value: datetime) -> datetime:
    """value (обычно aware UTC — как everywhere в проекте, см.
    datetime.now(timezone.utc)) -> тот же момент времени в Asia/Tbilisi.

    Naive datetime (без tzinfo) трактуется как UTC ЯВНО, а не как уже
    локальное время — так фактически приходят некоторые поля через SQLite
    CURRENT_TIMESTAMP (например FineMonitoringTask.last_checked_at/
    created_at/updated_at, см. reader/fines/task_repository.py — ISO-строка
    без offset, но по факту это UTC, как и остальные timestamps проекта).
    Молчаливая трактовка naive-значения как локального дала бы неверный
    результат именно для таких полей — поэтому здесь всегда UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(TBILISI_TZ)


def format_tbilisi(
    value: datetime, *, fmt: str = _DEFAULT_FORMAT, suffix: str | None = _DEFAULT_SUFFIX,
) -> str:
    """to_tbilisi(value).strftime(fmt) + необязательный суффикс ("по
    Тбилиси" по умолчанию, suffix=None — без него) — заменяет прежний
    hand-rolled "... UTC" в операторских сообщениях инвайтера (см.
    reader/inviter/service.py/manage.py)."""
    text = to_tbilisi(value).strftime(fmt)
    return f"{text} {suffix}" if suffix else text
