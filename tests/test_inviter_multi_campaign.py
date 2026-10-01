"""Мультикампании инвайтера: Грузия (существующая, без ограничения по
источнику — users.keywords) и Армения — Садахло (пул лидов из истории
@sadahlo, см. reader/inviter/lead_pool.py).

Всё — на временных SQLite-файлах и фейковых Telegram-клиентах: ни одного
реального подключения к Telegram, ни одного реального приглашения."""

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl.types import Channel, PeerChannel, User

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter import lead_pool  # noqa: E402
from reader.inviter import manage  # noqa: E402
from reader.inviter.lead_pool import (  # noqa: E402
    CampaignLeadRepository,
    import_scan,
    known_campaign_user_ids,
    load_checkpoints,
    scan_campaign_sources,
    summarize_scan,
)
from reader.inviter.repository import (  # noqa: E402
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)
from reader.inviter.runtime_state_repository import InviterRuntimeStateRepository  # noqa: E402
from reader.inviter.worker import InviterWorker  # noqa: E402
from reader.inviter_admin_bot import texts  # noqa: E402
from reader.inviter_admin_bot.conversation import AdminBotController  # noqa: E402
from reader.inviter_admin_bot.conversation_state_repository import (  # noqa: E402
    AdminBotConversationStateRepository,
)
from reader.inviter_admin_bot.keyboards import (  # noqa: E402
    CAMPAIGNS_BACK,
    campaign_card_keyboard,
    decode_campaign_disable_callback,
    decode_campaign_enable_callback,
    decode_campaign_open_callback,
    decode_campaign_refresh_callback,
)
from reader.inviter_admin_bot.service import InviterAdminService  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402
from test_inviter_candidates import (  # noqa: E402
    _BASE_TIME,
    _make_client_factory,
    _run_service,
    _seed_user,
    _setup_db,
)

_TRUSTED_ID = 42
_SADAHLO_RAW_ID = 1555971043
_SADAHLO_PEER_ID = -1001555971043
_TARGET_MEMBER_ID = 7001


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import reader.inviter.service as service_module

    async def instant_sleep(seconds):
        pass

    monkeypatch.setattr(service_module.asyncio, "sleep", instant_sleep)


# ---- helpers ----


def _create_campaigns(db_path, *, ge_enabled=True, am_enabled=False):
    repo = InviteCampaignRepository(db_path)
    try:
        ge = repo.create(
            name="Страхование", keyword="страх", target_chat="@tplgee", enabled=ge_enabled,
            slug="ge_insurance", display_name="🇬🇪 Грузия — страховка",
        )
        am = repo.create(
            name="Армения — Садахло", keyword="страх", target_chat="@osagoarmen", enabled=am_enabled,
            slug="am_insurance_sadakhlo", display_name="🇦🇲 Армения — Садахло",
            source_chats=["@sadahlo"], source_title="@sadahlo",
        )
        return ge, am
    finally:
        repo.close()


def _create_account(db_path, name="acc1", daily_limit=10):
    repo = TelegramAccountRepository(db_path)
    try:
        return repo.create(
            name=name, phone="+995500000001", session_name=name,
            session_path=f"{name}.session", daily_limit=daily_limit,
        )
    finally:
        repo.close()


def _user(user_id, *, username="u", bot=False, deleted=False):
    return User(
        id=user_id, access_hash=user_id * 10, username=None if username is None else f"{username}{user_id}",
        first_name=f"Имя{user_id}", bot=bot, deleted=deleted,
    )


def _msg(message_id, text, sender, *, days=0):
    return SimpleNamespace(
        id=message_id, raw_text=text, sender=sender,
        date=datetime(2026, 5, 1, tzinfo=timezone.utc) + timedelta(days=days),
    )


class _FakeHistoryClient:
    """Только методы чтения — get_entity/iter_messages/iter_participants.
    Любой другой вызов (send_message, __call__ с мутирующим запросом и
    т.п.) упал бы AttributeError."""

    def __init__(self, history, *, members=None, members_error=None):
        self._history = sorted(history, key=lambda m: m.id)
        self._members = members or []
        self._members_error = members_error
        self.iter_calls: list[dict] = []
        self.get_entity_calls: list = []

    async def get_entity(self, ref):
        self.get_entity_calls.append(ref)
        if ref == "@sadahlo":
            return PeerChannel(_SADAHLO_RAW_ID)
        return SimpleNamespace(id=999, ref=ref)

    async def iter_messages(self, entity, *, min_id=0, reverse=False):
        self.iter_calls.append({"entity": entity, "min_id": min_id, "reverse": reverse})
        for message in self._history:
            if message.id > min_id:
                yield message

    async def iter_participants(self, entity):
        if self._members_error is not None:
            raise self._members_error
        for member in self._members:
            yield member


def _sadahlo_history():
    alice, bob, carol = _user(101), _user(102), _user(103, username=None)
    bot, ghost, member = _user(104, bot=True), _user(105, deleted=True), _user(_TARGET_MEMBER_ID)
    return [
        _msg(1, "Где сделать СТРАХОВКУ на авто?", alice, days=0),
        _msg(2, "Привет всем", bob),
        _msg(3, "надо застраховать машину", alice, days=2),
        _msg(4, "автостраховка в Армении сколько?", bob, days=3),
        _msg(5, "страхование для армении", carol, days=4),
        _msg(6, "Страна красивая", bot),  # нет "страх" — не совпадает
        _msg(7, "страховка нужна", bot),
        _msg(8, "страховка?", ghost),
        _msg(9, "страховка есть", member),
        _msg(10, "страховка от канала", None),  # пост от имени канала — не пользователь
        _msg(11, "Погода", alice),
    ]


def _campaign(db_path, slug):
    repo = InviteCampaignRepository(db_path)
    try:
        return repo.get_by_slug(slug)
    finally:
        repo.close()


def _import(db_path, campaign, client):
    leads = CampaignLeadRepository(db_path)
    users = UserRepository(db_path)
    try:
        result = asyncio.run(scan_campaign_sources(client, campaign, load_checkpoints(leads, campaign)))
        import_scan(result, leads, users, now=datetime(2026, 10, 1, tzinfo=timezone.utc))
        return result
    finally:
        leads.close()
        users.close()


