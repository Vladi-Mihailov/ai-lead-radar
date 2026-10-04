"""Тесты reader/dm_campaigns/repository.py — только временная БД (tmp_path),
production data/users.db не открывается."""

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.dm_campaigns.models import normalize_list_input, normalize_text_input
from reader.dm_campaigns.repository import SEED_CAMPAIGNS, DmCampaignRepository
from reader.inviter.repository import TelegramAccountRepository


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "users.db"


@pytest.fixture
def repo(db_path):
    repository = DmCampaignRepository(db_path)
    yield repository
    repository.close()


@pytest.fixture
def accounts(db_path):
    repository = TelegramAccountRepository(db_path)
    yield repository
    repository.close()


def _account(accounts, name="@acc", **fields):
    return accounts.create(name=name, phone="+1", session_name=name, session_path=f"data/sessions/{name}", **fields)


def test_tables_created(db_path, repo):
    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"dm_campaigns", "dm_campaign_accounts"} <= tables
    # Phase 1 не создаёт таблицы будущих фаз.
    assert not tables & {"dm_outreach", "group_messages_recent", "account_session_leases"}


def test_exactly_three_disabled_seed_campaigns(repo):
    campaigns = repo.list_campaigns()
    assert [(c.key, c.title, c.scenario_name) for c in campaigns] == list(SEED_CAMPAIGNS)
    assert [c.key for c in campaigns] == ["fuel", "border_queue", "insurance"]
    assert all(not c.enabled for c in campaigns)
    assert all(not c.follow_up_enabled for c in campaigns)
    assert all(c.ai_guideline is None and c.resources == () and c.source_chats == () for c in campaigns)


def test_repeated_init_is_idempotent_and_keeps_manager_values(db_path, repo, accounts):
    fuel = repo.get_campaign_by_key("fuel")
    account = _account(accounts)
    repo.set_enabled(fuel.id, True)
    repo.update_guideline(fuel.id, "Сначала ответь по существу")
    repo.update_resources(fuel.id, ["@tplgee"])
    repo.set_campaign_account_enabled(fuel.id, account.id, True)
    repo.set_campaign_account_daily_limit(fuel.id, account.id, 3)

    for _ in range(2):
        again = DmCampaignRepository(db_path)
        campaigns = again.list_campaigns()
        reopened = again.get_campaign_by_key("fuel")
        allow = again.list_campaign_accounts(fuel.id)
        again.close()

        assert len(campaigns) == 3
        assert reopened.enabled is True
        assert reopened.ai_guideline == "Сначала ответь по существу"
        assert reopened.resources == ("@tplgee",)
        assert [(a.account_id, a.daily_limit) for a in allow] == [(account.id, 3)]


def test_reinit_does_not_enable_disabled_campaign(db_path, repo):
    border = repo.get_campaign_by_key("border_queue")
    repo.set_enabled(border.id, True)
    repo.set_enabled(border.id, False)
    again = DmCampaignRepository(db_path)
    assert again.get_campaign_by_key("border_queue").enabled is False
    again.close()


def test_get_and_missing(repo):
    insurance = repo.get_campaign_by_key("insurance")
    assert repo.get_campaign(insurance.id) == insurance
    assert repo.get_campaign(9999) is None
    assert repo.get_campaign_by_key("nope") is None


def test_toggle_enabled_only_changes_enabled(repo):
    fuel = repo.get_campaign_by_key("fuel")
    updated = repo.set_enabled(fuel.id, True)
    assert updated.enabled is True
    assert (updated.key, updated.title, updated.ai_guideline) == (fuel.key, fuel.title, fuel.ai_guideline)
    assert repo.set_enabled(fuel.id, False).enabled is False
    assert repo.set_enabled(9999, True) is None


def test_update_and_clear_guideline(repo):
    fuel = repo.get_campaign_by_key("fuel")
    assert repo.update_guideline(fuel.id, "  текст \n").ai_guideline == "текст"
    assert repo.update_guideline(fuel.id, "   ").ai_guideline is None
    assert repo.update_guideline(fuel.id, None).ai_guideline is None
    with pytest.raises(ValueError):
        repo.update_guideline(fuel.id, "x" * 2001)


def test_resources_normalization():
    assert normalize_list_input(" @tplgee, ,@ProtocolGEbot\n\n@TPLGEE , ") == ("@tplgee", "@ProtocolGEbot")
    assert normalize_list_input("") == ()
    assert normalize_list_input(None) == ()
    assert normalize_text_input("  ") is None


