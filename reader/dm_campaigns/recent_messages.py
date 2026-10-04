"""group_messages_recent — короткий (по умолчанию 48 часов) буфер сообщений
отслеживаемых групп для контекста ЛС-черновиков (reader/dm_campaigns/
context.py): сообщения до/после исходного, цепочка ответов, свежие
сообщения по теме из разных групп. Без лишних запросов к Telegram — пишется
из уже работающего live-обработчика Reader (Pipeline._process).

Хранится минимум: ни username, ни access_hash, ни raw-объекта Telegram —
только sender_id (для подсчёта независимых авторов) и текст.

Тот же стиль, что и у остальных репозиториев users.db: WAL, additive
CREATE TABLE IF NOT EXISTS при открытии. Время — ISO-строка UTC
"%Y-%m-%dT%H:%M:%S" (строковое сравнение = хронологическое)."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
_MAX_STORED_TEXT = 2000

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS group_messages_recent (
        chat_id          INTEGER NOT NULL,
        chat_identifier  TEXT,
        message_id       INTEGER NOT NULL,
        sender_id        INTEGER,
        text             TEXT NOT NULL,
        date             TEXT NOT NULL,
        reply_to_msg_id  INTEGER,
        PRIMARY KEY (chat_id, message_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_group_messages_recent_chat_date ON group_messages_recent (chat_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_group_messages_recent_date ON group_messages_recent (date)",
)

_COLUMNS = "chat_id, chat_identifier, message_id, sender_id, text, date, reply_to_msg_id"


def format_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime(_TIME_FORMAT)


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class RecentMessage:
    chat_id: int
    chat_identifier: str | None
    message_id: int
    sender_id: int | None
    text: str
    date: datetime
    reply_to_msg_id: int | None


def _row(row) -> RecentMessage:
    chat_id, chat_identifier, message_id, sender_id, text, date, reply_to_msg_id = row
    return RecentMessage(
        chat_id=chat_id, chat_identifier=chat_identifier, message_id=message_id,
        sender_id=sender_id, text=text, date=parse_time(date), reply_to_msg_id=reply_to_msg_id,
    )


class RecentMessageRepository:
    def __init__(self, db_path: Path | str):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        for statement in _SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    def add(
        self, *, chat_id: int, chat_identifier: str | None, message_id: int, sender_id: int | None,
        text: str, date: datetime, reply_to_msg_id: int | None,
    ) -> bool:
        """INSERT OR IGNORE — повторная доставка того же (chat_id,
        message_id) Telethon'ом не создаёт дубля. True — строка добавлена."""
        cursor = self._conn.execute(
            f"INSERT OR IGNORE INTO group_messages_recent ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, chat_identifier, message_id, sender_id, (text or "")[:_MAX_STORED_TEXT],
             format_time(date), reply_to_msg_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def get(self, chat_id: int, message_id: int) -> RecentMessage | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM group_messages_recent WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        return _row(row) if row else None

    def before(self, chat_id: int, message_id: int, *, since: datetime, limit: int) -> list[RecentMessage]:
        """До limit сообщений ПЕРЕД message_id (не старше since), по возрастанию."""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM group_messages_recent "
            "WHERE chat_id = ? AND message_id < ? AND date >= ? ORDER BY message_id DESC LIMIT ?",
            (chat_id, message_id, format_time(since), limit),
        ).fetchall()
        return [_row(row) for row in reversed(rows)]

    def after(self, chat_id: int, message_id: int, *, until: datetime, limit: int) -> list[RecentMessage]:
        """До limit сообщений ПОСЛЕ message_id (не позже until), по возрастанию."""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM group_messages_recent "
            "WHERE chat_id = ? AND message_id > ? AND date <= ? ORDER BY message_id ASC LIMIT ?",
            (chat_id, message_id, format_time(until), limit),
        ).fetchall()
        return [_row(row) for row in rows]

    def recent(self, *, since: datetime, until: datetime, limit: int) -> list[RecentMessage]:
        """Сообщения всех групп в окне [since, until], новые первыми, не
        больше limit (отбор по теме/группе — в context.py)."""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM group_messages_recent "
            "WHERE date >= ? AND date <= ? ORDER BY date DESC, message_id DESC LIMIT ?",
            (format_time(since), format_time(until), limit),
        ).fetchall()
        return [_row(row) for row in rows]

    def delete_older_than(self, cutoff: datetime) -> int:
        cursor = self._conn.execute(
            "DELETE FROM group_messages_recent WHERE date < ?", (format_time(cutoff),),
        )
        self._conn.commit()
        return cursor.rowcount

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM group_messages_recent").fetchone()[0]

    def close(self) -> None:
        self._conn.close()
