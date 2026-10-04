"""Тесты хранилищ ЛС-черновиков (Phase 2): dm_outreach и
group_messages_recent. Только временная БД."""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.dm_campaigns.outreach_repository import (
    MAX_ATTEMPTS,
    STATUS_DRAFT,
    STATUS_DRAFTING,
    STATUS_FAILED,
    STATUS_FILTERED,
    STATUS_PENDING_CONTEXT,
    DmOutreachRepository,
)
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.repository import DmCampaignRepository

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "users.db"
    campaigns = DmCampaignRepository(path)
    outreach = DmOutreachRepository(path)
    recent = RecentMessageRepository(path)
    yield path, campaigns, outreach, recent
    for repo in (campaigns, outreach, recent):
        repo.close()


def _candidate(outreach, campaign_id, *, message_id=10, chat_id=-1001, status=STATUS_PENDING_CONTEXT,
               now=T0, wait=timedelta(minutes=3), reason=None):
    return outreach.insert_candidate(
        campaign_id=campaign_id, source_chat_id=chat_id, source_chat_identifier="VerhniyLars",
        source_chat_title="Верхний Ларс", source_message_id=message_id, source_message_at=now,
        source_link=None, source_reply_to_msg_id=None, recipient_user_id=555, recipient_username="ivan",
        source_text="Что сейчас на Ларсе?", status=status, now=now,
        draft_after_at=now + wait if status == STATUS_PENDING_CONTEXT else None, filter_reason=reason,
    )


# ---- dm_outreach ----


def test_schema_created_and_idempotent(db):
    path, *_ = db
    again = DmOutreachRepository(path)
    again_recent = RecentMessageRepository(path)
    again.close()
    again_recent.close()
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    indexes = {r[0] for r in conn.execute("select name from sqlite_master where type='index'")}
    conn.close()
    assert {"dm_outreach", "group_messages_recent"} <= tables
    assert "account_session_leases" not in tables
    assert {"idx_dm_outreach_status_due", "idx_group_messages_recent_chat_date", "idx_group_messages_recent_date"} <= indexes


def test_global_source_message_unique_across_campaigns(db):
    _, campaigns, outreach, _ = db
    fuel, border = campaigns.get_campaign_by_key("fuel"), campaigns.get_campaign_by_key("border_queue")
    first = _candidate(outreach, border.id)
    assert first is not None
    assert _candidate(outreach, border.id) is None  # повтор того же сообщения
    assert _candidate(outreach, fuel.id) is None  # и в другой кампании тоже
    assert _candidate(outreach, fuel.id, chat_id=-1002) is not None  # другой чат — другое сообщение
    assert len(outreach.list_all()) == 2


def test_invalid_initial_status_rejected(db):
    _, campaigns, outreach, _ = db
    with pytest.raises(ValueError):
        _candidate(outreach, campaigns.get_campaign_by_key("fuel").id, status=STATUS_DRAFT)


def test_pending_query_respects_draft_after_at(db):
    _, campaigns, outreach, _ = db
    cid = campaigns.get_campaign_by_key("fuel").id
    oid = _candidate(outreach, cid)
    _candidate(outreach, cid, message_id=11, status=STATUS_FILTERED, reason="no_username")
    assert outreach.due_pending(T0 + timedelta(minutes=2), limit=10) == []
    due = outreach.due_pending(T0 + timedelta(minutes=3), limit=10)
    assert [r.id for r in due] == [oid]
    assert due[0].status == STATUS_PENDING_CONTEXT
    assert due[0].source_chat_identifier == "VerhniyLars"


def test_atomic_claim_and_transitions(db):
    _, campaigns, outreach, _ = db
    oid = _candidate(outreach, campaigns.get_campaign_by_key("fuel").id)
    now = T0 + timedelta(minutes=4)
    assert outreach.claim(oid, now) is True
    assert outreach.claim(oid, now) is False  # уже забран
    row = outreach.get(oid)
    assert (row.status, row.attempts) == (STATUS_DRAFTING, 1)
    assert outreach.mark_draft(
        oid, now=now, context_json="{}", primary_text="Ответ", follow_up_text=None,
        evidence_strength="none", used_context_refs=["B1"],
    ) is True
    row = outreach.get(oid)
    assert (row.status, row.primary_text, row.used_context_refs, row.generated_at) == (STATUS_DRAFT, "Ответ", ("B1",), now)
    # Завершённую строку повторно не перезаписать.
    assert outreach.mark_failed(oid, now=now, error_kind="ai_error") is False
    assert outreach.get(oid).status == STATUS_DRAFT


