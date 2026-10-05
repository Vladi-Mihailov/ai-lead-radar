"""Phase 2.6: @username не обязателен для ЛС-кандидата; sendability и
contact_require_premium сохраняются без сетевых запросов. Ничего не
отправляется, кампании в тестах включаются только во временной БД."""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from telethon.tl.types import Channel, User
from test_dm_outreach_drafts import _builder, _FakeService, _out

from reader.core.models import Message
from reader.dm_campaigns.observer import DmOutreachObserver
from reader.dm_campaigns.outreach_repository import (
    STATUS_DRAFT,
    STATUS_FILTERED,
    STATUS_PENDING_CONTEXT,
    DmOutreachRepository,
)
from reader.dm_campaigns.processor import DmDraftProcessor
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.sendability import (
    SENDABILITY_PREMIUM_REQUIRED,
    SENDABILITY_SOURCE_MESSAGE,
    SENDABILITY_UNRESOLVED,
    SENDABILITY_USERNAME,
    SENDABILITY_VALUES,
    assess_sendability,
    sender_meets_requirements,
)
from reader.scenarios import KeywordMatcher, load_scenarios
from reader.sources.telegram_source import _contact_require_premium

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
SCENARIOS = load_scenarios(PROJECT_ROOT / "config" / "scenarios.yaml")
MATCHER = KeywordMatcher(SCENARIOS)
INSURANCE_TEXT = "Подскажите, где оформить страховку на авто для въезда в Грузию?"


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach, recent = DmCampaignRepository(path), DmOutreachRepository(path), RecentMessageRepository(path)
    insurance = campaigns.set_enabled(campaigns.get_campaign_by_key("insurance").id, True)
    observer = DmOutreachObserver(campaigns, outreach, recent, context_wait_seconds=180, clock=lambda: T0,
                                  monotonic=lambda: 0.0)
    yield path, campaigns, outreach, recent, observer, insurance
    for repo in (campaigns, outreach, recent):
        repo.close()


def _msg(*, message_id=1, sender_id=555, username=None, premium=None, ident="VerhniyLars", text=INSURANCE_TEXT):
    return Message(
        id=message_id, chat_id=-1001, chat_title="Верхний Ларс", sender_id=sender_id, sender_username=username,
        sender_name="Ivan", text=text, date=T0, link=None, chat_identifier=ident, reply_to_msg_id=None,
        sender_contact_require_premium=premium,
    )


def _observe(observer, message):
    observer.observe(message, MATCHER.match(message.text))


# ---- assess_sendability ----


@pytest.mark.parametrize("username,user_id,chat_id,message_id,premium,expected", [
    ("ivan", 5, -1, 9, False, SENDABILITY_USERNAME),
    (None, 5, -1, 9, False, SENDABILITY_SOURCE_MESSAGE),
    (None, 5, -1, 9, None, SENDABILITY_SOURCE_MESSAGE),
    ("ivan", 5, -1, 9, True, SENDABILITY_PREMIUM_REQUIRED),
    (None, 5, -1, 9, True, SENDABILITY_PREMIUM_REQUIRED),
    (None, None, -1, 9, False, SENDABILITY_UNRESOLVED),
    (None, 5, None, None, None, SENDABILITY_UNRESOLVED),
], ids=["username", "source_message", "source_message_flag_unknown", "premium_wins_over_username",
        "premium_required", "no_user_id_no_username", "no_source_message"])
def test_assess_sendability(username, user_id, chat_id, message_id, premium, expected):
    assert assess_sendability(
        username=username, recipient_user_id=user_id, source_chat_id=chat_id, source_message_id=message_id,
        contact_require_premium=premium,
    ) == expected


# ---- observer: no username no longer filters ----


def test_no_username_candidate_is_pending_with_source_message_sendability(env):
    _, _, outreach, _, observer, _ = env
    _observe(observer, _msg(username=None, premium=False))
    (row,) = outreach.list_all()
    assert (row.status, row.filter_reason, row.recipient_username) == (STATUS_PENDING_CONTEXT, None, None)
    assert row.draft_after_at == T0 + timedelta(seconds=180)
    assert (row.sendability, row.contact_require_premium) == (SENDABILITY_SOURCE_MESSAGE, False)
    assert (row.recipient_user_id, row.source_chat_id, row.source_message_id) == (555, -1001, 1)