def _invite_count(db_path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_campaign_invites'"
        ).fetchone()
        return conn.execute("SELECT COUNT(*) FROM user_campaign_invites").fetchone()[0] if has else 0
    finally:
        conn.close()


# ---- Georgia unchanged ----


def test_georgia_candidates_unchanged_exact_token_from_all_groups(tmp_path):
    """Грузия — ровно прежнее правило: точный токен users.keywords, без
    какого-либо ограничения по источнику и без пула."""
    db_path = _setup_db(tmp_path)
    ge, _am = _create_campaigns(db_path)
    _seed_user(db_path, 1, keywords=["страх"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["страхов", "страховку"], access_hash=2, last_seen_at=_BASE_TIME)

    invites = UserCampaignInviteRepository(db_path)
    try:
        assert [c.user_id for c in invites.select_candidates(ge.id, limit=10)] == [1]
    finally:
        invites.close()


def test_georgia_existing_history_is_preserved_and_still_deduplicates(tmp_path):
    """Существующая история (campaign_id=1) не переинтерпретируется:
    приглашённый в Грузию не выбирается для Грузии повторно."""
    db_path = _setup_db(tmp_path)
    ge, _am = _create_campaigns(db_path)
    _seed_user(db_path, 1, keywords=["страх"], access_hash=1)
    invites = UserCampaignInviteRepository(db_path)
    try:
        invites.create(user_id=1, campaign_id=ge.id, status="joined")
        assert invites.select_candidates(ge.id, limit=10) == []
        assert [i.campaign_id for i in invites.list()] == [ge.id]
    finally:
        invites.close()


def test_legacy_campaign_table_migrates_additively(tmp_path):
    """БД до мультикампаний: строка Грузии сохраняется как есть, новые
    колонки — NULL (= прежнее поведение)."""
    db_path = tmp_path / "users.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE invite_campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "keyword TEXT NOT NULL, target_chat TEXT NOT NULL, enabled BOOLEAN NOT NULL DEFAULT TRUE, "
        "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute("INSERT INTO invite_campaigns (name, keyword, target_chat, enabled) VALUES ('Страхование', 'страх', '@tplgee', 1)")
    conn.commit()
    conn.close()

    for _ in range(2):  # идемпотентно
        repo = InviteCampaignRepository(db_path)
        try:
            (campaign,) = repo.list()
        finally:
            repo.close()
    assert (campaign.id, campaign.name, campaign.keyword, campaign.target_chat, campaign.enabled) == (
        1, "Страхование", "страх", "@tplgee", True,
    )
    assert campaign.slug is None and campaign.source_chats == () and campaign.label == "Страхование"


def test_ensure_campaign_assigns_slug_to_existing_georgia_without_changing_enabled(tmp_path):
    db_path = tmp_path / "users.db"
    legacy = manage.ensure_campaign(db_path, name="Страхование", keyword="страх", target_chat="@tplgee", enabled=False)

    tagged = manage.ensure_campaign(
        db_path, slug="ge_insurance", name="Страхование", display_name="🇬🇪 Грузия — страховка",
        keyword="страх", target_chat="@tplgee",
    )

    assert tagged.id == legacy.id
    assert tagged.slug == "ge_insurance"
    assert tagged.enabled is False  # не передан — не меняется
    assert tagged.source_chats == ()


def test_new_campaign_without_enabled_flag_is_created_disabled(tmp_path):
    campaign = manage.ensure_campaign(
        tmp_path / "users.db", slug="am_insurance_sadakhlo", name="Армения — Садахло",
        keyword="страх", target_chat="@osagoarmen", source_chats=["@sadahlo"],
    )
    assert campaign.enabled is False
    assert campaign.source_chats == ("@sadahlo",)


# ---- Armenia: historical scan ----


def test_scan_reads_only_sadahlo_full_history_and_matches_root_substring(tmp_path):
    db_path = _setup_db(tmp_path)
    _ge, _am = _create_campaigns(db_path)
    am = _campaign(db_path, "am_insurance_sadakhlo")
    client = _FakeHistoryClient(_sadahlo_history())

    result = asyncio.run(scan_campaign_sources(client, am))

    assert client.get_entity_calls == ["@sadahlo", "@osagoarmen"]  # источник + проверка участников target
    assert client.iter_calls == [{"entity": PeerChannel(_SADAHLO_RAW_ID), "min_id": 0, "reverse": True}]
    assert result.sources[0].source_chat_id == _SADAHLO_PEER_ID
    assert result.messages_scanned == 11
    # 1,3,4,5,7,8,9,10 — "страх" подстрока (СТРАХОВКУ/застраховать/автостраховка/
    # страхование) — та же семантика KeywordMatcher (lower + подстрока), без списка форм.
    assert result.matching_messages == 8
    assert result.sources[0].non_user_matches == 1
    assert set(result.observations) == {101, 102, 103, 104, 105, _TARGET_MEMBER_ID}


def test_multiple_messages_by_one_user_give_one_candidate_with_evidence(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path)
    am = _campaign(db_path, "am_insurance_sadakhlo")
    _import(db_path, am, _FakeHistoryClient(_sadahlo_history()))

    leads = CampaignLeadRepository(db_path)
    try:
        alice = leads.get(am.id, 101)
        assert alice.match_count == 2
        assert (alice.first_message_id, alice.last_message_id) == (1, 3)
        assert alice.source_chat_id == _SADAHLO_PEER_ID and alice.source_ref == "@sadahlo"
        assert alice.matched_keyword == "страх"
        assert len([lead for lead in leads.list(am.id) if lead.user_id == 101]) == 1
    finally:
        leads.close()


def test_bots_deleted_and_target_members_are_not_actionable(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path)
    am = _campaign(db_path, "am_insurance_sadakhlo")
    client = _FakeHistoryClient(_sadahlo_history(), members=[_user(_TARGET_MEMBER_ID)])
    _import(db_path, am, client)

    leads = CampaignLeadRepository(db_path)
    invites = UserCampaignInviteRepository(db_path)
    try:
        statuses = {lead.user_id: lead.status for lead in leads.list(am.id)}
        assert statuses == {
            101: "new", 102: "new", 103: "new", 104: "bot", 105: "deleted", _TARGET_MEMBER_ID: "already_member",
        }
        # 103 без username — общий фильтр инвайтера (не может резолвить).
        assert sorted(c.user_id for c in invites.select_candidates(am.id, limit=50)) == [101, 102]
    finally:
        leads.close()
        invites.close()


def test_dry_run_summary_writes_nothing_and_does_not_enable(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path)
    client = _FakeHistoryClient(_sadahlo_history(), members=[_user(_TARGET_MEMBER_ID)])
    preview = tmp_path / "preview.csv"

    campaign, _result, summary, path, outcome, checkpoints = asyncio.run(manage.build_lead_pool(
        db_path, "am_insurance_sadakhlo", client=client, preview_out=preview,
    ))

    assert outcome is None and checkpoints == {}
    assert (summary.messages_scanned, summary.matching_messages, summary.unique_users) == (11, 8, 6)
    assert (summary.bots, summary.deleted, summary.already_member) == (1, 1, 1)
    assert (summary.eligible, summary.eligible_without_username) == (2, 1)
    leads = CampaignLeadRepository(db_path)
    try:
        assert leads.list(campaign.id) == [] and leads.list_checkpoints(campaign.id) == []
    finally:
        leads.close()
    assert _invite_count(db_path) == 0
    assert _campaign(db_path, "am_insurance_sadakhlo").enabled is False
    csv_text = path.read_text(encoding="utf-8-sig")
    assert csv_text.splitlines()[0].startswith("telegram_user_id,username,display_name,source")
    assert "@u101" in csv_text
    # Тексты сообщений не выгружаются — только совпавшая словоформа.
    assert "где сделать" not in csv_text.lower() and "на авто" not in csv_text.lower()
    assert "страховку; застраховать" in csv_text


def test_import_is_idempotent(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path)
    am = _campaign(db_path, "am_insurance_sadakhlo")

    leads = CampaignLeadRepository(db_path)
    users = UserRepository(db_path)
    try:
        result = asyncio.run(scan_campaign_sources(_FakeHistoryClient(_sadahlo_history()), am))
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        import_scan(result, leads, users, now=now)
        first = leads.list(am.id)
        import_scan(result, leads, users, now=now)  # тот же результат повторно
        assert leads.list(am.id) == first
    finally:
        leads.close()
        users.close()

    # Повторный полный прогон после checkpoint ничего не добавляет.
    client = _FakeHistoryClient(_sadahlo_history())
    _import(db_path, am, client)
    assert client.iter_calls[0]["min_id"] == 11
    leads = CampaignLeadRepository(db_path)
    try:
        assert leads.list(am.id) == first
    finally:
        leads.close()


def test_incremental_scan_starts_after_checkpoint(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path)
    am = _campaign(db_path, "am_insurance_sadakhlo")
    history = _sadahlo_history()
    _import(db_path, am, _FakeHistoryClient(history))

    newer = history + [_msg(12, "ещё раз про страховку", _user(101), days=10), _msg(13, "страх", _user(201))]
    client = _FakeHistoryClient(newer)
    result = _import(db_path, am, client)

    assert client.iter_calls[0]["min_id"] == 11
    assert result.messages_scanned == 2
    leads = CampaignLeadRepository(db_path)
    try:
        assert leads.get(am.id, 101).match_count == 3
        assert leads.get(am.id, 201).status == "new"
        (checkpoint,) = leads.list_checkpoints(am.id)
        assert checkpoint.last_message_id == 13 and checkpoint.messages_scanned == 13
    finally:
        leads.close()


def test_target_membership_unknown_falls_back_to_invite_time_handling(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path)
    am = _campaign(db_path, "am_insurance_sadakhlo")
    client = _FakeHistoryClient(_sadahlo_history(), members_error=PermissionError("admin required"))

    result = asyncio.run(scan_campaign_sources(client, am))
    summary = summarize_scan(result, known_user_ids=set())

    assert result.target_member_ids is None
    assert summary.target_checked is False and "PermissionError" in summary.target_check_error


# ---- cross-campaign dedup / routing ----


def test_same_user_can_be_in_georgia_and_armenia_independently(tmp_path):
    db_path = _setup_db(tmp_path)
    ge, am = _create_campaigns(db_path)
    _import(db_path, am, _FakeHistoryClient(_sadahlo_history()))
    # Тот же человек (101) — также грузинский лид (точный токен).
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE users SET keywords = 'страх' WHERE user_id = 101")
    conn.commit()
    conn.close()

    invites = UserCampaignInviteRepository(db_path)
    try:
        invites.create(user_id=101, campaign_id=ge.id, status="joined")
        assert 101 not in {c.user_id for c in invites.select_candidates(ge.id, limit=50)}
        # Приглашение в Грузию НЕ исключает из Армении.
        assert 101 in {c.user_id for c in invites.select_candidates(am.id, limit=50)}
        assert 101 in known_campaign_user_ids(db_path, am.id)
        assert 101 in known_campaign_user_ids(db_path, ge.id)
    finally:
        invites.close()


def test_same_user_not_reselected_within_one_campaign(tmp_path):
    db_path = _setup_db(tmp_path)
    _ge, am = _create_campaigns(db_path)
    _import(db_path, am, _FakeHistoryClient(_sadahlo_history()))
    invites = UserCampaignInviteRepository(db_path)
    try:
        invites.create(user_id=101, campaign_id=am.id, status="pending")
        # 7001 здесь не помечен участником (список участников target не передан).
        assert {c.user_id for c in invites.select_candidates(am.id, limit=50)} == {102, _TARGET_MEMBER_ID}
    finally:
        invites.close()


def test_success_in_other_campaign_with_same_target_blocks_reinvite(tmp_path):
    db_path = _setup_db(tmp_path)
    repo = InviteCampaignRepository(db_path)
    try:
        a = repo.create(name="A", keyword="страх", target_chat="@same")
        b = repo.create(name="B", keyword="страх", target_chat="@SAME")
    finally:
        repo.close()
    _seed_user(db_path, 1, keywords=["страх"], access_hash=1)
    invites = UserCampaignInviteRepository(db_path)
    try:
        invites.create(user_id=1, campaign_id=a.id, status="joined")
        assert invites.select_candidates(b.id, limit=10) == []
    finally:
        invites.close()


def test_armenia_lead_never_routes_to_georgia_and_vice_versa(tmp_path):
    """Обе кампании включены, один аккаунт (общий): Армения приглашает
    только своего лида и только в @osagoarmen, Грузия — только своего и
    только в @tplgee. Всё на фейковом клиенте."""
    db_path = _setup_db(tmp_path)
    ge, am = _create_campaigns(db_path, ge_enabled=True, am_enabled=True)
    _seed_user(db_path, 1, username="georgian", keywords=["страх"], access_hash=1)
    _import(db_path, am, _FakeHistoryClient([_msg(1, "страховка", _user(101))]))
    _create_account(db_path)

    created: list = []
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(created=created), execute=True))

    invites = UserCampaignInviteRepository(db_path)
    try:
        routed = {(i.campaign_id, i.user_id) for i in invites.list()}
    finally:
        invites.close()
    assert routed == {(ge.id, 1), (am.id, 101)}
    targets = [call for client in created for call in client.get_entity_calls if isinstance(call, str)]
    assert targets.count("@tplgee") >= 1 and targets.count("@osagoarmen") >= 1
    # Каждый клиент работает в рамках одной кампании — ни одного клиента,
    # резолвившего обе цели.
    for client in created:
        resolved = {c for c in client.get_entity_calls if c in ("@tplgee", "@osagoarmen")}
        assert len(resolved) <= 1


