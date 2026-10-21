"""👤 Аккаунты в админ-боте: правая кнопка строки — "успешные / попытки / лимит"
за текущие сутки. Попытки — реальные invite RPC (InviteToChannelRequest/
AddChatUserRequest) по уже записанной истории user_campaign_invites, независимо
от исхода (CHAT_MEMBER_ADD_FAILED, приватность, PeerFlood, FloodWait на самой
RPC). Не попытки: резолв без RPC, отмена для бота, пропуск по лимиту/блокировке,
отсутствие кандидата, dry-run. Без реального Telegram."""

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from telethon.errors import RPCError, UserAlreadyParticipantError, UserPrivacyRestrictedError
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.types import Channel, InputPeerUser

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter.lead_pool import CampaignLeadRepository  # noqa: E402
from reader.inviter.repository import (  # noqa: E402
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)
from reader.inviter.runtime_state_repository import InviterRuntimeStateRepository  # noqa: E402
from reader.inviter_admin_bot.keyboards import accounts_page_keyboard  # noqa: E402
from reader.inviter_admin_bot.service import InviterAdminService  # noqa: E402
from test_inviter_candidates import _make_client_factory, _run_service, _seed_user, _setup_db  # noqa: E402
from test_inviter_retry_policy import _campaign, _invites  # noqa: E402

_INVITE_RPC = " (caused by InvokeWithoutUpdatesRequest(InviteToChannelRequest))"
_SUPERGROUP = Channel(id=1234567890, title="target", photo=None, date=None, megagroup=True, access_hash=987654321)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import reader.inviter.service as service_module

    async def instant_sleep(seconds):
        pass

    monkeypatch.setattr(service_module.asyncio, "sleep", instant_sleep)


def _account(db_path, name="acc1", *, daily_limit=15, blocked_until=None):
    repo = TelegramAccountRepository(db_path)
    try:
        account = repo.create(
            name=name, phone=f"+99550000{sum(map(ord, name)):04d}", session_name=name, session_path=f"{name}.session",
            daily_limit=daily_limit, can_invite_to_groups=True, verify_membership=False,
        )
        if blocked_until is not None:
            account = repo.update(account.id, blocked_until=blocked_until, blocked_reason="peer_flood")
        return account
    finally:
        repo.close()


def _attempts(db_path, account_id):
    repo = UserCampaignInviteRepository(db_path)
    try:
        return repo.count_today_invite_attempts(account_id)
    finally:
        repo.close()


def _row(db_path, account_id, user_id, *, status, error=None, invited=False, verified=False):
    repo = UserCampaignInviteRepository(db_path)
    try:
        now = datetime.now(timezone.utc)
        return repo.create(user_id=user_id, campaign_id=1, account_id=account_id, status=status, error=error,
                           invited_at=now if invited else None, verified_at=now if verified else None)
    finally:
        repo.close()


def _move_to_yesterday(db_path, invite_id):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE user_campaign_invites SET created_at = datetime('now', '-1 day') WHERE id = ?", (invite_id,))
        conn.commit()
    finally:
        conn.close()


def _admin_entries(db_path):
    accounts, campaigns = TelegramAccountRepository(db_path), InviteCampaignRepository(db_path)
    invites, runtime = UserCampaignInviteRepository(db_path), InviterRuntimeStateRepository(db_path)
    leads = CampaignLeadRepository(db_path)
    try:
        service = InviterAdminService(accounts, campaigns, invites, runtime, db_path=db_path, lead_repository=leads)
        return service.list_accounts()
    finally:
        for repo in (accounts, campaigns, invites, runtime, leads):
            repo.close()


def _invite_request():
    return InviteToChannelRequest(channel=_SUPERGROUP, users=[InputPeerUser(user_id=1, access_hash=1)])


# ---- правила подсчёта по истории ----


def test_every_invite_rpc_outcome_of_today_is_an_attempt(tmp_path):
    db_path = _setup_db(tmp_path)
    account = _account(db_path)
    _row(db_path, account.id, 1, status="pending", invited=True)                                   # успех
    _row(db_path, account.id, 2, status="joined", invited=True, verified=True)                     # уже участник
    _row(db_path, account.id, 3, status="failed", error="RPCError 400: CHAT_MEMBER_ADD_FAILED" + _INVITE_RPC)
    _row(db_path, account.id, 4, status="invalid",
         error="The user's privacy settings do not allow you to do this" + _INVITE_RPC)
    _row(db_path, account.id, 5, status="failed", error="Too many requests" + _INVITE_RPC)         # PeerFlood
    _row(db_path, account.id, 6, status="failed", error="A wait of 120 seconds is required" + _INVITE_RPC)
    _row(db_path, account.id, 7, status="failed", error="RPCError 400: USER_KICKED (caused by AddChatUserRequest)")

    assert _attempts(db_path, account.id) == 7


