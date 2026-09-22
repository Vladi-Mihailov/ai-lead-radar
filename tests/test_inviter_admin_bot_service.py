"""Тесты reader/inviter_admin_bot/service.py::InviterAdminService — вся
композиция поверх УЖЕ существующих reader/inviter/ репозиториев. Никакой
бизнес-логики выборки/лимитов не тестируется заново — только то, что этот
слой правильно её переиспользует (см. tests/test_inviter_repository.py/
test_inviter_candidates.py для самой repository/identity-логики)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from telethon.errors import RpcCallFailError

from reader.inviter.repository import (
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)
from reader.inviter.runtime_state_repository import (
    InviterRuntimeStateRepository,
)
from reader.inviter_admin_bot.service import InviterAdminService

_TRUSTED_ID = 111222333
_OTHER_ID = 999888777


class _Fixture:
    def __init__(self, tmp_path, *, trusted_admin_user_ids=frozenset({_TRUSTED_ID})):
        self.db_path = tmp_path / "inviter.db"
        self.accounts = TelegramAccountRepository(self.db_path)
        self.campaigns = InviteCampaignRepository(self.db_path)
        self.invites = UserCampaignInviteRepository(self.db_path)
        self.runtime_state = InviterRuntimeStateRepository(self.db_path)
        self.service = InviterAdminService(
            self.accounts, self.campaigns, self.invites, self.runtime_state,
            db_path=self.db_path, trusted_admin_user_ids=trusted_admin_user_ids,
            worker_poll_interval_seconds=600,
        )

    def close(self):
        self.accounts.close()
        self.campaigns.close()
        self.invites.close()
        self.runtime_state.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


def _make_account(fx, *, name="@vvz982", telegram_user_id=100, daily_limit=15, enabled=True, is_old=False):
    account = fx.accounts.create(
        name=name, phone="+995500000001", session_name=name.lstrip("@"),
        session_path=str(Path(fx.db_path).parent / "sessions" / name.lstrip("@")),
        daily_limit=daily_limit, enabled=enabled, telegram_user_id=telegram_user_id, is_old=is_old,
    )
    return account


# ---- 1. unauthorized user denied ----


def test_is_trusted_only_for_configured_numeric_ids(fx):
    assert fx.service.is_trusted(_TRUSTED_ID) is True
    assert fx.service.is_trusted(_OTHER_ID) is False


# ---- 2. accounts list ----


def test_list_accounts_shows_all_existing_accounts(fx):
    _make_account(fx, name="@vvz982")
    _make_account(fx, name="@ib85gnat", telegram_user_id=101)

    entries = fx.service.list_accounts()

    assert {e.display_name for e in entries} == {"@vvz982", "@ib85gnat"}


def test_list_accounts_empty_when_no_accounts(fx):
    assert fx.service.list_accounts() == []


# ---- 3. account card ----


def test_account_card_shows_usage_session_and_flags(fx):
    account = _make_account(fx, daily_limit=15)

    card = fx.service.account_card(account.id)

    assert card.display_name == "@vvz982"
    assert card.session_exists is False  # файла сессии физически нет в tmp_path
    assert card.usage.sent_today == 0
    assert card.usage.daily_limit == 15
    assert card.usage.remaining == 15


def test_account_card_returns_none_for_missing_account(fx):
    assert fx.service.account_card(999999) is None


def test_account_card_usage_counts_joined_and_pending_today(fx):
    account = _make_account(fx, daily_limit=15)
    campaign = fx.campaigns.create(name="Campaign", keyword="осаго", target_chat="@t")

    now = datetime.now(timezone.utc)
    fx.invites.create(user_id=1, campaign_id=campaign.id, account_id=account.id, status="joined", invited_at=now, verified_at=now)
    fx.invites.create(user_id=2, campaign_id=campaign.id, account_id=account.id, status="pending", invited_at=now)
    fx.invites.create(user_id=3, campaign_id=campaign.id, account_id=account.id, status="failed", invited_at=now)

    card = fx.service.account_card(account.id)

    # failed НЕ учитывается в "Сегодня" (та же формула, что и
    # InviterService._remaining_daily_budget — joined + pending).
    assert card.usage.sent_today == 2
    assert card.usage.remaining == 13


# ---- 7. missing username ----


def test_display_name_falls_back_to_telegram_id_when_username_missing(fx):
    account = fx.accounts.create(
        name="tg_995500000001", phone="+995500000001", session_name="tg_995500000001",
        session_path=str(fx.db_path.parent / "sessions" / "tg_995500000001"),
        telegram_user_id=123456789,
    )

    entries = fx.service.list_accounts()
    card = fx.service.account_card(account.id)

    assert entries[0].display_name == "Telegram ID 123456789"
    assert card.display_name == "Telegram ID 123456789"


def test_display_name_falls_back_to_raw_name_before_any_identity_confirmed(fx):
    """Ни username, ни telegram_user_id ещё не подтверждены (только что
    создана запись до первой синхронизации) — показываем хоть что-то, а не
    падаем (см. design: "а не падать")."""
    account = fx.accounts.create(
        name="tg_995500000001", phone="+995500000001", session_name="tg_995500000001",
        session_path=str(fx.db_path.parent / "sessions" / "tg_995500000001"),
    )

    card = fx.service.account_card(account.id)

    assert card.display_name == "tg_995500000001"


# ---- 4. enable/disable account ----


def test_toggle_enabled_flips_flag(fx):
    account = _make_account(fx, enabled=True)

    disabled = fx.service.toggle_enabled(account.id)
    assert disabled.enabled is False

    enabled = fx.service.toggle_enabled(account.id)
    assert enabled.enabled is True


def test_toggle_enabled_of_one_account_does_not_affect_another(fx):
    account_a = _make_account(fx, name="@a", telegram_user_id=1)
    account_b = _make_account(fx, name="@b", telegram_user_id=2)

    fx.service.toggle_enabled(account_a.id)

    assert fx.accounts.get(account_a.id).enabled is False
    assert fx.accounts.get(account_b.id).enabled is True


def test_toggle_enabled_returns_none_for_missing_account(fx):
    assert fx.service.toggle_enabled(999999) is None


# ---- 5. daily limit update ----


def test_set_daily_limit_updates_value(fx):
    account = _make_account(fx, daily_limit=15)

    updated = fx.service.set_daily_limit(account.id, 25)

    assert updated.daily_limit == 25
    assert fx.accounts.get(account.id).daily_limit == 25


def test_set_daily_limit_rejects_non_positive_value(fx):
    account = _make_account(fx, daily_limit=15)

    with pytest.raises(ValueError):
        fx.service.set_daily_limit(account.id, 0)

    assert fx.accounts.get(account.id).daily_limit == 15  # не изменилось


def test_set_daily_limit_returns_none_for_missing_account(fx):
    assert fx.service.set_daily_limit(999999, 10) is None


# ---- 12. broken session does not crash sync ----


class _FakeSyncClient:
    def __init__(self, account, *, connect_error=None, is_authorized=True, get_me_result=None):
        self.account = account
        self._connect_error = connect_error
        self._is_authorized = is_authorized
        self._get_me_result = get_me_result
        self.disconnect_called = False

    async def connect(self):
        if self._connect_error is not None:
            raise self._connect_error

    async def is_user_authorized(self):
        return self._is_authorized

    async def get_me(self):
        if self._get_me_result is not None:
            return self._get_me_result
        return SimpleNamespace(id=self.account.telegram_user_id or 900000, username=None, phone=None)

    async def disconnect(self):
        self.disconnect_called = True


async def test_sync_all_accounts_does_not_crash_when_one_account_has_broken_session(fx):
    good = _make_account(fx, name="@good", telegram_user_id=201)
    broken = _make_account(fx, name="@broken", telegram_user_id=202)

    def factory(account):
        if account.id == broken.id:
            return _FakeSyncClient(account, connect_error=RpcCallFailError(request=None))
        return _FakeSyncClient(
            account, get_me_result=SimpleNamespace(id=account.telegram_user_id, username="good", phone=None),
        )

    summary = await fx.service.sync_all_accounts(factory)

    assert summary.checked == 2
    assert summary.needs_attention == 1
    statuses = {r.account_id: r.status for r in summary.results}
    assert statuses[broken.id] == "connect_failed"
    assert statuses[good.id] in ("updated", "unchanged")


async def test_sync_one_account_broken_session_reports_error_without_raising(fx):
    account = _make_account(fx, name="@broken")

    client = _FakeSyncClient(account, connect_error=ConnectionError("boom"))
    outcome = await fx.service.sync_one_account(account.id, lambda a: client)

    assert outcome.status == "connect_failed"


async def test_sync_one_account_not_authorized(fx):
    account = _make_account(fx, name="@needsauth")
    client = _FakeSyncClient(account, is_authorized=False)

    outcome = await fx.service.sync_one_account(account.id, lambda _a: client)

    assert outcome.status == "not_authorized"


async def test_sync_one_account_returns_none_for_missing_account(fx):
    result = await fx.service.sync_one_account(999999, lambda a: None)
    assert result is None


# ---- 15. sync updates username instead of creating duplicate ----


async def test_sync_one_account_updates_username_without_duplicating(fx):
    account = _make_account(fx, name="@oldname", telegram_user_id=300)
    client = _FakeSyncClient(account, get_me_result=SimpleNamespace(id=300, username="newname", phone=None))

    outcome = await fx.service.sync_one_account(account.id, lambda _a: client)

    assert outcome.status == "username_updated"
    assert outcome.display_name == "@newname"
    assert len(fx.accounts.list()) == 1


# ---- 13. global inviter pause/resume ----


def test_set_global_enabled_toggles_and_persists(fx):
    assert fx.service.is_global_enabled() is True

    fx.service.set_global_enabled(False)
    assert fx.service.is_global_enabled() is False

    fx.service.set_global_enabled(True)
    assert fx.service.is_global_enabled() is True


# ---- 📊 Статус ----


def test_status_snapshot_counts_accounts_by_state(fx):
    active = _make_account(fx, name="@active", telegram_user_id=1, enabled=True)
    Path(f"{active.session_path}.session").parent.mkdir(parents=True, exist_ok=True)
    Path(f"{active.session_path}.session").touch()  # реальный файл — не "session error"

    _make_account(fx, name="@disabled", telegram_user_id=2, enabled=False)
    blocked = _make_account(fx, name="@blocked", telegram_user_id=3, enabled=True)
    fx.accounts.update(blocked.id, blocked_until=datetime.now(timezone.utc) + timedelta(hours=1), blocked_reason="flood_wait")

    snapshot = fx.service.status_snapshot()

    assert snapshot.active_count == 1
    assert snapshot.disabled_count == 1
    assert snapshot.blocked_count == 1
    assert len(snapshot.attention) == 1
    assert snapshot.attention[0].display_name == "@blocked"


def test_status_snapshot_flags_enabled_account_with_missing_session(fx):
    """Enabled-аккаунт без .session-файла — тоже "⚠️ Требуют внимания"
    (см. design: "session error"), даже если не заблокирован Telegram'ом."""
    _make_account(fx, name="@nosession", telegram_user_id=1, enabled=True)

    snapshot = fx.service.status_snapshot()

    assert len(snapshot.attention) == 1
    assert snapshot.attention[0].display_name == "@nosession"
    assert snapshot.attention[0].reason == "session error"


