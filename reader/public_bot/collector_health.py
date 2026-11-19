"""Предупреждения группе менеджеров @GEShtrafbot (settings.public_bot.
support_chat_id — та же группа, что и у manager_alerts.py) о состоянии
REDNET-коллектора общего списка police.ge (см. "Georgia remote collector
architecture (REDNET)", reader/georgia_collector/collector_job.py).

Коллектор после каждого цикла присылает POST /internal/georgia/health
(reader/fines/public_feed_traversal.py::TraversalOutcome.to_payload: status,
режим, счётчики, даты, last_success_at — без номеров и квитанций): status
"ok" (обход завершён), "recovering" (после простоя упёрся в лимит страниц;
прочитанное сохранено, следующий цикл продолжит, ничего не пропуская) или
"error" (сбой цикла; открытый circuit breaker police.ge — mode "skipped").
Человек узнаёт о проблеме двумя путями:
- observe(): "recovering" — сразу (был долгий простой); "error" — со
  второго отчёта не "ok" подряд (одиночный сбой, который следующий цикл сам
  исправит, не тревожит; отчёт, повторённый клиентом коллектора после
  таймаута, — тот же cycle_at — вторым не считается); пока проблема
  держится — напоминание не чаще раза в reminder_interval (12 ч); снова
  "ok" — "✅", но только если предупреждение было доставлено;
- сторож (check_silence): отчётов нет дольше silence_threshold (13 ч —
  больше самого длинного перерыва между циклами, 23:55 -> 09:00) —
  предупреждение и напоминания раз в reminder_interval; отчёты
  возобновились — "✅" (тоже только после доставленного предупреждения).
  Время последнего отчёта хранится в БД, поэтому рестарт бота сторожа не
  сбрасывает; до самого первого отчёта сторож не взведён (выкладка бота
  раньше обновлённого коллектора ложной тревоги не даёт).

Состояние и очередь предупреждений — в production-БД (CollectorHealthStore,
таблицы georgia_collector_health и georgia_collector_alert_outbox).
deliver(send) отправляет очередь по порядку; доставленным предупреждение
считается только после того, как send (Telegram) его принял; не принял —
повтор с растущей паузой (до 30 мин), а следующие в очереди ждут, чтобы
"✅" не обогнал предупреждение. Ещё не отправленное предупреждение
обновляется свежим отчётом, а не дублируется, и отменяется, если проблема
прошла раньше, чем его удалось доставить. Только текст и состояние — без
Telegram (send передаёт reader/public_bot/collector_api.py).

Тексты на русском; номеров в отчёте коллектора нет, а техническая причина
сбоя на всякий случай обрезается и чистится от похожего на госномер или
номер протокола."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from reader.time_display import format_tbilisi

logger = logging.getLogger(__name__)

DEFAULT_REMINDER_INTERVAL = timedelta(hours=12)
DEFAULT_SILENCE_THRESHOLD = timedelta(hours=13)
# Сколько ждать ответа Telegram на одно предупреждение: зависший вызов не
# держит очередь (и её блокировку) вечно — это просто неудачная попытка.
DEFAULT_SEND_TIMEOUT = timedelta(seconds=60)
# Пауза перед повтором недоставленного предупреждения: 1, 2, 4, ... мин, не больше 30.
RETRY_BASE_DELAY = timedelta(minutes=1)
RETRY_MAX_DELAY = timedelta(minutes=30)
# Доставленные записи очереди хранятся столько (для разбора), потом удаляются.
_SENT_RETENTION = timedelta(days=30)

_TITLE = "Сборщик штрафов REDNET (общий список police.ge)"
_DETAIL_MAX_LENGTH = 300
_PLATE_LIKE_RE = re.compile(r"\b[A-Z]{2}-?\d{3}-?[A-Z]{2}\b", re.IGNORECASE)
# Любое "слово" с пятью и больше цифрами подряд (номер протокола, квитанции).
_LONG_NUMBER_RE = re.compile(r"\b\w*\d{5,}\w*\b")
_WHITESPACE_RE = re.compile(r"\s+")

# Записи очереди: предупреждение по теме и "✅" по ней.
_PROBLEM = "problem"
_PROBLEM_RESOLVED = "problem_resolved"
_SILENCE = "silence"
_SILENCE_RESOLVED = "silence_resolved"
_RESOLVED_TEXT = {
    _PROBLEM: f"✅ {_TITLE}: обход снова завершается успешно.",
    _SILENCE: f"✅ {_TITLE}: снова на связи.",
}

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS georgia_collector_health (
        id                 INTEGER PRIMARY KEY CHECK (id = 1),
        last_report_at     TIMESTAMP,
        last_status        TEXT,
        last_cycle_at      TEXT,
        non_ok_streak      INTEGER NOT NULL DEFAULT 0,
        problem_kind       TEXT,
        problem_alerted_at TIMESTAMP,
        silence_alerted_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS georgia_collector_alert_outbox (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        kind            TEXT NOT NULL,
        text            TEXT NOT NULL,
        created_at      TIMESTAMP NOT NULL,
        attempts        INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMP NOT NULL,
        sent_at         TIMESTAMP
    )
    """,
)