def test_username_candidate_keeps_username_sendability(env):
    _, _, outreach, _, observer, _ = env
    _observe(observer, _msg(username="ivan", premium=False))
    (row,) = outreach.list_all()
    assert (row.status, row.recipient_username, row.sendability) == (STATUS_PENDING_CONTEXT, "ivan", SENDABILITY_USERNAME)


def test_premium_required_candidate_is_not_filtered(env):
    _, _, outreach, _, observer, _ = env
    _observe(observer, _msg(username=None, premium=True))
    (row,) = outreach.list_all()
    assert (row.status, row.filter_reason, row.sendability, row.contact_require_premium) == (
        STATUS_PENDING_CONTEXT, None, SENDABILITY_PREMIUM_REQUIRED, True)


def test_unknown_premium_flag_is_stored_as_null(env):
    path, _, outreach, _, observer, _ = env
    _observe(observer, _msg(premium=None))
    (row,) = outreach.list_all()
    assert row.contact_require_premium is None
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT contact_require_premium FROM dm_outreach").fetchone() == (None,)
    conn.close()


def test_no_sender_id_still_filtered(env):
    _, _, outreach, _, observer, _ = env
    _observe(observer, _msg(sender_id=None))
    (row,) = outreach.list_all()
    assert (row.status, row.filter_reason, row.sendability) == (STATUS_FILTERED, "no_sender_id", SENDABILITY_UNRESOLVED)


def test_filtered_rows_also_carry_sendability(env):
    _, campaigns, outreach, recent, _, insurance = env
    campaigns.update_source_chats(insurance.id, ["VerhniyLars"])
    observer = DmOutreachObserver(campaigns, outreach, recent, context_wait_seconds=180, clock=lambda: T0,
                                  monotonic=lambda: 0.0)
    _observe(observer, _msg(ident="tbilisi14", username=None, premium=False))
    (row,) = outreach.list_all()
    assert (row.status, row.filter_reason, row.sendability) == (
        STATUS_FILTERED, "source_chat_not_allowed", SENDABILITY_SOURCE_MESSAGE)


def test_campaign_state_untouched_by_detection(env):
    _, campaigns, _, _, observer, insurance = env
    _observe(observer, _msg())
    assert campaigns.get_campaign(insurance.id).enabled is True
    assert campaigns.get_campaign_by_key("fuel").enabled is False
    assert campaigns.get_campaign_by_key("border_queue").enabled is False


# ---- full path: candidate without username -> context -> draft ----


async def test_no_username_candidate_reaches_stored_draft(env):
    _, campaigns, outreach, recent, observer, _ = env
    _observe(observer, _msg(username=None, premium=False))
    (row,) = outreach.list_all()
    service = _FakeService(_out(used_context_refs=[]))
    processor = DmDraftProcessor(
        outreach, campaigns, recent, _builder(recent), service, interval_seconds=1,
        drafting_recovery_seconds=600, retention_hours=48, clock=lambda: T0 + timedelta(minutes=4),
        monotonic=lambda: 0.0,
    )
    assert await processor.run_once() == 1
    done = outreach.get(row.id)
    assert (done.status, done.primary_text, done.recipient_username) == (STATUS_DRAFT, "Ответ по делу.", None)
    assert done.sendability == SENDABILITY_SOURCE_MESSAGE  # переживает переход в draft
    assert len(service.calls) == 1
    assert "555" not in service.calls[0]  # recipient_user_id в OpenAI не уходит


async def test_premium_required_candidate_reaches_draft_without_assigned_sender(env):
    path, campaigns, outreach, recent, observer, _ = env
    _observe(observer, _msg(username="ivan", premium=True))
    (row,) = outreach.list_all()
    processor = DmDraftProcessor(
        outreach, campaigns, recent, _builder(recent), _FakeService(_out(used_context_refs=[])), interval_seconds=1,
        drafting_recovery_seconds=600, retention_hours=48, clock=lambda: T0 + timedelta(minutes=4),
        monotonic=lambda: 0.0,
    )
    assert await processor.run_once() == 1
    done = outreach.get(row.id)
    assert (done.status, done.filter_reason, done.sendability) == (STATUS_DRAFT, None, SENDABILITY_PREMIUM_REQUIRED)
    # Phase 2.6: sender не назначается -- ни поля в модели, ни колонки в БД.
    fields = set(vars(done))
    conn = sqlite3.connect(path)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(dm_outreach)")}
    conn.close()
    for forbidden in ("sender_account_id", "assigned_sender", "sender_session", "access_hash"):
        assert forbidden not in fields and forbidden not in columns


