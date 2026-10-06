"""Живой диалог пользователя @ProtocolGEbot/@ProtocolTRbot с оператором —
persistent-состояние в общем users.db (тот же стиль, что у остальных
репозиториев ботов: WAL, CREATE TABLE IF NOT EXISTS при открытии).

- protocol_support_dialogs — один диалог; ключ — bot_key ('ge'/'tr') +
  telegram_user_id; открытым (status='open') может быть максимум один на
  этот ключ (частичный UNIQUE-индекс). Один человек может одновременно иметь
  GE- и TR-диалог.
- protocol_support_messages — сопоставление сообщений группы менеджеров с
  диалогом: по нему ответ менеджера (Telegram Reply) находит пользователя
  после любого рестарта. Ни телефона, ни медиа-файлов здесь нет."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
DIRECTION_USER_TO_MANAGER = "user_to_manager"
DIRECTION_MANAGER_TO_USER = "manager_to_user"
CLOSED_BY_USER = "user"
CLOSED_BY_MANAGER = "manager"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS protocol_support_dialogs (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_key                  TEXT NOT NULL,
        telegram_user_id         INTEGER NOT NULL,
        username                 TEXT,
        display_name             TEXT,
        support_chat_id          INTEGER NOT NULL,
        support_thread_id        INTEGER,
        root_manager_message_id  INTEGER,
        status                   TEXT NOT NULL,
        closed_by                TEXT,
        created_at               TEXT NOT NULL,
        updated_at               TEXT NOT NULL,
        closed_at                TEXT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_protocol_support_open_dialog "
    "ON protocol_support_dialogs (bot_key, telegram_user_id) WHERE status = 'open'",
    """
    CREATE TABLE IF NOT EXISTS protocol_support_messages (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        dialog_id                 INTEGER NOT NULL REFERENCES protocol_support_dialogs(id),
        support_chat_id           INTEGER NOT NULL,
        manager_group_message_id  INTEGER NOT NULL,
        user_message_id           INTEGER,
        direction                 TEXT NOT NULL,
        created_at                TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_protocol_support_group_message "
    "ON protocol_support_messages (support_chat_id, manager_group_message_id)",
)

_DIALOG_COLUMNS = (
    "id, bot_key, telegram_user_id, username, display_name, support_chat_id, support_thread_id, "
    "root_manager_message_id, status, closed_by, created_at, updated_at, closed_at"
)


@dataclass(frozen=True)
class SupportDialog:
    id: int
    bot_key: str
    telegram_user_id: int
    username: str | None
    display_name: str | None
    support_chat_id: int
    support_thread_id: int | None
    root_manager_message_id: int | None
    status: str
    closed_by: str | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None

    @property
    def is_open(self) -> bool:
        return self.status == STATUS_OPEN


def _fmt(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row(row) -> SupportDialog:
    (id_, bot_key, user_id, username, display_name, chat_id, thread_id, root_id, status, closed_by, created,
     updated, closed) = row
    return SupportDialog(id_, bot_key, user_id, username, display_name, chat_id, thread_id, root_id, status,
                         closed_by, _parse(created), _parse(updated), _parse(closed))


class ProtocolSupportRepository:
    def __init__(self, db_path: Path | str):
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        for statement in _SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    def get(self, dialog_id: int) -> SupportDialog | None:
        row = self._conn.execute(
            f"SELECT {_DIALOG_COLUMNS} FROM protocol_support_dialogs WHERE id = ?", (dialog_id,),
        ).fetchone()
        return _row(row) if row else None

    def get_open(self, bot_key: str, telegram_user_id: int) -> SupportDialog | None:
        row = self._conn.execute(
            f"SELECT {_DIALOG_COLUMNS} FROM protocol_support_dialogs WHERE bot_key = ? AND telegram_user_id = ? "
            "AND status = ?", (bot_key, telegram_user_id, STATUS_OPEN),
        ).fetchone()
        return _row(row) if row else None

    def open_or_get(self, *, bot_key: str, telegram_user_id: int, username: str | None, display_name: str | None,
                    support_chat_id: int, now: datetime) -> tuple[SupportDialog, bool]:
        """(диалог, создан_ли_новый). Уже открытый диалог этого bot_key +
        пользователя возвращается как есть — второй не создаётся."""
        existing = self.get_open(bot_key, telegram_user_id)
        if existing is not None:
            return existing, False
        stamp = _fmt(now)
        try:
            cursor = self._conn.execute(
                "INSERT INTO protocol_support_dialogs (bot_key, telegram_user_id, username, display_name, "
                "support_chat_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bot_key, telegram_user_id, username, display_name, support_chat_id, STATUS_OPEN, stamp, stamp),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:  # гонка: открыт параллельно
            self._conn.rollback()
            return self.get_open(bot_key, telegram_user_id), False
        return self.get(cursor.lastrowid), True

    def set_root_message(self, dialog_id: int, message_id: int, *, now: datetime) -> None:
        self._conn.execute(
            "UPDATE protocol_support_dialogs SET root_manager_message_id = COALESCE(root_manager_message_id, ?), "
            "updated_at = ? WHERE id = ?", (message_id, _fmt(now), dialog_id),
        )
        self._conn.commit()

    def touch(self, dialog_id: int, *, username: str | None, display_name: str | None, now: datetime) -> None:
        self._conn.execute(
            "UPDATE protocol_support_dialogs SET username = ?, display_name = ?, updated_at = ? WHERE id = ?",
            (username, display_name, _fmt(now), dialog_id),
        )
        self._conn.commit()

    def add_message(self, *, dialog_id: int, support_chat_id: int, manager_group_message_id: int,
                    user_message_id: int | None, direction: str, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO protocol_support_messages (dialog_id, support_chat_id, manager_group_message_id, "
            "user_message_id, direction, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (dialog_id, support_chat_id, manager_group_message_id, user_message_id, direction, _fmt(now)),
        )
        self._conn.commit()

    def find_by_group_message(self, support_chat_id: int, message_id: int) -> SupportDialog | None:
        row = self._conn.execute(
            f"SELECT {', '.join('d.' + c.strip() for c in _DIALOG_COLUMNS.split(','))} "
            "FROM protocol_support_messages m JOIN protocol_support_dialogs d ON d.id = m.dialog_id "
            "WHERE m.support_chat_id = ? AND m.manager_group_message_id = ? ORDER BY m.id DESC LIMIT 1",
            (support_chat_id, message_id),
        ).fetchone()
        return _row(row) if row else None

    def close_dialog(self, dialog_id: int, *, closed_by: str, now: datetime) -> bool:
        """Атомарно open -> closed; False — уже закрыт (идемпотентно)."""
        stamp = _fmt(now)
        cursor = self._conn.execute(
            "UPDATE protocol_support_dialogs SET status = ?, closed_by = ?, closed_at = ?, updated_at = ? "
            "WHERE id = ? AND status = ?", (STATUS_CLOSED, closed_by, stamp, stamp, dialog_id, STATUS_OPEN),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def close(self) -> None:
        self._conn.close()
