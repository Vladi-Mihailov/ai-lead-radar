"""Phase 3A: независимые права Telegram-аккаунта (can_send_dm /
can_invite_to_groups / is_premium) — миграция, защита инвайтера, контракт
будущего DM sender selector и UI admin-бота. Без Telegram/OpenAI."""

import asyncio
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from test_inviter_admin_bot_conversation import _TRUSTED_ID, _Fixture
from test_inviter_candidates import (
    _make_client_factory,
    _run_service,
    _seed_user,
    _setup_db,
)

from reader.dm_campaigns.sendability import (
    SENDABILITY_PREMIUM_REQUIRED,
    SENDABILITY_SOURCE_MESSAGE,
    SENDABILITY_USERNAME,
    dm_sender_eligible,
)
from reader.inviter.repository import (
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)
from reader.inviter.service import InviterService
from reader.inviter.worker import InviterWorker
from reader.inviter_admin_bot import texts
from reader.inviter_admin_bot.keyboards import (
    account_card_keyboard,
    encode_account_invite_confirm_callback,
    encode_account_open_callback,
    invite_confirm_keyboard,
)

_OTHER_ID = 999888777


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import reader.inviter.service as service_module

    async def instant_sleep(seconds):
        pass

    monkeypatch.setattr(service_module.asyncio, "sleep", instant_sleep)


def _accounts(db_path):
    return TelegramAccountRepository(db_path)


def _create(repo, name, **caps):
    return repo.create(name=name, phone="+995500000001", session_name=name.lstrip("@"),
                       session_path=f"{name.lstrip('@')}.session", daily_limit=10, **caps)


# ---- migration ----


