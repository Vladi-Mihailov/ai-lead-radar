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
Ручной режим (оператор в inviter_admin_bot): draft -> approved | skipped —
атомарно (UPDATE ... WHERE status='draft', rowcount): один черновик не
может быть обработан дважды. approved — решение оператора ДО Phase 3C:
такие строки никогда не отправляются (отправка — только из draft).

Phase 3C (reader/dm_campaigns/send_service.py): draft -> sending (атомарный
claim перед Telegram RPC) -> sent | blocked | send_failed, либо sending ->
draft, если отправка гарантированно не произошла (FloodWait/PeerFlood/
транспорт до send_message). blocked возможен и сразу из draft (проверки до
RPC). is_synthetic=1 — тестовая строка: не отправляется никогда; выставляется
ЯВНО кодом, создающим тестовую строку (insert_candidate(is_synthetic=True)),
обычный кандидат Reader всегда 0.
Время — ISO-строки UTC (тот же формат, что и в recent_messages.py)."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from reader.dm_campaigns.recent_messages import format_time, parse_time
from reader.dm_campaigns.sendability import SENDABILITY_UNRESOLVED, SENDABILITY_VALUES

STATUS_PENDING_CONTEXT = "pending_context"
STATUS_DRAFTING = "drafting"
STATUS_DRAFT = "draft"
STATUS_FILTERED = "filtered"
STATUS_FAILED = "failed"
STATUS_APPROVED = "approved"
STATUS_SKIPPED = "skipped"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_SEND_FAILED = "send_failed"
STATUS_BLOCKED = "blocked"

# send_error с этим префиксом — исход отправки неизвестен (транспорт упал во
# время send_message): автоматически не повторяется и для кулдауна/лимитов
# считается как отправленное.
UNCERTAIN_PREFIX = "uncertain"

# Предел текста черновика после правки оператором (лимит сообщения
# Telegram — 4096, оставлен запас).
MAX_EDITED_TEXT = 3500

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
    "updated_at, generated_at, sendability, contact_require_premium, original_primary_text, edited_by, "
    "edited_at, reviewed_by, reviewed_at, operator_notified_at, is_synthetic, approved_by, approved_at, "
    "sender_account_id, send_started_at, sent_at, send_error, telegram_message_id"
)

# Аддитивные колонки для таблиц, созданных до Phase 2.6 (строки-старожилы
# получают sendability='unresolved', contact_require_premium=NULL).
_COLUMN_MIGRATIONS = (
    ("sendability", "TEXT NOT NULL DEFAULT 'unresolved'"),
    ("contact_require_premium", "INTEGER"),
    # Ручной режим: исходный AI-текст (заполняется при первой правке),
    # кто/когда правил, кто/когда одобрил или пропустил (telegram user id
    # оператора), когда карточка разослана операторам.
    ("original_primary_text", "TEXT"),
    ("edited_by", "INTEGER"),
    ("edited_at", "TEXT"),
    ("reviewed_by", "INTEGER"),
    ("reviewed_at", "TEXT"),
    ("operator_notified_at", "TEXT"),
    # Phase 3C: тестовая строка (никогда не отправляется) и аудит отправки.
    # Ни auth_key, ни access_hash не хранятся.
    ("is_synthetic", "INTEGER NOT NULL DEFAULT 0"),
    ("approved_by", "INTEGER"),
    ("approved_at", "TEXT"),
    ("sender_account_id", "INTEGER"),
    ("send_started_at", "TEXT"),
    ("sent_at", "TEXT"),
    ("send_error", "TEXT"),
    ("telegram_message_id", "INTEGER"),
)

# Одноразовая миграция: маркер, которым помечена существующая синтетическая
# проверка (#32) в context_json. НЕ правило для новых строк — у них
# is_synthetic выставляет явно тот код, что создаёт тестовую строку.
_LEGACY_SYNTHETIC_MARKER = '%"synthetic": true%'

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
    original_primary_text: str | None = None
    edited_by: int | None = None
    edited_at: datetime | None = None
    reviewed_by: int | None = None
    reviewed_at: datetime | None = None
    operator_notified_at: datetime | None = None
    is_synthetic: bool = False
    approved_by: int | None = None
    approved_at: datetime | None = None
    sender_account_id: int | None = None
    send_started_at: datetime | None = None
    sent_at: datetime | None = None
    send_error: str | None = None
    telegram_message_id: int | None = None