def test_dry_run_single_disabled_campaign_only_touches_its_target(tmp_path):
    db_path = _setup_db(tmp_path)
    _ge, am = _create_campaigns(db_path, ge_enabled=True, am_enabled=False)
    _seed_user(db_path, 1, keywords=["страх"], access_hash=1)
    _import(db_path, am, _FakeHistoryClient([_msg(1, "страховка", _user(101))]))
    _create_account(db_path)

    created: list = []
    account_repo, campaign_repo = TelegramAccountRepository(db_path), InviteCampaignRepository(db_path)
    invite_repo = UserCampaignInviteRepository(db_path)
    try:
        from reader.inviter.service import InviterService

        service = InviterService(
            account_repo, campaign_repo, invite_repo, client_factory=_make_client_factory(created=created),
            session_checker=lambda account: True,
        )
        asyncio.run(service.run(execute=False, only_campaign_id=am.id))
        # execute=True с выключенной кампанией — ничего не делает вовсе.
        asyncio.run(service.run(execute=True, only_campaign_id=am.id))
    finally:
        account_repo.close()
        campaign_repo.close()
        invite_repo.close()

    calls = [c for client in created for c in client.get_entity_calls if isinstance(c, str)]
    assert calls == ["@osagoarmen"]
    assert all(not client.call_requests for client in created)
    assert _invite_count(db_path) == 0


