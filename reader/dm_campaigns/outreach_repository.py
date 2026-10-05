"""dm_outreach — кандидаты и ЛС-черновики (Phase 2: только черновики, ничего
не отправляется). SQLite — единственный источник истины: кандидат
переживает перезапуск Reader (pending_context), обработчик атомарно
забирает его (pending_context -> drafting) и завершает (draft/filtered/
failed).

UNIQUE(source_chat_id, source_message_id) — ГЛОБАЛЬНЫЙ, не по кампании:
одно исходное Telegram-сообщение даёт максимум одну строку во всех
кампаниях сразу; вставка — INSERT OR IGNORE (повторная доставка/рестарт
не создают второй кандидат).

Статусы Phase 2: pending_context, drafting, draft, filtered, failed.
Время — ISO-строки UTC (тот же формат, что и в recent_messages.py)."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from reader.dm_campaigns.recent_messages import format_time, parse_time
from reader.dm_campaigns.sendability import SENDABILITY_UNRESOLVED, SENDABILITY_VALUES

STATUS_PENDING_CONTEXT = "pending_context"
STATUS_DRAFTING = "drafting"
STATUS_DRAFT = "draft"
STATUS_FILTERED = "filtered"
STATUS_FAILED = "failed"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS dm_outreach (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id             INTEGER NOT NULL REFERENCES dm_campaigns(id),
        source_chat_id          INTEGER NOT NULL,
        source_chat_identifier  TEXT,
        source_chat_title       TEXT,
        source_message_id       INTEGER NOT NULL,
        source_message_at       TEXT,
        source_link             TEXT,
        source_reply_to_msg_id  INTEGER,
        recipient_user_id       INTEGER,
        recipient_username      TEXT,
        source_text             TEXT NOT NULL,
        status                  TEXT NOT NULL,
        draft_after_at          TEXT,
        attempts                INTEGER NOT NULL DEFAULT 0,
        context_json            TEXT,
        primary_text            TEXT,
        follow_up_text          TEXT,
        evidence_strength       TEXT,
        used_context_refs_json  TEXT,
        filter_reason           TEXT,
        error_kind              TEXT,
        error                   TEXT,
        created_at              TEXT NOT NULL,
        updated_at              TEXT NOT NULL,
        generated_at            TEXT,
        sendability             TEXT NOT NULL DEFAULT 'unresolved',
        contact_require_premium INTEGER,
        UNIQUE (source_chat_id, source_message_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dm_outreach_status_due ON dm_outreach (status, draft_after_at)",
    "CREATE INDEX IF NOT EXISTS idx_dm_outreach_campaign ON dm_outreach (campaign_id, status)",
)

_COLUMNS = (
    "id, campaign_id, source_chat_id, source_chat_identifier, source_chat_title, source_message_id, "
    "source_message_at, source_link, source_reply_to_msg_id, recipient_user_id, recipient_username, "
    "source_text, status, draft_after_at, attempts, context_json, primary_text, follow_up_text, "
    "evidence_strength, used_context_refs_json, filter_reason, error_kind, error, created_at, "
    "updated_at, generated_at, sendability, contact_require_premium"
)

# Аддитивные колонки для таблиц, созданных до Phase 2.6 (строки-старожилы
# получают sendability='unresolved', contact_require_premium=NULL).
_COLUMN_MIGRATIONS = (
    ("sendability", "TEXT NOT NULL DEFAULT 'unresolved'"),
    ("contact_require_premium", "INTEGER"),
)

# Сколько раз кандидат может быть забран обработчиком (включая повторы
# после восстановления зависшего drafting) — защита от бесконечного цикла,
# если именно этот кандидат роняет процесс.
MAX_ATTEMPTS = 3

_MAX_SOURCE_TEXT = 4000
_MAX_ERROR = 500


@dataclass(frozen=True)
class DmOutreach:
    id: int
    campaign_id: int
    source_chat_id: int
    source_chat_identifier: str | None
    source_chat_title: str | None
    source_message_id: int
    source_message_at: datetime | None
    source_link: str | None
    source_reply_to_msg_id: int | None
    recipient_user_id: int | None
    recipient_username: str | None
    source_text: str
    status: str
    draft_after_at: datetime | None
    attempts: int
    context_json: str | None
    primary_text: str | None
    follow_up_text: str | None
    evidence_strength: str | None
    used_context_refs: tuple[str, ...]
    filter_reason: str | None
    error_kind: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    generated_at: datetime | None
    # Phase 2.6 (см. reader/dm_campaigns/sendability.py). access_hash НЕ
    # хранится: он привязан к читающему аккаунту и отправителю бесполезен.
    sendability: str = SENDABILITY_UNRESOLVED
    contact_require_premium: bool | None = None


def _opt_time(value: str | None) -> datetime | None:
    return parse_time(value) if value else None


def _row(row) -> DmOutreach:
    (
        id_, campaign_id, source_chat_id, source_chat_identifier, source_chat_title, source_message_id,
        source_message_at, source_link, source_reply_to_msg_id, recipient_user_id, recipient_username,
        source_text, status, draft_after_at, attempts, context_json, primary_text, follow_up_text,
        evidence_strength, used_context_refs_json, filter_reason, error_kind, error, created_at,
        updated_at, generated_at, sendability, contact_require_premium,
    ) = row
    return DmOutreach(
        id=id_, campaign_id=campaign_id, source_chat_id=source_chat_id,
        source_chat_identifier=source_chat_identifier, source_chat_title=source_chat_title,
        source_message_id=source_message_id, source_message_at=_opt_time(source_message_at),
        source_link=source_link, source_reply_to_msg_id=source_reply_to_msg_id,
        recipient_user_id=recipient_user_id, recipient_username=recipient_username,
        source_text=source_text, status=status, draft_after_at=_opt_time(draft_after_at),
        attempts=attempts, context_json=context_json, primary_text=primary_text,
        follow_up_text=follow_up_text, evidence_strength=evidence_strength,
        used_context_refs=tuple(json.loads(used_context_refs_json)) if used_context_refs_json else (),
        filter_reason=filter_reason, error_kind=error_kind, error=error,
        created_at=parse_time(created_at), updated_at=parse_time(updated_at),
        generated_at=_opt_time(generated_at),
        sendability=sendability or SENDABILITY_UNRESOLVED,
        contact_require_premium=None if contact_require_premium is None else bool(contact_require_premium),
    )


class DmOutreachRepository:
    def __init__(self, db_path: Path | str):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        for statement in _SCHEMA:
            self._conn.execute(statement)
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(dm_outreach)")}
        for column, definition in _COLUMN_MIGRATIONS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE dm_outreach ADD COLUMN {column} {definition}")
        self._conn.commit()

    def insert_candidate(
        self, *, campaign_id: int, source_chat_id: int, source_chat_identifier: str | None,
        source_chat_title: str | None, source_message_id: int, source_message_at: datetime,
        source_link: str | None, source_reply_to_msg_id: int | None,
        recipient_user_id: int | None, recipient_username: str | None, source_text: str,
        status: str, now: datetime, draft_after_at: datetime | None = None,
        filter_reason: str | None = None, sendability: str = SENDABILITY_UNRESOLVED,
        contact_require_premium: bool | None = None,
    ) -> int | None:
        """INSERT OR IGNORE по глобальному UNIQUE(source_chat_id,
        source_message_id). id новой строки, либо None — для этого
        сообщения строка уже есть (в любой кампании)."""
        if status not in (STATUS_PENDING_CONTEXT, STATUS_FILTERED):
            raise ValueError(f"Недопустимый начальный статус: {status!r}")
        if sendability not in SENDABILITY_VALUES:
            raise ValueError(f"Недопустимое значение sendability: {sendability!r}")
        stamp = format_time(now)
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO dm_outreach (
                campaign_id, source_chat_id, source_chat_identifier, source_chat_title,
                source_message_id, source_message_at, source_link, source_reply_to_msg_id,
                recipient_user_id, recipient_username, source_text, status, draft_after_at,
                filter_reason, created_at, updated_at, sendability, contact_require_premium
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id, source_chat_id, source_chat_identifier, source_chat_title,
                source_message_id, format_time(source_message_at), source_link, source_reply_to_msg_id,
                recipient_user_id, recipient_username, (source_text or "")[:_MAX_SOURCE_TEXT], status,
                format_time(draft_after_at) if draft_after_at else None, filter_reason, stamp, stamp,
                sendability, None if contact_require_premium is None else int(bool(contact_require_premium)),
            ),
        )
        self._conn.commit()
        return cursor.lastrowid if cursor.rowcount == 1 else None

    def get(self, outreach_id: int) -> DmOutreach | None:
        row = self._conn.execute(f"SELECT {_COLUMNS} FROM dm_outreach WHERE id = ?", (outreach_id,)).fetchone()
        return _row(row) if row else None

    def list_all(self) -> list[DmOutreach]:
        rows = self._conn.execute(f"SELECT {_COLUMNS} FROM dm_outreach ORDER BY id").fetchall()
        return [_row(row) for row in rows]

    def due_pending(self, now: datetime, *, limit: int) -> list[DmOutreach]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM dm_outreach WHERE status = ? AND draft_after_at <= ? "
            "ORDER BY draft_after_at, id LIMIT ?",
            (STATUS_PENDING_CONTEXT, format_time(now), limit),
        ).fetchall()
        return [_row(row) for row in rows]

    def claim(self, outreach_id: int, now: datetime) -> bool:
        """Атомарно pending_context -> drafting (rowcount проверяется):
        второй обработчик/повторный вызов ту же строку не получит."""
        cursor = self._conn.execute(
            "UPDATE dm_outreach SET status = ?, attempts = attempts + 1, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (STATUS_DRAFTING, format_time(now), outreach_id, STATUS_PENDING_CONTEXT),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def recover_stale(self, *, stale_before: datetime, now: datetime) -> int:
        """drafting, не завершённый к stale_before (процесс упал посреди
        генерации), -> снова pending_context."""
        cursor = self._conn.execute(
            "UPDATE dm_outreach SET status = ?, updated_at = ? WHERE status = ? AND updated_at < ?",
            (STATUS_PENDING_CONTEXT, format_time(now), STATUS_DRAFTING, format_time(stale_before)),
        )
        self._conn.commit()
        return cursor.rowcount

    def _finish(self, outreach_id: int, now: datetime, **fields) -> bool:
        """Завершение только из drafting — устаревший/повторный вызов не
        перезапишет уже завершённую строку."""
        assignments = ", ".join(f"{column} = :{column}" for column in fields)
        cursor = self._conn.execute(
            f"UPDATE dm_outreach SET {assignments}, updated_at = :updated_at "
            "WHERE id = :id AND status = :expected",
            {**fields, "updated_at": format_time(now), "id": outreach_id, "expected": STATUS_DRAFTING},
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def mark_draft(
        self, outreach_id: int, *, now: datetime, context_json: str, primary_text: str,
        follow_up_text: str | None, evidence_strength: str, used_context_refs: list[str],
    ) -> bool:
        return self._finish(
            outreach_id, now, status=STATUS_DRAFT, context_json=context_json, primary_text=primary_text,
            follow_up_text=follow_up_text, evidence_strength=evidence_strength,
            used_context_refs_json=json.dumps(list(used_context_refs), ensure_ascii=False),
            generated_at=format_time(now), filter_reason=None, error_kind=None, error=None,
        )

    def mark_filtered(
        self, outreach_id: int, *, now: datetime, reason: str, context_json: str | None = None,
    ) -> bool:
        return self._finish(
            outreach_id, now, status=STATUS_FILTERED, filter_reason=reason, context_json=context_json,
        )

    def mark_failed(
        self, outreach_id: int, *, now: datetime, error_kind: str, error: str | None = None,
        context_json: str | None = None,
    ) -> bool:
        return self._finish(
            outreach_id, now, status=STATUS_FAILED, error_kind=error_kind,
            error=(error or "")[:_MAX_ERROR] or None, context_json=context_json,
        )

    def status_counts(self) -> dict[str, int]:
        return dict(self._conn.execute("SELECT status, COUNT(*) FROM dm_outreach GROUP BY status").fetchall())

    def close(self) -> None:
        self._conn.close()
