"""CHAT_MEMBER_ADD_FAILED — результат по КАНДИДАТУ, а не ограничение аккаунта.

Регрессия production 12–20.10: 98 таких ответов пришлись на 17 кандидатов,
каждого перепробовали 3–7 разных аккаунтов (ни один не добавился), а прежняя
пауза аккаунта (1/6/24 ч) останавливала инвайтеры, которые в это же время
успешно приглашали других. Теперь: аккаунт не блокируется и не уведомляет
оператора; тот же аккаунт кандидата не повторяет (invite_attempts); кандидата
пробует не больше двух РАЗНЫХ аккаунтов, после второго отказа он 'invalid'
для кампании. PeerFlood/FloodWait — как раньше. Без реального Telegram."""

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from telethon.errors import (
    FloodWaitError,
    InputUserDeactivatedError,
    PeerFloodError,
    RPCError,
    UserChannelsTooMuchError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UserNotMutualContactError,
    UserPrivacyRestrictedError,
)
from telethon.tl.functions.messages import GetHistoryRequest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter.repository import (  # noqa: E402
    ADD_FAILED_MAX_ACCOUNTS,
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)
from reader.inviter.service import InviteErrorAction, InviterService, _classify_invite_error  # noqa: E402
from reader.inviter.worker import InviterWorker  # noqa: E402
from test_inviter_candidates import (  # noqa: E402
    _FakeOperatorNotifier,
    _make_client_factory,
    _run_service,
    _seed_user,
    _setup_db,
)
from test_inviter_retry_policy import _accounts, _campaign, _candidates, _expire_backoff, _invites  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import reader.inviter.service as service_module

    async def instant_sleep(seconds):
        pass

    monkeypatch.setattr(service_module.asyncio, "sleep", instant_sleep)


def _add_failed():
    return RPCError(request=None, message="CHAT_MEMBER_ADD_FAILED", code=400)


def _seed(db_path, n):
    now = datetime.now(timezone.utc)
    for i in range(1, n + 1):
        _seed_user(db_path, i, username=f"lead{i}", keywords=["страхов"], access_hash=10 + i, is_bot=False,
                   last_seen_at=now - timedelta(minutes=i))


def _account(db_path, account_id):
    repo = TelegramAccountRepository(db_path)
    try:
        return repo.get(account_id)
    finally:
        repo.close()


def _only_enabled(db_path, *accounts):
    ids = {a.id for a in accounts}
    repo = TelegramAccountRepository(db_path)
    try:
        for acc in repo.list():
            repo.update(acc.id, enabled=acc.id in ids)
    finally:
        repo.close()


def _run(db_path, errors_by_account=None, created=None, notifier=None):
    factory = _make_client_factory(call_errors=errors_by_account or {}, created=created)
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True, notifier=notifier))


def _rpc_users(created):
    users = []
    for client in created:
        for request in client.call_requests:
            # InviteToChannelRequest.users[0] либо AddChatUserRequest.user_id (обычная группа в фейке).
            target = request.users[0] if getattr(request, "users", None) else request.user_id
            users.append((client.account.name, getattr(target, "user_id", target)))
    return users


def test_classification_is_candidate_level():
    c = _classify_invite_error(_add_failed())
    # wait_seconds=None -> blocked_until аккаунту не ставится (blocked_reason без него не применяется).
    assert (c.action, c.db_status, c.failure_kind, c.wait_seconds) == (
        InviteErrorAction.SKIP_USER_END_CALL, "failed", "add_failed", None)
    assert not c.operator_message  # оператор не уведомляется


def test_add_failed_does_not_block_account_or_notify_operator(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 1)
    _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    notifier = _FakeOperatorNotifier()
    _run(db_path, {"acc1": [_add_failed()]}, notifier=notifier)
    acc = _account(db_path, acc1.id)
    assert (acc.blocked_until, acc.blocked_reason) == (None, None)
    assert [m for m in notifier.sent if m.startswith("⚠️")] == []
    (row,) = _invites(db_path)
    assert (row.status, row.failure_kind) == ("failed", "add_failed") and row.next_attempt_at is not None