def _opt_time(value: str | None) -> datetime | None:
    return parse_time(value) if value else None


def _row(row) -> DmOutreach:
    (
        id_, campaign_id, source_chat_id, source_chat_identifier, source_chat_title, source_message_id,
        source_message_at, source_link, source_reply_to_msg_id, recipient_user_id, recipient_username,
        source_text, status, draft_after_at, attempts, context_json, primary_text, follow_up_text,
        evidence_strength, used_context_refs_json, filter_reason, error_kind, error, created_at,
        updated_at, generated_at, sendability, contact_require_premium, original_primary_text, edited_by,
        edited_at, reviewed_by, reviewed_at, operator_notified_at, is_synthetic, approved_by, approved_at,
        sender_account_id, send_started_at, sent_at, send_error, telegram_message_id,
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
        original_primary_text=original_primary_text, edited_by=edited_by, edited_at=_opt_time(edited_at),
        reviewed_by=reviewed_by, reviewed_at=_opt_time(reviewed_at),
        operator_notified_at=_opt_time(operator_notified_at),
        is_synthetic=bool(is_synthetic), approved_by=approved_by, approved_at=_opt_time(approved_at),
        sender_account_id=sender_account_id, send_started_at=_opt_time(send_started_at),
        sent_at=_opt_time(sent_at), send_error=send_error, telegram_message_id=telegram_message_id,
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
        if "operator_notified_at" not in existing:
            # Один раз, при появлении ручного режима: строки, созданные ДО
            # него, считаются уже разосланными — после деплоя операторам не
            # прилетает накопленный backlog (в очереди "🆕 Новые" они видны).
            self._conn.execute(
                "UPDATE dm_outreach SET operator_notified_at = ? WHERE operator_notified_at IS NULL",
                (format_time(datetime.now(timezone.utc)),),
            )
        if "is_synthetic" not in existing:
            # Один раз (при появлении колонки): уже существующие тестовые
            # строки помечаются по маркеру синтетики в context_json.
            self._conn.execute(
                "UPDATE dm_outreach SET is_synthetic = 1 WHERE context_json LIKE ?", (_LEGACY_SYNTHETIC_MARKER,),
            )
        self._conn.commit()

    def insert_candidate(
        self, *, campaign_id: int, source_chat_id: int, source_chat_identifier: str | None,
        source_chat_title: str | None, source_message_id: int, source_message_at: datetime,
        source_link: str | None, source_reply_to_msg_id: int | None,
        recipient_user_id: int | None, recipient_username: str | None, source_text: str,
        status: str, now: datetime, draft_after_at: datetime | None = None,
        filter_reason: str | None = None, sendability: str = SENDABILITY_UNRESOLVED,
        contact_require_premium: bool | None = None, is_synthetic: bool = False,
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
                filter_reason, created_at, updated_at, sendability, contact_require_premium, is_synthetic
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id, source_chat_id, source_chat_identifier, source_chat_title,
                source_message_id, format_time(source_message_at), source_link, source_reply_to_msg_id,
                recipient_user_id, recipient_username, (source_text or "")[:_MAX_SOURCE_TEXT], status,
                format_time(draft_after_at) if draft_after_at else None, filter_reason, stamp, stamp,
                sendability, None if contact_require_premium is None else int(bool(contact_require_premium)),
                int(bool(is_synthetic)),
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

    # ---- ручной режим: очередь оператора ----

    def list_by_status(self, status: str, *, limit: int = 10) -> list[DmOutreach]:
        """Новые сверху (по id)."""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM dm_outreach WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit),
        ).fetchall()
        return [_row(row) for row in rows]

    def count_by_status(self, status: str) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM dm_outreach WHERE status = ?", (status,)).fetchone()[0]

    def _review(self, outreach_id: int, status: str, operator_id: int, now: datetime) -> bool:
        cursor = self._conn.execute(
            "UPDATE dm_outreach SET status = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (status, operator_id, format_time(now), format_time(now), outreach_id, STATUS_DRAFT),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def approve(self, outreach_id: int, *, operator_id: int, now: datetime) -> bool:
        """draft -> approved атомарно; False — черновик уже обработан (или
        его нет). Ничего не отправляет."""
        return self._review(outreach_id, STATUS_APPROVED, operator_id, now)

    def skip(self, outreach_id: int, *, operator_id: int, now: datetime) -> bool:
        """draft -> skipped атомарно; False — черновик уже обработан."""
        return self._review(outreach_id, STATUS_SKIPPED, operator_id, now)

    def edit_primary_text(self, outreach_id: int, text: str, *, operator_id: int, now: datetime) -> bool:
        """Заменяет видимый пользователю primary_text; исходный AI-текст
        сохраняется в original_primary_text при ПЕРВОЙ правке и больше не
        перезаписывается. Только для status='draft'."""
        cleaned = (text or "").strip()
        if not cleaned:
            raise ValueError("Пустой текст.")
        if len(cleaned) > MAX_EDITED_TEXT:
            raise ValueError(f"Текст длиннее {MAX_EDITED_TEXT} символов.")
        cursor = self._conn.execute(
            "UPDATE dm_outreach SET original_primary_text = COALESCE(original_primary_text, primary_text), "
            "primary_text = ?, edited_by = ?, edited_at = ?, updated_at = ? WHERE id = ? AND status = ?",
            (cleaned, operator_id, format_time(now), format_time(now), outreach_id, STATUS_DRAFT),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def claim_unnotified_drafts(self, *, now: datetime, generated_after: datetime, limit: int) -> list[DmOutreach]:
        """Черновики, ещё не разосланные операторам (и не старше
        generated_after) — помечаются operator_notified_at атомарно по
        одному: при нескольких рассыльщиках каждый получит свои строки."""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM dm_outreach WHERE status = ? AND operator_notified_at IS NULL "
            "AND generated_at >= ? ORDER BY id LIMIT ?",
            (STATUS_DRAFT, format_time(generated_after), limit),
        ).fetchall()
        claimed = []
        for row in rows:
            cursor = self._conn.execute(
                "UPDATE dm_outreach SET operator_notified_at = ? WHERE id = ? AND operator_notified_at IS NULL",
                (format_time(now), row[0]),
            )
            if cursor.rowcount == 1:
                claimed.append(_row(row))
        self._conn.commit()
        return claimed

    # ---- Phase 3C: отправка ----

    def list_by_statuses(self, statuses: tuple[str, ...], *, limit: int = 10) -> list[DmOutreach]:
        marks = ", ".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM dm_outreach WHERE status IN ({marks}) ORDER BY id DESC LIMIT ?",
            (*statuses, limit),
        ).fetchall()
        return [_row(row) for row in rows]

    def count_by_statuses(self, statuses: tuple[str, ...]) -> int:
        marks = ", ".join("?" for _ in statuses)
        return self._conn.execute(
            f"SELECT COUNT(*) FROM dm_outreach WHERE status IN ({marks})", statuses,
        ).fetchone()[0]

    def block_draft(self, outreach_id: int, *, reason: str, operator_id: int, now: datetime) -> bool:
        """draft -> blocked (проверка до Telegram RPC не пройдена)."""
        cursor = self._conn.execute(
            "UPDATE dm_outreach SET status = ?, send_error = ?, approved_by = ?, approved_at = ?, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (STATUS_BLOCKED, reason, operator_id, format_time(now), format_time(now), outreach_id, STATUS_DRAFT),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def claim_for_send(self, outreach_id: int, *, operator_id: int, sender_account_id: int, now: datetime) -> bool:
        """Атомарно draft -> sending ПЕРЕД Telegram RPC. Синтетическая
        строка не может быть захвачена ни при каком вызове."""
        stamp = format_time(now)
        cursor = self._conn.execute(
            "UPDATE dm_outreach SET status = ?, approved_by = ?, approved_at = ?, sender_account_id = ?, "
            "send_started_at = ?, send_error = NULL, updated_at = ? "
            "WHERE id = ? AND status = ? AND is_synthetic = 0",
            (STATUS_SENDING, operator_id, stamp, sender_account_id, stamp, stamp, outreach_id, STATUS_DRAFT),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def _from_sending(self, outreach_id: int, now: datetime, **fields) -> bool:
        assignments = ", ".join(f"{column} = :{column}" for column in fields)
        cursor = self._conn.execute(
            f"UPDATE dm_outreach SET {assignments}, updated_at = :updated_at WHERE id = :id AND status = :expected",
            {**fields, "updated_at": format_time(now), "id": outreach_id, "expected": STATUS_SENDING},
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def mark_sent(self, outreach_id: int, *, now: datetime, telegram_message_id: int | None) -> bool:
        return self._from_sending(
            outreach_id, now, status=STATUS_SENT, sent_at=format_time(now), telegram_message_id=telegram_message_id,
            send_error=None,
        )

    def mark_send_blocked(self, outreach_id: int, *, now: datetime, reason: str) -> bool:
        return self._from_sending(outreach_id, now, status=STATUS_BLOCKED, send_error=reason[:_MAX_ERROR])

    def mark_send_failed(self, outreach_id: int, *, now: datetime, error: str) -> bool:
        return self._from_sending(outreach_id, now, status=STATUS_SEND_FAILED, send_error=error[:_MAX_ERROR])

    def release_to_draft(self, outreach_id: int, *, now: datetime, error: str) -> bool:
        """sending -> draft: отправка ГАРАНТИРОВАННО не произошла (FloodWait,
        PeerFlood, сбой до send_message) — оператор сможет повторить позже."""
        return self._from_sending(
            outreach_id, now, status=STATUS_DRAFT, send_error=error[:_MAX_ERROR], sender_account_id=None,
        )

    # Что считается "уже писали" для кулдауна и лимитов: отправлено, в
    # процессе, либо исход неизвестен (транспорт во время send_message).
    _COUNTED_SENDS = (
        "(status IN ('sent', 'sending') OR (status = 'send_failed' AND send_error LIKE 'uncertain%'))"
    )

    def recipient_recently_messaged(
        self, *, recipient_user_id: int | None, recipient_username: str | None, since: datetime,
        exclude_id: int | None = None,
    ) -> bool:
        if recipient_user_id is None and not recipient_username:
            return False
        row = self._conn.execute(
            f"SELECT 1 FROM dm_outreach WHERE {self._COUNTED_SENDS} AND send_started_at >= ? AND id != ? "
            "AND ((? IS NOT NULL AND recipient_user_id = ?) OR (? IS NOT NULL AND lower(recipient_username) = ?)) "
            "LIMIT 1",
            (format_time(since), exclude_id if exclude_id is not None else -1, recipient_user_id, recipient_user_id,
             recipient_username, (recipient_username or "").lower()),
        ).fetchone()
        return row is not None

    def sends_since(self, *, sender_account_id: int, since: datetime, campaign_id: int | None = None) -> int:
        query = (f"SELECT COUNT(*) FROM dm_outreach WHERE {self._COUNTED_SENDS} AND sender_account_id = ? "
                 "AND send_started_at >= ?")
        params: list = [sender_account_id, format_time(since)]
        if campaign_id is not None:
            query += " AND campaign_id = ?"
            params.append(campaign_id)
        return self._conn.execute(query, params).fetchone()[0]

    def status_counts(self) -> dict[str, int]:
        return dict(self._conn.execute("SELECT status, COUNT(*) FROM dm_outreach GROUP BY status").fetchall())

    def close(self) -> None:
        self._conn.close()