def _legacy_accounts_table(path: Path, names):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE telegram_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT NOT NULL,
            session_name TEXT NOT NULL, session_path TEXT NOT NULL, daily_limit INTEGER NOT NULL DEFAULT 30,
            enabled BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP)"""
    )
    for i, name in enumerate(names):
        conn.execute("INSERT INTO telegram_accounts (name, phone, session_name, session_path, enabled) "
                     "VALUES (?, '+1', ?, ?, ?)", (name, name, f"{name}.session", i % 2))
    conn.commit()
    conn.close()


def test_migration_grants_no_capability_to_anyone(tmp_path):
    """БЕЗ blanket backfill: старые строки, как и новые, получают 0/0/NULL;
    enabled и остальные поля не трогаются; повторное открытие идемпотентно."""
    path = tmp_path / "users.db"
    _legacy_accounts_table(path, ["@a", "@b"])
    repo = _accounts(path)
    try:
        before = repo.list()
        assert [(a.can_invite_to_groups, a.can_send_dm, a.is_premium) for a in before] == [(False, False, None)] * 2
        assert [a.enabled for a in before] == [False, True]
        repo.update(before[1].id, can_invite_to_groups=True)  # явная выдача (set-capabilities/admin-бот)
        new = _create(repo, "@new_manager")
        assert (new.can_invite_to_groups, new.can_send_dm, new.is_premium) == (False, False, None)
    finally:
        repo.close()
    reopened = _accounts(path)
    try:
        caps = {a.name: a.can_invite_to_groups for a in reopened.list()}
        assert caps == {"@a": False, "@b": True, "@new_manager": False}
    finally:
        reopened.close()


def test_set_capabilities_cli_dry_run_then_apply(tmp_path):
    from reader.inviter.manage import set_capabilities

    path = tmp_path / "users.db"
    repo = _accounts(path)
    try:
        inviter = _create(repo, "@inviter", enabled=False)
        manager = _create(repo, "@manager")
        old = _create(repo, "@old", is_old=True)
    finally:
        repo.close()
    dry = set_capabilities(path, [inviter.id], invite=True, dm=None, apply=False)
    assert dry[0].startswith("DRY RUN") and "invite 0->1" in dry[0]
    repo = _accounts(path)
    try:
        assert repo.get(inviter.id).can_invite_to_groups is False  # dry-run ничего не пишет
    finally:
        repo.close()
    set_capabilities(path, [inviter.id], invite=True, dm=None, apply=True)
    set_capabilities(path, [manager.id], invite=False, dm=True, apply=True)
    with pytest.raises(SystemExit):
        set_capabilities(path, [old.id], invite=True, dm=None, apply=True)  # OLD — fail-closed
    repo = _accounts(path)
    try:
        assert (repo.get(inviter.id).can_invite_to_groups, repo.get(inviter.id).can_send_dm,
                repo.get(inviter.id).enabled) == (True, False, False)  # enabled не тронут
        assert (repo.get(manager.id).can_invite_to_groups, repo.get(manager.id).can_send_dm) == (False, True)
        assert repo.get(old.id).can_invite_to_groups is False
    finally:
        repo.close()


def test_fresh_db_new_accounts_have_no_capabilities(tmp_path):
    repo = _accounts(tmp_path / "users.db")
    try:
        account = _create(repo, "@fresh")
        assert (account.can_send_dm, account.can_invite_to_groups, account.is_premium) == (False, False, None)
    finally:
        repo.close()


def test_dm_only_account(tmp_path):
    repo = _accounts(tmp_path / "users.db")
    try:
        account = _create(repo, "@alenaogi_like", can_send_dm=True)
        assert (account.can_send_dm, account.can_invite_to_groups) == (True, False)
    finally:
        repo.close()


# ---- inviter safety ----


def _inviter_setup(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["страхов"], access_hash=11, is_bot=False)
    campaigns = InviteCampaignRepository(db_path)
    try:
        campaign = campaigns.create(name="Страхование", keyword="страхов", target_chat="@car_ins_georgia", enabled=True)
    finally:
        campaigns.close()
    return db_path, campaign


def _invites(db_path):
    repo = UserCampaignInviteRepository(db_path)
    try:
        return repo.list()
    finally:
        repo.close()


def test_inviter_excludes_dm_only_account(tmp_path):
    db_path, _ = _inviter_setup(tmp_path)
    repo = _accounts(db_path)
    try:
        _create(repo, "@dm_only", can_send_dm=True, can_invite_to_groups=False)
    finally:
        repo.close()
    created: list = []
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(created=created), execute=True))
    assert created == []          # ни одного подключения сессии
    assert _invites(db_path) == []


def test_inviter_uses_only_invite_capable_accounts(tmp_path):
    db_path, _ = _inviter_setup(tmp_path)
    repo = _accounts(db_path)
    try:
        _create(repo, "@dm_only", can_send_dm=True)
        inviter = _create(repo, "@inviter", can_invite_to_groups=True)
    finally:
        repo.close()
    created: list = []
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(created=created), execute=True))
    assert [c.account.name for c in created] == ["@inviter"]
    assert {i.account_id for i in _invites(db_path)} == {inviter.id}


@pytest.mark.parametrize("execute", [True, False], ids=["execute", "dry_run"])
def test_direct_internal_call_with_dm_only_account_is_rejected(tmp_path, execute):
    db_path, campaign = _inviter_setup(tmp_path)
    accounts, campaigns, invites = _accounts(db_path), InviteCampaignRepository(db_path), UserCampaignInviteRepository(db_path)
    try:
        dm_only = _create(accounts, "@dm_only", can_send_dm=True)
        created: list = []
        service = InviterService(accounts, campaigns, invites, client_factory=_make_client_factory(created=created),
                                 session_checker=lambda account: True)
        if execute:
            assert asyncio.run(service._execute_account(campaign, dm_only, found=1)) is None
            assert asyncio.run(service.run_one_worker_attempt(campaign, dm_only, hourly_limit=10)) is None
        else:
            assert asyncio.run(service._dry_run_account(campaign, dm_only, invites.select_candidates(campaign.id, limit=5))) is None
        assert created == [] and invites.list() == []
    finally:
        accounts.close()
        campaigns.close()
        invites.close()


def test_worker_rotation_excludes_dm_only_accounts(tmp_path):
    db_path, _ = _inviter_setup(tmp_path)
    accounts, campaigns, invites = _accounts(db_path), InviteCampaignRepository(db_path), UserCampaignInviteRepository(db_path)
    try:
        _create(accounts, "@dm_only", can_send_dm=True)
        _create(accounts, "@inviter", can_invite_to_groups=True)
        worker = InviterWorker.__new__(InviterWorker)
        worker._account_repository, worker._campaign_repository = accounts, campaigns
        assert [account.name for _, account in worker._enabled_pairs()] == ["@inviter"]
    finally:
        accounts.close()
        campaigns.close()
        invites.close()


def test_retry_escalation_counts_only_invite_capable_accounts(tmp_path):
    """Кандидат, которого не смог резолвить единственный invite-аккаунт,
    становится invalid: DM-only аккаунт не считается «ещё не пробовавшим»."""
    db_path, _ = _inviter_setup(tmp_path)
    repo = _accounts(db_path)
    try:
        _create(repo, "@dm_only", can_send_dm=True)
        _create(repo, "@inviter", can_invite_to_groups=True)
    finally:
        repo.close()
    factory = _make_client_factory(
        get_input_entity_errors={"@inviter": ValueError("не в кэше")},
        entity_responses={"@inviter": {"@user1": ValueError('No user has "user1" as username'), 1: KeyError(1)}},
    )
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
    (row,) = _invites(db_path)
    assert (row.status, row.failure_kind) == ("invalid", "unresolved")


# ---- future DM sender selector contract ----


def test_dm_sender_contract(tmp_path):
    repo = _accounts(tmp_path / "users.db")
    try:
        invite_only = _create(repo, "@invite_only", can_invite_to_groups=True, is_premium=True)
        dm_plain = _create(repo, "@dm_plain", can_send_dm=True, is_premium=False)
        dm_unknown = _create(repo, "@dm_unknown", can_send_dm=True)
        dm_premium = _create(repo, "@dm_premium", can_send_dm=True, is_premium=True)
    finally:
        repo.close()
    for sendability in (SENDABILITY_USERNAME, SENDABILITY_SOURCE_MESSAGE, SENDABILITY_PREMIUM_REQUIRED):
        assert dm_sender_eligible(invite_only, sendability) is False  # инвайты не дают права на ЛС
        assert dm_sender_eligible(dm_premium, sendability) is True
    for sendability in (SENDABILITY_USERNAME, SENDABILITY_SOURCE_MESSAGE):
        assert dm_sender_eligible(dm_plain, sendability) is True      # Premium не нужен
        assert dm_sender_eligible(dm_unknown, sendability) is True
    assert dm_sender_eligible(dm_plain, SENDABILITY_PREMIUM_REQUIRED) is False
    assert dm_sender_eligible(dm_unknown, SENDABILITY_PREMIUM_REQUIRED) is False  # None = не Premium


def test_capabilities_independent_from_enabled(tmp_path):
    repo = _accounts(tmp_path / "users.db")
    try:
        account = _create(repo, "@x", can_send_dm=True, enabled=False)
        assert dm_sender_eligible(account, SENDABILITY_USERNAME) is True   # enabled — забота selector, не права
        flipped = repo.update(account.id, enabled=True)
        assert (flipped.can_send_dm, flipped.can_invite_to_groups) == (True, False)
    finally:
        repo.close()


# ---- admin bot UI ----


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


def _account(fx, **caps):
    return fx.accounts.create(name="@manager", phone="+995500000001", session_name="manager",
                              session_path=str(fx.db_path.parent / "manager"), daily_limit=15,
                              telegram_user_id=100, **caps)


def _labels(rows):
    return [button.text for row in rows for button in row]


def test_card_shows_capabilities(fx):
    account = _account(fx, can_send_dm=True, is_premium=True)
    reply = fx.controller.handle_account_open(account.id, telegram_user_id=_TRUSTED_ID)
    assert "📩 ЛС: ✅" in reply.text and "👥 Инвайты: ❌" in reply.text and "⭐ Premium: да" in reply.text
    assert "Лимит инвайтов: 15 в день" in reply.text
    assert (reply.account_card_dm, reply.account_card_invite) == (True, False)
    labels = _labels(account_card_keyboard(account.id, enabled=True, can_send_dm=True, can_invite=False))
    assert "🚫 Выключить ЛС" in labels and "✅ Разрешить инвайты" in labels  # подпись = действие
    flipped = _labels(account_card_keyboard(account.id, enabled=True, can_send_dm=False, can_invite=True))
    assert "✅ Разрешить ЛС" in flipped and "🚫 Запретить инвайты" in flipped


def test_card_shows_unknown_premium(fx):
    account = _account(fx)
    assert "⭐ Premium: неизвестно" in fx.controller.handle_account_open(account.id, telegram_user_id=_TRUSTED_ID).text


def test_dm_toggle_changes_only_can_send_dm(fx):
    account = _account(fx)
    reply = fx.controller.handle_account_dm_toggle(account.id, telegram_user_id=_TRUSTED_ID)
    after = fx.accounts.get(account.id)
    assert (after.can_send_dm, after.can_invite_to_groups, after.enabled, after.daily_limit) == (True, False, True, 15)
    assert reply.account_card_dm is True and "📩 ЛС: ✅" in reply.text
    fx.controller.handle_account_dm_toggle(account.id, telegram_user_id=_TRUSTED_ID)
    assert fx.accounts.get(account.id).can_send_dm is False


def test_invite_enable_requires_confirmation(fx):
    account = _account(fx, can_send_dm=True)
    ask = fx.controller.handle_account_invite_toggle(account.id, telegram_user_id=_TRUSTED_ID)
    assert texts.INVITE_CONFIRM_TEXT in ask.text and ask.invite_confirm_account_id == account.id
    assert fx.accounts.get(account.id).can_invite_to_groups is False  # ещё не включено
    rows = invite_confirm_keyboard(account.id)
    assert _labels(rows) == ["✅ Да, разрешить", "❌ Отмена"]
    assert rows[0][0].data == encode_account_invite_confirm_callback(account.id)
    assert rows[1][0].data == encode_account_open_callback(account.id)  # отмена — назад на карточку

    done = fx.controller.handle_account_invite_confirm(account.id, telegram_user_id=_TRUSTED_ID)
    after = fx.accounts.get(account.id)
    assert (after.can_invite_to_groups, after.can_send_dm) == (True, True)
    assert done.account_card_invite is True


def test_invite_cancel_leaves_state_unchanged(fx):
    account = _account(fx)
    fx.controller.handle_account_invite_toggle(account.id, telegram_user_id=_TRUSTED_ID)
    fx.controller.handle_account_open(account.id, telegram_user_id=_TRUSTED_ID)  # «❌ Отмена»
    assert fx.accounts.get(account.id).can_invite_to_groups is False


def test_invite_disable_needs_no_confirmation(fx):
    account = _account(fx, can_invite_to_groups=True)
    reply = fx.controller.handle_account_invite_toggle(account.id, telegram_user_id=_TRUSTED_ID)
    assert fx.accounts.get(account.id).can_invite_to_groups is False
    assert reply.invite_confirm_account_id is None and reply.account_card_invite is False


@pytest.mark.parametrize("action", ["handle_account_dm_toggle", "handle_account_invite_toggle",
                                    "handle_account_invite_confirm"])
def test_untrusted_user_cannot_change_capabilities(fx, action):
    account = _account(fx)
    reply = getattr(fx.controller, action)(account.id, telegram_user_id=_OTHER_ID)
    assert reply.text == texts.ACCESS_DENIED_TEXT
    after = fx.accounts.get(account.id)
    assert (after.can_send_dm, after.can_invite_to_groups) == (False, False)


@pytest.mark.parametrize("action", ["handle_account_dm_toggle", "handle_account_invite_confirm"])
def test_old_account_capabilities_are_fail_closed(fx, action):
    account = fx.accounts.create(name="@old", phone="+1", session_name="old", session_path="old", daily_limit=15,
                                 telegram_user_id=200, is_old=True)
    reply = getattr(fx.controller, action)(account.id, telegram_user_id=_TRUSTED_ID)
    assert reply.text == texts.OLD_ACCOUNT_CAPABILITY_BLOCKED_TEXT
    after = fx.accounts.get(account.id)
    assert (after.can_send_dm, after.can_invite_to_groups) == (False, False)


@pytest.mark.parametrize("test_mode", [None, 1], ids=["regular", "test_mode"])
def test_manager_dm_only_account_is_never_selected_by_inviter(tmp_path, test_mode):
    """Менеджерский аккаунт (как @alenaogi/@Vladi_mihailov после настройки):
    enabled, Premium, ЛС — да, инвайты — нет. Ни обычный, ни тестовый (--test)
    прогон его не подключает и не приглашает от его имени."""
    db_path, _ = _inviter_setup(tmp_path)
    repo = _accounts(db_path)
    try:
        manager = _create(repo, "@manager", enabled=True, can_send_dm=True, can_invite_to_groups=False, is_premium=True)
        assert dm_sender_eligible(manager, SENDABILITY_PREMIUM_REQUIRED) is True  # годится для ЛС
    finally:
        repo.close()
    created: list = []
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(created=created), execute=True,
                             max_successful_invites=test_mode))
    assert created == [] and _invites(db_path) == []
