"""TurkeyConversationStateRepository — turkey_bot_conversation_state
поверх SQLite. Тот же приём, что и
reader/public_bot/conversation_state_repository.py (см. design report
Stage 3: "Turkey — новый отдельный процесс/приложение", НЕ переиспользует
таблицу Георгии) — собственная таблица, собственный класс, ничего общего
кроме одинаковой формы схемы.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from reader.turkey_bot_test.models import ConversationState

_MEMORY_DB = ":memory:"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turkey_bot_conversation_state (
    chat_id           INTEGER PRIMARY KEY,
    telegram_user_id  INTEGER NOT NULL,
    step              TEXT NOT NULL,
    payload           TEXT,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_UPSERT = """
INSERT INTO turkey_bot_conversation_state (chat_id, telegram_user_id, step, payload, updated_at)
VALUES (:chat_id, :telegram_user_id, :step, :payload, CURRENT_TIMESTAMP)
ON CONFLICT(chat_id) DO UPDATE SET
    telegram_user_id = excluded.telegram_user_id,
    step = excluded.step,
    payload = excluded.payload,
    updated_at = CURRENT_TIMESTAMP
"""

_SELECT = (
    "SELECT chat_id, telegram_user_id, step, payload, updated_at "
    "FROM turkey_bot_conversation_state WHERE chat_id = ?"
)

_DELETE = "DELETE FROM turkey_bot_conversation_state WHERE chat_id = ?"


def _row_to_state(row) -> ConversationState:
    chat_id, telegram_user_id, step, payload_raw, updated_at = row
    return ConversationState(
        chat_id=chat_id,
        telegram_user_id=telegram_user_id,
        step=step,
        payload=json.loads(payload_raw) if payload_raw else None,
        updated_at=datetime.fromisoformat(updated_at),
    )


class TurkeyConversationStateRepository:
    def __init__(self, db_path: Path | str):
        is_memory = db_path == _MEMORY_DB
        self._path = db_path if is_memory else Path(db_path)
        if not is_memory:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        if not is_memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def get(self, chat_id: int) -> ConversationState | None:
        row = self._conn.execute(_SELECT, (chat_id,)).fetchone()
        return _row_to_state(row) if row else None

    def set(
        self, chat_id: int, *, telegram_user_id: int, step: str, payload: dict | None = None,
    ) -> ConversationState:
        """Полностью заменяет состояние диалога этого chat_id (upsert)."""
        self._conn.execute(
            _UPSERT,
            {
                "chat_id": chat_id,
                "telegram_user_id": telegram_user_id,
                "step": step,
                "payload": json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            },
        )
        self._conn.commit()

        state = self.get(chat_id)
        if state is None:
            raise RuntimeError("Не удалось прочитать только что записанное состояние диалога")
        return state

    def clear(self, chat_id: int) -> None:
        """Не бросает исключение, если состояния и так не было."""
        self._conn.execute(_DELETE, (chat_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