def test_one_rpc_per_call_then_next_candidate_on_next_call(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 3)
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    created = []
    failing = _make_client_factory(call_errors={"acc1": [_add_failed()]}, created=created)
    healthy = _make_client_factory(created=created)
    calls_made = []

    def factory(account):  # отказ только у первого подключения (первый тик)
        calls_made.append(account.id)
        return failing(account) if len(calls_made) == 1 else healthy(account)

    accounts, campaigns, invites = (TelegramAccountRepository(db_path), InviteCampaignRepository(db_path),
                                    UserCampaignInviteRepository(db_path))
    try:
        service = InviterService(accounts, campaigns, invites, client_factory=factory, session_checker=lambda a: True)
        worker = InviterWorker(service, campaigns, accounts, invitations_per_account_per_hour=2,
                               poll_interval_seconds=0, shutdown_event=asyncio.Event())
        asyncio.run(worker.run_one_tick())
        assert len(_rpc_users(created)) == 1                      # отказ -> вызов закончен, второго RPC нет
        asyncio.run(worker.run_one_tick())                        # следующий тик — тот же аккаунт
    finally:
        accounts.close()
        campaigns.close()
        invites.close()
    calls = _rpc_users(created)
    assert len(calls) == 2 and calls[0][1] == 1 and calls[1][1] == 2   # взял следующего кандидата
    assert _account(db_path, acc1.id).blocked_until is None
    assert [(r.user_id, r.status) for r in _invites(db_path)] == [(1, "failed"), (2, "pending")]
    assert campaign.id


def test_same_account_never_retries_the_candidate(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 1)
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    _run(db_path, {"acc1": [_add_failed()]})
    _expire_backoff(db_path)
    assert _candidates(db_path, campaign.id, account_id=acc1.id) == []


def test_candidate_gets_one_other_account_then_becomes_invalid_for_campaign(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 1)
    campaign = _campaign(db_path)
    acc1, acc2, acc3 = _accounts(db_path, ["acc1", "acc2", "acc3"])
    _only_enabled(db_path, acc1)
    _run(db_path, {"acc1": [_add_failed()]})
    assert _candidates(db_path, campaign.id, account_id=acc2.id) == []   # 1 ч backoff для других аккаунтов
    _expire_backoff(db_path)
    assert _candidates(db_path, campaign.id, account_id=acc2.id) == [1]  # доступен одному другому аккаунту

    _only_enabled(db_path, acc2)
    _run(db_path, {"acc2": [_add_failed()]})
    rows = _invites(db_path)
    assert [(r.account_id, r.status, r.failure_kind) for r in rows] == [
        (acc1.id, "failed", "add_failed"), (acc2.id, "invalid", "add_failed")]
    assert f"от {ADD_FAILED_MAX_ACCOUNTS} разных аккаунтов" in rows[-1].error

    _expire_backoff(db_path)
    assert _candidates(db_path, campaign.id, account_id=acc3.id) == []   # третий аккаунт его не получает
    created = []
    _only_enabled(db_path, acc3)
    _run(db_path, created=created)
    assert _rpc_users(created) == []


def test_legacy_rows_from_two_accounts_exclude_candidate_for_third(tmp_path):
    """Старые строки (failure_kind='transient', до этого изменения) тоже
    учитываются — по тексту ошибки."""
    db_path = _setup_db(tmp_path)
    _seed(db_path, 1)
    campaign = _campaign(db_path)
    acc1, acc2, acc3 = _accounts(db_path, ["acc1", "acc2", "acc3"])
    repo = UserCampaignInviteRepository(db_path)
    try:
        for acc in (acc1, acc2):
            repo.create(user_id=1, campaign_id=campaign.id, account_id=acc.id, status="failed",
                        error="RPCError 400: CHAT_MEMBER_ADD_FAILED (caused by InviteToChannelRequest)",
                        failure_kind="transient")
    finally:
        repo.close()
    assert _candidates(db_path, campaign.id, account_id=acc3.id) == []
    repo = UserCampaignInviteRepository(db_path)
    try:
        assert repo.count_candidates(campaign.id) == 0
    finally:
        repo.close()


def test_history_survives_restart(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 1)
    campaign = _campaign(db_path)
    acc1, acc2, acc3 = _accounts(db_path, ["acc1", "acc2", "acc3"])
    _only_enabled(db_path, acc1)
    _run(db_path, {"acc1": [_add_failed()]})
    _expire_backoff(db_path)
    _only_enabled(db_path, acc2)
    _run(db_path, {"acc2": [_add_failed()]})
    fresh = UserCampaignInviteRepository(db_path)            # «рестарт» — новое подключение
    try:
        assert fresh.add_failed_account_ids(1, campaign.id) == {acc1.id, acc2.id}
        assert fresh.select_candidates(campaign.id, limit=10, account_id=acc3.id) == []
        assert fresh.select_candidates(campaign.id, limit=10, account_id=acc1.id) == []
    finally:
        fresh.close()


