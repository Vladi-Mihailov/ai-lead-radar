"""Local-only pending-submission outbox for the REDNET collector — see
"Georgia remote collector architecture (REDNET)", requirement G: "If
production API is unavailable... does not lose already fetched matched
fines." This table lives ONLY in the collector's own small local SQLite
file (never shared with/visible to production) — it is operational
retry-state, not a second fines database: every row is just an already-
built wire payload (see reader/fines/collector_payload.py) waiting to be
POSTed, and a row is deleted the moment its POST succeeds.

Resubmitting the SAME payload twice (because, say, the collector crashed
right after a successful POST but before deleting the outbox row) is safe
by construction on the production side — persist_feed_match()'s fingerprint
dedup and mark_completed()'s idempotent re-check make a duplicate feed-match
or bootstrap-result POST a safe no-op (see reader/public_bot/
collector_ingest_service.py). This repository does not need its own
idempotency key for that reason — see collector_job.py's module docstring
for why a second dedup layer here would be redundant and risky."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS collector_outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TIMESTAMP NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0
)
"""

KIND_FEED_MATCH = "feed_match"
KIND_BOOTSTRAP_RESULT = "bootstrap_result"


@dataclass(frozen=True)
class OutboxEntry:
    id: int
    kind: str
    payload: dict
    created_at: datetime
    attempts: int


class CollectorOutboxRepository:
    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def enqueue(self, kind: str, payload: dict) -> None:
        self._conn.execute(
            "INSERT INTO collector_outbox (kind, payload, created_at, attempts) "
            "VALUES (:kind, :payload, :created_at, 0)",
            {
                "kind": kind,
                "payload": json.dumps(payload),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        self._conn.commit()

    def list_all(self) -> list[OutboxEntry]:
        rows = self._conn.execute(
            "SELECT id, kind, payload, created_at, attempts FROM collector_outbox ORDER BY id"
        ).fetchall()
        return [
            OutboxEntry(
                id=row[0], kind=row[1], payload=json.loads(row[2]),
                created_at=datetime.fromisoformat(row[3]), attempts=row[4],
            )
            for row in rows
        ]

    def record_attempt(self, entry_id: int) -> None:
        self._conn.execute(
            "UPDATE collector_outbox SET attempts = attempts + 1 WHERE id = :id", {"id": entry_id},
        )
        self._conn.commit()

    def remove(self, entry_id: int) -> None:
        self._conn.execute("DELETE FROM collector_outbox WHERE id = :id", {"id": entry_id})
        self._conn.commit()

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM collector_outbox").fetchone()
        return row[0]

    def close(self) -> None:
        self._conn.close()
