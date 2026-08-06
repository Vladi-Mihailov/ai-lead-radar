import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_INCREMENTAL_MODE = "incremental"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history_sync_state (
    chat_id INTEGER NOT NULL,
    chat_name TEXT,
    oldest_processed_message_id INTEGER,
    oldest_processed_date TIMESTAMP,
    processed_messages INTEGER NOT NULL DEFAULT 0,
    saved_users INTEGER NOT NULL DEFAULT 0,
    history_completed INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'incremental',
    updated_at TIMESTAMP,
    PRIMARY KEY (chat_id, mode)
)
"""

_SAVE = """
INSERT INTO history_sync_state (
    chat_id, chat_name, oldest_processed_message_id, oldest_processed_date,
    processed_messages, saved_users, history_completed, mode, updated_at
) VALUES (
    :chat_id, :chat_name, :oldest_processed_message_id, :oldest_processed_date,
    :processed_messages, :saved_users, :history_completed, :mode, CURRENT_TIMESTAMP
)
ON CONFLICT(chat_id, mode) DO UPDATE SET
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
       processed_messages, saved_users, history_completed, mode
FROM history_sync_state WHERE chat_id = ? AND mode = ?
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
    mode: str = _INCREMENTAL_MODE


class HistorySyncStateRepository:
    """Чекпоинты синхронизации истории по группам (SQLite).

    Инкрементальный режим (mode="incremental", по умолчанию) и --reindex
    (mode="reindex") ведут независимый прогресс по одной и той же группе —
    ключ таблицы (chat_id, mode), а не просто chat_id. Это позволяет
    прерванному --reindex продолжаться с места остановки при следующем
    запуске, не трогая и не завися от обычного checkpoint.
    """

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
        self._migrate_legacy_schema()
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def _migrate_legacy_schema(self) -> None:
        """Старые базы имели PRIMARY KEY только по chat_id (без mode) — под
        такой схемой нельзя хранить второй ряд для того же chat_id (нужен для
        независимого reindex-checkpoint), поэтому таблицу нужно пересоздать с
        новым составным ключом, перенеся существующие данные как
        mode='incremental' (обычный checkpoint не должен измениться)."""
        table_exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_sync_state'"
        ).fetchone()
        if not table_exists:
            return

        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(history_sync_state)")}
        if "mode" in columns:
            return

        self._conn.execute("ALTER TABLE history_sync_state RENAME TO history_sync_state_legacy")
        self._conn.execute(_SCHEMA)
        self._conn.execute(
            """
            INSERT INTO history_sync_state (
                chat_id, chat_name, oldest_processed_message_id, oldest_processed_date,
                processed_messages, saved_users, history_completed, mode, updated_at
            )
            SELECT
                chat_id, chat_name, oldest_processed_message_id, oldest_processed_date,
                processed_messages, saved_users, history_completed, 'incremental', updated_at
            FROM history_sync_state_legacy
            """
        )
        self._conn.execute("DROP TABLE history_sync_state_legacy")
        self._conn.commit()

    def get(self, chat_id: int, mode: str = _INCREMENTAL_MODE) -> HistorySyncCheckpoint | None:
        row = self._conn.execute(_SELECT, (chat_id, mode)).fetchone()
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
            mode,
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
            mode=mode,
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
        mode: str = _INCREMENTAL_MODE,
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
                "mode": mode,
            },
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
