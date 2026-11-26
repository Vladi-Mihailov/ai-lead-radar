"""Общие таблицы MAX-ботов в том же SQLite-файле (settings.app.users_db_file),
что и у Telegram-ботов, — только новые таблицы с префиксом max_, существующие
не меняются. bot — "ge" или "tr": у двух MAX-ботов разные пользователи,
диалоги и дедуп (один и тот же MAX user_id может писать обоим).

- max_bot_users — кто писал боту (MAX позволяет писать первым только тем,
  кто начал диалог), имя/username для менеджеров, blocked_at после
  bot_stopped или отказа доставки;
- max_conversation_state — шаг пошагового диалога (как у Telegram-ботов);
- max_processed_updates — ключи уже принятых апдейтов (повторная доставка
  webhook не обрабатывается второй раз);
- max_link_codes / max_account_links — безопасная привязка MAX <-> Telegram
  по одноразовому коду (см. reader/max_bot/linking.py).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reader.max_bot.updates import MaxUser

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS max_bot_users (
        bot           TEXT NOT NULL,
        max_user_id   INTEGER NOT NULL,
        chat_id       INTEGER,
        first_name    TEXT,
        last_name     TEXT,
        username      TEXT,
        first_seen_at TIMESTAMP NOT NULL,
        last_seen_at  TIMESTAMP NOT NULL,
        blocked_at    TIMESTAMP,
        PRIMARY KEY (bot, max_user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS max_conversation_state (
        bot          TEXT NOT NULL,
        max_user_id  INTEGER NOT NULL,
        step         TEXT NOT NULL,
        payload      TEXT,
        updated_at   TIMESTAMP NOT NULL,
        PRIMARY KEY (bot, max_user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS max_processed_updates (
        bot          TEXT NOT NULL,
        dedup_key    TEXT NOT NULL,
        received_at  TIMESTAMP NOT NULL,
        PRIMARY KEY (bot, dedup_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_max_processed_updates_received ON max_processed_updates (received_at)",
    """
    CREATE TABLE IF NOT EXISTS max_link_codes (
        code_hash    TEXT PRIMARY KEY,
        bot          TEXT NOT NULL,
        max_user_id  INTEGER NOT NULL,
        created_at   TIMESTAMP NOT NULL,
        expires_at   TIMESTAMP NOT NULL,
        used_at      TIMESTAMP,
        used_by_telegram_user_id INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS max_link_attempts (
        telegram_user_id INTEGER NOT NULL,
        attempted_at     TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS max_account_links (
        bot              TEXT NOT NULL,
        max_user_id      INTEGER NOT NULL,
        telegram_user_id INTEGER NOT NULL,
        linked_at        TIMESTAMP NOT NULL,
        PRIMARY KEY (bot, max_user_id)
    )
    """,
)

# Ключи апдейтов храним дольше окна повторной доставки MAX (8 часов).
PROCESSED_UPDATES_RETENTION = timedelta(days=3)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fmt_ts(value: datetime) -> str:
    """Naive-UTC ISO, как CURRENT_TIMESTAMP SQLite в остальном проекте."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(sep=" ", timespec="seconds")


def parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@dataclass(frozen=True)
class MaxKnownUser:
    bot: str
    max_user_id: int
    chat_id: int | None
    first_name: str | None
    last_name: str | None
    username: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    blocked_at: datetime | None


@dataclass(frozen=True)
class MaxConversationState:
    step: str
    payload: dict
    updated_at: datetime


class MaxBotStorage:
    def __init__(self, db_path: Path | str, *, bot: str):
        if bot not in ("ge", "tr"):
            raise ValueError("bot должен быть 'ge' или 'tr'")
        self.bot = bot
        self._conn = connect(db_path)
        for statement in _SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- дедуп апдейтов ----

    def claim_update(self, dedup_key: str, *, now: datetime | None = None) -> bool:
        """True — апдейт новый и теперь помечен принятым; False — уже был
        (повторная доставка webhook / повтор после рестарта)."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO max_processed_updates (bot, dedup_key, received_at) VALUES (?, ?, ?)",
            (self.bot, dedup_key, fmt_ts(now or _now())),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def purge_processed_updates(self, *, now: datetime | None = None) -> int:
        cutoff = fmt_ts((now or _now()) - PROCESSED_UPDATES_RETENTION)
        cursor = self._conn.execute(
            "DELETE FROM max_processed_updates WHERE bot = ? AND received_at < ?", (self.bot, cutoff),
        )
        self._conn.commit()
        return cursor.rowcount

    # ---- пользователи ----

    def record_user(self, user: MaxUser, *, chat_id: int | None, now: datetime | None = None) -> bool:
        """True — пользователь пишет этому боту впервые. Любое входящее
        событие снимает отметку blocked_at: пользователь снова в диалоге."""
        ts = fmt_ts(now or _now())
        is_new = self.get_user(user.user_id) is None
        self._conn.execute(
            """
            INSERT INTO max_bot_users
                (bot, max_user_id, chat_id, first_name, last_name, username, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (bot, max_user_id) DO UPDATE SET
                chat_id = COALESCE(excluded.chat_id, chat_id),
                first_name = COALESCE(excluded.first_name, first_name),
                last_name = COALESCE(excluded.last_name, last_name),
                username = COALESCE(excluded.username, username),
                last_seen_at = excluded.last_seen_at,
                blocked_at = NULL
            """,
            (self.bot, user.user_id, chat_id, user.first_name, user.last_name, user.username, ts, ts),
        )
        self._conn.commit()
        return is_new

    def mark_blocked(self, max_user_id: int, *, now: datetime | None = None) -> None:
        self._conn.execute(
            "UPDATE max_bot_users SET blocked_at = ? WHERE bot = ? AND max_user_id = ?",
            (fmt_ts(now or _now()), self.bot, max_user_id),
        )
        self._conn.commit()

    def get_user(self, max_user_id: int) -> MaxKnownUser | None:
        row = self._conn.execute(
            "SELECT bot, max_user_id, chat_id, first_name, last_name, username, first_seen_at, last_seen_at, "
            "blocked_at FROM max_bot_users WHERE bot = ? AND max_user_id = ?",
            (self.bot, max_user_id),
        ).fetchone()
        if row is None:
            return None
        return MaxKnownUser(
            bot=row[0], max_user_id=row[1], chat_id=row[2], first_name=row[3], last_name=row[4],
            username=row[5], first_seen_at=parse_ts(row[6]), last_seen_at=parse_ts(row[7]),
            blocked_at=parse_ts(row[8]),
        )

    def count_users(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM max_bot_users WHERE bot = ?", (self.bot,)).fetchone()[0]

    # ---- диалоги ----

    def get_state(self, max_user_id: int) -> MaxConversationState | None:
        row = self._conn.execute(
            "SELECT step, payload, updated_at FROM max_conversation_state WHERE bot = ? AND max_user_id = ?",
            (self.bot, max_user_id),
        ).fetchone()
        if row is None:
            return None
        return MaxConversationState(step=row[0], payload=json.loads(row[1]) if row[1] else {},
                                    updated_at=parse_ts(row[2]))

    def set_state(self, max_user_id: int, step: str, payload: dict | None = None) -> None:
        self._conn.execute(
            """
            INSERT INTO max_conversation_state (bot, max_user_id, step, payload, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (bot, max_user_id) DO UPDATE SET
                step = excluded.step, payload = excluded.payload, updated_at = excluded.updated_at
            """,
            (self.bot, max_user_id, step, json.dumps(payload or {}, ensure_ascii=False), fmt_ts(_now())),
        )
        self._conn.commit()

    def clear_state(self, max_user_id: int) -> None:
        self._conn.execute(
            "DELETE FROM max_conversation_state WHERE bot = ? AND max_user_id = ?", (self.bot, max_user_id),
        )
        self._conn.commit()

    # ---- привязка к Telegram ----

    @property
    def connection(self) -> sqlite3.Connection:
        """Для reader/max_bot/linking.py (те же таблицы, та же транзакционность)."""
        return self._conn
