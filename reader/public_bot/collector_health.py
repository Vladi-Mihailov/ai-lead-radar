"""Предупреждения группе менеджеров @GEShtrafbot (settings.public_bot.
support_chat_id — та же группа, что и у manager_alerts.py) о состоянии
REDNET-коллектора общего списка police.ge (см. "Georgia remote collector
architecture (REDNET)", reader/georgia_collector/collector_job.py::
FeedPollOutcome).

Коллектор после каждого цикла присылает POST /internal/georgia/health:
status "ok" (обход завершён), "recovering" (после простоя упёрся в лимит
страниц; следующий цикл продолжит с того же места, ничего не пропуская) или
"error". Раньше простой коллектора дольше ~1,5 суток проходил молча; теперь
человек узнаёт о нём двумя путями:
- сторож (check_silence): отчётов нет дольше silence_threshold (13 ч —
  больше самого длинного перерыва между циклами, 23:55 -> 09:00) — одно
  предупреждение, и "✅", когда отчёты возобновятся. Взводится только
  первым отчётом после старта процесса, поэтому выкладка бота раньше
  обновлённого коллектора (который отчётов ещё не шлёт) ложной тревоги не
  даёт;
- observe(): первый отчёт не "ok" — предупреждение; пока не "ok" —
  напоминание не чаще раза в reminder_interval (12 ч); снова "ok" после
  предупреждения — "✅".

Состояние только в памяти: рестарт бота его сбрасывает. Только текст и
состояние — без Telegram (отправка — reader/public_bot/collector_api.py).
Номеров в отчёте коллектора нет (режим, счётчики, даты); detail на всякий
случай обрезается и чистится от похожего на госномер."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from reader.time_display import format_tbilisi

DEFAULT_REMINDER_INTERVAL = timedelta(hours=12)
DEFAULT_SILENCE_THRESHOLD = timedelta(hours=13)

_TITLE = "Сборщик штрафов REDNET (общий список police.ge)"
_DETAIL_MAX_LENGTH = 300
_PLATE_LIKE_RE = re.compile(r"\b[A-Z]{2}-?\d{3}-?[A-Z]{2}\b", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_date(value) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _clean_detail(value) -> str:
    if not isinstance(value, str):
        return ""
    text = _PLATE_LIKE_RE.sub("<номер>", _WHITESPACE_RE.sub(" ", value).strip())
    return text if len(text) <= _DETAIL_MAX_LENGTH else text[: _DETAIL_MAX_LENGTH - 1] + "…"


def format_problem_alert(payload: dict, *, reminder: bool) -> str:
    """"recovering" — сколько прочитано и до какой даты нужно дочитать;
    остальное — общее "цикл не завершён" с подробностями коллектора."""
    prefix = "⏰ Напоминание. " if reminder else ""
    last_success = _parse_datetime(payload.get("last_success_at"))
    last_line = (
        f"Последний полный обход: {format_tbilisi(last_success)}." if last_success
        else "Полного обхода ещё не было."
    )
    if payload.get("status") == "recovering":
        lines = [f"{prefix}⚠️ {_TITLE} догоняет список после перерыва.", last_line]
        oldest = _parse_date(payload.get("oldest_publish_date"))
        horizon = _parse_date(payload.get("horizon_date"))
        if oldest is not None and horizon is not None:
            lines.append(
                f"Этот цикл: {payload.get('pages')} стр., дошёл до публикаций за {oldest:%d.%m.%Y}; "
                f"догнать нужно до публикаций за {horizon:%d.%m.%Y}."
            )
        lines.append("Следующие циклы продолжат с того же места, ничего не пропуская.")
    else:
        lines = [f"{prefix}⚠️ {_TITLE}: цикл обхода не завершён.", last_line]
    detail = _clean_detail(payload.get("detail"))
    if detail:
        lines.append(f"Подробности: {detail}")
    return "\n".join(lines)


class CollectorHealthMonitor:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utc_now,
        reminder_interval: timedelta = DEFAULT_REMINDER_INTERVAL,
        silence_threshold: timedelta = DEFAULT_SILENCE_THRESHOLD,
    ):
        self._clock = clock
        self._reminder_interval = reminder_interval
        self._silence_threshold = silence_threshold
        self._last_report_at: datetime | None = None
        # Когда отправлено (последнее) предупреждение о проблеме; None — его
        # нет или проблема уже прошла.
        self._problem_alerted_at: datetime | None = None
        self._silence_alerted = False

    def observe(self, payload: dict) -> str | None:
        """Отчёт коллектора -> текст для менеджеров или None (ничего
        отправлять не нужно)."""
        now = self._clock()
        messages: list[str] = []
        if self._silence_alerted:
            self._silence_alerted = False
            messages.append(f"✅ {_TITLE}: снова на связи.")
        self._last_report_at = now
        if payload.get("status") == "ok":
            if self._problem_alerted_at is not None:
                self._problem_alerted_at = None
                messages.append(f"✅ {_TITLE}: обход снова завершается успешно.")
        elif self._problem_alerted_at is None or now - self._problem_alerted_at >= self._reminder_interval:
            reminder = self._problem_alerted_at is not None
            self._problem_alerted_at = now
            messages.append(format_problem_alert(payload, reminder=reminder))
        return "\n\n".join(messages) or None

    def check_silence(self) -> str | None:
        """Сторож: текст одного предупреждения, когда отчётов нет дольше
        silence_threshold, иначе None (до первого отчёта — всегда None)."""
        if self._last_report_at is None or self._silence_alerted:
            return None
        silent_for = self._clock() - self._last_report_at
        if silent_for <= self._silence_threshold:
            return None
        self._silence_alerted = True
        return (
            f"⚠️ {_TITLE}: нет отчётов {int(silent_for.total_seconds() // 3600)} ч "
            f"(последний — {format_tbilisi(self._last_report_at)}).\n"
            "Пока он молчит, новые штрафы из общего списка не находятся; вернувшись, он сам дочитает "
            "пропущенное. Проверьте сервис georgia-collector на REDNET."
        )