def test_status_snapshot_worker_alive_reflects_recent_heartbeat(fx):
    fx.runtime_state.record_tick(datetime.now(timezone.utc))

    snapshot = fx.service.status_snapshot()

    assert snapshot.worker_alive is True
    assert snapshot.last_tick_at is not None
    assert snapshot.next_tick_at is not None


def test_status_snapshot_worker_not_alive_without_heartbeat(fx):
    snapshot = fx.service.status_snapshot()
    assert snapshot.worker_alive is False


def test_status_snapshot_worker_not_alive_when_heartbeat_stale(fx):
    fx.runtime_state.record_tick(datetime.now(timezone.utc) - timedelta(hours=2))

    snapshot = fx.service.status_snapshot()

    assert snapshot.worker_alive is False


def test_status_snapshot_reflects_todays_invite_counts(fx):
    account = _make_account(fx)
    campaign = fx.campaigns.create(name="Страхование", keyword="осаго", target_chat="@car_ins_georgia")
    now = datetime.now(timezone.utc)
    fx.invites.create(user_id=1, campaign_id=campaign.id, account_id=account.id, status="joined", invited_at=now, verified_at=now)
    fx.invites.create(user_id=2, campaign_id=campaign.id, account_id=account.id, status="pending", invited_at=now)
    fx.invites.create(user_id=3, campaign_id=campaign.id, account_id=account.id, status="failed", invited_at=now)

    snapshot = fx.service.status_snapshot()

    assert snapshot.sent_today == 2  # joined + pending
    assert snapshot.pending_today == 1
    assert snapshot.failed_today == 1
    assert snapshot.campaign_name == "Страхование"
    assert snapshot.campaign_target_chat == "@car_ins_georgia"


