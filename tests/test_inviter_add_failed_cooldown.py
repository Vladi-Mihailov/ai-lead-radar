"""Пауза АККАУНТА после CHAT_MEMBER_ADD_FAILED: 1-й за 24 ч -> 1 ч, 2-й ->
6 ч, 3-й и далее -> 24 ч; успешное приглашение обнуляет серию. Регрессия
production 13–14.10: аккаунт после такого ответа продолжал приглашать
следующих кандидатов и доходил до PeerFlood. PeerFlood/FloodWait и
дедупликация invite_attempts не меняются. Без реального Telegram."""

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from telethon.errors import FloodWaitError, PeerFloodError, RPCError
from telethon.tl.functions.messages import GetHistoryRequest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter.repository import TelegramAccountRepository, UserCampaignInviteRepository  # noqa: E402
from reader.inviter.worker import InviterWorker  # noqa: E402
from test_inviter_candidates import _make_client_factory, _run_service, _seed_user, _setup_db  # noqa: E402
from test_inviter_retry_policy import _accounts, _campaign, _invites  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import reader.inviter.service as service_module

    async def instant_sleep(seconds):
        pass

    monkeypatch.setattr(service_module.asyncio, "sleep", instant_sleep)


def _add_failed():
    return RPCError(request=None, message="CHAT_MEMBER_ADD_FAILED", code=400)


def _account(db_path, account_id):
    repo = TelegramAccountRepository(db_path)
    try:
        return repo.get(account_id)
    finally:
        repo.close()


def _unblock(db_path, account_id):
    """Имитация истечения паузы (blocked_until в прошлом)."""
    repo = TelegramAccountRepository(db_path)
    try:
        repo.update(account_id, blocked_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    finally:
        repo.close()


def _cooldown(db_path, account_id):
    acc = _account(db_path, account_id)
    return acc.blocked_reason, acc.blocked_until - datetime.now(timezone.utc)


def _seed(db_path, n):
    now = datetime.now(timezone.utc)
    for i in range(1, n + 1):
        _seed_user(db_path, i, username=f"lead{i}", keywords=["страхов"], access_hash=10 + i, is_bot=False,
                   last_seen_at=now - timedelta(minutes=i))


def _run_one(db_path, errors_for_acc1, created=None):
    factory = _make_client_factory(call_errors={"acc1": errors_for_acc1}, created=created)
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))


def _near(delta, expected):
    return abs(delta - expected) < timedelta(minutes=2)


def test_cooldown_grows_one_six_twenty_four_hours(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 4)
    _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    for expected in (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24), timedelta(hours=24)):
        created = []
        _run_one(db_path, [_add_failed()], created)
        assert len([r for c in created for r in c.call_requests]) == 1   # один RPC — дальше аккаунт стоп
        reason, delta = _cooldown(db_path, acc1.id)
        assert reason == "chat_member_add_failed" and _near(delta, expected), (expected, delta)
        _unblock(db_path, acc1.id)


def test_successful_invite_resets_streak(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 4)
    _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"], daily_limit=1)
    _run_one(db_path, [_add_failed()])
    _unblock(db_path, acc1.id)
    _run_one(db_path, [_add_failed()])
    assert _near(_cooldown(db_path, acc1.id)[1], timedelta(hours=6))
    _unblock(db_path, acc1.id)
    _run_one(db_path, [None])                                              # успех
    assert [r.status for r in _invites(db_path)][-1] == "pending"
    repo = TelegramAccountRepository(db_path)                              # место в дневном лимите
    repo.update(acc1.id, daily_limit=5)
    repo.close()
    _unblock(db_path, acc1.id)
    _run_one(db_path, [_add_failed()])
    assert _near(_cooldown(db_path, acc1.id)[1], timedelta(hours=1))


def test_cooled_account_is_skipped_and_another_account_continues(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 3)
    campaign = _campaign(db_path)
    acc1, acc2 = _accounts(db_path, ["acc1", "acc2"])
    created = []
    factory = _make_client_factory(call_errors={"acc1": [_add_failed()]}, created=created)
    account_repo = TelegramAccountRepository(db_path)
    campaign_repo = __import__("reader.inviter.repository", fromlist=["x"]).InviteCampaignRepository(db_path)
    invite_repo = UserCampaignInviteRepository(db_path)
    try:
        from reader.inviter.service import InviterService
        service = InviterService(account_repo, campaign_repo, invite_repo, client_factory=factory,
                                 session_checker=lambda a: True)
        worker = InviterWorker(service, campaign_repo, account_repo, invitations_per_account_per_hour=2,
                               poll_interval_seconds=0, shutdown_event=asyncio.Event())
        for _ in range(4):  # acc1, acc2, acc1 (на паузе — пропуск), acc2
            asyncio.run(worker.run_one_tick())
    finally:
        account_repo.close()
        campaign_repo.close()
        invite_repo.close()
    by_account = {}
    for c in created:
        by_account[c.account.name] = by_account.get(c.account.name, 0) + len(c.call_requests)
    assert by_account.get("acc1") == 1                                     # после CHAT_MEMBER_ADD_FAILED — ни одного RPC
    assert by_account.get("acc2") == 2                                     # другой аккаунт продолжает
    assert _cooldown(db_path, acc1.id)[0] == "chat_member_add_failed"
    assert _account(db_path, acc2.id).blocked_until is None
    assert campaign.id


def test_peer_flood_handling_unchanged(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 1)
    _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    _run_one(db_path, [PeerFloodError(request=None)])
    reason, delta = _cooldown(db_path, acc1.id)
    assert reason == "peer_flood" and _near(delta, timedelta(hours=24))


def test_flood_wait_handling_unchanged(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 1)
    _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    _run_one(db_path, [FloodWaitError(request=GetHistoryRequest, capture=3600)])
    reason, delta = _cooldown(db_path, acc1.id)
    assert reason == "flood_wait" and _near(delta, timedelta(hours=1))


def test_other_rpc_errors_do_not_start_cooldown(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 1)
    _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    _run_one(db_path, [RPCError(request=None, message="SOME_OTHER_ERROR", code=400)])
    assert _account(db_path, acc1.id).blocked_until is None


def test_invite_attempts_dedup_unchanged(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 1)
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    _run_one(db_path, [_add_failed()])
    repo = UserCampaignInviteRepository(db_path)
    try:
        assert repo.attempted(campaign.id, 1, acc1.id)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE user_campaign_invites SET next_attempt_at = '2000-01-01T00:00:00'")
        conn.commit()
        conn.close()
        assert repo.select_candidates(campaign.id, limit=10, account_id=acc1.id) == []
    finally:
        repo.close()
