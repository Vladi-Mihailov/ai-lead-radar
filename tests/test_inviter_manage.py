"""
Тесты reader.inviter.manage — штатное создание/обновление аккаунтов и
кампаний инвайтера без ручного редактирования SQLite (см. docstring
manage.py: data/users.db не входит в git, поэтому на новом окружении
TelegramAccountRepository/InviteCampaignRepository всегда пустые).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter import manage  # noqa: E402
from reader.inviter.repository import (  # noqa: E402
    InviteCampaignRepository,
    TelegramAccountRepository,
)


# ---- _parse_args ----


def test_parse_args_add_account_defaults():
    args = manage._parse_args(
        [
            "add-account", "--name", "@vladimihailov",
            "--session-name", "vladimihailov", "--session-path", "data/sessions/vladimihailov",
        ]
    )
    assert args.command == "add-account"
    assert args.name == "@vladimihailov"
    assert args.phone == ""  # опционален
    assert args.session_name == "vladimihailov"
    assert args.session_path == "data/sessions/vladimihailov"
    assert args.daily_limit == 30
    assert args.enabled is True


def test_parse_args_add_account_explicit_values():
    args = manage._parse_args(
        [
            "add-account", "--name", "@vladimihailov", "--phone", "+995593498317",
            "--session-name", "vladimihailov", "--session-path", "data/sessions/vladimihailov",
            "--daily-limit", "1", "--no-enabled",
        ]
    )
    assert args.phone == "+995593498317"
    assert args.daily_limit == 1
    assert args.enabled is False


def test_parse_args_add_campaign_defaults():
    args = manage._parse_args(
        ["add-campaign", "--name", "Страхование", "--keyword", "страх", "--target-chat", "@tplgee"]
    )
    assert args.command == "add-campaign"
    assert args.name == "Страхование"
    assert args.keyword == "страх"
    assert args.target_chat == "@tplgee"
    assert args.enabled is True


def test_parse_args_add_campaign_disabled():
    args = manage._parse_args(
        [
            "add-campaign", "--name", "Страхование", "--keyword", "страх",
            "--target-chat", "@tplgee", "--no-enabled",
        ]
    )
    assert args.enabled is False


# ---- ensure_account ----


def test_ensure_account_creates_when_absent(tmp_path):
    db_path = tmp_path / "users.db"

    account = manage.ensure_account(
        db_path, name="@vladimihailov", phone="+995593498317",
        session_name="vladimihailov", session_path="data/sessions/vladimihailov",
        daily_limit=1, enabled=True,
    )

    assert account.name == "@vladimihailov"
    assert account.phone == "+995593498317"
    assert account.daily_limit == 1
    assert account.enabled is True

    repository = TelegramAccountRepository(db_path)
    try:
        assert len(repository.list()) == 1
    finally:
        repository.close()


def test_ensure_account_updates_existing_instead_of_duplicating(tmp_path):
    """Идемпотентность — требование задачи: повторный вызов с тем же name
    не создаёт вторую запись."""
    db_path = tmp_path / "users.db"

    first = manage.ensure_account(
        db_path, name="@vladimihailov", phone="", session_name="vladimihailov",
        session_path="data/sessions/vladimihailov", daily_limit=30, enabled=True,
    )

    second = manage.ensure_account(
        db_path, name="@vladimihailov", phone="+995593498317",
        session_name="vladimihailov", session_path="data/sessions/vladimihailov",
        daily_limit=1, enabled=False,
    )

    assert second.id == first.id  # тот же ряд, не новый
    assert second.phone == "+995593498317"
    assert second.daily_limit == 1
    assert second.enabled is False

    repository = TelegramAccountRepository(db_path)
    try:
        accounts = repository.list()
        assert len(accounts) == 1  # не задублировалось
        assert accounts[0].daily_limit == 1
    finally:
        repository.close()


def test_ensure_account_defaults_phone_to_empty_string_when_omitted(tmp_path):
    db_path = tmp_path / "users.db"

    account = manage.ensure_account(
        db_path, name="@vladimihailov", phone="", session_name="vladimihailov",
        session_path="data/sessions/vladimihailov", daily_limit=30, enabled=True,
    )

    assert account.phone == ""


# ---- ensure_campaign ----


def test_ensure_campaign_creates_when_absent(tmp_path):
    db_path = tmp_path / "users.db"

    campaign = manage.ensure_campaign(
        db_path, name="Страхование", keyword="страх", target_chat="@tplgee", enabled=True,
    )

    assert campaign.name == "Страхование"
    assert campaign.keyword == "страх"
    assert campaign.target_chat == "@tplgee"
    assert campaign.enabled is True

    repository = InviteCampaignRepository(db_path)
    try:
        assert len(repository.list()) == 1
    finally:
        repository.close()


def test_ensure_campaign_updates_existing_instead_of_duplicating(tmp_path):
    db_path = tmp_path / "users.db"

    first = manage.ensure_campaign(
        db_path, name="Страхование", keyword="старое", target_chat="@old_chat", enabled=True,
    )

    second = manage.ensure_campaign(
        db_path, name="Страхование", keyword="страх", target_chat="@tplgee", enabled=False,
    )

    assert second.id == first.id
    assert second.keyword == "страх"
    assert second.target_chat == "@tplgee"
    assert second.enabled is False

    repository = InviteCampaignRepository(db_path)
    try:
        campaigns = repository.list()
        assert len(campaigns) == 1
        assert campaigns[0].target_chat == "@tplgee"
    finally:
        repository.close()


def test_ensure_account_and_ensure_campaign_are_independent(tmp_path):
    """Разные сущности (аккаунт/кампания) с одинаковым db_path не мешают
    друг другу — разные таблицы, разное сопоставление по name."""
    db_path = tmp_path / "users.db"

    manage.ensure_account(
        db_path, name="Страхование", phone="", session_name="s", session_path="p",
        daily_limit=30, enabled=True,
    )
    manage.ensure_campaign(
        db_path, name="Страхование", keyword="страх", target_chat="@tplgee", enabled=True,
    )

    account_repository = TelegramAccountRepository(db_path)
    campaign_repository = InviteCampaignRepository(db_path)
    try:
        assert len(account_repository.list()) == 1
        assert len(campaign_repository.list()) == 1
    finally:
        account_repository.close()
        campaign_repository.close()
