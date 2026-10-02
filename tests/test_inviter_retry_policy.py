"""Политика повторов инвайтера (см. reader/inviter/service.py
_classify_invite_error/_apply_retry_policy и UserCampaignInvite.failure_kind/
next_attempt_at).

Регрессия production-инцидента @Margosha_070 (873077267) / @richmove77
(585788032): каждый тик воркера заново выбирал их и "приглашал" — 118 и 111
попыток за 4 дня всеми 8 активными аккаунтами. Реальная цепочка (Telethon
1.44, telethon/client/users.py):
  1. get_input_entity(user_id) — промах кэша аккаунта;
  2. get_entity("@username") -> ValueError('No user has "..." as username')
     (Telethon сам оборачивает UsernameNotOccupiedError в ValueError);
  3. get_entity(InputPeerUser(id, stored_access_hash)) -> GetUsersRequest
     вернул пустой список -> KeyError(user_id), str(exc) == "873077267";
  4. KeyError не был "подтверждённо постоянным" -> status='failed', который
     никогда не исключался из выборки -> снова первый в очереди.

Всё — на временных SQLite-файлах и фейковых клиентах (см.
tests/test_inviter_candidates.py), без реального Telegram."""

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.errors import (
    FloodWaitError,
    UserAlreadyParticipantError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.messages import GetHistoryRequest
from telethon.tl.types import Channel, User

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter.repository import (  # noqa: E402
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)
from reader.inviter.service import (  # noqa: E402
    InviteErrorAction,
    _CandidateUnresolvableError,
    _classify_invite_error,
    _next_attempt_delay,
)
from reader.inviter.worker import InviterWorker  # noqa: E402
from test_inviter_candidates import (  # noqa: E402
    _BASE_TIME,
    _make_client_factory,
    _run_service,
    _seed_user,
    _setup_db,
)

_MARGOSHA = 873077267
_RICHMOVE = 585788032


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import reader.inviter.service as service_module

    async def instant_sleep(seconds):
        pass

    monkeypatch.setattr(service_module.asyncio, "sleep", instant_sleep)


# ---- helpers ----


def _campaign(db_path, *, name="Страхование", keyword="страхов", target="@car_ins_georgia", enabled=True, **extra):
    repo = InviteCampaignRepository(db_path)
    try:
        return repo.create(name=name, keyword=keyword, target_chat=target, enabled=enabled, **extra)
    finally:
        repo.close()


def _accounts(db_path, names, *, daily_limit=10):
    repo = TelegramAccountRepository(db_path)
    try:
        return [
            repo.create(
                name=name, phone=f"+99550000{i:04d}", session_name=name, session_path=f"{name}.session",
                daily_limit=daily_limit, verify_membership=False,
            )
            for i, name in enumerate(names)
        ]
    finally:
        repo.close()


def _invites(db_path):
    repo = UserCampaignInviteRepository(db_path)
    try:
        return repo.list()
    finally:
        repo.close()


def _candidates(db_path, campaign_id, account_id=None):
    repo = UserCampaignInviteRepository(db_path)
    try:
        return [c.user_id for c in repo.select_candidates(campaign_id, limit=100, account_id=account_id)]
    finally:
        repo.close()


def _expire_backoff(db_path):
    """Сдвиг "часов": все next_attempt_at — в прошлое (имитация того, что
    прошло достаточно времени до следующего тика)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE user_campaign_invites SET next_attempt_at = '2000-01-01T00:00:00' "
                     "WHERE next_attempt_at IS NOT NULL")
        conn.commit()
    finally:
        conn.close()


def _production_unresolvable(user_id, username):
    """Ровно то, что Telethon 1.44 отдаёт в production для этих двух
    пользователей (см. модульный докстрок)."""
    return {
        f"@{username}": ValueError(f'No user has "{username.lower()}" as username'),
        user_id: KeyError(user_id),
    }


def _factory_unresolvable(account_names, users):
    """users — [(user_id, username)]: каждый аккаунт не знает их в кэше и
    получает production-ошибки на username/stored access_hash."""
    responses = {}
    for user_id, username in users:
        responses.update(_production_unresolvable(user_id, username))
    return _make_client_factory(
        get_input_entity_errors={name: ValueError("не известен этому аккаунту") for name in account_names},
        entity_responses={name: dict(responses) for name in account_names},
    )


# ---- the production failure path ----


def test_production_path_is_classified_unresolved_not_transient_and_keeps_both_step_errors(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, _MARGOSHA, username="Margosha_070", keywords=["страхов"], access_hash=11,
               last_seen_at=_BASE_TIME, is_bot=False)
    campaign = _campaign(db_path)
    (acc1, _acc2) = _accounts(db_path, ["acc1", "acc2"])

    asyncio.run(_run_service(db_path, client_factory=_factory_unresolvable(["acc1", "acc2"], [(_MARGOSHA, "Margosha_070")]),
                             execute=True))

    (row,) = _invites(db_path)
    assert (row.user_id, row.campaign_id, row.account_id) == (_MARGOSHA, campaign.id, acc1.id)
    assert row.status == "failed" and row.failure_kind == "unresolved"
    assert row.next_attempt_at is not None and row.next_attempt_at > datetime.now(timezone.utc)
    # Причина КАЖДОГО шага сохранена (раньше в error оставался только "873077267").
    assert 'username: ValueError: No user has "margosha_070" as username' in row.error
    assert f"stored_access_hash: KeyError: {_MARGOSHA}" in row.error


def test_unresolved_user_is_not_reselected_on_next_tick(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, _MARGOSHA, username="Margosha_070", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    (acc1, acc2) = _accounts(db_path, ["acc1", "acc2"])
    factory = _factory_unresolvable(["acc1", "acc2"], [(_MARGOSHA, "Margosha_070")])

    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
    # Сразу же (в т.ч. в том же прогоне — acc2) кандидат не выбирается никем.
    assert len(_invites(db_path)) == 1
    assert _candidates(db_path, campaign.id) == []
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
    assert len(_invites(db_path)) == 1  # следующий тик — ни одной новой попытки

    # После backoff — только ДРУГОЙ аккаунт; acc1, уже не смогший, — никогда.
    _expire_backoff(db_path)
    assert _candidates(db_path, campaign.id, account_id=acc1.id) == []
    assert _candidates(db_path, campaign.id, account_id=acc2.id) == [_MARGOSHA]


def test_unresolved_by_every_active_account_becomes_terminal_invalid(tmp_path):
    db_path = _setup_db(tmp_path)
    for user_id, username in ((_MARGOSHA, "Margosha_070"), (_RICHMOVE, "richmove77")):
        _seed_user(db_path, user_id, username=username, keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    names = ["acc1", "acc2", "acc3"]
    _accounts(db_path, names)
    factory = _factory_unresolvable(names, [(_MARGOSHA, "Margosha_070"), (_RICHMOVE, "richmove77")])

    for _ in range(10):  # с запасом: больше тиков, чем аккаунтов
        asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
        _expire_backoff(db_path)

    for user_id in (_MARGOSHA, _RICHMOVE):
        rows = [r for r in _invites(db_path) if r.user_id == user_id]
        # Ровно по одной попытке на аккаунт, последняя — терминальная.
        assert [r.status for r in rows] == ["failed", "failed", "invalid"]
        assert len({r.account_id for r in rows}) == 3
        assert "НИ ОДНИМ активным аккаунтом" in rows[-1].error
    assert _candidates(db_path, campaign.id) == []
    total = len(_invites(db_path))
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
    assert len(_invites(db_path)) == total  # терминальные больше не трогаются


def test_disabled_or_old_accounts_do_not_block_terminal_escalation(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, _MARGOSHA, username="Margosha_070", keywords=["страхов"], access_hash=11, is_bot=False)
    _campaign(db_path)
    _accounts(db_path, ["acc1"])
    repo = TelegramAccountRepository(db_path)
    try:
        repo.create(name="off", phone="+1", session_name="off", session_path="off.session", enabled=False)
    finally:
        repo.close()

    asyncio.run(_run_service(db_path, client_factory=_factory_unresolvable(["acc1"], [(_MARGOSHA, "Margosha_070")]),
                             execute=True))
    assert [r.status for r in _invites(db_path)] == ["invalid"]


def test_confirmed_permanent_identity_error_is_terminal_immediately(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="gone", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    _accounts(db_path, ["acc1", "acc2"])
    factory = _make_client_factory(
        get_input_entity_errors={"acc1": ValueError("нет в кэше")},
        entity_responses={"acc1": {
            "@gone": ValueError('No user has "gone" as username'),
            1: UsernameNotOccupiedError(request=GetHistoryRequest),
        }},
    )
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
    assert [r.status for r in _invites(db_path)] == ["invalid"]
    assert _candidates(db_path, campaign.id) == []


def test_username_now_owned_by_a_channel_is_unresolved_not_invited(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="taken", keywords=["страхов"], access_hash=11, is_bot=False)
    _campaign(db_path)
    _accounts(db_path, ["acc1", "acc2"])
    channel = Channel(id=777, title="Канал", photo=None, date=None, access_hash=5)
    created = []
    factory = _make_client_factory(
        get_input_entity_errors={"acc1": ValueError("нет в кэше")},
        entity_responses={"acc1": {"@taken": channel, 1: KeyError(1)}},
        created=created,
    )
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
    (row,) = _invites(db_path)
    assert row.status == "failed" and row.failure_kind == "unresolved"
    assert "_ResolvedNonUserError" in row.error
    assert all(not c.call_requests for c in created)  # приглашение каналу не отправлялось


# ---- success / joined ----


def test_already_participant_becomes_joined_and_is_never_selected_again(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="member", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    _accounts(db_path, ["acc1"])
    factory = _make_client_factory(call_errors={"acc1": [UserAlreadyParticipantError(request=GetHistoryRequest)]})
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
    assert [r.status for r in _invites(db_path)] == ["joined"]
    _expire_backoff(db_path)
    assert _candidates(db_path, campaign.id) == []


def test_successful_invite_is_never_selected_again_even_by_another_account(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="ok", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    (acc1, acc2) = _accounts(db_path, ["acc1", "acc2"])
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(), execute=True))
    (row,) = _invites(db_path)
    assert row.status == "pending" and row.account_id == acc1.id
    # Свежий pending не выбирается ни этим, ни другим аккаунтом.
    assert _candidates(db_path, campaign.id, account_id=acc1.id) == []
    assert _candidates(db_path, campaign.id, account_id=acc2.id) == []


def test_stale_pending_is_not_reinvited(tmp_path):
    """pending пишется ТОЛЬКО после того, как Telegram ПРИНЯЛ приглашение
    (это не резерв перед отправкой) — "зависший" pending означает, что
    приглашение уже доставлено; повторная отправка пригласила бы того же
    человека второй раз. Поэтому давность pending ничего не меняет."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="ok", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    repo = UserCampaignInviteRepository(db_path)
    try:
        repo.create(user_id=1, campaign_id=campaign.id, account_id=acc1.id, status="pending",
                    invited_at=datetime.now(timezone.utc) - timedelta(days=60))
    finally:
        repo.close()
    assert _candidates(db_path, campaign.id) == []


def test_crash_after_send_before_record_recovers_as_joined(tmp_path):
    """Обрыв МЕЖДУ отправкой и записью результата: строки нет, кандидат
    выбирается снова, Telegram отвечает UserAlreadyParticipantError ->
    'joined' (без повторного приглашения)."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="ok", keywords=["страхов"], access_hash=11, is_bot=False)
    _campaign(db_path)
    _accounts(db_path, ["acc1"])
    factory = _make_client_factory(call_errors={"acc1": [UserAlreadyParticipantError(request=GetHistoryRequest)]})
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
    assert [r.status for r in _invites(db_path)] == ["joined"]


# ---- transient / backoff / FloodWait ----


def test_transient_network_failure_is_not_terminal_and_backs_off(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="flaky", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    factory = _make_client_factory(
        get_input_entity_errors={"acc1": ValueError("нет в кэше")},
        entity_responses={"acc1": {"@flaky": ConnectionError("network down"), 1: KeyError(1)}},
    )
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
    (row,) = _invites(db_path)
    assert row.status == "failed" and row.failure_kind == "transient"
    assert row.next_attempt_at > datetime.now(timezone.utc) + timedelta(minutes=50)
    # Не долбим на каждом тике...
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))
    assert len(_invites(db_path)) == 1
    # ...но после backoff — снова кандидат (в т.ч. для ТОГО ЖЕ аккаунта: сбой не про identity).
    _expire_backoff(db_path)
    assert _candidates(db_path, campaign.id, account_id=acc1.id) == [1]


def test_transient_backoff_grows_exponentially_and_is_capped():
    assert _next_attempt_delay("transient", 0) == timedelta(hours=1)
    assert _next_attempt_delay("transient", 1) == timedelta(hours=2)
    assert _next_attempt_delay("transient", 3) == timedelta(hours=8)
    assert _next_attempt_delay("transient", 50) == timedelta(days=7)
    assert _next_attempt_delay("user_state", 0) == timedelta(days=3)
    assert _next_attempt_delay("user_state", 9) == timedelta(days=30)
    assert _next_attempt_delay("unresolved", 7) == timedelta(minutes=30)
    assert _next_attempt_delay("account", 3) is None


def test_floodwait_blocks_the_account_but_does_not_penalize_the_candidate(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="ok", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    (acc1, acc2) = _accounts(db_path, ["acc1", "acc2"])
    factory = _make_client_factory(call_errors={"acc1": [FloodWaitError(request=GetHistoryRequest, capture=3600)]})
    asyncio.run(_run_service(db_path, client_factory=factory, execute=True))

    statuses = [(r.account_id, r.status, r.failure_kind, r.next_attempt_at) for r in _invites(db_path)]
    assert statuses[0] == (acc1.id, "failed", "account", None)
    accounts = TelegramAccountRepository(db_path)
    try:
        blocked = accounts.get(acc1.id)
    finally:
        accounts.close()
    assert blocked.blocked_until is not None and blocked.blocked_reason == "flood_wait"
    # Кандидат не штрафуется: acc2 в этом же прогоне успешно пригласил его.
    assert statuses[1][:2] == (acc2.id, "pending")


def test_unresolvable_error_classification_is_centralized():
    unresolved = _classify_invite_error(_CandidateUnresolvableError("x", kind="unresolved"))
    transient = _classify_invite_error(_CandidateUnresolvableError("x", kind="transient"))
    flood = _classify_invite_error(FloodWaitError(request=GetHistoryRequest, capture=5))
    assert (unresolved.db_status, unresolved.failure_kind) == ("failed", "unresolved")
    assert (transient.db_status, transient.failure_kind) == ("failed", "transient")
    assert (flood.action, flood.failure_kind) == (InviteErrorAction.RETRY_LATER, "account")


def test_legacy_failed_rows_without_retry_fields_keep_old_selection(tmp_path):
    """Строки, записанные до появления failure_kind/next_attempt_at (NULL),
    не меняют выборку — существующая история не переинтерпретируется."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="u", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    repo = UserCampaignInviteRepository(db_path)
    try:
        repo.create(user_id=1, campaign_id=campaign.id, status="failed", error="legacy")
    finally:
        repo.close()
    assert _candidates(db_path, campaign.id) == [1]


# ---- multi-campaign ----


def test_georgia_terminal_state_does_not_exclude_armenia(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, _MARGOSHA, username="Margosha_070", keywords=["страхов"], access_hash=11, is_bot=False)
    ge = _campaign(db_path)
    am = _campaign(db_path, name="Армения", keyword="страхов", target="@osagoarmen")
    (acc1,) = _accounts(db_path, ["acc1"])
    repo = UserCampaignInviteRepository(db_path)
    try:
        repo.create(user_id=_MARGOSHA, campaign_id=ge.id, account_id=acc1.id, status="invalid",
                    failure_kind="unresolved")
        repo.create(user_id=_MARGOSHA, campaign_id=ge.id, account_id=acc1.id, status="failed",
                    failure_kind="unresolved", next_attempt_at=datetime.now(timezone.utc) + timedelta(hours=5))
    finally:
        repo.close()
    assert _candidates(db_path, ge.id) == []
    assert _candidates(db_path, am.id) == [_MARGOSHA]
    assert _candidates(db_path, am.id, account_id=acc1.id) == [_MARGOSHA]  # per-account исключение — тоже по кампании


def test_armenia_disabled_is_never_processed_by_worker(tmp_path):
    db_path = _setup_db(tmp_path)
    _campaign(db_path, enabled=True)
    _campaign(db_path, name="Армения", target="@osagoarmen", enabled=False)
    _accounts(db_path, ["acc1", "acc2"])
    calls = []

    class _Recorder:
        async def run_one_worker_attempt(self, campaign, account, *, hourly_limit):
            calls.append(campaign.target_chat)

    campaigns, accounts = InviteCampaignRepository(db_path), TelegramAccountRepository(db_path)
    try:
        worker = InviterWorker(_Recorder(), campaigns, accounts, invitations_per_account_per_hour=2,
                               poll_interval_seconds=1, shutdown_event=asyncio.Event())
        for _ in range(6):
            asyncio.run(worker.run_one_tick())
    finally:
        campaigns.close()
        accounts.close()
    assert set(calls) == {"@car_ins_georgia"}


def test_not_joined_is_backed_off_for_a_week(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="ok", keywords=["страхов"], access_hash=11, is_bot=False)
    campaign = _campaign(db_path)
    (acc1,) = _accounts(db_path, ["acc1"])
    repo = UserCampaignInviteRepository(db_path)
    try:
        invite = repo.create(user_id=1, campaign_id=campaign.id, account_id=acc1.id, status="pending",
                             invited_at=datetime.now(timezone.utc))
        repo.update(invite.id, status="not_joined", verified_at=datetime.now(timezone.utc),
                    next_attempt_at=datetime.now(timezone.utc) + timedelta(days=7))
    finally:
        repo.close()
    assert _candidates(db_path, campaign.id) == []
    _expire_backoff(db_path)
    assert _candidates(db_path, campaign.id) == [1]
