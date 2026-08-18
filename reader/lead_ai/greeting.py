"""Приветствие для первого сообщения AI follow-up (см.
reader/sinks/lead_ai_sink.py) — зависит от текущего времени в Asia/Tbilisi
(см. задачу), а не придумывается моделью: OpenAI не имеет надёжного
доступа к реальному текущему времени, поэтому reader/lead_ai/prompt.py
явно инструктирует модель НЕ включать приветствие в suggested_messages —
оно добавляется здесь, кодом, детерминированно."""

from datetime import datetime, timezone

from reader.time_display import to_tbilisi

# Границы — обычная бытовая договорённость (не строгий стандарт): ночь
# (23:00-04:59) объединена с "Добрый вечер", т.к. в задаче разрешено
# ровно три варианта (утро/день/вечер), без отдельного "ночь".
_MORNING_START_HOUR = 5
_DAY_START_HOUR = 12
_EVENING_START_HOUR = 18


def tbilisi_greeting(now: datetime | None = None) -> str:
    """now — момент времени (любая timezone, naive трактуется как UTC, см.
    to_tbilisi); по умолчанию — реальное текущее время. Параметр существует
    ради тестируемости (детерминированные тесты без ожидания реальных
    часов)."""
    moment = to_tbilisi(now if now is not None else datetime.now(timezone.utc))
    hour = moment.hour
    if _MORNING_START_HOUR <= hour < _DAY_START_HOUR:
        return "Доброе утро"
    if _DAY_START_HOUR <= hour < _EVENING_START_HOUR:
        return "Добрый день"
    return "Добрый вечер"