def test_outcomes_without_invite_rpc_are_not_attempts(tmp_path):
    db_path = _setup_db(tmp_path)
    account = _account(db_path)
    _row(db_path, account.id, 1, status="failed",
         error='1 (@x): не резолвится ни одним известным способом: username: ValueError: No user has "x" as username')
    _row(db_path, account.id, 2, status="failed",
         error="A wait of 30 seconds is required (caused by ResolveUsernameRequest)")             # FloodWait на резолве
    _row(db_path, account.id, 3, status="invalid",
         error="3 — Telegram-бот (подтверждено при резолве), приглашение отменено")

    assert _attempts(db_path, account.id) == 0


def test_only_today_and_only_this_account(tmp_path):
    db_path = _setup_db(tmp_path)
    account, other = _account(db_path, "acc1"), _account(db_path, "acc2")
    _row(db_path, account.id, 1, status="pending", invited=True)
    old = _row(db_path, account.id, 2, status="failed", error="RPCError 400: CHAT_MEMBER_ADD_FAILED" + _INVITE_RPC)
    _move_to_yesterday(db_path, old.id)
    _row(db_path, other.id, 3, status="pending", invited=True)

    assert _attempts(db_path, account.id) == 1
    assert _attempts(db_path, other.id) == 1


def test_verifying_yesterdays_pending_today_is_not_a_new_attempt(tmp_path):
    db_path = _setup_db(tmp_path)
    account = _account(db_path)
    invite = _row(db_path, account.id, 1, status="pending", invited=True)
    _move_to_yesterday(db_path, invite.id)
    repo = UserCampaignInviteRepository(db_path)
    try:
        repo.update(invite.id, status="joined", verified_at=datetime.now(timezone.utc))  # как _verify_pending_invites
    finally:
        repo.close()

    assert _attempts(db_path, account.id) == 0


# ---- сквозной прогон InviterService -> админ-бот ----


def test_success_attempts_limit_after_real_service_run(tmp_path):
    """4 кандидата с RPC (успех, приватность, уже участник, CMAF) и один, чей
    резолв упал без RPC: успешные = 2 (pending + joined), попытки = 4."""
    db_path = _setup_db(tmp_path)
    now = datetime.now(timezone.utc)
    for i in range(1, 6):
        _seed_user(db_path, i, username=f"lead{i}", keywords=["страхов"], access_hash=10 + i,
                   is_bot=None if i == 3 else False, last_seen_at=now - timedelta(minutes=i))
    _campaign(db_path, target="@osagoarmen")
    account = _account(db_path, "acc1", daily_limit=15)
    factory = _make_client_factory(
        call_errors={"acc1": [
            None,                                                                  # lead1: успех
            UserPrivacyRestrictedError(request=_invite_request()),                 # lead2
            UserAlreadyParticipantError(request=_invite_request()),                # lead4 (lead3 — без RPC)
            RPCError(request=_invite_request(), message="CHAT_MEMBER_ADD_FAILED", code=400),  # lead5
        ]},
        target_entities={"acc1": _SUPERGROUP},
        entity_responses={"acc1": {3: ConnectionError("network down")}},
    )
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))

    statuses = sorted((r.user_id, r.status) for r in _invites(db_path))
    assert statuses == [(1, "pending"), (2, "invalid"), (3, "failed"), (4, "joined"), (5, "failed")]
    (entry,) = _admin_entries(db_path)
    assert (entry.sent_today, entry.attempts_today, entry.daily_limit) == (2, 4, 15)
    labels = [b.text for row in accounts_page_keyboard(
        [(entry.id, entry.display_name, entry.enabled, entry.sent_today, entry.attempts_today, entry.daily_limit)]
    ) for b in row]
    assert labels[-1] == "2 / 4 / 15"
    assert account.id == entry.id


def test_dry_run_blocked_account_and_no_candidates_give_zero_attempts(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="lead1", keywords=["страхов"], access_hash=11, is_bot=False)
    _campaign(db_path, target="@osagoarmen")
    account = _account(db_path, "acc1")
    blocked = _account(db_path, "acc2", blocked_until=datetime.now(timezone.utc) + timedelta(hours=1))
    created = []
    factory = _make_client_factory(target_entities={"acc1": _SUPERGROUP, "acc2": _SUPERGROUP}, created=created)

    asyncio.run(_run_service(db_path, client_factory=factory, execute=False))       # dry-run
    assert _invites(db_path) == [] and _attempts(db_path, account.id) == 0

    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))        # acc1 приглашает, acc2 заблокирован
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))        # кандидатов больше нет
    assert _attempts(db_path, account.id) == 1
    assert _attempts(db_path, blocked.id) == 0
    assert sum(len(c.call_requests) for c in created) == 1
