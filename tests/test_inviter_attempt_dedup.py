"""Один приглашающий аккаунт — не более ОДНОЙ реальной invite RPC на пару
(кампания, кандидат): факт попытки хранится в invite_attempts и занимается
атомарно ДО отправки. Регрессия production: после CHAT_MEMBER_ADD_FAILED
тот же аккаунт через backoff снова приглашал того же кандидата (10 пар,
11 лишних RPC в «Армения — Садахло»). Другой аккаунт пробовать может.
Без реального Telegram — фейковые клиенты tests/test_inviter_candidates.py."""

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest
from telethon.errors import FloodWaitError, RPCError, UserChannelsTooMuchError, UserPrivacyRestrictedError
from telethon.tl.functions.messages import GetHistoryRequest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter.repository import TelegramAccountRepository, UserCampaignInviteRepository  # noqa: E402
from test_inviter_candidates import _make_client_factory, _run_service, _seed_user, _setup_db  # noqa: E402
from test_inviter_retry_policy import _accounts, _campaign, _candidates, _expire_backoff, _invites  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import reader.inviter.service as service_module

    async def instant_sleep(seconds):
        pass

    monkeypatch.setattr(service_module.asyncio, "sleep", instant_sleep)


def _chat_member_add_failed():
    return RPCError(request=None, message="CHAT_MEMBER_ADD_FAILED", code=400)


def _only_enabled(db_path, account):
    """Оставить включённым только этот аккаунт (остальные — enabled=False)."""
    repo = TelegramAccountRepository(db_path)
    try:
        for other in repo.list():
            repo.update(other.id, enabled=other.id == account.id)
    finally:
        repo.close()


def _attempted(db_path, campaign_id, user_id, account_id):
    repo = UserCampaignInviteRepository(db_path)
    try:
        return repo.attempted(campaign_id, user_id, account_id)
    finally:
        repo.close()


@pytest.mark.parametrize("error", [None, "chat_member_add_failed", "privacy", "channels_too_much"])
def test_same_account_never_retries_same_candidate(tmp_path, error):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="lead", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    acc1, acc2 = _accounts(db_path, ["acc1", "acc2"])
    _only_enabled(db_path, acc1)
    exc = {None: None, "chat_member_add_failed": _chat_member_add_failed(),
           "privacy": UserPrivacyRestrictedError(request=None),
           "channels_too_much": UserChannelsTooMuchError(request=None)}[error]
    created = []
    factory = _make_client_factory(call_errors={"acc1": [exc]} if exc else None, created=created)
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
    assert len([r for c in created for r in c.call_requests]) == 1
    assert _attempted(db_path, campaign.id, 1, acc1.id)

    # Сколько бы ни прошло: backoff истёк, pending стал not_joined — acc1 его больше не получает.
    _expire_backoff(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE user_campaign_invites SET status = 'not_joined' WHERE status = 'pending'")
    conn.commit()
    conn.close()
    assert _candidates(db_path, campaign.id, account_id=acc1.id) == []
    created.clear()
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(created=created), execute=True))
    assert [r for c in created for r in c.call_requests] == []

    # Другой аккаунт, который его не пробовал, — может; кроме приватности:
    # она и раньше делала кандидата invalid для всей кампании (не меняется).
    assert _candidates(db_path, campaign.id, account_id=acc2.id) == ([] if error == "privacy" else [1])


def test_different_account_can_invite_after_chat_member_add_failed(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="lead", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    acc1, acc2 = _accounts(db_path, ["acc1", "acc2"])
    _only_enabled(db_path, acc1)
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(
        call_errors={"acc1": [_chat_member_add_failed()]}), execute=True))
    _expire_backoff(db_path)
    _only_enabled(db_path, acc2)
    created = []
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(created=created), execute=True))
    assert [c.account.name for c in created if c.call_requests] == ["acc2"]
    assert [(r.account_id, r.status) for r in _invites(db_path)][-1] == (acc2.id, "pending")