# ---- Phase 3 contract: premium_required -> Premium sender only ----


def test_premium_required_allows_only_premium_sender():
    assert sender_meets_requirements(SENDABILITY_PREMIUM_REQUIRED, sender_is_premium=True) is True
    assert sender_meets_requirements(SENDABILITY_PREMIUM_REQUIRED, sender_is_premium=False) is False


@pytest.mark.parametrize("sendability", [SENDABILITY_USERNAME, SENDABILITY_SOURCE_MESSAGE, SENDABILITY_UNRESOLVED])
@pytest.mark.parametrize("premium", [True, False])
def test_other_sendability_does_not_require_premium(sendability, premium):
    assert sender_meets_requirements(sendability, sender_is_premium=premium) is True


def test_contract_rejects_unknown_sendability():
    assert "blocked" not in SENDABILITY_VALUES
    with pytest.raises(ValueError):
        sender_meets_requirements("blocked", sender_is_premium=True)


# ---- storage / migration ----


def test_legacy_table_is_migrated_and_old_rows_unresolved(tmp_path):
    path = tmp_path / "users.db"
    campaigns = DmCampaignRepository(path)
    campaign_id = campaigns.get_campaign_by_key("insurance").id
    campaigns.close()
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE dm_outreach (
            id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER NOT NULL, source_chat_id INTEGER NOT NULL,
            source_chat_identifier TEXT, source_chat_title TEXT, source_message_id INTEGER NOT NULL,
            source_message_at TEXT, source_link TEXT, source_reply_to_msg_id INTEGER, recipient_user_id INTEGER,
            recipient_username TEXT, source_text TEXT NOT NULL, status TEXT NOT NULL, draft_after_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, context_json TEXT, primary_text TEXT, follow_up_text TEXT,
            evidence_strength TEXT, used_context_refs_json TEXT, filter_reason TEXT, error_kind TEXT, error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, generated_at TEXT,
            UNIQUE (source_chat_id, source_message_id))"""
    )
    conn.execute(
        "INSERT INTO dm_outreach (campaign_id, source_chat_id, source_message_id, source_text, status, filter_reason,"
        " created_at, updated_at) VALUES (?, -1001, 7, 'старое', 'filtered', 'no_username',"
        " '2026-10-04T18:00:00', '2026-10-04T18:00:00')", (campaign_id,),
    )
    conn.commit()
    conn.close()

    outreach = DmOutreachRepository(path)
    try:
        (old,) = outreach.list_all()
        assert (old.status, old.filter_reason, old.sendability, old.contact_require_premium) == (
            STATUS_FILTERED, "no_username", SENDABILITY_UNRESOLVED, None)
        DmOutreachRepository(path).close()  # повторное открытие — миграция идемпотентна
    finally:
        outreach.close()
    conn = sqlite3.connect(path)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(dm_outreach)")]
    conn.close()
    assert columns.count("sendability") == 1 and columns.count("contact_require_premium") == 1
    assert "access_hash" not in " ".join(columns)


def test_invalid_sendability_is_rejected(env):
    _, _, outreach, _, _, insurance = env
    with pytest.raises(ValueError):
        outreach.insert_candidate(
            campaign_id=insurance.id, source_chat_id=-1, source_chat_identifier="VerhniyLars", source_chat_title=None,
            source_message_id=1, source_message_at=T0, source_link=None, source_reply_to_msg_id=None,
            recipient_user_id=5, recipient_username=None, source_text="x", status=STATUS_PENDING_CONTEXT, now=T0,
            draft_after_at=T0, sendability="maybe",
        )


# ---- telegram_source: flag only from the already-fetched sender ----


def test_contact_require_premium_from_loaded_user():
    assert _contact_require_premium(SimpleNamespace(sender=User(id=1, contact_require_premium=True))) is True
    assert _contact_require_premium(SimpleNamespace(sender=User(id=1))) is False


def test_contact_require_premium_unknown_without_user_object():
    assert _contact_require_premium(SimpleNamespace(sender=None)) is None
    assert _contact_require_premium(SimpleNamespace()) is None
    channel = Channel(id=1, title="c", photo=None, date=None)
    assert _contact_require_premium(SimpleNamespace(sender=channel)) is None
