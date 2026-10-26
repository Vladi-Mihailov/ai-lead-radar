"""Доступ ЛС-отправителя к исходной группе черновика (путь source_message):
что Telegram ответил, когда отправитель читал исходное сообщение. Отдельная
таблица dm_sender_source_access, telegram_accounts/инвайтер не затрагиваются.

- has_access=1 — отправитель прочитал исходное сообщение этой группы.
- has_access=0 — группа недоступна отправителю (ChannelPrivateError,
  UserNotParticipantError, группа не резолвится и т.п.). Selector не берёт
  такого отправителя для черновиков этой группы NO_ACCESS_TTL — потом снова
  пробует (доступ мог появиться: вступил, разбанили).

Пишут send_service (по факту резолва при отправке) и manage
check-source-access (проверка только чтением); читает sender_selection."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from reader.dm_campaigns.recent_messages import format_time, parse_time

NO_ACCESS_TTL = timedelta(days=7)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dm_sender_source_access (
    account_id      INTEGER NOT NULL,
    source_chat_id  INTEGER NOT NULL,
    has_access      INTEGER NOT NULL,
    reason          TEXT,
    checked_at      TEXT NOT NULL,
    PRIMARY KEY (account_id, source_chat_id)
)
"""


@dataclass(frozen=True)
class SourceAccess:
    account_id: int
    source_chat_id: int
    has_access: bool
    reason: str | None
    checked_at: datetime

    def denies(self, now: datetime) -> bool:
        """Действующий запрет: доступа нет и запись не старше NO_ACCESS_TTL."""
        return not self.has_access and now - self.checked_at < NO_ACCESS_TTL


class DmSourceAccessRepository:
    def __init__(self, db_path: Path | str):
        self._conn = sqlite3.connect(Path(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def get(self, account_id: int, source_chat_id: int) -> SourceAccess | None:
        row = self._conn.execute(
            "SELECT has_access, reason, checked_at FROM dm_sender_source_access "
            "WHERE account_id = ? AND source_chat_id = ?",
            (account_id, source_chat_id),
        ).fetchone()
        if row is None:
            return None
        has_access, reason, checked_at = row
        return SourceAccess(account_id, source_chat_id, bool(has_access), reason, parse_time(checked_at))

    def record(self, account_id: int, source_chat_id: int, *, has_access: bool, reason: str | None,
               now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO dm_sender_source_access (account_id, source_chat_id, has_access, reason, checked_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_id, source_chat_id) DO UPDATE SET "
            "has_access = excluded.has_access, reason = excluded.reason, checked_at = excluded.checked_at",
            (account_id, source_chat_id, int(has_access), reason, format_time(now)),
        )
        self._conn.commit()

    def list(self) -> list[SourceAccess]:
        keys = self._conn.execute(
            "SELECT account_id, source_chat_id FROM dm_sender_source_access ORDER BY account_id, source_chat_id"
        ).fetchall()
        return [self.get(account_id, chat_id) for account_id, chat_id in keys]

    def close(self) -> None:
        self._conn.close()