# ---- ⚙️ Лимиты: список для "⚙️ Лимиты" (USED / daily_limit, не enabled) ----


def test_list_accounts_for_limits_shows_daily_limit(fx):
    _make_account(fx, name="@vladimihailov", telegram_user_id=1, daily_limit=15)
    _make_account(fx, name="@vvz982", telegram_user_id=2, daily_limit=25, enabled=False)

    entries = fx.service.list_accounts_for_limits()

    limits_by_name = {e.display_name: e.daily_limit for e in entries}
    assert limits_by_name == {"@vladimihailov": 15, "@vvz982": 25}


def test_list_accounts_for_limits_sent_today_uses_same_formula_as_account_usage(fx):
    """sent_today — ТА ЖЕ формула joined_today + pending_today, что и
    _account_usage()/InviterService._remaining_daily_budget (см. design
    "не придумывать новый счётчик")."""
    account = _make_account(fx, daily_limit=15)
    campaign = fx.campaigns.create(name="Campaign", keyword="осаго", target_chat="@t")
    now = datetime.now(timezone.utc)
    fx.invites.create(user_id=1, campaign_id=campaign.id, account_id=account.id, status="joined", invited_at=now, verified_at=now)
    fx.invites.create(user_id=2, campaign_id=campaign.id, account_id=account.id, status="pending", invited_at=now)
    fx.invites.create(user_id=3, campaign_id=campaign.id, account_id=account.id, status="failed", invited_at=now)

    entries = fx.service.list_accounts_for_limits()
    card = fx.service.account_card(account.id)

    assert entries[0].sent_today == 2  # failed не считается
    assert entries[0].sent_today == card.usage.sent_today  # та же формула, что и в карточке


