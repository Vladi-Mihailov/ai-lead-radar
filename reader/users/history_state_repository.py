import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history_sync_state (
    chat_id INTEGER PRIMARY KEY,
    chat_name TEXT,
    oldest_processed_message_id INTEGER,
    oldest_processed_date TIMESTAMP,
    processed_messages INTEGER NOT NULL DEFAULT 0,
    saved_users INTEGER NOT NULL DEFAULT 0,
    history_completed INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP
)
"""

_SAVE = """
INSERT INTO history_sync_state (
    chat_id, chat_name, oldest_processed_message_id, oldest_processed_date,
    processed_messages, saved_users, history_completed, updated_at
) VALUES (
    :chat_id, :chat_name, :oldest_processed_message_id, :oldest_processed_date,
    :processed_messages, :saved_users, :history_completed, CURRENT_TIMESTAMP
)
ON CONFLICT(chat_id) DO UPDATE SET
    chat_name=excluded.chat_name,
    oldest_processed_message_id=excluded.oldest_processed_message_id,
    oldest_processed_date=excluded.oldest_processed_date,
    processed_messages=excluded.processed_messages,
    saved_users=excluded.saved_users,
    history_completed=excluded.history_completed,
    updated_at=CURRENT_TIMESTAMP
"""

_SELECT = """
SELECT chat_id, chat_name, oldest_processed_message_id, oldest_processed_date,
       processed_messages, saved_users, history_completed
FROM history_sync_state WHERE chat_id = ?
"""


@dataclass(frozen=True)
class HistorySyncCheckpoint:
    chat_id: int
    chat_name: str | None
    oldest_processed_message_id: int | None
    oldest_processed_date: datetime | None
    processed_messages: int
    saved_users: int
    history_completed: bool


class HistorySyncStateRepository:
    """Чекпоинты инкрементальной синхронизации истории по группам (SQLite)."""

    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # FULL (не NORMAL, как в UserRepository): чекпоинты пишутся редко
        # (раз в CHECKPOINT_INTERVAL сообщений), поэтому стоимость fsync на
        # commit пренебрежима, а именно эта таблица — источник истины после
        # аварийного завершения процесса.
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def get(self, chat_id: int) -> HistorySyncCheckpoint | None:
        row = self._conn.execute(_SELECT, (chat_id,)).fetchone()
        if row is None:
            return None

        (
            chat_id,
            chat_name,
            oldest_processed_message_id,
            oldest_processed_date,
            processed_messages,
            saved_users,
            history_completed,
        ) = row

        return HistorySyncCheckpoint(
            chat_id=chat_id,
            chat_name=chat_name,
            oldest_processed_message_id=oldest_processed_message_id,
            oldest_processed_date=(
                datetime.fromisoformat(oldest_processed_date) if oldest_processed_date else None
            ),
            processed_messages=processed_messages,
            saved_users=saved_users,
            history_completed=bool(history_completed),
        )

    def save_progress(
        self,
        *,
        chat_id: int,
        chat_name: str | None,
        oldest_processed_message_id: int | None,
        oldest_processed_date: datetime | None,
        processed_messages: int,
        saved_users: int,
        history_completed: bool,
    ) -> None:
        self._conn.execute(
            _SAVE,
            {
                "chat_id": chat_id,
                "chat_name": chat_name,
                "oldest_processed_message_id": oldest_processed_message_id,
                "oldest_processed_date": (
                    oldest_processed_date.isoformat() if oldest_processed_date else None
                ),
                "processed_messages": processed_messages,
                "saved_users": saved_users,
                "history_completed": int(history_completed),
            },
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
