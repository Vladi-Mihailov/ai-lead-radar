"""TurkeyCheckRepository — turkey_fine_checks поверх SQLite.

Append-only журнал ЗАВЕРШЁННЫХ одноразовых проверок (см. design report
Stage 3) — НЕ используется для восстановления состояния GIB-сессии после
рестарта (это архитектурно невозможно, см.
reader/turkey_bot/live_session_registry.py) и НЕ используется для принятия
решений внутри самого диалога — только для support/отладки/аудита (сколько
проверок, с каким исходом, сколько попыток CAPTCHA потребовалось).

raw_response/gib_message_text — САНИТИЗИРОВАННЫЙ (см. задачу) вывод
GibSubmitOutcome: ТОЛЬКО messages/raw_data, НИКОГДА captcha_code (его
здесь просто нет ни в одном параметре) и НИКОГДА cookies/заголовки запроса
(session.py их сюда и не передаёт)."""

import sqlite3
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turkey_fine_checks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id  INTEGER NOT NULL,
    telegram_chat_id  INTEGER NOT NULL,
    plate             TEXT NOT NULL,
    requested_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    captcha_attempts  INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,
    gib_message_text  TEXT,
    raw_response      TEXT
)
"""

_INSERT = """
INSERT INTO turkey_fine_checks (
    telegram_user_id, telegram_chat_id, plate, captcha_attempts,
    status, gib_message_text, raw_response
) VALUES (:telegram_user_id, :telegram_chat_id, :plate, :captcha_attempts,
          :status, :gib_message_text, :raw_response)
"""


class TurkeyCheckRepository:
    def __init__(self, db_path: Path | str):
        is_memory = db_path == ":memory:"
        self._path = db_path if is_memory else Path(db_path)
        if not is_memory:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        if not is_memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def record_result(
        self,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        plate: str,
        captcha_attempts: int,
        status: str,
        gib_message_text: str | None,
        raw_response: str | None,
    ) -> None:
        self._conn.execute(
            _INSERT,
            {
                "telegram_user_id": telegram_user_id,
                "telegram_chat_id": telegram_chat_id,
                "plate": plate,
                "captcha_attempts": captcha_attempts,
                "status": status,
                "gib_message_text": gib_message_text,
                "raw_response": raw_response,
            },
        )
        self._conn.commit()

    def count_total(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM turkey_fine_checks").fetchone()[0]

    def count_by_status(self, status: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM turkey_fine_checks WHERE status = ?", (status,),
        ).fetchone()
        return row[0]

    def count_since(self, since: datetime) -> int:
        """Сколько проверок ЗАВЕРШИЛОСЬ, начиная с since — по requested_at
        (см. модуль docstring: несмотря на имя, эта колонка на самом деле
        проставляется в момент ЗАВЕРШЕНИЯ проверки, см. conversation.py::
        _finish/record_result — единственный вызывающий код, вызываемый
        ТОЛЬКО для терминальных исходов no_debt/has_debt/unexpected/error,
        никогда для CAPTCHA/rejected/незавершённых диалогов, см. задачу:
        "Do not count CAPTCHA creation/refresh or incomplete conversations
        as a completed check" — это уже так структурно, без специальной
        фильтрации здесь). since — aware datetime, сравнивается как
        текстовая ISO-строка того же формата, что и SQLite
        CURRENT_TIMESTAMP (тот же приём, что и BotKnownUsersRepository::
        count_first_seen_since)."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM turkey_fine_checks WHERE requested_at >= ?",
            (since.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchone()
        return row[0]

    def close(self) -> None:
        self._conn.close()