def test_list_accounts_for_limits_reflects_updated_value(fx):
    account = _make_account(fx, daily_limit=15)

    fx.service.set_daily_limit(account.id, 20)
    entries = fx.service.list_accounts_for_limits()

    assert entries[0].daily_limit == 20


def test_list_accounts_for_limits_empty_when_no_accounts(fx):
    assert fx.service.list_accounts_for_limits() == []


def test_list_accounts_for_limits_missing_username_falls_back_to_telegram_id(fx):
    fx.accounts.create(
        name="tg_995500000001", phone="+995500000001", session_name="tg_995500000001",
        session_path=str(fx.db_path.parent / "sessions" / "tg_995500000001"),
        telegram_user_id=123456789,
    )

    entries = fx.service.list_accounts_for_limits()

    assert entries[0].display_name == "Telegram ID 123456789"


# ---- 📊 Статус: список per-account (enabled/blocked раздельно) ----


def test_list_account_statuses_enabled_no_block(fx):
    _make_account(fx, name="@vvz982", telegram_user_id=1, enabled=True)

    entries = fx.service.list_account_statuses()

    assert entries[0].display_name == "@vvz982"
    assert entries[0].enabled is True
    assert entries[0].is_blocked is False


def test_list_account_statuses_disabled_no_block(fx):
    _make_account(fx, name="@Mihailov_vm", telegram_user_id=1, enabled=False)

    entries = fx.service.list_account_statuses()

    assert entries[0].enabled is False
    assert entries[0].is_blocked is False