_ADDED_HEALTH_COLUMNS = (
    ("problem_kind", "TEXT"),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _parse_date(value) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _clean_detail(value) -> str:
    if not isinstance(value, str):
        return ""
    text = _WHITESPACE_RE.sub(" ", value).strip()
    text = _LONG_NUMBER_RE.sub("<номер>", _PLATE_LIKE_RE.sub("<номер>", text))
    return text if len(text) <= _DETAIL_MAX_LENGTH else text[: _DETAIL_MAX_LENGTH - 1] + "…"


def _last_success_line(payload: dict) -> str | None:
    if "last_success_at" not in payload:
        # Коллектор старой версии время не присылает.
        return None
    last_success = _parse_datetime(payload.get("last_success_at"))
    if last_success is None:
        return "Полного обхода ещё не было."
    return f"Последний полный обход: {format_tbilisi(last_success)}."


def format_problem_alert(payload: dict, *, reminder: bool, failures_in_a_row: int = 1) -> str:
    """"recovering" — сколько прочитано и до какой даты нужно дочитать;
    "skipped" — police.ge недоступен; остальное — "цикл не завершён" с
    технической причиной."""
    prefix = "⏰ Напоминание. " if reminder else ""
    status = payload.get("status")
    if status == "recovering":
        lines = [f"{prefix}⚠️ {_TITLE} догоняет список после перерыва."]
    elif payload.get("mode") == "skipped":
        lines = [f"{prefix}⚠️ {_TITLE}: police.ge недоступен (сработала защита от сбоев), циклы пропускаются."]
    else:
        lines = [f"{prefix}⚠️ {_TITLE}: цикл обхода не завершён."]
    last_line = _last_success_line(payload)
    if last_line:
        lines.append(last_line)
    if status == "recovering":
        oldest = _parse_date(payload.get("oldest_publish_date"))
        horizon = _parse_date(payload.get("horizon_date"))
        if oldest is not None and horizon is not None:
            lines.append(
                f"Этот цикл: {payload.get('pages')} стр., дошёл до публикаций за {oldest:%d.%m.%Y}; "
                f"догнать нужно до публикаций за {horizon:%d.%m.%Y}."
            )
        rows = payload.get("backlog_rows_read")
        if isinstance(rows, int) and rows > 0:
            lines.append(f"Уже прочитано и сохранено строк списка: {rows}.")
        lines.append("Следующие циклы продолжат с этого места, ничего не пропуская.")
    else:
        if failures_in_a_row > 1:
            lines.append(f"Неудачных циклов подряд: {failures_in_a_row}.")
        detail = _clean_detail(payload.get("detail"))
        if detail and payload.get("mode") != "skipped":
            lines.append(f"Техническая причина: {detail}")
        rows = payload.get("backlog_rows_read")
        if isinstance(rows, int) and rows > 0:
            lines.append("Прочитанное сохранено — следующий цикл продолжит с этого места.")
    return "\n".join(lines)


def format_silence_alert(last_report_at: datetime, now: datetime, *, reminder: bool) -> str:
    prefix = "⏰ Напоминание. " if reminder else ""
    hours = int((now - last_report_at).total_seconds() // 3600)
    return (
        f"{prefix}⚠️ {_TITLE}: нет отчётов {hours} ч (последний — {format_tbilisi(last_report_at)}).\n"
        "Пока он молчит, новые штрафы из общего списка не находятся; вернувшись, он сам дочитает "
        "пропущенное. Проверьте сервис georgia-collector на REDNET."
    )


@dataclass(frozen=True)
class HealthState:
    last_report_at: datetime | None = None
    last_status: str | None = None
    # cycle_at последнего учтённого отчёта (время цикла коллектора).
    last_cycle_at: str | None = None
    non_ok_streak: int = 0
    # Вид проблемы последнего предупреждения ("recovering", "error", "skipped"):
    # другой вид — новое предупреждение сразу, а не напоминание через 12 ч.
    problem_kind: str | None = None
    # Когда ДОСТАВЛЕНО (последнее) предупреждение по теме; None — не было или тема закрыта.
    problem_alerted_at: datetime | None = None
    silence_alerted_at: datetime | None = None


@dataclass(frozen=True)
class QueuedAlert:
    id: int
    kind: str
    text: str
    attempts: int
    next_attempt_at: datetime


class CollectorHealthStore:
    """Состояние монитора и очередь предупреждений (SQLite, production-БД
    settings.app.users_db_file). Методы не коммитят сами — вызывающий код
    объединяет их в transaction()."""

    def __init__(self, db_path: Path | str):
        self._conn = sqlite3.connect(db_path)
        for statement in _SCHEMA:
            self._conn.execute(statement)
        # Колонки, добавленные после первого варианта таблицы (24be82a): на
        # уже созданной таблице их добавляет открытие хранилища.
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(georgia_collector_health)")}
        for name, column_type in _ADDED_HEALTH_COLUMNS:
            if name not in existing:
                try:
                    self._conn.execute(f"ALTER TABLE georgia_collector_health ADD COLUMN {name} {column_type}")
                except sqlite3.OperationalError as exc:
                    # Другой процесс добавил её между PRAGMA и ALTER.
                    if "duplicate column name" not in str(exc).lower():
                        raise
        self._conn.execute("INSERT OR IGNORE INTO georgia_collector_health (id) VALUES (1)")
        self._conn.commit()

    def transaction(self):
        return self._conn

    def load(self) -> HealthState:
        row = self._conn.execute(
            "SELECT last_report_at, last_status, last_cycle_at, non_ok_streak, problem_kind, problem_alerted_at, "
            "silence_alerted_at FROM georgia_collector_health WHERE id = 1"
        ).fetchone()
        last_report_at, last_status, last_cycle_at, streak, problem_kind, problem_at, silence_at = row
        return HealthState(
            last_report_at=_parse_datetime(last_report_at), last_status=last_status, last_cycle_at=last_cycle_at,
            non_ok_streak=streak or 0, problem_kind=problem_kind, problem_alerted_at=_parse_datetime(problem_at),
            silence_alerted_at=_parse_datetime(silence_at),
        )

    def save(self, state: HealthState) -> None:
        self._conn.execute(
            "UPDATE georgia_collector_health SET last_report_at = ?, last_status = ?, last_cycle_at = ?, "
            "non_ok_streak = ?, problem_kind = ?, problem_alerted_at = ?, silence_alerted_at = ? WHERE id = 1",
            (_iso(state.last_report_at), state.last_status, state.last_cycle_at, state.non_ok_streak,
             state.problem_kind, _iso(state.problem_alerted_at), _iso(state.silence_alerted_at)),
        )

    def queued(self) -> list[QueuedAlert]:
        rows = self._conn.execute(
            "SELECT id, kind, text, attempts, next_attempt_at FROM georgia_collector_alert_outbox "
            "WHERE sent_at IS NULL ORDER BY id"
        ).fetchall()
        return [QueuedAlert(id=r[0], kind=r[1], text=r[2], attempts=r[3], next_attempt_at=_parse_datetime(r[4]))
                for r in rows]

    def queued_of(self, kind: str) -> QueuedAlert | None:
        return next((alert for alert in self.queued() if alert.kind == kind), None)

    def enqueue(self, kind: str, text: str, *, at: datetime) -> None:
        self._conn.execute(
            "INSERT INTO georgia_collector_alert_outbox (kind, text, created_at, next_attempt_at) VALUES (?, ?, ?, ?)",
            (kind, text, at.isoformat(), at.isoformat()),
        )

    def update_text(self, alert_id: int, text: str) -> None:
        self._conn.execute("UPDATE georgia_collector_alert_outbox SET text = ? WHERE id = ?", (text, alert_id))

    def cancel(self, kind: str) -> None:
        self._conn.execute("DELETE FROM georgia_collector_alert_outbox WHERE kind = ? AND sent_at IS NULL", (kind,))

    def mark_sent(self, alert_id: int, *, at: datetime) -> bool:
        """False — записи уже нет: её отменили, пока она отправлялась."""
        updated = self._conn.execute(
            "UPDATE georgia_collector_alert_outbox SET sent_at = ?, attempts = attempts + 1 WHERE id = ?",
            (at.isoformat(), alert_id),
        ).rowcount
        self._conn.execute(
            "DELETE FROM georgia_collector_alert_outbox WHERE sent_at IS NOT NULL AND sent_at < ?",
            ((at - _SENT_RETENTION).isoformat(),),
        )
        return updated == 1

    def mark_failed(self, alert_id: int, *, next_attempt_at: datetime) -> None:
        self._conn.execute(
            "UPDATE georgia_collector_alert_outbox SET attempts = attempts + 1, next_attempt_at = ? WHERE id = ?",
            (next_attempt_at.isoformat(), alert_id),
        )

    def close(self) -> None:
        self._conn.close()


def retry_delay(attempts: int) -> timedelta:
    """Пауза после attempts-й неудачной попытки доставки."""
    return min(RETRY_BASE_DELAY * 2 ** min(max(attempts - 1, 0), 10), RETRY_MAX_DELAY)


class CollectorHealthMonitor:
    def __init__(
        self,
        store: CollectorHealthStore | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
        reminder_interval: timedelta = DEFAULT_REMINDER_INTERVAL,
        silence_threshold: timedelta = DEFAULT_SILENCE_THRESHOLD,
        send_timeout: timedelta = DEFAULT_SEND_TIMEOUT,
    ):
        # Без хранилища — только в памяти процесса (тесты, отладка).
        self._store = store or CollectorHealthStore(":memory:")
        self._clock = clock
        self._reminder_interval = reminder_interval
        self._silence_threshold = silence_threshold
        self._send_timeout = send_timeout
        self._delivery_lock = asyncio.Lock()

    @property
    def store(self) -> CollectorHealthStore:
        return self._store

    def observe(self, payload: dict) -> None:
        """Отчёт коллектора: обновляет состояние и ставит в очередь
        предупреждения (отправка — deliver())."""
        now = self._clock()
        store = self._store
        with store.transaction():
            state = store.load()
            cycle_at = payload.get("cycle_at")
            cycle_at = cycle_at if isinstance(cycle_at, str) and cycle_at else None
            if cycle_at is not None and cycle_at == state.last_cycle_at:
                # Тот же отчёт ещё раз (клиент коллектора повторил POST после
                # таймаута) — уже учтён, вторым неудачным циклом не считается.
                return
            state = self._close_topic(replace(state, last_cycle_at=cycle_at), _SILENCE, _RESOLVED_TEXT[_SILENCE], now)
            status = payload.get("status")
            if status == "ok":
                line = _last_success_line(payload)
                text = _RESOLVED_TEXT[_PROBLEM] + (f"\n{line}" if line else "")
                state = self._close_topic(replace(state, non_ok_streak=0), _PROBLEM, text, now)
            else:
                streak = state.non_ok_streak + 1
                state = replace(state, non_ok_streak=streak)
                if status == "recovering" or streak >= 2:
                    kind = "skipped" if payload.get("mode") == "skipped" else str(status)
                    queued = store.queued_of(_PROBLEM)
                    # Другой вид проблемы ("догоняет" -> "цикл не завершён") —
                    # новое предупреждение сразу, а не напоминание через 12 ч.
                    changed = state.problem_kind is not None and kind != state.problem_kind
                    reminder = state.problem_alerted_at is not None and not changed
                    text = format_problem_alert(payload, reminder=reminder, failures_in_a_row=streak)
                    if queued is not None:
                        store.update_text(queued.id, text)
                        state = replace(state, problem_kind=kind)
                    elif not reminder or now - state.problem_alerted_at >= self._reminder_interval:
                        store.enqueue(_PROBLEM, text, at=now)
                        state = replace(state, problem_kind=kind)
            store.save(replace(state, last_report_at=now, last_status=status))

    def check_silence(self) -> None:
        """Сторож: отчётов нет дольше silence_threshold — предупреждение (и
        напоминания) в очередь. До самого первого отчёта не взведён."""
        now = self._clock()
        store = self._store
        with store.transaction():
            state = store.load()
            if state.last_report_at is None or now - state.last_report_at <= self._silence_threshold:
                return
            queued = store.queued_of(_SILENCE)
            reminder = state.silence_alerted_at is not None
            text = format_silence_alert(state.last_report_at, now, reminder=reminder)
            if queued is not None:
                store.update_text(queued.id, text)
            elif not reminder or now - state.silence_alerted_at >= self._reminder_interval:
                store.enqueue(_SILENCE, text, at=now)

    def _close_topic(self, state: HealthState, topic: str, resolved_text: str, now: datetime) -> HealthState:
        """Проблема прошла: недоставленное предупреждение отменяется, а если
        оно было доставлено — в очередь "✅"."""
        self._store.cancel(topic)
        if topic == _PROBLEM:
            state = replace(state, problem_kind=None)
        alerted_field = f"{topic}_alerted_at"
        if getattr(state, alerted_field) is None:
            return state
        self._store.enqueue(f"{topic}_resolved", resolved_text, at=now)
        return replace(state, **{alerted_field: None})

    async def deliver(self, send: Callable[[str], Awaitable[None]]) -> int:
        """Отправляет очередь по порядку: send(text) бросает исключение —
        не доставлено, повтор позже, следующие ждут. Возвращает, сколько
        доставлено."""
        delivered = 0
        async with self._delivery_lock:
            for alert in self._store.queued():
                now = self._clock()
                if alert.next_attempt_at is not None and alert.next_attempt_at > now:
                    break
                try:
                    await asyncio.wait_for(send(alert.text), timeout=self._send_timeout.total_seconds())
                except Exception as exc:  # noqa: BLE001 — любой сбой отправки (и таймаут) = повтор позже
                    delay = retry_delay(alert.attempts + 1)
                    logger.warning(
                        "Georgia collector alert not delivered (attempt %d, %s), next try in %d s",
                        alert.attempts + 1, type(exc).__name__, int(delay.total_seconds()),
                    )
                    with self._store.transaction():
                        self._store.mark_failed(alert.id, next_attempt_at=now + delay)
                    break
                sent_at = self._clock()
                with self._store.transaction():
                    if self._store.mark_sent(alert.id, at=sent_at):
                        if alert.kind in (_PROBLEM, _SILENCE):
                            state = self._store.load()
                            self._store.save(replace(state, **{f"{alert.kind}_alerted_at": sent_at}))
                    elif alert.kind in (_PROBLEM, _SILENCE):
                        # Отменено, пока отправлялось (проблема уже прошла), а
                        # Telegram сообщение принял: сразу и "✅", доставленным
                        # предупреждение не считается.
                        self._store.enqueue(f"{alert.kind}_resolved", _RESOLVED_TEXT[alert.kind], at=sent_at)
                delivered += 1
        return delivered
