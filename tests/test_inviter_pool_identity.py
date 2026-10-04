"""Пул кампании (Армения) без ограничения давности и без обязательного
username: candidate без username резолвится приглашающим аккаунтом по его
сообщению в источнике (InputPeerUserFromMessage), а не по access_hash
читающего аккаунта (production: тот для инвайтеров -> KeyError). Политика
повторов (failure_kind/next_attempt_at/per-account 'unresolved'/escalation
в 'invalid') — без изменений.

Всё — на временных SQLite-файлах и фейковых клиентах, без реального
Telegram."""

import asyncio
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.types import InputPeerUser, InputPeerUserFromMessage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from test_inviter_candidates import (
    _make_client_factory,
    _run_service,
    _seed_user,
    _setup_db,
)
from test_inviter_multi_campaign import (
    _campaign,
    _create_campaigns,
    _FakeHistoryClient,
    _import,
    _user,
)
from test_inviter_retry_policy import _expire_backoff

from reader.inviter import manage
from reader.inviter.repository import (
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)

_NO_USERNAME = 103


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import reader.inviter.service as service_module

    async def instant_sleep(seconds):
        pass

    monkeypatch.setattr(service_module.asyncio, "sleep", instant_sleep)


def _msg_at(message_id, text, sender, when):
    return SimpleNamespace(id=message_id, raw_text=text, sender=sender, date=when)


def _history():
    """101 — с username, свежий; 102 — с username, сообщение 2022 года (за
    пределами любых 180 дней); 103 — без username, 2023 год."""
    return [
        _msg_at(10, "нужна страховка", _user(102), datetime(2022, 4, 16, tzinfo=timezone.utc)),
        _msg_at(20, "где страховка?", _user(_NO_USERNAME, username=None), datetime(2023, 4, 6, tzinfo=timezone.utc)),
        _msg_at(30, "страховка на авто", _user(101), datetime.now(timezone.utc)),
    ]


def _armenia(db_path, *, lead_max_age_days, enabled=True):
    _create_campaigns(db_path, ge_enabled=False, am_enabled=enabled)
    manage.ensure_campaign(
        db_path, slug="am_insurance_sadakhlo", name="Армения — Садахло", keyword="страх",
        target_chat="@osagoarmen", match_rule="ru_insurance", lead_max_age_days=lead_max_age_days,
    )
    am = _campaign(db_path, "am_insurance_sadakhlo")
    _import(db_path, am, _FakeHistoryClient(_history()))
    return am


def _accounts(db_path, names):
    repo = TelegramAccountRepository(db_path)
    try:
        return [
            repo.create(name=n, phone=f"+99550000{i:04d}", session_name=n, session_path=f"{n}.session",
                        daily_limit=10, verify_membership=False)
            for i, n in enumerate(names)
        ]
    finally:
        repo.close()


def _candidates(db_path, campaign_id, account_id=None):
    repo = UserCampaignInviteRepository(db_path)
    try:
        return [c.user_id for c in repo.select_candidates(campaign_id, limit=100, account_id=account_id)]
    finally:
        repo.close()


def _invites(db_path):
    repo = UserCampaignInviteRepository(db_path)
    try:
        return repo.list()
    finally:
        repo.close()


def _sql(db_path, statement, params=()):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


# ---- age limit ----


def test_age_limit_zero_means_no_limit_and_old_leads_are_eligible(tmp_path):
    db_path = _setup_db(tmp_path)
    am = _armenia(db_path, lead_max_age_days=180)
    assert am.lead_max_age_days == 180
    assert _candidates(db_path, am.id) == [101]  # 2022/2023 — за пределами 180 дней

    # Обычный путь снятия ограничения (CLI --lead-max-age-days 0) -> NULL.
    manage.ensure_campaign(db_path, slug="am_insurance_sadakhlo", name="Армения — Садахло",
                           keyword="страх", target_chat="@osagoarmen", lead_max_age_days=0)
    am = _campaign(db_path, "am_insurance_sadakhlo")
    assert am.lead_max_age_days is None
    # Порядок — по последнему совпавшему сообщению, свежие первыми.
    assert _candidates(db_path, am.id) == [101, _NO_USERNAME, 102]


# ---- identity ----


def test_username_lead_is_resolved_by_username_and_invited(tmp_path):
    db_path = _setup_db(tmp_path)
    am = _armenia(db_path, lead_max_age_days=None)
    _accounts(db_path, ["acc1"])
    created: list = []
    factory = _make_client_factory(
        created=created, get_input_entity_errors={"acc1": ValueError("не в кэше")},
        entity_responses={"acc1": {"@u101": _user(101), "@u102": _user(102)}},
    )
    _sql(db_path, "DELETE FROM campaign_leads WHERE user_id = ?", (_NO_USERNAME,))

    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))

    assert {(i.user_id, i.campaign_id, i.status) for i in _invites(db_path)} == {
        (101, am.id, "pending"), (102, am.id, "pending"),
    }
    (client,) = created
    assert not any(isinstance(c, InputPeerUserFromMessage) for c in client.get_entity_calls)