@pytest.mark.parametrize("exc", [
    UserPrivacyRestrictedError(request=GetHistoryRequest),
    UserChannelsTooMuchError(request=GetHistoryRequest),
    UserNotMutualContactError(request=GetHistoryRequest),
])
def test_explicit_candidate_errors_exclude_candidate_immediately(tmp_path, exc):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 1)
    campaign = _campaign(db_path)
    acc1, acc2 = _accounts(db_path, ["acc1", "acc2"])
    _only_enabled(db_path, acc1)
    _run(db_path, {"acc1": [exc]})
    (row,) = _invites(db_path)
    assert row.status == "invalid"
    _expire_backoff(db_path)
    assert _candidates(db_path, campaign.id, account_id=acc2.id) == []
    assert _account(db_path, acc1.id).blocked_until is None


def test_peer_flood_still_blocks_account(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 1)
    _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    _run(db_path, {"acc1": [PeerFloodError(request=None)]})
    acc = _account(db_path, acc1.id)
    assert acc.blocked_reason == "peer_flood" and acc.blocked_until > datetime.now(timezone.utc) + timedelta(hours=23)


def test_flood_wait_still_blocks_account(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 1)
    _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    _run(db_path, {"acc1": [FloodWaitError(request=GetHistoryRequest, capture=3600)]})
    acc = _account(db_path, acc1.id)
    assert acc.blocked_reason == "flood_wait"
    assert timedelta(minutes=58) < acc.blocked_until - datetime.now(timezone.utc) <= timedelta(hours=1)


def test_other_unknown_rpc_errors_still_stop_account(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed(db_path, 2)
    _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    notifier = _FakeOperatorNotifier()
    _run(db_path, {"acc1": [RPCError(request=None, message="SOMETHING_ELSE", code=400)]}, notifier=notifier)
    assert len([m for m in notifier.sent if m.startswith("⚠️")]) == 1
    assert _account(db_path, acc1.id).blocked_until is None


def test_add_failed_row_is_not_a_legacy_db_cooldown(tmp_path):
    """Старое значение blocked_reason из прошлой версии больше нигде не ставится."""
    db_path = _setup_db(tmp_path)
    _seed(db_path, 3)
    _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    for _ in range(3):
        _run(db_path, {"acc1": [_add_failed()]})
        _expire_backoff(db_path)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM telegram_accounts WHERE blocked_reason = 'chat_member_add_failed'"
                            ).fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("exc", [UserDeactivatedError(request=None), UserDeactivatedBanError(request=None)])
def test_our_account_deactivated_is_account_level_not_candidate(tmp_path, exc):
    """401 USER_DEACTIVATED(_BAN) в Telethon 1.44 — удалён/забанен НАШ аккаунт:
    аккаунт останавливается, кандидат НЕ становится invalid и остаётся другим."""
    c = _classify_invite_error(exc)
    assert (c.action, c.db_status, c.failure_kind) == (InviteErrorAction.STOP_ACCOUNT, "failed", "account")
    db_path = _setup_db(tmp_path)
    _seed(db_path, 1)
    campaign = _campaign(db_path)
    acc1, acc2 = _accounts(db_path, ["acc1", "acc2"])
    _only_enabled(db_path, acc1)
    notifier = _FakeOperatorNotifier()
    _run(db_path, {"acc1": [exc]}, notifier=notifier)
    (row,) = _invites(db_path)
    assert (row.status, row.failure_kind, row.next_attempt_at) == ("failed", "account", None)
    assert len([m for m in notifier.sent if m.startswith("⚠️")]) == 1
    assert _candidates(db_path, campaign.id, account_id=acc2.id) == [1]


def test_deleted_candidate_is_still_excluded_for_campaign(tmp_path):
    """400 INPUT_USER_DEACTIVATED — удалён именно приглашаемый пользователь."""
    db_path = _setup_db(tmp_path)
    _seed(db_path, 1)
    campaign = _campaign(db_path)
    acc1, acc2 = _accounts(db_path, ["acc1", "acc2"])
    _only_enabled(db_path, acc1)
    _run(db_path, {"acc1": [InputUserDeactivatedError(request=None)]})
    (row,) = _invites(db_path)
    assert row.status == "invalid"
    assert _candidates(db_path, campaign.id, account_id=acc2.id) == []