# ---- independent enable/disable (worker) ----


class _RecordingService:
    def __init__(self):
        self.calls = []

    async def run_one_worker_attempt(self, campaign, account, *, hourly_limit):
        self.calls.append(campaign.slug)


@pytest.mark.parametrize(
    "ge_enabled, am_enabled, expected",
    [
        (True, False, {"ge_insurance"}),
        (False, True, {"am_insurance_sadakhlo"}),
        (True, True, {"ge_insurance", "am_insurance_sadakhlo"}),
        (False, False, set()),
    ],
)
def test_worker_processes_only_enabled_campaigns(tmp_path, ge_enabled, am_enabled, expected):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path, ge_enabled=ge_enabled, am_enabled=am_enabled)
    _create_account(db_path, "acc1")
    _create_account(db_path, "acc2")
    service = _RecordingService()
    campaigns, accounts = InviteCampaignRepository(db_path), TelegramAccountRepository(db_path)
    try:
        worker = InviterWorker(
            service, campaigns, accounts, invitations_per_account_per_hour=2,
            poll_interval_seconds=1, shutdown_event=asyncio.Event(),
        )
        for _ in range(8):
            asyncio.run(worker.run_one_tick())
    finally:
        campaigns.close()
        accounts.close()
    assert set(service.calls) == expected


def test_account_rate_limits_are_shared_across_campaigns(tmp_path):
    """Дневной/часовой лимит — свойство аккаунта: приглашения в Армению
    расходуют тот же бюджет, что и в Грузию (одни и те же счётчики)."""
    db_path = _setup_db(tmp_path)
    ge, am = _create_campaigns(db_path)
    account = _create_account(db_path)
    now = datetime.now(timezone.utc)
    invites = UserCampaignInviteRepository(db_path)
    try:
        invites.create(user_id=1, campaign_id=ge.id, account_id=account.id, status="pending", invited_at=now)
        invites.create(user_id=2, campaign_id=am.id, account_id=account.id, status="pending", invited_at=now)
        assert invites.count_today_pending(account.id) == 2
        assert invites.count_recent_sent(account.id, now - timedelta(hours=1)) == 2
    finally:
        invites.close()


# ---- restart / global pause migration ----


def test_restart_preserves_per_campaign_enabled_states(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path, ge_enabled=False, am_enabled=True)
    InviterRuntimeStateRepository(db_path).close()
    InviterRuntimeStateRepository(db_path).close()  # повторный старт
    assert _campaign(db_path, "ge_insurance").enabled is False
    assert _campaign(db_path, "am_insurance_sadakhlo").enabled is True