def test_update_resources(repo):
    fuel = repo.get_campaign_by_key("fuel")
    updated = repo.update_resources(fuel.id, normalize_list_input("@tplgee\n@ProtocolGEbot"))
    assert updated.resources == ("@tplgee", "@ProtocolGEbot")
    assert repo.update_resources(fuel.id, ()).resources == ()
    with pytest.raises(ValueError):
        repo.update_resources(fuel.id, [f"@r{i}" for i in range(31)])
    with pytest.raises(ValueError):
        repo.update_resources(fuel.id, ["x" * 101])


def test_source_chats_update_and_empty_means_all(repo):
    fuel = repo.get_campaign_by_key("fuel")
    assert repo.update_source_chats(fuel.id, ["VerhniyLars", "Sadahlo"]).source_chats == ("VerhniyLars", "Sadahlo")
    assert repo.update_source_chats(fuel.id, []).source_chats == ()


def test_fresh_context_chats_stored(repo):
    fuel = repo.get_campaign_by_key("fuel")
    assert repo.update_fresh_context_chats(fuel.id, ["geolars"]).fresh_context_chats == ("geolars",)


def test_follow_up(repo):
    fuel = repo.get_campaign_by_key("fuel")
    assert repo.set_follow_up_enabled(fuel.id, True).follow_up_enabled is True
    assert repo.update_follow_up_guideline(fuel.id, "Про штраф").follow_up_guideline == "Про штраф"
    assert repo.update_follow_up_guideline(fuel.id, "").follow_up_guideline is None
    assert repo.set_follow_up_enabled(fuel.id, False).follow_up_enabled is False


def test_account_allowlist_add_remove_and_no_duplicates(repo, accounts):
    fuel = repo.get_campaign_by_key("fuel")
    account = _account(accounts)
    assert repo.set_campaign_account_enabled(fuel.id, account.id, True) is True
    assert repo.set_campaign_account_enabled(fuel.id, account.id, True) is True  # идемпотентно
    assert [a.account_id for a in repo.list_campaign_accounts(fuel.id)] == [account.id]
    assert repo.list_campaign_accounts(repo.get_campaign_by_key("insurance").id) == []

    assert repo.set_campaign_account_enabled(fuel.id, account.id, False) is True
    assert repo.list_campaign_accounts(fuel.id) == []
    # telegram_accounts не затронут.
    assert accounts.get(account.id) == account


def test_duplicate_relation_impossible_at_db_level(db_path, repo, accounts):
    fuel = repo.get_campaign_by_key("fuel")
    account = _account(accounts)
    repo.set_campaign_account_enabled(fuel.id, account.id, True)
    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO dm_campaign_accounts (campaign_id, account_id) VALUES (?, ?)", (fuel.id, account.id))
    conn.close()


def test_nonexistent_ids_handled(repo, accounts):
    fuel = repo.get_campaign_by_key("fuel")
    assert repo.set_campaign_account_enabled(fuel.id, 12345, True) is False
    assert repo.set_campaign_account_enabled(9999, 1, True) is False
    assert repo.set_campaign_account_daily_limit(fuel.id, 12345, 3) is None


def test_allowlist_without_telegram_accounts_table(tmp_path):
    repository = DmCampaignRepository(tmp_path / "fresh.db")
    fuel = repository.get_campaign_by_key("fuel")
    assert repository.set_campaign_account_enabled(fuel.id, 1, True) is False
    repository.close()


def test_daily_limit_validation(repo, accounts):
    fuel = repo.get_campaign_by_key("fuel")
    account = _account(accounts)
    repo.set_campaign_account_enabled(fuel.id, account.id, True)
    assert repo.get_campaign_account(fuel.id, account.id).daily_limit is None  # по умолчанию не задан
    assert repo.set_campaign_account_daily_limit(fuel.id, account.id, 5).daily_limit == 5
    assert repo.set_campaign_account_daily_limit(fuel.id, account.id, None).daily_limit is None
    for bad in (0, -1, 51):
        with pytest.raises(ValueError):
            repo.set_campaign_account_daily_limit(fuel.id, account.id, bad)
    # Лимит инвайтера не используется как лимит ЛС.
    assert accounts.get(account.id).daily_limit == 30
