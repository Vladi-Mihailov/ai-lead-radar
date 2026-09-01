"""
Тесты reader.inviter.manage — штатное создание/обновление аккаунтов и
кампаний инвайтера без ручного редактирования SQLite (см. docstring
manage.py: data/users.db не входит в git, поэтому на новом окружении
TelegramAccountRepository/InviteCampaignRepository всегда пустые).
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter import manage  # noqa: E402
from reader.inviter.repository import (  # noqa: E402
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
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


def test_parse_args_sync_accounts():
    args = manage._parse_args(["sync-accounts"])
    assert args.command == "sync-accounts"


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


# ---- sync_accounts (backfill/сверка telegram_user_id, см. задачу про -----
# ---- физическую идентичность Telegram-аккаунта) ---------------------------


class _FakeSyncClient:
    """Ровно то, что нужно sync_accounts() от TelegramClient — connect()/
    is_user_authorized()/get_me()/disconnect(). connect_error, если задан,
    имитирует сбой подключения (битая/удалённая сессия)."""

    def __init__(self, *, connect_error=None, authorized=True, me=None):
        self._connect_error = connect_error
        self._authorized = authorized
        self._me = me
        self.disconnected = False

    async def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error

    async def is_user_authorized(self) -> bool:
        return self._authorized

    async def get_me(self):
        return self._me

    async def disconnect(self) -> None:
        self.disconnected = True


def _make_sync_client_factory(*, by_name=None):
    """by_name — {account.name: _FakeSyncClient(...)} для аккаунтов,
    которым нужно смоделировать конкретный сценарий; остальные получают
    клиент по умолчанию (авторизован, get_me().id уникален для account.id
    — как и в tests/test_inviter_candidates.py _FakeTelegramClient)."""
    by_name = by_name or {}

    def factory(account):
        if account.name in by_name:
            return by_name[account.name]
        return _FakeSyncClient(me=SimpleNamespace(id=900_000_000 + account.id, username=None, phone=None))

    return factory


def test_sync_accounts_fills_telegram_user_id_for_new_account(tmp_path):
    db_path = tmp_path / "users.db"
    manage.ensure_account(
        db_path, name="@vladimihailov", phone="+995500000001",
        session_name="vladimihailov", session_path="data/sessions/vladimihailov",
        daily_limit=1, enabled=True,
    )

    client = _FakeSyncClient(me=SimpleNamespace(id=5502837130, username=None, phone=None))
    results = asyncio.run(
        manage.sync_accounts(db_path, client_factory=_make_sync_client_factory(
            by_name={"@vladimihailov": client},
        ))
    )

    assert [r.status for r in results] == ["updated"]
    assert client.disconnected is True

    account_repository = TelegramAccountRepository(db_path)
    try:
        accounts = account_repository.list()
        assert len(accounts) == 1
        assert accounts[0].telegram_user_id == 5502837130
    finally:
        account_repository.close()


def test_sync_accounts_processes_disabled_accounts_too(tmp_path):
    """Историческая отключённая запись-дубликат (см. id=7/id=8 в проде)
    тоже должна пройти sync — backfill не фильтрует по enabled."""
    db_path = tmp_path / "users.db"
    manage.ensure_account(
        db_path, name="@Misha_Offroad", phone="+995568759201",
        session_name="Misha_Offroad", session_path="data/sessions/Misha_Offroad",
        daily_limit=1, enabled=False,
    )

    client = _FakeSyncClient(me=SimpleNamespace(id=8838087889, username=None, phone=None))
    results = asyncio.run(
        manage.sync_accounts(db_path, client_factory=_make_sync_client_factory(
            by_name={"@Misha_Offroad": client},
        ))
    )

    assert [r.status for r in results] == ["updated"]

    account_repository = TelegramAccountRepository(db_path)
    try:
        accounts = account_repository.list()
        assert accounts[0].telegram_user_id == 8838087889
        # enabled НЕ тронут — backfill никогда не включает/выключает аккаунты.
        assert accounts[0].enabled is False
    finally:
        account_repository.close()


def test_sync_accounts_never_changes_enabled_flag(tmp_path):
    db_path = tmp_path / "users.db"
    manage.ensure_account(
        db_path, name="@acc1", phone="", session_name="acc1", session_path="data/sessions/acc1",
        daily_limit=1, enabled=True,
    )
    manage.ensure_account(
        db_path, name="@acc2", phone="", session_name="acc2", session_path="data/sessions/acc2",
        daily_limit=1, enabled=False,
    )

    asyncio.run(manage.sync_accounts(db_path, client_factory=_make_sync_client_factory()))

    account_repository = TelegramAccountRepository(db_path)
    try:
        enabled_by_name = {a.name: a.enabled for a in account_repository.list()}
    finally:
        account_repository.close()
    assert enabled_by_name == {"@acc1": True, "@acc2": False}


def test_sync_accounts_never_deletes_rows(tmp_path):
    db_path = tmp_path / "users.db"
    manage.ensure_account(
        db_path, name="@acc1", phone="", session_name="acc1", session_path="data/sessions/acc1",
        daily_limit=1, enabled=True,
    )
    manage.ensure_account(
        db_path, name="@acc2", phone="", session_name="acc2", session_path="data/sessions/acc2",
        daily_limit=1, enabled=True,
    )

    asyncio.run(manage.sync_accounts(db_path, client_factory=_make_sync_client_factory()))

    account_repository = TelegramAccountRepository(db_path)
    try:
        assert len(account_repository.list()) == 2
    finally:
        account_repository.close()


def test_sync_accounts_never_touches_user_campaign_invites(tmp_path):
    db_path = tmp_path / "users.db"
    account = manage.ensure_account(
        db_path, name="@acc1", phone="", session_name="acc1", session_path="data/sessions/acc1",
        daily_limit=1, enabled=True,
    )
    campaign = manage.ensure_campaign(
        db_path, name="ОСАГО", keyword="осаго", target_chat="@t", enabled=True,
    )
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invite_repository.create(
            user_id=1, campaign_id=campaign.id, account_id=account.id, status="joined",
        )
        before = invite_repository.list()

        asyncio.run(manage.sync_accounts(db_path, client_factory=_make_sync_client_factory()))

        after = invite_repository.list()
    finally:
        invite_repository.close()
    assert after == before


def test_sync_accounts_connect_failure_does_not_corrupt_identity(tmp_path):
    """Битая/удалённая сессия — connect() падает — telegram_user_id
    остаётся None (или прежним значением), запись не удаляется, отчёт
    показывает connect_failed."""
    db_path = tmp_path / "users.db"
    manage.ensure_account(
        db_path, name="@acc1", phone="", session_name="acc1", session_path="data/sessions/acc1",
        daily_limit=1, enabled=True,
    )

    client = _FakeSyncClient(connect_error=RuntimeError("session file is corrupted"))
    results = asyncio.run(
        manage.sync_accounts(db_path, client_factory=_make_sync_client_factory(
            by_name={"@acc1": client},
        ))
    )

    assert [r.status for r in results] == ["connect_failed"]

    account_repository = TelegramAccountRepository(db_path)
    try:
        accounts = account_repository.list()
        assert len(accounts) == 1
        assert accounts[0].telegram_user_id is None
    finally:
        account_repository.close()


def test_sync_accounts_not_authorized_does_not_corrupt_identity(tmp_path):
    db_path = tmp_path / "users.db"
    manage.ensure_account(
        db_path, name="@acc1", phone="", session_name="acc1", session_path="data/sessions/acc1",
        daily_limit=1, enabled=True,
    )

    client = _FakeSyncClient(authorized=False)
    results = asyncio.run(
        manage.sync_accounts(db_path, client_factory=_make_sync_client_factory(
            by_name={"@acc1": client},
        ))
    )

    assert [r.status for r in results] == ["not_authorized"]

    account_repository = TelegramAccountRepository(db_path)
    try:
        assert account_repository.list()[0].telegram_user_id is None
    finally:
        account_repository.close()


def test_sync_accounts_identity_mismatch_does_not_overwrite_stored_tg_id(tmp_path):
    db_path = tmp_path / "users.db"
    account_repository = TelegramAccountRepository(db_path)
    try:
        account_repository.create(
            name="@acc1", phone="+995500000001", session_name="acc1",
            session_path="data/sessions/acc1", daily_limit=1, telegram_user_id=111,
        )
    finally:
        account_repository.close()

    client = _FakeSyncClient(me=SimpleNamespace(id=999, username=None, phone=None))
    results = asyncio.run(
        manage.sync_accounts(db_path, client_factory=_make_sync_client_factory(
            by_name={"@acc1": client},
        ))
    )

    assert [r.status for r in results] == ["identity_mismatch"]

    account_repository = TelegramAccountRepository(db_path)
    try:
        assert account_repository.list()[0].telegram_user_id == 111
    finally:
        account_repository.close()


def test_sync_accounts_auto_resolves_duplicates_marks_loser_old_and_disabled(tmp_path, capsys):
    """Реальный случай из прода (id=6/id=7): дубликат telegram_user_id
    теперь разрешается АВТОМАТИЧЕСКИ — ровно одна запись остаётся CURRENT
    (is_old=False), вторая помечается is_old=True/enabled=False. Ни одна
    строка не удаляется/не сливается (см. reader/inviter/identity.py
    resolve_duplicate_group)."""
    db_path = tmp_path / "users.db"
    manage.ensure_account(
        db_path, name="@Iv_vla_sov", phone="+995568759201", session_name="Iv_vla_sov",
        session_path="data/sessions/Iv_vla_sov", daily_limit=1, enabled=True,
    )
    manage.ensure_account(
        db_path, name="@Misha_Offroad", phone="+995568759201", session_name="Misha_Offroad",
        session_path="data/sessions/Misha_Offroad", daily_limit=1, enabled=False,
    )

    shared_me = SimpleNamespace(id=8838087889, username=None, phone=None)
    asyncio.run(
        manage.sync_accounts(db_path, client_factory=_make_sync_client_factory(
            by_name={
                "@Iv_vla_sov": _FakeSyncClient(me=shared_me),
                "@Misha_Offroad": _FakeSyncClient(me=shared_me),
            },
        ))
    )

    output = capsys.readouterr().out
    assert "CURRENT" in output
    assert "OLD" in output
    assert "8838087889" in output
    assert "DUPLICATES RESOLVED: 1" in output

    account_repository = TelegramAccountRepository(db_path)
    try:
        accounts = {a.name: a for a in account_repository.list()}
        assert len(accounts) == 2  # ничего не слито и не удалено
    finally:
        account_repository.close()

    assert accounts["@Iv_vla_sov"].is_old is False
    assert accounts["@Iv_vla_sov"].enabled is True
    assert accounts["@Misha_Offroad"].is_old is True
    assert accounts["@Misha_Offroad"].enabled is False
    assert accounts["@Misha_Offroad"].old_reason == "duplicate_telegram_user_id"


def test_sync_accounts_does_not_auto_enable_when_all_duplicates_disabled(tmp_path):
    """Реальный случай из прода (id=8/id=9 до вмешательства оператора):
    обе записи disabled — CURRENT определяется, но enabled НИКОГДА не
    включается автоматически ни для одной из них."""
    db_path = tmp_path / "users.db"
    manage.ensure_account(
        db_path, name="@m_vlad_i_mir", phone="+79495447392", session_name="inviter_m_vlad_i_mir",
        session_path="data/sessions/inviter_m_vlad_i_mir", daily_limit=1, enabled=False,
    )
    manage.ensure_account(
        db_path, name="@bdlapq", phone="+79495447392", session_name="inviter_bdlapq",
        session_path="data/sessions/inviter_bdlapq", daily_limit=1, enabled=False,
    )

    shared_me = SimpleNamespace(id=8847286898, username=None, phone=None)
    asyncio.run(
        manage.sync_accounts(db_path, client_factory=_make_sync_client_factory(
            by_name={
                "@m_vlad_i_mir": _FakeSyncClient(me=shared_me),
                "@bdlapq": _FakeSyncClient(me=shared_me),
            },
        ))
    )

    account_repository = TelegramAccountRepository(db_path)
    try:
        accounts = account_repository.list()
    finally:
        account_repository.close()
    assert all(a.enabled is False for a in accounts)  # ни одна не включена автоматически
    assert sum(1 for a in accounts if not a.is_old) == 1  # ровно одна CURRENT


def test_sync_accounts_is_idempotent(tmp_path):
    """Повторный запуск sync-accounts с теми же данными — второй прогон
    не меняет ни is_old/enabled/telegram_user_id, ни account-count (см.
    задачу: "repeated sync идемпотентен")."""
    db_path = tmp_path / "users.db"
    manage.ensure_account(
        db_path, name="@Iv_vla_sov", phone="+995568759201", session_name="Iv_vla_sov",
        session_path="data/sessions/Iv_vla_sov", daily_limit=1, enabled=True,
    )
    manage.ensure_account(
        db_path, name="@Misha_Offroad", phone="+995568759201", session_name="Misha_Offroad",
        session_path="data/sessions/Misha_Offroad", daily_limit=1, enabled=False,
    )

    def make_factory():
        shared_me = SimpleNamespace(id=8838087889, username=None, phone=None)
        return _make_sync_client_factory(by_name={
            "@Iv_vla_sov": _FakeSyncClient(me=shared_me),
            "@Misha_Offroad": _FakeSyncClient(me=shared_me),
        })

    asyncio.run(manage.sync_accounts(db_path, client_factory=make_factory()))

    account_repository = TelegramAccountRepository(db_path)
    try:
        after_first = account_repository.list()
    finally:
        account_repository.close()

    second_results = asyncio.run(manage.sync_accounts(db_path, client_factory=make_factory()))

    account_repository = TelegramAccountRepository(db_path)
    try:
        after_second = account_repository.list()
    finally:
        account_repository.close()

    assert after_first == after_second
    # Второй прогон не меняет данные — статус "unchanged" для обоих аккаунтов.
    assert {r.status for r in second_results} == {"unchanged"}
