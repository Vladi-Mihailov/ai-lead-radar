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


def test_parse_args_add_account_verify_membership_defaults_true():
    args = manage._parse_args(
        [
            "add-account", "--name", "@vladimihailov",
            "--session-name", "vladimihailov", "--session-path", "data/sessions/vladimihailov",
        ]
    )
    assert args.verify_membership is True


def test_parse_args_add_account_no_verify_membership():
    args = manage._parse_args(
        [
            "add-account", "--name", "@car_ins_account",
            "--session-name", "car_ins_account", "--session-path", "data/sessions/car_ins_account",
            "--no-verify-membership",
        ]
    )
    assert args.verify_membership is False


def test_parse_args_add_account_verify_membership_explicit_true():
    """--verify-membership не только "по умолчанию" — можно явно включить
    обратно (см. задачу: "также должна быть возможность снова включить его")."""
    args = manage._parse_args(
        [
            "add-account", "--name", "@car_ins_account",
            "--session-name", "car_ins_account", "--session-path", "data/sessions/car_ins_account",
            "--verify-membership",
        ]
    )
    assert args.verify_membership is True


def test_parse_args_verify_membership_independent_from_enabled():
    """enabled и verify_membership — независимые флаги, не связанные друг
    с другом (см. задачу)."""
    args = manage._parse_args(
        [
            "add-account", "--name", "@car_ins_account",
            "--session-name", "car_ins_account", "--session-path", "data/sessions/car_ins_account",
            "--no-verify-membership",
        ]
    )
    assert args.enabled is True
    assert args.verify_membership is False


def test_parse_args_list_accounts():
    args = manage._parse_args(["list-accounts"])
    assert args.command == "list-accounts"


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


def test_ensure_account_defaults_verify_membership_to_true(tmp_path):
    db_path = tmp_path / "users.db"

    account = manage.ensure_account(
        db_path, name="@vladimihailov", phone="", session_name="vladimihailov",
        session_path="data/sessions/vladimihailov", daily_limit=30, enabled=True,
    )

    assert account.verify_membership is True


def test_ensure_account_can_disable_and_re_enable_verify_membership(tmp_path):
    """python -m reader.inviter.manage add-account ... --no-verify-membership,
    затем снова ... --verify-membership — обе стороны переключения
    (см. задачу: "также должна быть возможность снова включить его")."""
    db_path = tmp_path / "users.db"

    disabled = manage.ensure_account(
        db_path, name="@car_ins_account", phone="", session_name="car_ins_account",
        session_path="data/sessions/car_ins_account", daily_limit=24, enabled=True,
        verify_membership=False,
    )
    assert disabled.verify_membership is False
    assert disabled.enabled is True  # enabled не затронут

    re_enabled = manage.ensure_account(
        db_path, name="@car_ins_account", phone="", session_name="car_ins_account",
        session_path="data/sessions/car_ins_account", daily_limit=24, enabled=True,
        verify_membership=True,
    )
    assert re_enabled.id == disabled.id  # тот же ряд, не новый
    assert re_enabled.verify_membership is True


# ---- list_accounts ----


def test_list_accounts_returns_all_accounts_with_verify_membership(tmp_path):
    db_path = tmp_path / "users.db"
    manage.ensure_account(
        db_path, name="@acc1", phone="", session_name="acc1", session_path="data/sessions/acc1",
        daily_limit=24, enabled=True, verify_membership=True,
    )
    manage.ensure_account(
        db_path, name="@acc2", phone="", session_name="acc2", session_path="data/sessions/acc2",
        daily_limit=24, enabled=True, verify_membership=False,
    )

    accounts = manage.list_accounts(db_path)

    assert {a.name: a.verify_membership for a in accounts} == {
        "@acc1": True, "@acc2": False,
    }


def test_list_accounts_returns_empty_list_when_no_accounts(tmp_path):
    db_path = tmp_path / "users.db"
    assert manage.list_accounts(db_path) == []


# ---- _format_account_line (list-accounts вывод) ----


def test_format_account_line_shows_no_blocked_until_when_not_blocked(tmp_path):
    db_path = tmp_path / "users.db"
    account = manage.ensure_account(
        db_path, name="@acc1", phone="", session_name="acc1", session_path="data/sessions/acc1",
        daily_limit=24, enabled=True,
    )

    line = manage._format_account_line(account)

    assert "blocked_until=нет" in line


def test_format_account_line_shows_blocked_until_in_tbilisi_time_not_utc(tmp_path):
    """Перевод отображения времени на Asia/Tbilisi (см. задачу) —
    list-accounts больше не должен показывать "UTC" и должен показывать
    время, сдвинутое на +4 часа относительно сохранённого в БД UTC-значения."""
    from datetime import datetime, timedelta, timezone

    from reader.inviter.repository import TelegramAccountRepository

    db_path = tmp_path / "users.db"
    account = manage.ensure_account(
        db_path, name="@acc1", phone="", session_name="acc1", session_path="data/sessions/acc1",
        daily_limit=24, enabled=True,
    )

    blocked_until_utc = datetime(2026, 8, 14, 11, 44, tzinfo=timezone.utc)
    repository = TelegramAccountRepository(db_path)
    try:
        account = repository.update(account.id, blocked_until=blocked_until_utc, blocked_reason="flood_wait")
    finally:
        repository.close()

    line = manage._format_account_line(account)

    assert "UTC" not in line
    assert "blocked_until=до 2026-08-14 15:44 по Тбилиси" in line
    # Значение в БД, которое мы читаем обратно, всё ещё UTC — форматирование
    # не затронуло сами данные.
    assert account.blocked_until == blocked_until_utc
    assert account.blocked_until.utcoffset() == timedelta(0)


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
