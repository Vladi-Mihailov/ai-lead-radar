"""BotKnownUsersRepository — bot_known_users поверх SQLite.

Единственный источник истины "написал ли этот numeric Telegram user_id
боту хотя бы раз" (см. design report: Telegram не позволяет боту первым
писать пользователю, который никогда не начинал с ним диалог — резолв
@username в numeric id сам по себе НЕ гарантирует возможность что-либо
ему доставить). Обновляется на КАЖДОЕ входящее событие (текстовое
сообщение или нажатие inline-кнопки), независимо от его содержимого — см.
reader/public_bot/handlers.py.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from reader.public_bot.models import BotKnownUser

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_known_users (
    telegram_user_id  INTEGER PRIMARY KEY,
    telegram_chat_id  INTEGER NOT NULL,
    telegram_username TEXT,
    first_seen_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_UPSERT = """
INSERT INTO bot_known_users (telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at)
VALUES (:telegram_user_id, :telegram_chat_id, :telegram_username, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT(telegram_user_id) DO UPDATE SET
    telegram_chat_id = excluded.telegram_chat_id,
    telegram_username = COALESCE(excluded.telegram_username, bot_known_users.telegram_username),
    last_seen_at = CURRENT_TIMESTAMP
"""

_SELECT = (
    "SELECT telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at "
    "FROM bot_known_users WHERE telegram_user_id = ?"
)


def _row_to_known_user(row) -> BotKnownUser:
    telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at = row
    return BotKnownUser(
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        telegram_username=telegram_username,
        first_seen_at=datetime.fromisoformat(first_seen_at),
        last_seen_at=datetime.fromisoformat(last_seen_at),
    )


class BotKnownUsersRepository:
    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def record_seen(
        self, *, telegram_user_id: int, telegram_chat_id: int, telegram_username: str | None,
    ) -> None:
        """Идемпотентный upsert — безопасно вызывать на КАЖДОЕ входящее
        событие без проверки "а был ли он уже здесь" заранее."""
        self._conn.execute(
            _UPSERT,
            {
                "telegram_user_id": telegram_user_id,
                "telegram_chat_id": telegram_chat_id,
                "telegram_username": telegram_username,
            },
        )
        self._conn.commit()

    def is_known(self, telegram_user_id: int) -> bool:
        return self._conn.execute(_SELECT, (telegram_user_id,)).fetchone() is not None

    def get(self, telegram_user_id: int) -> BotKnownUser | None:
        row = self._conn.execute(_SELECT, (telegram_user_id,)).fetchone()
        return _row_to_known_user(row) if row else None

    def close(self) -> None:
        self._conn.close()
