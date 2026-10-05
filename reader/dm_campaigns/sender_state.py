"""Состояние безопасности ЛС-отправителей (Phase 3C) — отдельная таблица
dm_sender_state, telegram_accounts/инвайтер не затрагиваются.

- flood_wait_until — Telegram вернул FloodWait: до этого времени аккаунт ЛС
  не отправляет (снимается само по времени).
- blocked_reason — PeerFlood или неавторизованная сессия: аккаунт ЛС не
  отправляет до РУЧНОЙ проверки (python -m reader.dm_campaigns.manage
  clear-sender-block --account-id N --apply)."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from reader.dm_campaigns.recent_messages import format_time, parse_time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dm_sender_state (
    account_id        INTEGER PRIMARY KEY,
    flood_wait_until  TEXT,
    blocked_reason    TEXT,
    blocked_at        TEXT,
    updated_at        TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class DmSenderState:
    account_id: int
    flood_wait_until: datetime | None
    blocked_reason: str | None
    blocked_at: datetime | None


class DmSenderStateRepository:
    def __init__(self, db_path: Path | str):
        self._conn = sqlite3.connect(Path(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def get(self, account_id: int) -> DmSenderState:
        row = self._conn.execute(
            "SELECT flood_wait_until, blocked_reason, blocked_at FROM dm_sender_state WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            return DmSenderState(account_id, None, None, None)
        flood, reason, blocked_at = row
        return DmSenderState(
            account_id, parse_time(flood) if flood else None, reason, parse_time(blocked_at) if blocked_at else None,
        )

    def list(self) -> list[DmSenderState]:
        ids = [row[0] for row in self._conn.execute("SELECT account_id FROM dm_sender_state ORDER BY account_id")]
        return [self.get(account_id) for account_id in ids]

    def _upsert(self, account_id: int, now: datetime, **fields) -> None:
        columns = ", ".join(fields)
        marks = ", ".join(f":{c}" for c in fields)
        updates = ", ".join(f"{c} = excluded.{c}" for c in fields)
        self._conn.execute(
            f"INSERT INTO dm_sender_state (account_id, {columns}, updated_at) VALUES (:account_id, {marks}, :now) "
            f"ON CONFLICT(account_id) DO UPDATE SET {updates}, updated_at = excluded.updated_at",
            {**fields, "account_id": account_id, "now": format_time(now)},
        )
        self._conn.commit()

    def set_flood_wait(self, account_id: int, *, until: datetime, now: datetime) -> None:
        self._upsert(account_id, now, flood_wait_until=format_time(until))

    def block(self, account_id: int, *, reason: str, now: datetime) -> None:
        self._upsert(account_id, now, blocked_reason=reason, blocked_at=format_time(now))

    def clear_block(self, account_id: int, *, now: datetime) -> None:
        self._upsert(account_id, now, blocked_reason=None, blocked_at=None)

    def close(self) -> None:
        self._conn.close()