def test_legacy_global_pause_is_folded_into_campaigns_once(tmp_path):
    """До мультикампаний: inviter_enabled=0 (⏸) при campaign.enabled=1 —
    эффективно выключено. После миграции — то же эффективное состояние,
    выраженное флагом кампании; повторные запуски ничего не меняют."""
    db_path = _setup_db(tmp_path)
    ge, _am = _create_campaigns(db_path, ge_enabled=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE inviter_runtime_state (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "inviter_enabled INTEGER NOT NULL DEFAULT 1, last_tick_at TIMESTAMP, "
        "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute("INSERT INTO inviter_runtime_state (id, inviter_enabled) VALUES (1, 0)")
    conn.commit()
    conn.close()

    repo = InviterRuntimeStateRepository(db_path)
    try:
        assert repo.get().inviter_enabled is True
    finally:
        repo.close()
    assert _campaign(db_path, "ge_insurance").enabled is False

    campaigns = InviteCampaignRepository(db_path)
    campaigns.update(ge.id, enabled=True)
    campaigns.close()
    InviterRuntimeStateRepository(db_path).close()
    assert _campaign(db_path, "ge_insurance").enabled is True  # второй раз не трогает


def test_global_running_state_leaves_campaigns_untouched(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path, ge_enabled=True, am_enabled=False)
    InviterRuntimeStateRepository(db_path).close()
    assert _campaign(db_path, "ge_insurance").enabled is True
    assert _campaign(db_path, "am_insurance_sadakhlo").enabled is False


# ---- admin bot ----


class _Bot:
    def __init__(self, tmp_path, *, scan_client=None):
        self.db_path = _setup_db(tmp_path)
        self.ge, self.am = _create_campaigns(self.db_path)
        self.accounts = TelegramAccountRepository(self.db_path)
        self.campaigns = InviteCampaignRepository(self.db_path)
        self.invites = UserCampaignInviteRepository(self.db_path)
        self.runtime = InviterRuntimeStateRepository(self.db_path)
        self.states = AdminBotConversationStateRepository(":memory:")
        self.service = InviterAdminService(
            self.accounts, self.campaigns, self.invites, self.runtime,
            db_path=self.db_path, trusted_admin_user_ids=frozenset({_TRUSTED_ID}),
            lead_scan_client_factory=(lambda: scan_client) if scan_client else None,
        )
        self.controller = AdminBotController(self.service, None, self.states, sync_client_factory=lambda a: None)

    def close(self):
        for repo in (self.accounts, self.campaigns, self.invites, self.runtime, self.states):
            repo.close()


class _ScanClient(_FakeHistoryClient):
    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def disconnect(self):
        pass


@pytest.fixture
def bot(tmp_path):
    instance = _Bot(tmp_path)
    yield instance
    instance.close()


async def test_campaign_selector_lists_campaigns_separately(bot):
    reply = await bot.controller.handle_text(texts.CAMPAIGNS_LABEL, chat_id=1, telegram_user_id=_TRUSTED_ID)

    assert reply.text == (
        "📣 Кампании приглашений\n\n🇬🇪 Грузия — страховка\n🟢 Включена\n\n🇦🇲 Армения — Садахло\n🔴 Выключена"
    )
    assert reply.campaigns_page_options == [
        (bot.ge.id, "🇬🇪 Грузия — страховка", True), (bot.am.id, "🇦🇲 Армения — Садахло", False),
    ]


async def test_campaign_selector_denied_for_untrusted(bot):
    reply = await bot.controller.handle_text(texts.CAMPAIGNS_LABEL, chat_id=1, telegram_user_id=999)
    assert reply.text == texts.ACCESS_DENIED_TEXT
    assert bot.controller.handle_campaign_set_enabled(bot.am.id, True, telegram_user_id=999).text == texts.ACCESS_DENIED_TEXT
    assert _campaign(bot.db_path, "am_insurance_sadakhlo").enabled is False


def test_campaign_card_shows_source_filter_target_and_buttons(bot):
    reply = bot.controller.handle_campaign_open(bot.am.id, telegram_user_id=_TRUSTED_ID)

    assert reply.text.startswith("🇦🇲 Армения — Садахло\n\nИсточник: @sadahlo\nФильтр: страх\nЦель: @osagoarmen")
    assert "🔴 Приглашения выключены" in reply.text
    assert (reply.campaign_card_enabled, reply.campaign_card_has_pool) == (False, True)
    rows = campaign_card_keyboard(bot.am.id, enabled=False, has_pool=True)
    labels = [b.text for row in rows for b in row]
    assert labels == [texts.CAMPAIGN_ENABLE_LABEL, texts.CAMPAIGN_REFRESH_LABEL, texts.CAMPAIGN_STATS_LABEL, texts.BACK_BUTTON_LABEL]
    assert decode_campaign_enable_callback(rows[0][0].data) == bot.am.id
    assert decode_campaign_refresh_callback(rows[1][0].data) == bot.am.id
    assert rows[3][0].data == CAMPAIGNS_BACK
    ge_rows = campaign_card_keyboard(bot.ge.id, enabled=True, has_pool=False)
    assert decode_campaign_disable_callback(ge_rows[0][0].data) == bot.ge.id
    assert texts.CAMPAIGN_REFRESH_LABEL not in [b.text for row in ge_rows for b in row]


@pytest.mark.parametrize("ge_target, am_target", [(True, False), (False, True), (True, True), (False, False)])
def test_independent_enable_disable_buttons(bot, ge_target, am_target):
    bot.controller.handle_campaign_set_enabled(bot.ge.id, ge_target, telegram_user_id=_TRUSTED_ID)
    bot.controller.handle_campaign_set_enabled(bot.am.id, am_target, telegram_user_id=_TRUSTED_ID)

    assert _campaign(bot.db_path, "ge_insurance").enabled is ge_target
    assert _campaign(bot.db_path, "am_insurance_sadakhlo").enabled is am_target
    assert bot.service.is_global_enabled() is True  # глобальный флаг не трогается


def test_turning_armenia_off_does_not_stop_georgia(bot):
    bot.controller.handle_campaign_set_enabled(bot.am.id, True, telegram_user_id=_TRUSTED_ID)
    reply = bot.controller.handle_campaign_set_enabled(bot.am.id, False, telegram_user_id=_TRUSTED_ID)
    assert reply.campaign_card_enabled is False
    assert _campaign(bot.db_path, "ge_insurance").enabled is True


def test_campaign_statistics_are_campaign_specific(tmp_path):
    bot = _Bot(tmp_path)
    try:
        client = _FakeHistoryClient(_sadahlo_history(), members=[_user(_TARGET_MEMBER_ID)])
        _import(bot.db_path, bot.am, client)
        _seed_user(bot.db_path, 1, keywords=["страх"], access_hash=1)
        _seed_user(bot.db_path, 2, keywords=["страх"], access_hash=2)
        bot.invites.create(user_id=1, campaign_id=bot.ge.id, status="joined")
        bot.invites.create(user_id=2, campaign_id=bot.ge.id, status="failed")
        bot.invites.create(user_id=101, campaign_id=bot.am.id, status="pending")

        am = bot.service.campaign_stats(bot.am.id)
        ge = bot.service.campaign_stats(bot.ge.id)
    finally:
        bot.close()

    assert (am.leads_found, am.awaiting, am.sent_pending, am.joined, am.failed) == (6, 1, 1, 0, 0)
    assert (am.pool_bots, am.pool_deleted, am.pool_already_member, am.without_username) == (1, 1, 1, 1)
    assert am.skipped == 4
    assert (ge.leads_found, ge.awaiting, ge.joined, ge.failed) == (2, 1, 1, 1)
    assert ge.has_pool is False


def test_failed_then_joined_counts_once_by_latest_status(bot):
    _seed_user(bot.db_path, 1, keywords=["страх"], access_hash=1)
    bot.invites.create(user_id=1, campaign_id=bot.ge.id, status="failed")
    bot.invites.create(user_id=1, campaign_id=bot.ge.id, status="joined")
    stats = bot.service.campaign_stats(bot.ge.id)
    assert (stats.joined, stats.failed) == (1, 0)


async def test_refresh_leads_refuses_before_initial_import(bot):
    reply = await bot.controller.handle_campaign_refresh(bot.am.id, telegram_user_id=_TRUSTED_ID)
    assert "Первичный импорт пула ещё не выполнен" in reply.text
    leads = CampaignLeadRepository(bot.db_path)
    try:
        assert leads.list(bot.am.id) == []
    finally:
        leads.close()


def test_refresh_leads_scans_only_after_checkpoint_and_never_enables(tmp_path):
    history = _sadahlo_history()
    scan_client = _ScanClient(history + [_msg(12, "страховка нужна", _user(301))])
    bot = _Bot(tmp_path, scan_client=scan_client)
    try:
        _import(bot.db_path, bot.am, _FakeHistoryClient(history))
        reply = asyncio.run(bot.controller.handle_campaign_refresh(bot.am.id, telegram_user_id=_TRUSTED_ID))
    finally:
        bot.close()

    assert scan_client.iter_calls[0]["min_id"] == 11
    assert "Новых сообщений: 1" in reply.text and "Новых пользователей: 1" in reply.text
    assert _campaign(bot.db_path, "am_insurance_sadakhlo").enabled is False
    assert _invite_count(bot.db_path) == 0


def test_campaign_open_callback_roundtrip():
    from reader.inviter_admin_bot.keyboards import encode_campaign_open_callback

    assert decode_campaign_open_callback(encode_campaign_open_callback(17)) == 17
    assert decode_campaign_open_callback(b"acc_open:17") is None


def test_lead_pool_marked_chat_id_matches_live_event_format():
    channel = Channel(
        id=_SADAHLO_RAW_ID, title="Садахло", photo=None, date=None, megagroup=True, username="sadahlo",
    )
    assert lead_pool.marked_chat_id(channel) == _SADAHLO_PEER_ID


def test_build_lead_pool_cli_defaults_to_dry_run():
    args = manage._parse_args(["build-lead-pool", "--campaign", "am_insurance_sadakhlo"])
    assert args.command == "build-lead-pool" and args.do_import is False
    assert manage._parse_args(["build-lead-pool", "--campaign", "x", "--import"]).do_import is True


def test_add_campaign_cli_accepts_campaign_fields():
    args = manage._parse_args([
        "add-campaign", "--slug", "am_insurance_sadakhlo", "--name", "Армения — Садахло",
        "--display-name", "🇦🇲 Армения — Садахло", "--keyword", "страх",
        "--source-chat", "@sadahlo", "--source-title", "@sadahlo", "--target-chat", "@osagoarmen",
    ])
    assert (args.slug, args.source_chats, args.target_chat, args.enabled) == (
        "am_insurance_sadakhlo", ["@sadahlo"], "@osagoarmen", None,
    )


def test_set_campaign_enabled_cli_requires_explicit_value():
    with pytest.raises(SystemExit):
        manage._parse_args(["set-campaign-enabled", "--campaign", "am_insurance_sadakhlo"])


def test_inviter_main_campaign_flag_only_for_dry_run():
    from reader.inviter import main as inviter_main

    assert inviter_main._parse_args(["--dry-run", "--campaign", "am_insurance_sadakhlo"]).campaign == "am_insurance_sadakhlo"
    for forbidden in (["--execute", "--campaign", "x"], ["--worker", "--campaign", "x"]):
        with pytest.raises(SystemExit):
            inviter_main._parse_args(forbidden)


def test_matched_word_forms_report_root_variants_without_text():
    assert lead_pool.matched_word_forms("Где сделать СТРАХОВКУ на авто, застраховать?", "страх") == [
        "страховку", "застраховать",
    ]
    assert lead_pool.matched_word_forms("автострахование и страх", "страх") == ["автострахование", "страх"]
    assert lead_pool.matched_word_forms("ничего", "страх") == []


def test_scan_summary_top_forms_counts_all_matching_messages(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path)
    am = _campaign(db_path, "am_insurance_sadakhlo")
    result = asyncio.run(scan_campaign_sources(_FakeHistoryClient(_sadahlo_history()), am))
    summary = summarize_scan(result, known_user_ids=set())
    forms = dict(summary.top_forms)
    assert forms["страховка"] == 3 and forms["застраховать"] == 1 and forms["автостраховка"] == 1
    assert forms["страхование"] == 1


# ---- candidate quality: ru_insurance match rule, recency, membership ----


def _am_with_rule(db_path, *, lead_max_age_days=None):
    manage.ensure_campaign(
        db_path, slug="am_insurance_sadakhlo", name="Армения — Садахло", keyword="страх",
        target_chat="@osagoarmen", match_rule="ru_insurance", lead_max_age_days=lead_max_age_days,
    )
    return _campaign(db_path, "am_insurance_sadakhlo")


@pytest.mark.parametrize("word", [
    "страховка", "страховки", "страховку", "страхование", "страховании", "страховкой", "страховками",
    "автостраховка", "медстраховка", "застраховать", "застраховаться", "застрахован", "страхуйтесь",
    "Страховщик", "АВТОСТРАХОВАНИЕ",
])
def test_ru_insurance_rule_matches_insurance_morphology(word):
    assert lead_pool._ru_insurance_words(f"нужна {word}, где?", "страх") == [word.lower()]


@pytest.mark.parametrize("text", [
    "еду в Астрахань", "из астрахани", "бензин в канистрах", "на свой страх и риск",
    "от страха", "в страхом случае", "лучше перестраховаться", "это перестраховка", "к страху",
])
def test_ru_insurance_rule_rejects_non_insurance_words(text):
    assert lead_pool._ru_insurance_words(text, "страх") == []


def test_substring_rule_unchanged_for_campaigns_without_match_rule(tmp_path):
    """Без match_rule (как у всех кампаний до этого изменения) — прежняя
    семантика подстроки; Грузия (users.keywords, без пула) этим кодом не
    затрагивается вовсе."""
    db_path = _setup_db(tmp_path)
    ge, am = _create_campaigns(db_path)
    assert am.match_rule is None and ge.match_rule is None
    assert lead_pool.campaign_match_words(am, "еду в Астрахань") == ["астрахань"]
    assert lead_pool.campaign_match_words(am, "привет") == []


def test_georgia_candidate_sql_unaffected_by_armenia_rule_and_recency(tmp_path):
    db_path = _setup_db(tmp_path)
    ge, _am = _create_campaigns(db_path)
    _am_with_rule(db_path, lead_max_age_days=30)
    _seed_user(db_path, 1, keywords=["страх"], access_hash=1, last_seen_at=_BASE_TIME)
    invites = UserCampaignInviteRepository(db_path)
    try:
        assert [c.user_id for c in invites.select_candidates(ge.id, limit=10)] == [1]
    finally:
        invites.close()
    assert _campaign(db_path, "ge_insurance").match_rule is None
    assert _campaign(db_path, "ge_insurance").lead_max_age_days is None


def _quality_history(now):
    old, mid, fresh = _user(401), _user(402), _user(403)
    astra, joker = _user(404), _user(405)

    def at(message_id, text, sender, days_ago):
        return SimpleNamespace(id=message_id, raw_text=text, sender=sender, date=now - timedelta(days=days_ago))

    # message_id растёт со временем, как в Telegram.
    return [
        at(1, "страховка нужна", old, 400),
        at(2, "страховка", fresh, 399),              # старое сообщение fresh — давность по ПОСЛЕДНЕМУ
        at(3, "где страховку купить", mid, 300),
        at(4, "застраховать машину", mid, 100),     # последний матч mid — 100 дней назад
        at(5, "на свой страх и риск", joker, 20),   # ложное совпадение по подстроке
        at(6, "автостраховка?", fresh, 10),
        at(7, "еду в Астрахань", astra, 5),         # ложное совпадение по подстроке
    ]


def test_ru_insurance_scan_counts_false_positives_separately(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path)
    am = _am_with_rule(db_path)
    now = datetime.now(timezone.utc)
    result = asyncio.run(scan_campaign_sources(_FakeHistoryClient(_quality_history(now)), am))

    assert result.matching_messages == 5
    assert set(result.observations) == {401, 402, 403}
    assert result.false_positive_messages == 2
    assert result.false_positive_users == {404, 405}
    assert dict(result.false_positive_forms) == {"астрахань": 1, "страх": 1}


def test_recency_uses_last_matching_message_and_windows(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path)
    am = _am_with_rule(db_path)
    now = datetime.now(timezone.utc)
    result = asyncio.run(scan_campaign_sources(_FakeHistoryClient(_quality_history(now)), am))

    report = {label: s for label, _since, s in lead_pool.recency_report(result, known_user_ids=set(), now=now)}
    assert report["30d"].unique_users == 1 and report["30d"].eligible == 1        # fresh (последний — 10 дней)
    assert report["90d"].unique_users == 1
    assert report["180d"].unique_users == 2                                        # + mid (100 дней)
    assert report["all"].unique_users == 3 and report["all"].too_old == 0
    assert report["30d"].too_old == 2
    assert report["30d"].false_positive_users == 2 and report["all"].false_positive_users == 2
    statuses = {r.user_id: r.status for r in report["90d"].rows}
    assert statuses == {401: "too_old", 402: "too_old", 403: "eligible"}


def test_campaign_lead_max_age_days_limits_candidates_by_last_match(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path)
    am = _am_with_rule(db_path)
    now = datetime.now(timezone.utc)
    _import(db_path, am, _FakeHistoryClient(_quality_history(now)))

    def candidates():
        invites = UserCampaignInviteRepository(db_path)
        try:
            return {c.user_id for c in invites.select_candidates(am.id, limit=50)}
        finally:
            invites.close()

    assert candidates() == {401, 402, 403}                    # без ограничения
    for days, expected in ((30, {403}), (90, {403}), (180, {402, 403}), (0, {401, 402, 403})):
        _am_with_rule(db_path, lead_max_age_days=days)
        assert candidates() == expected, days
    # Пул не теряет старых лидов — они просто не выбираются.
    leads = CampaignLeadRepository(db_path)
    try:
        assert {lead.user_id for lead in leads.list(am.id)} == {401, 402, 403}
    finally:
        leads.close()


def test_hidden_member_list_is_unavailable_not_zero(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path)
    am = _campaign(db_path, "am_insurance_sadakhlo")
    client = _FakeHistoryClient(_sadahlo_history(), members=[])  # скрытый список — пустой ответ

    result = asyncio.run(scan_campaign_sources(client, am))
    summary = summarize_scan(result, known_user_ids=set())

    assert result.target_member_ids is None and result.target_check_status == "unavailable"
    assert summary.target_check_status == "unavailable" and "скрыт" in summary.target_check_error
    text = lead_pool.format_summary(am, summary, mode="DRY RUN")
    assert "Already in target group: unavailable" in text
    report = lead_pool.recency_report(result, known_user_ids=set(), now=datetime.now(timezone.utc))
    assert "unavailable" in lead_pool.format_recency_report(report, target_checked=False)


def test_visible_member_list_is_checked(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path)
    am = _campaign(db_path, "am_insurance_sadakhlo")
    client = _FakeHistoryClient(_sadahlo_history(), members=[_user(_TARGET_MEMBER_ID)])
    result = asyncio.run(scan_campaign_sources(client, am))
    assert result.target_check_status == "checked" and result.target_member_ids == {_TARGET_MEMBER_ID}


def test_dry_run_with_recency_report_persists_nothing(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path)
    _am_with_rule(db_path)
    now = datetime.now(timezone.utc)
    out = asyncio.run(manage.build_lead_pool(
        db_path, "am_insurance_sadakhlo", client=_FakeHistoryClient(_quality_history(now)),
        preview_out=tmp_path / "p.csv", max_age_days=90, with_recency_report=True,
    ))
    campaign, _result, summary, _path, outcome, _cp, report = out
    assert outcome is None and summary.cutoff is not None and summary.eligible == 1
    assert [label for label, _s, _r in report] == ["30d", "90d", "180d", "2026", "all"]
    leads = CampaignLeadRepository(db_path)
    try:
        assert leads.list(campaign.id) == [] and leads.list_checkpoints(campaign.id) == []
    finally:
        leads.close()
    assert _invite_count(db_path) == 0
    am = _campaign(db_path, "am_insurance_sadakhlo")
    assert am.enabled is False and am.lead_max_age_days is None  # --max-age-days не меняет кампанию


def test_match_distribution_buckets():
    rows = [
        lead_pool.PreviewRow(i, "u", None, "@s", None, None, n, "страх", "c", status)
        for i, (n, status) in enumerate([
            (1, "eligible"), (2, "eligible"), (5, "eligible_no_username"), (6, "eligible"),
            (10, "eligible"), (11, "eligible"), (20, "eligible"), (21, "eligible"), (99, "bot"),
        ])
    ]
    summary = lead_pool.ScanSummary(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, False, None, rows)
    assert summary.match_distribution() == {"1": 1, "2-5": 2, "6-10": 2, "11-20": 2, "21+": 1}


def test_add_campaign_cli_match_rule_and_recency_options():
    args = manage._parse_args([
        "add-campaign", "--slug", "am_insurance_sadakhlo", "--name", "x", "--keyword", "страх",
        "--target-chat", "@osagoarmen", "--match-rule", "ru_insurance", "--lead-max-age-days", "90",
    ])
    assert (args.match_rule, args.lead_max_age_days) == ("ru_insurance", 90)
    with pytest.raises(SystemExit):
        manage._parse_args([
            "add-campaign", "--name", "x", "--keyword", "k", "--target-chat", "@t", "--match-rule", "regex",
        ])
    pool_args = manage._parse_args(["build-lead-pool", "--campaign", "x", "--recency-report", "--max-age-days", "30"])
    assert pool_args.recency_report is True and pool_args.max_age_days == 30 and pool_args.do_import is False


def test_import_never_modifies_existing_users_and_georgia_queue(tmp_path):
    """Импорт пула не меняет ни одной существующей строки users (порядок и
    состав очереди Грузии — ORDER BY last_seen_at, обязательные username/
    access_hash — неизменны); username/access_hash из сканирования
    хранятся в campaign_leads и делают лида кандидатом ТОЛЬКО Армении."""
    db_path = _setup_db(tmp_path)
    ge, _am = _create_campaigns(db_path)
    am = _campaign(db_path, "am_insurance_sadakhlo")
    repo = InviteCampaignRepository(db_path)
    try:
        repo.update(ge.id, keyword="страхов")
    finally:
        repo.close()
    _seed_user(db_path, 101, username="u101", keywords=["страхов"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 102, username=None, keywords=["страхов"], access_hash=None, last_seen_at=_BASE_TIME)

    def row(user_id):
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT username, access_hash, last_seen_at, keywords, is_bot FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()

    def candidates(campaign_id):
        invites = UserCampaignInviteRepository(db_path)
        try:
            return {c.user_id: (c.username, c.access_hash) for c in invites.select_candidates(campaign_id, limit=50)}
        finally:
            invites.close()

    before = {uid: row(uid) for uid in (101, 102)}
    georgia_before = candidates(ge.id)
    _import(db_path, am, _FakeHistoryClient(_sadahlo_history()))

    assert {uid: row(uid) for uid in (101, 102)} == before          # ни одного изменения
    assert candidates(ge.id) == georgia_before == {101: ("u101", 1)}  # 102 НЕ стал кандидатом Грузии
    am_candidates = candidates(am.id)
    assert am_candidates[101] == ("u101", 1)                        # users приоритетнее
    assert am_candidates[102] == ("u102", 1020)                     # из пула
    assert row(103) is not None and row(103)[3] is None             # отсутствовавший — создан без keywords
    leads = CampaignLeadRepository(db_path)
    try:
        lead = leads._conn.execute(
            "SELECT username, access_hash, display_name FROM campaign_leads WHERE campaign_id = ? AND user_id = 102",
            (am.id,),
        ).fetchone()
    finally:
        leads.close()
    assert lead == ("u102", 1020, "Имя102")


def test_pool_candidates_ordered_by_most_recent_insurance_message(tmp_path):
    db_path = _setup_db(tmp_path)
    _create_campaigns(db_path)
    am = _am_with_rule(db_path)
    now = datetime.now(timezone.utc)
    _import(db_path, am, _FakeHistoryClient(_quality_history(now)))
    invites = UserCampaignInviteRepository(db_path)
    try:
        assert [c.user_id for c in invites.select_candidates(am.id, limit=10)] == [403, 402, 401]
    finally:
        invites.close()


def test_legacy_campaign_lead_table_gets_identity_columns(tmp_path):
    db_path = tmp_path / "users.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE campaign_leads (campaign_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
        "source_chat_id INTEGER NOT NULL, source_ref TEXT, matched_keyword TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'new', match_count INTEGER NOT NULL DEFAULT 0, "
        "first_message_id INTEGER, first_message_at TIMESTAMP, last_message_id INTEGER, "
        "last_message_at TIMESTAMP, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (campaign_id, user_id))"
    )
    conn.commit()
    conn.close()
    CampaignLeadRepository(db_path).close()
    CampaignLeadRepository(db_path).close()
    conn = sqlite3.connect(db_path)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(campaign_leads)")}
    finally:
        conn.close()
    assert {"username", "access_hash", "display_name"} <= columns


def test_campaign_card_shows_recency_when_configured(tmp_path):
    bot = _Bot(tmp_path)
    try:
        manage.ensure_campaign(
            bot.db_path, slug="am_insurance_sadakhlo", name="Армения — Садахло", keyword="страх",
            target_chat="@osagoarmen", lead_max_age_days=180,
        )
        am_card = bot.controller.handle_campaign_open(bot.am.id, telegram_user_id=_TRUSTED_ID).text
        ge_card = bot.controller.handle_campaign_open(bot.ge.id, telegram_user_id=_TRUSTED_ID).text
        stats_text = bot.controller.handle_campaign_stats(bot.am.id, telegram_user_id=_TRUSTED_ID).text
    finally:
        bot.close()
    assert "Давность лидов: последнее сообщение не старше 180 дн." in am_card
    assert "Давность лидов" in stats_text
    assert "Давность лидов" not in ge_card