def test_pre_rpc_failure_does_not_consume_the_pair(tmp_path):
    """Резолв identity упал (до invite RPC) — пара не занята, тот же аккаунт
    после backoff снова может пробовать."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="flaky", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    factory = _make_client_factory(
        get_input_entity_errors={"acc1": ValueError("нет в кэше")},
        entity_responses={"acc1": {"@flaky": ConnectionError("network down"), 1: KeyError(1)}},
    )
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
    assert not _attempted(db_path, campaign.id, 1, acc1.id)
    _expire_backoff(db_path)
    assert _candidates(db_path, campaign.id, account_id=acc1.id) == [1]


def test_blocked_account_skip_does_not_consume_the_pair(tmp_path):
    from datetime import datetime, timedelta, timezone
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="lead", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    repo = TelegramAccountRepository(db_path)
    repo.update(acc1.id, blocked_until=datetime.now(timezone.utc) + timedelta(hours=1), blocked_reason="peer_flood")
    repo.close()
    created = []
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(created=created), execute=True))
    assert [r for c in created for r in c.call_requests] == []
    assert not _attempted(db_path, campaign.id, 1, acc1.id)


def test_floodwait_on_invite_releases_the_pair(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="lead", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(
        call_errors={"acc1": [FloodWaitError(request=GetHistoryRequest, capture=3600)]}), execute=True))
    assert not _attempted(db_path, campaign.id, 1, acc1.id)


def test_attempted_pair_survives_restart(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="lead", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(
        call_errors={"acc1": [_chat_member_add_failed()]}), execute=True))
    _expire_backoff(db_path)
    # «Рестарт» — новые подключения к БД, без какого-либо состояния в памяти.
    fresh = UserCampaignInviteRepository(db_path)
    try:
        assert fresh.attempted(campaign.id, 1, acc1.id)
        assert [c.user_id for c in fresh.select_candidates(campaign.id, limit=10, account_id=acc1.id)] == []
    finally:
        fresh.close()


def test_already_attempted_candidate_is_skipped_and_next_one_is_invited(tmp_path):
    from datetime import datetime, timedelta, timezone
    db_path = _setup_db(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_user(db_path, 1, username="first", keywords=["страхов"], access_hash=11, is_bot=False,
               last_seen_at=now)
    _seed_user(db_path, 2, username="second", keywords=["страхов"], access_hash=22, is_bot=False,
               last_seen_at=now - timedelta(days=1))
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"], daily_limit=1)
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(
        call_errors={"acc1": [_chat_member_add_failed()]}), execute=True))
    _expire_backoff(db_path)
    created = []
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(created=created), execute=True))
    sent = [r for c in created for r in c.call_requests]
    assert len(sent) == 1 and _attempted(db_path, campaign.id, 2, acc1.id)
    assert [(r.user_id, r.status) for r in _invites(db_path)] == [(1, "failed"), (2, "pending")]


def test_concurrent_claim_only_one_wins(tmp_path):
    db_path = _setup_db(tmp_path)
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    first, second = UserCampaignInviteRepository(db_path), UserCampaignInviteRepository(db_path)
    try:
        results = [first.claim_attempt(campaign.id, 1, acc1.id), second.claim_attempt(campaign.id, 1, acc1.id)]
    finally:
        first.close()
        second.close()
    assert sorted(results) == [False, True]


def test_pair_claimed_elsewhere_gets_no_invite_rpc_and_next_candidate_is_used(tmp_path):
    """Параллельный тик занял пару между выборкой и отправкой: RPC для неё не
    делается, волна берёт следующего кандидата."""
    from datetime import datetime, timedelta, timezone
    import reader.inviter.repository as repo_module
    db_path = _setup_db(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_user(db_path, 1, username="first", keywords=["страхов"], access_hash=11, is_bot=False, last_seen_at=now)
    _seed_user(db_path, 2, username="second", keywords=["страхов"], access_hash=22, is_bot=False,
               last_seen_at=now - timedelta(days=1))
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"], daily_limit=1)
    original = repo_module.UserCampaignInviteRepository.select_candidates

    def select_then_race(self, campaign_id, *, limit, account_id=None):
        rows = original(self, campaign_id, limit=limit, account_id=account_id)
        if account_id is not None:
            other = UserCampaignInviteRepository(db_path)  # «другой процесс»
            other.claim_attempt(campaign_id, 1, account_id)
            other.close()
        return rows

    repo_module.UserCampaignInviteRepository.select_candidates = select_then_race
    try:
        created = []
        asyncio.run(_run_service(db_path, client_factory=_make_client_factory(created=created), execute=True))
    finally:
        repo_module.UserCampaignInviteRepository.select_candidates = original
    sent = [r for c in created for r in c.call_requests]
    assert len(sent) == 1
    assert [(r.user_id, r.status) for r in _invites(db_path)] == [(2, "pending")]


def test_backfill_marks_existing_rpc_attempts_but_not_pre_rpc_failures(tmp_path):
    db_path = _setup_db(tmp_path)
    campaign = _campaign(db_path)
    acc1, acc2 = _accounts(db_path, ["acc1", "acc2"])
    repo = UserCampaignInviteRepository(db_path)
    try:
        repo.create(user_id=1, campaign_id=campaign.id, account_id=acc1.id, status="failed",
                    error="RPCError 400: CHAT_MEMBER_ADD_FAILED", failure_kind="transient")
        repo.create(user_id=2, campaign_id=campaign.id, account_id=acc1.id, status="pending")
        repo.create(user_id=3, campaign_id=campaign.id, account_id=acc1.id, status="failed", failure_kind="unresolved")
        repo.create(user_id=4, campaign_id=campaign.id, account_id=acc2.id, status="failed", failure_kind="account")
    finally:
        repo.close()
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE invite_attempts")  # БД до появления таблицы
    conn.commit()
    conn.close()
    assert _attempted(db_path, campaign.id, 1, acc1.id) and _attempted(db_path, campaign.id, 2, acc1.id)
    assert not _attempted(db_path, campaign.id, 3, acc1.id) and not _attempted(db_path, campaign.id, 4, acc2.id)


def test_backfill_is_idempotent_and_peer_flood_claim_is_kept(tmp_path):
    db_path = _setup_db(tmp_path)
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    repo = UserCampaignInviteRepository(db_path)
    try:
        repo.create(user_id=1, campaign_id=campaign.id, account_id=acc1.id, status="failed",
                    error="RPCError 400: CHAT_MEMBER_ADD_FAILED", failure_kind="transient")
        repo.create(user_id=1, campaign_id=campaign.id, account_id=acc1.id, status="failed",
                    error="RPCError 400: CHAT_MEMBER_ADD_FAILED", failure_kind="transient")
    finally:
        repo.close()
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE invite_attempts")
    conn.commit()
    conn.close()
    for _ in range(3):  # повторные открытия (рестарты) не добавляют строк
        UserCampaignInviteRepository(db_path).close()
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM invite_attempts").fetchone()[0] == 1
    conn.close()


def test_peer_flood_on_invite_keeps_the_claim(tmp_path):
    from telethon.errors import PeerFloodError
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="lead", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(
        call_errors={"acc1": [PeerFloodError(request=None)]}), execute=True))
    assert _attempted(db_path, campaign.id, 1, acc1.id)


def test_backfill_counts_legacy_peer_flood_but_not_flood_wait_or_write_forbidden(tmp_path):
    db_path = _setup_db(tmp_path)
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    repo = UserCampaignInviteRepository(db_path)
    try:
        for user_id, error in (
            (1, "Too many requests (caused by InvokeWithoutUpdatesRequest(InviteToChannelRequest))"),
            (2, "A wait of 3600 seconds is required (caused by InviteToChannelRequest)"),
            (3, "You can't write in this chat (caused by InvokeWithoutUpdatesRequest(InviteToChannelRequest))"),
        ):
            repo.create(user_id=user_id, campaign_id=campaign.id, account_id=acc1.id, status="failed",
                        error=error, failure_kind="account")
    finally:
        repo.close()
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE invite_attempts")
    conn.commit()
    conn.close()
    assert _attempted(db_path, campaign.id, 1, acc1.id)          # PeerFlood — попытка
    assert not _attempted(db_path, campaign.id, 2, acc1.id)      # FloodWait — нет
    assert not _attempted(db_path, campaign.id, 3, acc1.id)      # ChatWriteForbidden — нет