def test_filtered_and_failed(db):
    _, campaigns, outreach, _ = db
    cid = campaigns.get_campaign_by_key("fuel").id
    a, b = _candidate(outreach, cid, message_id=1), _candidate(outreach, cid, message_id=2)
    for oid in (a, b):
        outreach.claim(oid, T0)
    outreach.mark_filtered(a, now=T0, reason="ai_not_suitable")
    outreach.mark_failed(b, now=T0, error_kind="ai_error", error="x" * 2000)
    assert (outreach.get(a).status, outreach.get(a).filter_reason) == (STATUS_FILTERED, "ai_not_suitable")
    failed = outreach.get(b)
    assert (failed.status, failed.error_kind, len(failed.error)) == (STATUS_FAILED, "ai_error", 500)
    assert outreach.status_counts() == {STATUS_FILTERED: 1, STATUS_FAILED: 1}


def test_crash_recovery_returns_stale_drafting(db):
    _, campaigns, outreach, _ = db
    oid = _candidate(outreach, campaigns.get_campaign_by_key("fuel").id)
    outreach.claim(oid, T0)
    assert outreach.recover_stale(stale_before=T0 - timedelta(minutes=1), now=T0) == 0  # ещё свежий
    assert outreach.recover_stale(stale_before=T0 + timedelta(minutes=11), now=T0 + timedelta(minutes=11)) == 1
    row = outreach.get(oid)
    assert (row.status, row.attempts) == (STATUS_PENDING_CONTEXT, 1)
    assert MAX_ATTEMPTS == 3


# ---- group_messages_recent ----


def _add(recent, message_id, *, chat_id=-1001, ident="VerhniyLars", sender=1, text="текст", at=T0, reply=None):
    return recent.add(chat_id=chat_id, chat_identifier=ident, message_id=message_id, sender_id=sender,
                      text=text, date=at, reply_to_msg_id=reply)


def test_recent_insert_idempotent(db):
    *_, recent = db
    assert _add(recent, 1) is True
    assert _add(recent, 1, text="другой") is False
    assert recent.count() == 1
    assert recent.get(-1001, 1).text == "текст"


def test_recent_before_after_and_reply(db):
    *_, recent = db
    for i in range(1, 11):
        _add(recent, i, at=T0 + timedelta(minutes=i), sender=i, reply=3 if i == 7 else None)
    before = recent.before(-1001, 6, since=T0, limit=3)
    assert [m.message_id for m in before] == [3, 4, 5]
    after = recent.after(-1001, 6, until=T0 + timedelta(minutes=9), limit=5)
    assert [m.message_id for m in after] == [7, 8, 9]
    assert recent.get(-1001, 7).reply_to_msg_id == 3
    # Другой чат не смешивается.
    _add(recent, 5, chat_id=-1002)
    assert [m.chat_id for m in recent.before(-1001, 6, since=T0, limit=10)] == [-1001] * 5


def test_recent_window_newest_first_and_limit(db):
    *_, recent = db
    for i in range(1, 6):
        _add(recent, i, at=T0 - timedelta(hours=i))
    rows = recent.recent(since=T0 - timedelta(hours=3, minutes=30), until=T0, limit=2)
    assert [m.message_id for m in rows] == [1, 2]


def test_retention_cleanup(db):
    *_, recent = db
    _add(recent, 1, at=T0 - timedelta(hours=49))
    _add(recent, 2, at=T0 - timedelta(hours=47))
    assert recent.delete_older_than(T0 - timedelta(hours=48)) == 1
    assert recent.get(-1001, 1) is None and recent.get(-1001, 2) is not None


def test_recent_stores_no_username_columns(db):
    path, *_ = db
    conn = sqlite3.connect(path)
    columns = {r[1] for r in conn.execute("pragma table_info(group_messages_recent)")}
    conn.close()
    assert columns == {"chat_id", "chat_identifier", "message_id", "sender_id", "text", "date", "reply_to_msg_id"}