def test_no_username_lead_is_resolved_via_source_message_and_invited(tmp_path):
    db_path = _setup_db(tmp_path)
    am = _armenia(db_path, lead_max_age_days=None)
    _accounts(db_path, ["acc1"])
    _sql(db_path, "DELETE FROM campaign_leads WHERE user_id IN (101, 102)")
    created: list = []
    factory = _make_client_factory(created=created, get_input_entity_errors={"acc1": ValueError("не в кэше")})

    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))

    ((invite),) = _invites(db_path)
    assert (invite.user_id, invite.campaign_id, invite.status) == (_NO_USERNAME, am.id, "pending")
    (client,) = created
    from_message = [c for c in client.get_entity_calls if isinstance(c, InputPeerUserFromMessage)]
    assert [(c.user_id, c.msg_id) for c in from_message] == [(_NO_USERNAME, 20)]
    assert "@sadahlo" in client.get_input_entity_calls
    # В приглашение уходит access_hash, выданный ЭТОМУ аккаунту, а не
    # сохранённый читающим аккаунтом (_user(): user_id * 10).
    # (Фейковый target — не Channel, поэтому AddChatUserRequest.)
    (request,) = [r for r in client.call_requests if isinstance(r, (InviteToChannelRequest, AddChatUserRequest))]
    invited = request.users[0] if isinstance(request, InviteToChannelRequest) else request.user_id
    assert invited == InputPeerUser(user_id=_NO_USERNAME, access_hash=_NO_USERNAME * 7)


def test_no_username_without_access_hash_is_not_selected(tmp_path):
    db_path = _setup_db(tmp_path)
    am = _armenia(db_path, lead_max_age_days=None)
    _sql(db_path, "UPDATE users SET access_hash = NULL WHERE user_id = ?", (_NO_USERNAME,))
    _sql(db_path, "UPDATE campaign_leads SET access_hash = NULL WHERE user_id = ?", (_NO_USERNAME,))
    assert _NO_USERNAME not in _candidates(db_path, am.id)


def test_no_username_without_source_message_is_not_selected(tmp_path):
    db_path = _setup_db(tmp_path)
    am = _armenia(db_path, lead_max_age_days=None)
    _sql(db_path, "UPDATE campaign_leads SET last_message_id = NULL WHERE user_id = ?", (_NO_USERNAME,))
    assert _NO_USERNAME not in _candidates(db_path, am.id)
    assert {101, 102} <= set(_candidates(db_path, am.id))


@pytest.mark.parametrize("status", ["invited", "pending", "joined", "invalid"])
def test_existing_invite_history_excludes_no_username_and_old_leads(tmp_path, status):
    db_path = _setup_db(tmp_path)
    am = _armenia(db_path, lead_max_age_days=None)
    repo = UserCampaignInviteRepository(db_path)
    try:
        repo.create(user_id=_NO_USERNAME, campaign_id=am.id, status=status)
        repo.create(user_id=102, campaign_id=am.id, status=status)
    finally:
        repo.close()
    assert _candidates(db_path, am.id) == [101]


# ---- retry policy ----


def test_no_username_retry_policy_is_per_account_then_terminal(tmp_path):
    db_path = _setup_db(tmp_path)
    am = _armenia(db_path, lead_max_age_days=None)
    _sql(db_path, "DELETE FROM campaign_leads WHERE user_id IN (101, 102)")
    acc1, acc2 = _accounts(db_path, ["acc1", "acc2"])
    # Сообщение удалено/недоступно: GetUsers по нему пуст -> KeyError, как и
    # сохранённый access_hash читающего аккаунта.
    responses = {("from_message", _NO_USERNAME): KeyError(_NO_USERNAME), _NO_USERNAME: KeyError(_NO_USERNAME)}
    factory = _make_client_factory(
        get_input_entity_errors={"acc1": ValueError("не в кэше"), "acc2": ValueError("не в кэше")},
        entity_responses={"acc1": dict(responses), "acc2": dict(responses)},
    )

    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
    (first,) = _invites(db_path)
    assert (first.account_id, first.status, first.failure_kind) == (acc1.id, "failed", "unresolved")
    assert "source_message: KeyError" in first.error and "stored_access_hash: KeyError" in first.error
    assert _candidates(db_path, am.id) == []  # backoff

    _expire_backoff(db_path)
    assert _candidates(db_path, am.id, account_id=acc1.id) == []
    assert _candidates(db_path, am.id, account_id=acc2.id) == [_NO_USERNAME]

    for _ in range(5):
        asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
        _expire_backoff(db_path)
    rows = _invites(db_path)
    assert [r.status for r in rows] == ["failed", "invalid"]
    assert _candidates(db_path, am.id) == []


# ---- Georgia / dry-run ----


def test_georgia_still_requires_username_and_keeps_its_settings(tmp_path):
    db_path = _setup_db(tmp_path)
    _armenia(db_path, lead_max_age_days=None)
    ge = _campaign(db_path, "ge_insurance")
    _seed_user(db_path, 1, username="georgian", keywords=["страх"], access_hash=1)
    _seed_user(db_path, 2, username=None, keywords=["страх"], access_hash=2)
    assert _candidates(db_path, ge.id) == [1]
    ge = _campaign(db_path, "ge_insurance")
    assert (ge.enabled, ge.lead_max_age_days, ge.source_chats) == (False, None, ())


def test_dry_run_sends_zero_invites_for_expanded_pool(tmp_path):
    db_path = _setup_db(tmp_path)
    am = _armenia(db_path, lead_max_age_days=None)
    _accounts(db_path, ["acc1"])
    created: list = []
    assert len(_candidates(db_path, am.id)) == 3

    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(created=created), execute=False))

    assert _invites(db_path) == []
    assert all(not client.call_requests for client in created)
