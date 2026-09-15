"""TurkeyTollCheckRepository — turkey_toll_checks поверх SQLite.

Структурная копия reader/turkey_bot/check_repository.py::
TurkeyCheckRepository (GIB) — та же append-only журнал ЗАВЕРШЁННЫХ
одноразовых проверок, ТОЛЬКО для support/отладки/аудита, НЕ используется
для восстановления состояния живой сессии после рестарта (см.
reader/turkey_bot/avrasya/live_session_registry.py) и НЕ используется
внутри самого диалога для принятия решений.

ОТДЕЛЬНАЯ, additive таблица (см. design report Stage 1: "Prefer an
additive design and do not modify existing GİB historical data") — ни
разу не трогает turkey_fine_checks и его строки. `provider` — задел на
будущие toll-провайдеры, кроме Avrasya (см. design report: "design the
internal architecture so additional toll-road providers can be added
later") — сейчас всегда 'avrasya' (единственный существующий), но
колонка уже есть, чтобы не потребовалась вторая миграция/вторая таблица
для второго провайдера.

raw_response — САНИТИЗИРОВАННЫЙ (см. conversation.py::
_sanitize_avrasya_outcome_for_storage) вывод AvrasyaSubmitOutcome:
messages/raw_data/status_code, НИКОГДА captcha_code (его здесь просто нет
ни в одном параметре) и НИКОГДА cookies/заголовки запроса (avrasya/
session.py их сюда и не передаёт)."""

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turkey_toll_checks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id  INTEGER NOT NULL,
    telegram_chat_id  INTEGER NOT NULL,
    provider          TEXT NOT NULL DEFAULT 'avrasya',
    plate             TEXT NOT NULL,
    requested_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    captcha_attempts  INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,
    message_text      TEXT,
    raw_response      TEXT
)
"""

_INSERT = """
INSERT INTO turkey_toll_checks (
    telegram_user_id, telegram_chat_id, provider, plate, captcha_attempts,
    status, message_text, raw_response
) VALUES (:telegram_user_id, :telegram_chat_id, :provider, :plate,
          :captcha_attempts, :status, :message_text, :raw_response)
"""


class TurkeyTollCheckRepository:
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
        provider: str = "avrasya",
        plate: str,
        captcha_attempts: int,
        status: str,
        message_text: str | None,
        raw_response: str | None,
    ) -> None:
        self._conn.execute(
            _INSERT,
            {
                "telegram_user_id": telegram_user_id,
                "telegram_chat_id": telegram_chat_id,
                "provider": provider,
                "plate": plate,
                "captcha_attempts": captcha_attempts,
                "status": status,
                "message_text": message_text,
                "raw_response": raw_response,
            },
        )
        self._conn.commit()

    def count_total(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM turkey_toll_checks").fetchone()[0]

    def count_by_status(self, status: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM turkey_toll_checks WHERE status = ?", (status,),
        ).fetchone()
        return row[0]

    def close(self) -> None:
        self._conn.close()
