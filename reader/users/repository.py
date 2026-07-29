import sqlite3
from pathlib import Path

from reader.users.models import TelegramUserInfo

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    is_bot INTEGER,
    last_seen_at TIMESTAMP,
    updated_at TIMESTAMP
)
"""

_UPSERT = """
INSERT INTO users (user_id, username, first_name, last_name, is_bot, last_seen_at, updated_at)
VALUES (:user_id, :username, :first_name, :last_name, :is_bot, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT(user_id) DO UPDATE SET
    username=COALESCE(excluded.username, users.username),
    first_name=COALESCE(excluded.first_name, users.first_name),
    last_name=COALESCE(excluded.last_name, users.last_name),
    is_bot=excluded.is_bot,
    last_seen_at=CURRENT_TIMESTAMP,
    updated_at=CURRENT_TIMESTAMP
"""

_SELECT = "SELECT user_id, username, first_name, last_name, is_bot FROM users WHERE user_id = ?"


class UserRepository:
    """Локальный кэш пользователей Telegram (username/имя) поверх SQLite."""

    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def upsert(self, user: TelegramUserInfo) -> None:
        self._conn.execute(
            _UPSERT,
            {
                "user_id": user.user_id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "is_bot": int(user.is_bot),
            },
        )
        self._conn.commit()

    def get(self, user_id: int) -> TelegramUserInfo | None:
        row = self._conn.execute(_SELECT, (user_id,)).fetchone()
        if row is None:
            return None

        user_id, username, first_name, last_name, is_bot = row
        return TelegramUserInfo(
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            is_bot=bool(is_bot),
        )

    def count(self) -> int:
        """Общее количество пользователей в локальном кэше (SELECT COUNT(*))."""
        return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def close(self) -> None:
        self._conn.close()