def test_list_account_statuses_blocked_until_future_is_blocked(fx):
    account = _make_account(fx, name="@wwww86w", telegram_user_id=1, enabled=True)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    fx.accounts.update(account.id, blocked_until=future, blocked_reason="peer_flood")

    entries = fx.service.list_account_statuses()

    assert entries[0].is_blocked is True
    assert entries[0].blocked_reason == "peer_flood"
    assert entries[0].blocked_until == future


def test_list_account_statuses_blocked_until_past_is_not_blocked(fx):
    """blocked_until уже прошёл -> is_blocked=False, даже если
    blocked_reason остался в БД (см. design: "показывать как Не
    заблокирован")."""
    account = _make_account(fx, name="@vvz982", telegram_user_id=1, enabled=True)
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    fx.accounts.update(account.id, blocked_until=past, blocked_reason="peer_flood")

    entries = fx.service.list_account_statuses()

    assert entries[0].is_blocked is False
    # blocked_reason остаётся в возвращаемых данных (исторический факт из
    # БД) — именно texts.py решает не показывать его, когда is_blocked=False.
    assert entries[0].blocked_reason == "peer_flood"


def test_list_account_statuses_enabled_independent_from_blocked(fx):
    """enabled=True + is_blocked=True одновременно — два независимых поля,
    оба должны быть видны вызывающей стороне (см. design "Это два разных
    состояния")."""
    account = _make_account(fx, name="@wwww86w", telegram_user_id=1, enabled=True)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    fx.accounts.update(account.id, blocked_until=future, blocked_reason="peer_flood")

    entries = fx.service.list_account_statuses()

    assert entries[0].enabled is True
    assert entries[0].is_blocked is True


def test_list_account_statuses_includes_sent_today_and_daily_limit(fx):
    account = _make_account(fx, daily_limit=15)
    campaign = fx.campaigns.create(name="Campaign", keyword="осаго", target_chat="@t")
    now = datetime.now(timezone.utc)
    fx.invites.create(user_id=1, campaign_id=campaign.id, account_id=account.id, status="joined", invited_at=now, verified_at=now)
    fx.invites.create(user_id=2, campaign_id=campaign.id, account_id=account.id, status="pending", invited_at=now)

    entries = fx.service.list_account_statuses()

    assert entries[0].sent_today == 2
    assert entries[0].daily_limit == 15


# ---- E. is_old=True исключены из operational-списков (👤/⚙️/📊) ----


def test_list_accounts_excludes_old_accounts(fx):
    current = _make_account(fx, name="@Iv_vla_sov", telegram_user_id=6, is_old=False)
    _make_account(fx, name="@Iv_vla_sov", telegram_user_id=6, is_old=True, enabled=False)

    entries = fx.service.list_accounts()

    assert [e.id for e in entries] == [current.id]


def test_list_accounts_for_limits_excludes_old_accounts(fx):
    current = _make_account(fx, name="@bdlapq", telegram_user_id=9, is_old=False)
    _make_account(fx, name="@bdlapq", telegram_user_id=9, is_old=True, enabled=False)

    entries = fx.service.list_accounts_for_limits()

    assert [e.id for e in entries] == [current.id]


def test_list_account_statuses_excludes_old_accounts(fx):
    current = _make_account(fx, name="@bdlapq", telegram_user_id=9, is_old=False)
    _make_account(fx, name="@bdlapq", telegram_user_id=9, is_old=True, enabled=False)

    entries = fx.service.list_account_statuses()

    assert len(entries) == 1
    assert entries[0].display_name == current.name


# ---- F/G. toggle_enabled fail-closed для is_old, CURRENT — как раньше ----


def test_toggle_enabled_blocked_for_old_account(fx):
    old = _make_account(fx, name="@bdlapq", telegram_user_id=9, is_old=True, enabled=True)

    result = fx.service.toggle_enabled(old.id)

    assert result is not None
    assert result.is_old is True
    assert result.enabled is True  # НЕ изменилось
    assert fx.accounts.get(old.id).enabled is True  # и в БД тоже не изменилось


def test_toggle_enabled_still_works_for_current_account(fx):
    current = _make_account(fx, is_old=False, enabled=True)

    result = fx.service.toggle_enabled(current.id)

    assert result.enabled is False
    assert fx.accounts.get(current.id).enabled is False
