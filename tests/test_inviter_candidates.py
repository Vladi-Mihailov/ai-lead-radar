"""
Тесты отбора кандидатов на приглашение (reader.inviter, этап 2 — только
выборка, без Telethon и без записи в user_campaign_invites):
UserCampaignInviteRepository.select_candidates()/count_candidates() и
InviterService.run().
"""

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from telethon.errors import (  # noqa: E402
    ChatAdminRequiredError,
    FloodWaitError,
    PeerFloodError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.messages import AddChatUserRequest, GetHistoryRequest  # noqa: E402

from reader.inviter.repository import (  # noqa: E402
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)
from reader.inviter.service import InviterService, _format_duration  # noqa: E402
from reader.users.models import TelegramUserInfo  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """_pause_between_invites() реально ждёт 20-60 сек. после КАЖДОГО
    приглашения — без этой подмены большинство тестов execute=True стали бы
    занимать минуты. Тесты, которым нужно проверить сам факт/длительность
    сна (FloodWait, случайная пауза), переопределяют asyncio.sleep у себя в
    теле — это просто перекрывает подмену этого фикстура на время теста."""
    import reader.inviter.service as service_module

    async def instant_sleep(seconds):
        pass

    monkeypatch.setattr(service_module.asyncio, "sleep", instant_sleep)


def _seed_user(
    db_path: Path,
    user_id: int,
    *,
    username: str | None = None,
    keywords: list[str] | None = None,
    access_hash: int | None = None,
    last_seen_at: datetime | None = None,
) -> None:
    """Пишет пользователя напрямую в users, минуя UserRepository.upsert()
    (который всегда ставит last_seen_at=CURRENT_TIMESTAMP) — тестам нужен
    полный контроль над last_seen_at, чтобы детерминированно проверить
    сортировку."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (user_id, username, keywords, access_hash, last_seen_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                user_id,
                username,
                ", ".join(keywords) if keywords else None,
                access_hash,
                last_seen_at.isoformat() if last_seen_at else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _setup_db(tmp_path) -> Path:
    db_path = tmp_path / "inviter.db"
    # Создаёт/мигрирует таблицу users — до этого её в файле нет.
    UserRepository(db_path).close()
    return db_path


class _FakeTelegramClient:
    """Ровно тот минимум, что использует InviterService — connect()/
    get_entity()/disconnect() (dry-run и execute) и __call__() (только
    execute — сама отправка запроса). Никаких других мутирующих методов
    (ImportChatInviteRequest и т.п.) здесь нет вовсе — случайный вызов
    такого метода упал бы AttributeError и завалил тест."""

    def __init__(
        self, account, *, connect_error=None, get_entity_error=None,
        target_entity=None, call_errors=None,
    ):
        self.account = account
        self._connect_error = connect_error
        self._get_entity_error = get_entity_error
        self._target_entity = target_entity if target_entity is not None else SimpleNamespace(id=999)
        # Список ошибок (или None = успех) — по одной на очередной вызов
        # __call__(), в порядке кандидатов этого аккаунта.
        self._call_errors = list(call_errors) if call_errors is not None else []
        self.connected = False
        self.disconnected = False
        self.get_entity_calls: list = []
        self.call_requests: list = []

    async def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    async def get_entity(self, entity):
        self.get_entity_calls.append(entity)
        if self._get_entity_error is not None:
            raise self._get_entity_error
        return self._target_entity

    async def __call__(self, request):
        self.call_requests.append(request)
        error = self._call_errors.pop(0) if self._call_errors else None
        if error is not None:
            raise error
        return object()

    async def disconnect(self) -> None:
        self.disconnected = True


def _make_client_factory(
    *, connect_errors=None, get_entity_errors=None, call_errors=None, created=None,
):
    """connect_errors/get_entity_errors/call_errors — {account.name: ...} для
    аккаунтов, которым нужно смоделировать сбой (call_errors — список ошибок,
    по одной на кандидата, см. _FakeTelegramClient). created — список, в
    который складываются все созданные фейковые клиенты (по одному на
    аккаунт), чтобы тест мог проверить connected/disconnected после run()."""
    connect_errors = connect_errors or {}
    get_entity_errors = get_entity_errors or {}
    call_errors = call_errors or {}

    def factory(account):
        client = _FakeTelegramClient(
            account,
            connect_error=connect_errors.get(account.name),
            get_entity_error=get_entity_errors.get(account.name),
            call_errors=call_errors.get(account.name),
        )
        if created is not None:
            created.append(client)
        return client

    return factory


class _FakeOperatorNotifier:
    """Ровно то, что нужно InviterService от OperatorNotifier —
    notify_text(text) -> bool. raise_error, если задан, имитирует сбой
    отправки (сервис не должен из-за этого падать, см.
    test_notify_failure_does_not_stop_service)."""

    def __init__(self, *, raise_error=None):
        self.sent: list[str] = []
        self._raise_error = raise_error

    async def notify_text(self, text: str) -> bool:
        self.sent.append(text)
        if self._raise_error is not None:
            raise self._raise_error
        return True


async def _run_service(
    db_path: Path, client_factory=None, execute: bool = False, notifier=None,
    session_checker=None,
) -> None:
    account_repository = TelegramAccountRepository(db_path)
    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        service = InviterService(
            account_repository, campaign_repository, invite_repository,
            client_factory=client_factory or _make_client_factory(),
            notifier=notifier,
            # По умолчанию — "сессия всегда есть": тестовые аккаунты
            # используют фиктивные session_path (например "acc1.session"),
            # реального файла на диске для них нет и не должно требоваться,
            # кроме тестов, которые ЯВНО проверяют поведение при её
            # отсутствии (см. test_missing_session_*).
            session_checker=session_checker or (lambda account: True),
        )
        await service.run(execute=execute)
    finally:
        account_repository.close()
        campaign_repository.close()
        invite_repository.close()


# ---- UserCampaignInviteRepository.select_candidates()/count_candidates() ----


def test_select_candidates_sorted_by_last_seen_at_desc(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=2))
    _seed_user(db_path, 3, keywords=["осаго"], access_hash=3, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")

        candidates = invite_repository.select_candidates(campaign.id, limit=10)
        assert [c.user_id for c in candidates] == [2, 3, 1]
    finally:
        campaign_repository.close()
        invite_repository.close()


def test_select_candidates_filters_by_campaign_keyword(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["каско"], access_hash=2, last_seen_at=_BASE_TIME)
    # Похожий, но не точный токен — не должен ложно совпасть с "каско".
    _seed_user(db_path, 3, keywords=["ко"], access_hash=3, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 4, keywords=["осаго", "каско"], access_hash=4, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        campaign = campaign_repository.create(name="Каско", keyword="каско", target_chat="@t")

        candidates = invite_repository.select_candidates(campaign.id, limit=10)
        assert {c.user_id for c in candidates} == {2, 4}
    finally:
        campaign_repository.close()
        invite_repository.close()


def test_select_candidates_excludes_users_without_access_hash(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=None, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=99, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")

        candidates = invite_repository.select_candidates(campaign.id, limit=10)
        assert [c.user_id for c in candidates] == [2]
    finally:
        campaign_repository.close()
        invite_repository.close()


def test_select_candidates_excludes_already_invited_for_this_campaign(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        campaign_a = campaign_repository.create(name="A", keyword="осаго", target_chat="@a")
        campaign_b = campaign_repository.create(name="B", keyword="осаго", target_chat="@b")

        invite_repository.create(user_id=1, campaign_id=campaign_a.id, status="invited")
        # Тот же пользователь, но для ДРУГОЙ кампании — не должен блокировать
        # его выбор здесь: "уже приглашён" ограничено конкретной кампанией.
        invite_repository.create(user_id=2, campaign_id=campaign_b.id, status="invited")

        candidates_a = invite_repository.select_candidates(campaign_a.id, limit=10)
        assert [c.user_id for c in candidates_a] == [2]

        candidates_b = invite_repository.select_candidates(campaign_b.id, limit=10)
        assert [c.user_id for c in candidates_b] == [1]
    finally:
        campaign_repository.close()
        invite_repository.close()


def test_select_candidates_non_invited_status_does_not_exclude(tmp_path):
    """Записи со статусом, отличным от 'invited' (например 'failed' или
    'pending'), не должны исключать пользователя — условие именно про
    status='invited'."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        campaign = campaign_repository.create(name="A", keyword="осаго", target_chat="@a")
        invite_repository.create(user_id=1, campaign_id=campaign.id, status="failed")

        candidates = invite_repository.select_candidates(campaign.id, limit=10)
        assert [c.user_id for c in candidates] == [1]
    finally:
        campaign_repository.close()
        invite_repository.close()


def test_select_candidates_respects_limit(tmp_path):
    db_path = _setup_db(tmp_path)
    for user_id in range(1, 6):
        _seed_user(
            db_path, user_id, keywords=["осаго"], access_hash=user_id,
            last_seen_at=_BASE_TIME + timedelta(days=user_id),
        )

    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")

        candidates = invite_repository.select_candidates(campaign.id, limit=2)
        # limit=2 — не больше двух, и именно с самым свежим last_seen_at.
        assert [c.user_id for c in candidates] == [5, 4]

        assert invite_repository.count_candidates(campaign.id) == 5
    finally:
        campaign_repository.close()
        invite_repository.close()


# ---- миграция users (access_hash/keywords) при первом же открытии инвайтера ----


def test_user_campaign_invite_repository_migrates_legacy_users_table_without_access_hash(tmp_path):
    """Реальный баг: users создана СТАРОЙ версией схемы (до keywords/
    access_hash), а UserRepository (владелец её схемы/миграций) в этом
    окружении ещё не открывался ни sync_users.py, ни main.py —
    UserCampaignInviteRepository.select_candidates()/count_candidates()
    падали с sqlite3.OperationalError: no such column: u.access_hash.
    Открытие UserCampaignInviteRepository теперь мигрирует users само,
    без ручных правок SQLite — как и для остальных таблиц проекта."""
    db_path = tmp_path / "users.db"

    # Схема users до появления keywords/access_hash — как в самом первом
    # легаси-фикстуре test_user_repository.py
    # (test_migration_adds_keywords_column_to_legacy_database).
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_bot INTEGER,
            last_seen_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO users (user_id, username, last_seen_at) VALUES "
        "(555, 'ivan', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    campaign_repository = InviteCampaignRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")
    finally:
        campaign_repository.close()

    # Открываем UserCampaignInviteRepository НАПРЯМУЮ, минуя UserRepository —
    # именно так это происходит в реальном сервисе (см.
    # reader/inviter/service.py), и именно это раньше приводило к
    # "no such column: u.access_hash".
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        # Не падает — миграция уже выполнена при открытии репозитория.
        assert invite_repository.count_candidates(campaign.id) == 0
        assert invite_repository.select_candidates(campaign.id, limit=10) == []

        conn = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        finally:
            conn.close()
        # Как минимум эти две колонки нужны select_candidates() — обе
        # добавляются той же миграцией (_migrate_missing_columns), что и
        # peer_type/peer_updated_at, поэтому отдельно их не проверяем.
        assert {"access_hash", "keywords"} <= columns

        # Сквозная проверка: после миграции обычный путь (UserRepository)
        # тоже видит ту же таблицу и реальная выборка отрабатывает целиком.
        user_repository = UserRepository(db_path)
        try:
            user_repository.upsert(
                TelegramUserInfo(
                    user_id=555, username="ivan", first_name=None, last_name=None,
                    access_hash=42,
                )
            )
            user_repository.add_keywords(555, ["осаго"])
        finally:
            user_repository.close()

        candidates = invite_repository.select_candidates(campaign.id, limit=10)
        assert [c.user_id for c in candidates] == [555]
    finally:
        invite_repository.close()


# ---- InviterService.run() — только выборка, никаких записей/Telegram ----


def test_service_run_selects_up_to_daily_limit_per_account(tmp_path):
    db_path = _setup_db(tmp_path)
    for user_id in range(1, 4):
        _seed_user(
            db_path, user_id, keywords=["осаго"], access_hash=user_id,
            last_seen_at=_BASE_TIME + timedelta(days=user_id),
        )

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")
        account_repository.create(
            name="acc1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=2,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    asyncio.run(_run_service(db_path))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        # "Только выборка" — run() не создаёт ни одной записи о приглашении.
        assert invite_repository.list() == []
    finally:
        invite_repository.close()


def test_service_run_ignores_disabled_campaigns_and_accounts(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        disabled_campaign = campaign_repository.create(
            name="Отключена", keyword="осаго", target_chat="@t", enabled=False,
        )
        campaign_repository.create(
            name="Включена", keyword="осаго", target_chat="@t2", enabled=True,
        )
        account_repository.create(
            name="acc-disabled", phone="+995500000002", session_name="acc2",
            session_path="acc2.session", enabled=False,
        )
        assert disabled_campaign.enabled is False
    finally:
        campaign_repository.close()
        account_repository.close()

    # Нет ни одного enabled-аккаунта — run() должен пройти без ошибок и без
    # единой записи, несмотря на подходящего кандидата и активную кампанию.
    asyncio.run(_run_service(db_path))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert invite_repository.list() == []
    finally:
        invite_repository.close()


def _user_ids_in_block(block: str) -> list[int]:
    return [int(line.split()[0]) for line in block.splitlines() if line[:1].isdigit()]


def test_service_run_distributes_candidates_sequentially_without_overlap(tmp_path, monkeypatch):
    """Несколько аккаунтов с разным daily_limit: total_limit = SUM(daily_limit)
    выбирается ОДНИМ вызовом select_candidates(), затем делится между
    аккаунтами последовательно (Account1 — первые 3, Account2 — следующие 3,
    Account3 — оставшиеся 5) — без пересечений между аккаунтами."""
    db_path = _setup_db(tmp_path)
    total_users = 11
    for user_id in range(1, total_users + 1):
        _seed_user(
            db_path, user_id, keywords=["осаго"], access_hash=user_id,
            last_seen_at=_BASE_TIME + timedelta(days=user_id),
        )

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")
        account_repository.create(
            name="Account1", phone="+995500000001", session_name="a1",
            session_path="a1.session", daily_limit=3,
        )
        account_repository.create(
            name="Account2", phone="+995500000002", session_name="a2",
            session_path="a2.session", daily_limit=3,
        )
        account_repository.create(
            name="Account3", phone="+995500000003", session_name="a3",
            session_path="a3.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    import reader.inviter.service as service_module

    all_logs: list[str] = []
    monkeypatch.setattr(service_module.logger, "info", all_logs.append)

    asyncio.run(_run_service(db_path))

    # Кроме сводки "Campaign: ..." (по одной на аккаунт) run() теперь также
    # логирует блоки "[DRY RUN]" на каждого кандидата — здесь интересует
    # только распределение, поэтому отфильтровываем сводки.
    logged_blocks = [b for b in all_logs if b.startswith("Campaign:")]
    assert len(logged_blocks) == 3  # один лог-блок на аккаунт

    ids1 = _user_ids_in_block(logged_blocks[0])
    ids2 = _user_ids_in_block(logged_blocks[1])
    ids3 = _user_ids_in_block(logged_blocks[2])

    # last_seen_at DESC — самые "свежие" (наибольший user_id) идут первыми.
    assert ids1 == [11, 10, 9]
    assert ids2 == [8, 7, 6]
    assert ids3 == [5, 4, 3, 2, 1]

    assert not set(ids1) & set(ids2)
    assert not set(ids1) & set(ids3)
    assert not set(ids2) & set(ids3)

    for block in logged_blocks:
        assert "найдено кандидатов: 11" in block


def test_service_run_handles_insufficient_candidates_without_overlap(tmp_path, monkeypatch):
    """Кандидатов меньше, чем суммарный daily_limit всех аккаунтов —
    последним аккаунтам должно достаться меньше или совсем ничего, без
    ошибок и без дублей с предыдущими аккаунтами."""
    db_path = _setup_db(tmp_path)
    total_users = 4
    for user_id in range(1, total_users + 1):
        _seed_user(
            db_path, user_id, keywords=["осаго"], access_hash=user_id,
            last_seen_at=_BASE_TIME + timedelta(days=user_id),
        )

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")
        # daily_limit 3 + 3 + 2 = 8, кандидатов всего 4.
        account_repository.create(
            name="Account1", phone="+995500000001", session_name="a1",
            session_path="a1.session", daily_limit=3,
        )
        account_repository.create(
            name="Account2", phone="+995500000002", session_name="a2",
            session_path="a2.session", daily_limit=3,
        )
        account_repository.create(
            name="Account3", phone="+995500000003", session_name="a3",
            session_path="a3.session", daily_limit=2,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    import reader.inviter.service as service_module

    all_logs: list[str] = []
    monkeypatch.setattr(service_module.logger, "info", all_logs.append)

    asyncio.run(_run_service(db_path))

    logged_blocks = [b for b in all_logs if b.startswith("Campaign:")]
    assert len(logged_blocks) == 3

    ids1 = _user_ids_in_block(logged_blocks[0])
    ids2 = _user_ids_in_block(logged_blocks[1])
    ids3 = _user_ids_in_block(logged_blocks[2])

    assert ids1 == [4, 3, 2]
    assert ids2 == [1]
    assert ids3 == []  # кандидаты закончились — третий аккаунт не дублирует

    assert not set(ids1) & set(ids2)

    assert "выбрано: 3" in logged_blocks[0]
    assert "выбрано: 1" in logged_blocks[1]
    assert "выбрано: 0" in logged_blocks[2]
    for block in logged_blocks:
        assert "найдено кандидатов: 4" in block


def test_service_run_logs_candidates_block(tmp_path, caplog):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 555, username="ivan", keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО осень", keyword="осаго", target_chat="@t")
        account_repository.create(
            name="Основной", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    with caplog.at_level("INFO", logger="reader.inviter.service"):
        asyncio.run(_run_service(db_path))

    log_text = caplog.text
    assert "Campaign: ОСАГО осень" in log_text
    assert "Account: Основной" in log_text
    assert "555 @ivan" in log_text
    assert "keywords: осаго" in log_text
    assert "найдено кандидатов: 1" in log_text
    assert "выбрано: 1" in log_text


# ---- Telethon dry-run (подключение/резолв target_chat/InputPeerUser) ----


def _dry_run_blocks(all_logs: list[str]) -> list[str]:
    # "User:" отличает per-candidate блок от account-уровневого лога о сбое
    # подключения (у него тоже префикс "[DRY RUN]", но кандидата ещё нет).
    return [b for b in all_logs if b.startswith("[DRY RUN]") and "User:" in b]


def test_dry_run_logs_ready_for_each_candidate_and_disconnects(tmp_path, monkeypatch):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="ivan", keywords=["осаго"], access_hash=11, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, username="petr", keywords=["осаго"], access_hash=22, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="Основной", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    import reader.inviter.service as service_module

    all_logs: list[str] = []
    monkeypatch.setattr(service_module.logger, "info", all_logs.append)
    monkeypatch.setattr(service_module.logger, "warning", all_logs.append)

    asyncio.run(_run_service(db_path, client_factory=client_factory))

    dry_run_blocks = _dry_run_blocks(all_logs)
    assert len(dry_run_blocks) == 2  # один блок на кандидата

    for block in dry_run_blocks:
        assert "Account: Основной" in block
        assert "Target: @target_chat" in block
        assert block.strip().endswith("READY")

    assert any("User: 2 @petr" in block for block in dry_run_blocks)
    assert any("User: 1 @ivan" in block for block in dry_run_blocks)

    # Ни одной записи о приглашении — это всё ещё только подготовка (dry run).
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert invite_repository.list() == []
    finally:
        invite_repository.close()

    assert len(created_clients) == 1
    client = created_clients[0]
    assert client.connected is True
    assert client.disconnected is True
    assert client.get_entity_calls == ["@target_chat"]  # резолв ОДИН раз на аккаунт, не на кандидата


def test_dry_run_logs_failed_when_target_chat_not_found(tmp_path, monkeypatch):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="ivan", keywords=["осаго"], access_hash=11, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, username="petr", keywords=["осаго"], access_hash=22, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@missing_chat")
        account_repository.create(
            name="Основной", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(
        get_entity_errors={"Основной": ValueError("no such chat")}, created=created_clients,
    )

    import reader.inviter.service as service_module

    all_logs: list[str] = []
    monkeypatch.setattr(service_module.logger, "info", all_logs.append)
    monkeypatch.setattr(service_module.logger, "warning", all_logs.append)

    asyncio.run(_run_service(db_path, client_factory=client_factory))

    dry_run_blocks = _dry_run_blocks(all_logs)
    # target_chat не резолвится — оба кандидата получают FAILED, а не READY,
    # и никакого InviteToChannelRequest/AddChatUserRequest не вызывается
    # (у _FakeTelegramClient таких методов вообще нет).
    assert len(dry_run_blocks) == 2
    for block in dry_run_blocks:
        assert "FAILED" in block
        assert "no such chat" in block
        assert "READY" not in block

    client = created_clients[0]
    assert client.connected is True
    assert client.disconnected is True  # отключение обязательно и после сбоя


def test_dry_run_continues_with_other_accounts_when_one_fails_to_connect(tmp_path, monkeypatch):
    db_path = _setup_db(tmp_path)
    # 6 кандидатов — "Плохому" (daily_limit=5) достаётся 5, "Хорошему"
    # достаётся оставшийся 1 (последовательное деление, см. InviterService.
    # run()) — независимо от того, что "Плохой" не подключится.
    for user_id in range(1, 7):
        _seed_user(
            db_path, user_id, username=f"user{user_id}", keywords=["осаго"],
            access_hash=user_id, last_seen_at=_BASE_TIME + timedelta(days=user_id),
        )

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="Плохой", phone="+995500000001", session_name="bad",
            session_path="bad.session", daily_limit=5,
        )
        account_repository.create(
            name="Хороший", phone="+995500000002", session_name="good",
            session_path="good.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(
        connect_errors={"Плохой": ConnectionError("сессия недействительна")},
        created=created_clients,
    )

    import reader.inviter.service as service_module

    all_logs: list[str] = []
    monkeypatch.setattr(service_module.logger, "info", all_logs.append)
    monkeypatch.setattr(service_module.logger, "warning", all_logs.append)

    asyncio.run(_run_service(db_path, client_factory=client_factory))

    # "Плохой" не подключился — ни одного per-candidate dry-run блока для
    # него, но это не должно мешать "Хорошему" получить свой READY.
    dry_run_blocks = _dry_run_blocks(all_logs)
    assert len(dry_run_blocks) == 1
    assert "Account: Хороший" in dry_run_blocks[0]
    assert "READY" in dry_run_blocks[0]

    assert any(
        "Плохой" in entry and "сессия недействительна" in entry for entry in all_logs
    )

    bad_client, good_client = created_clients
    assert bad_client.connected is False
    assert bad_client.disconnected is True  # disconnect() вызван даже после сбоя connect()
    assert good_client.connected is True
    assert good_client.disconnected is True


# ---- execute=True — реальные приглашения ----


def _setup_single_candidate_campaign(db_path, *, daily_limit=5):
    _seed_user(db_path, 1, username="ivan", keywords=["осаго"], access_hash=11, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account = account_repository.create(
            name="Основной", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=daily_limit,
        )
        return campaign, account
    finally:
        campaign_repository.close()
        account_repository.close()


def test_execute_creates_invited_record_on_success(tmp_path):
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        assert len(invites) == 1
        invite = invites[0]
        assert invite.user_id == 1
        assert invite.campaign_id == campaign.id
        assert invite.account_id == account.id
        assert invite.status == "invited"
        assert invite.error is None
        assert invite.invited_at is not None
    finally:
        invite_repository.close()

    client = created_clients[0]
    assert client.connected is True
    assert client.disconnected is True
    assert len(client.call_requests) == 1
    # target_entity — не Channel (см. _make_client_factory/_FakeTelegramClient
    # по умолчанию), поэтому построен AddChatUserRequest, а не
    # InviteToChannelRequest — ровно один Telegram-мутирующий вызов.
    assert isinstance(client.call_requests[0], AddChatUserRequest)


def test_execute_creates_failed_record_on_rpc_error(tmp_path):
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    # ChatAdminRequiredError — просто пример "любой другой RPCError";
    # PeerFloodError теперь ведёт себя иначе (останавливает аккаунт, см.
    # test_execute_peer_flood_stops_account_and_notifies_operator).
    client_factory = _make_client_factory(
        call_errors={"Основной": [ChatAdminRequiredError(request=GetHistoryRequest)]},
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        assert len(invites) == 1
        invite = invites[0]
        assert invite.user_id == 1
        assert invite.campaign_id == campaign.id
        assert invite.account_id == account.id
        assert invite.status == "failed"
        assert invite.error  # непустая причина
        assert "admin" in invite.error.lower()
        assert invite.invited_at is None
    finally:
        invite_repository.close()


def test_execute_flood_wait_records_failed_sleeps_and_continues(tmp_path, monkeypatch):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="u1", keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, username="u2", keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account = account_repository.create(
            name="Основной", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    # Первый (по last_seen_at DESC) кандидат — user 2 — получает FloodWait,
    # второй — user 1 — должен всё равно быть обработан (сервис не падает).
    client_factory = _make_client_factory(
        call_errors={"Основной": [FloodWaitError(request=GetHistoryRequest, capture=7), None]},
    )

    import reader.inviter.service as service_module

    monkeypatch.setattr(service_module.random, "random", lambda: 0.9)  # форсируем короткую паузу

    sleep_calls: list = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    # sleep_calls[0] — "разогрев" аккаунта сразу после connect() (см.
    # test_warm_up_account_...), [1] — ожидание самого FloodWait (7 сек., как
    # и раньше), [2] — случайная пауза после ВТОРОГО (успешного) кандидата
    # (см. _pause_between_invites) — в диапазоне [20, 60).
    assert len(sleep_calls) == 3
    assert 5 <= sleep_calls[0] < 15
    assert sleep_calls[1] == 7
    assert 20 <= sleep_calls[2] < 60

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = {i.user_id: i for i in invite_repository.list()}
        assert invites[2].status == "failed"
        assert "FloodWaitError" in invites[2].error
        assert invites[2].invited_at is None

        assert invites[1].status == "invited"
        assert invites[1].invited_at is not None
    finally:
        invite_repository.close()


def test_execute_user_already_participant_marks_invited(tmp_path):
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    client_factory = _make_client_factory(
        call_errors={"Основной": [UserAlreadyParticipantError(request=GetHistoryRequest)]},
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        assert len(invites) == 1
        # UserAlreadyParticipantError — цель кампании для пользователя уже
        # достигнута: status='invited', а не 'failed' (см. требование задачи).
        assert invites[0].status == "invited"
        assert invites[0].user_id == 1
        assert invites[0].campaign_id == campaign.id
    finally:
        invite_repository.close()


def test_execute_does_not_reinvite_user_on_next_run(tmp_path):
    """После успешного приглашения (status='invited') повторный запуск —
    даже другим аккаунтом/клиентом — не должен снова выбрать и пригласить
    того же пользователя для этой же кампании."""
    db_path = _setup_db(tmp_path)
    _setup_single_candidate_campaign(db_path)

    first_created: list = []
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(created=first_created), execute=True))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert len(invite_repository.list()) == 1
    finally:
        invite_repository.close()

    second_created: list = []
    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(created=second_created), execute=True))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        # Ни одной новой записи — пользователь больше не выбирается
        # select_candidates() для этой кампании (status='invited' уже есть).
        assert len(invite_repository.list()) == 1
    finally:
        invite_repository.close()

    # Второй прогон подключился (кандидатов на распределение не было бы,
    # но аккаунт всё равно enabled) — при отсутствии кандидатов
    # _execute_account() не должен даже создавать клиента.
    assert second_created == []


# ---- уведомления оператору о ходе приглашений (execute=True) ----


def test_execute_sends_account_and_campaign_notifications_with_correct_counts(tmp_path):
    """3 кандидата: успешный invite, UserAlreadyParticipant и обычная
    RPCError — по одному аккаунту на кампанию. Проверяет, что после
    аккаунта отправляется его статистика, а после кампании — итоговая, и
    что счётчики (включая "осталось кандидатов") верны."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="u1", keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME + timedelta(days=3))
    _seed_user(db_path, 2, username="u2", keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=2))
    _seed_user(db_path, 3, username="u3", keywords=["осаго"], access_hash=3, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="account_1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    # По last_seen_at DESC: 1 (успех), 2 (уже участник), 3 (обычная ошибка —
    # не PeerFlood/FloodWait, у которых теперь особое поведение остановки
    # аккаунта, см. test_execute_peer_flood_stops_account_and_notifies_operator).
    client_factory = _make_client_factory(
        call_errors={"account_1": [None, UserAlreadyParticipantError(request=GetHistoryRequest), ChatAdminRequiredError(request=GetHistoryRequest)]},
    )
    notifier = _FakeOperatorNotifier()

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True, notifier=notifier))

    # Ровно два уведомления: по аккаунту и итоговое по кампании, в этом порядке.
    assert len(notifier.sent) == 2
    account_message, campaign_message = notifier.sent

    assert "📨 Кампания: ОСАГО" in account_message
    assert "👤 Аккаунт: account_1" in account_message
    assert "✅ Приглашено: 1" in account_message
    assert "☑️ Уже состояли в группе: 1" in account_message
    assert "🚫 Недоступны (invalid): 0" in account_message
    assert "❌ Ошибок: 1" in account_message
    # 3-й кандидат получил RPCError (status='failed') — не исключается из
    # будущей выборки, поэтому остаётся кандидатом.
    assert "Осталось кандидатов: 1" in account_message

    assert '📊 Итоги кампании "ОСАГО"' in campaign_message
    assert "Аккаунтов обработано: 1" in campaign_message
    assert "✅ Приглашено: 1" in campaign_message
    assert "☑️ Уже состояли: 1" in campaign_message
    assert "🚫 Недоступны: 0" in campaign_message
    assert "❌ Ошибок: 1" in campaign_message
    assert "Осталось кандидатов: 1" in campaign_message


def test_execute_flood_wait_excluded_from_error_count_in_notification(tmp_path, monkeypatch):
    """FloodWaitError не должен увеличивать счётчик "Ошибок" в
    уведомлении — это общее временное ограничение API, а не отказ
    конкретному пользователю (см. InviteStats)."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME + timedelta(days=1))
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="account_1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    client_factory = _make_client_factory(
        call_errors={"account_1": [FloodWaitError(request=GetHistoryRequest, capture=3), None]},
    )
    notifier = _FakeOperatorNotifier()

    import reader.inviter.service as service_module

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True, notifier=notifier))

    account_message = notifier.sent[0]
    assert "✅ Приглашено: 1" in account_message
    assert "❌ Ошибок: 0" in account_message  # FloodWait не считается ошибкой


def test_execute_aggregates_stats_across_multiple_accounts_in_campaign_summary(tmp_path):
    """Итоговая сводка по кампании должна суммировать статистику всех
    обработанных аккаунтов, а не только последнего."""
    db_path = _setup_db(tmp_path)
    for user_id in range(1, 5):
        _seed_user(
            db_path, user_id, keywords=["осаго"], access_hash=user_id,
            last_seen_at=_BASE_TIME + timedelta(days=user_id),
        )

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="account_1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=2,
        )
        account_repository.create(
            name="account_2", phone="+995500000002", session_name="acc2",
            session_path="acc2.session", daily_limit=2,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    # account_1 (кандидаты 4,3 по last_seen_at DESC): оба успешны.
    # account_2 (кандидаты 2,1): один RPCError, один успешен.
    client_factory = _make_client_factory(
        call_errors={
            "account_1": [None, None],
            "account_2": [ChatAdminRequiredError(request=GetHistoryRequest), None],
        },
    )
    notifier = _FakeOperatorNotifier()

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True, notifier=notifier))

    # 2 уведомления по аккаунтам + 1 итоговое по кампании.
    assert len(notifier.sent) == 3
    campaign_message = notifier.sent[-1]

    assert "Аккаунтов обработано: 2" in campaign_message
    assert "✅ Приглашено: 3" in campaign_message  # 2 (account_1) + 1 (account_2)
    assert "☑️ Уже состояли: 0" in campaign_message
    assert "❌ Ошибок: 1" in campaign_message
    # Кандидат с RPCError (status='failed') не исключается из будущей
    # выборки — остаётся ровно один "оставшийся" кандидат.
    assert "Осталось кандидатов: 1" in campaign_message


def test_notify_failure_does_not_stop_service(tmp_path):
    """Сбой отправки уведомления оператору не должен мешать самим
    приглашениям — запись в user_campaign_invites всё равно создаётся."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    notifier = _FakeOperatorNotifier(raise_error=RuntimeError("сеть недоступна"))

    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(), execute=True, notifier=notifier))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        assert len(invites) == 1
        assert invites[0].status == "invited"
    finally:
        invite_repository.close()

    # Уведомление было ПОПЫТАНО (иначе сбой было бы неоткуда взять).
    assert len(notifier.sent) >= 1


def test_execute_without_notifier_does_not_raise(tmp_path):
    """notifier=None (по умолчанию) — обычный случай для существующих
    тестов/вызовов без операторских уведомлений — не должен приводить к
    ошибке."""
    db_path = _setup_db(tmp_path)
    _setup_single_candidate_campaign(db_path)

    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(), execute=True))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert len(invite_repository.list()) == 1
    finally:
        invite_repository.close()


def test_dry_run_does_not_send_operator_notifications(tmp_path):
    """Уведомления оператору — только для execute=True; dry-run не должен
    отправлять ничего оператору (логи READY/FAILED остаются как есть)."""
    db_path = _setup_db(tmp_path)
    _setup_single_candidate_campaign(db_path)

    notifier = _FakeOperatorNotifier()

    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(), execute=False, notifier=notifier))

    assert notifier.sent == []


class _FakeTimeModule:
    """Подставляется вместо имени `time` в ГЛОБАЛЬНОМ пространстве имён
    модуля reader.inviter.service (см. _patch_monotonic_sequence) — не
    трогает настоящий модуль time.monotonic(), которым продолжает
    пользоваться сам asyncio (event loop дёргает time.monotonic() для
    своих внутренних нужд, и подмена НАСТОЯЩЕГО time.monotonic() съедала
    бы значения из заданной последовательности раньше кода сервиса)."""

    def __init__(self, values: list[float]):
        self._values = list(values)
        self._index = 0

    def monotonic(self) -> float:
        value = self._values[self._index]
        if self._index < len(self._values) - 1:
            self._index += 1
        return value


def _patch_monotonic_sequence(monkeypatch, service_module, values: list[float]) -> None:
    """Отдаёт time.monotonic() ровно len(values) заданных значений по
    порядку (последнее — повторяется, если понадобится больше вызовов)."""
    monkeypatch.setattr(service_module, "time", _FakeTimeModule(values))


# ---- время выполнения в уведомлениях (time.monotonic(), не datetime.now()) ----


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "00:00:00"),
        (5, "00:00:05"),
        (59, "00:00:59"),
        (60, "00:01:00"),
        (108, "00:01:48"),  # пример из задачи — отчёт по аккаунту
        (397, "00:06:37"),  # пример из задачи — итоги кампании
        (3599, "00:59:59"),
        (3600, "01:00:00"),
        (3661, "01:01:01"),
        (108.9, "00:01:48"),  # дробная часть отбрасывается, не округляется
    ],
)
def test_format_duration_formats_seconds_as_hh_mm_ss(seconds, expected):
    assert _format_duration(seconds) == expected


def test_notifications_include_elapsed_time_without_changing_other_lines(tmp_path, monkeypatch):
    """Время выполнения — ДОБАВЛЕННАЯ строка: остальные строки отчёта по
    аккаунту и по кампании должны остаться такими же, как до этой задачи."""
    db_path = _setup_db(tmp_path)
    _setup_single_candidate_campaign(db_path)

    notifier = _FakeOperatorNotifier()

    import reader.inviter.service as service_module

    # time.monotonic() вызывается 4 раза за этот прогон (1 кампания,
    # 1 аккаунт): старт кампании, старт аккаунта, конец аккаунта, конец
    # кампании — задаём фиксированную последовательность, чтобы получить
    # ровно те же примеры длительности, что в самой задаче.
    _patch_monotonic_sequence(monkeypatch, service_module, [0.0, 0.0, 108.0, 397.0])

    asyncio.run(
        _run_service(db_path, client_factory=_make_client_factory(), execute=True, notifier=notifier)
    )

    assert len(notifier.sent) == 2
    account_message, campaign_message = notifier.sent

    assert "Время выполнения: 00:01:48" in account_message
    assert "Время выполнения: 00:06:37" in campaign_message

    # Остальные строки не тронуты (не датчик времени не должен был их
    # сдвинуть/удалить) — тот же набор, что и в тестах без time.monotonic().
    assert "📨 Кампания: ОСАГО" in account_message
    assert "👤 Аккаунт: Основной" in account_message
    assert "✅ Приглашено: 1" in account_message
    assert "☑️ Уже состояли в группе: 0" in account_message
    assert "🚫 Недоступны (invalid): 0" in account_message
    assert "❌ Ошибок: 0" in account_message
    assert "Осталось кандидатов: 0" in account_message

    assert '📊 Итоги кампании "ОСАГО"' in campaign_message
    assert "Аккаунтов обработано: 1" in campaign_message
    assert "✅ Приглашено: 1" in campaign_message
    assert "☑️ Уже состояли: 0" in campaign_message
    assert "🚫 Недоступны: 0" in campaign_message
    assert "❌ Ошибок: 0" in campaign_message
    assert "Осталось кандидатов: 0" in campaign_message


def test_elapsed_time_uses_monotonic_not_wall_clock(tmp_path, monkeypatch):
    """Требование задачи: время выполнения должно считаться через
    time.monotonic(), а не через wall-clock. Реальный прогон теста занимает
    миллисекунды — если бы длительность считалась через настоящее
    прошедшее время (или через datetime.now(), не используемый здесь для
    этого расчёта), в уведомлении было бы "00:00:00", а не заданное через
    подмену time.monotonic() значение "01:15:00" (намеренно нереалистично
    большое для этого теста, чтобы не совпасть с реальным elapsed случайно)."""
    db_path = _setup_db(tmp_path)
    _setup_single_candidate_campaign(db_path)

    notifier = _FakeOperatorNotifier()

    import reader.inviter.service as service_module

    _patch_monotonic_sequence(monkeypatch, service_module, [0.0, 0.0, 4500.0, 4500.0])

    asyncio.run(
        _run_service(db_path, client_factory=_make_client_factory(), execute=True, notifier=notifier)
    )

    account_message = notifier.sent[0]
    assert "Время выполнения: 01:15:00" in account_message


# ---- случайная пауза + остановка аккаунта на PeerFlood/большом FloodWait ----


def test_pause_between_invites_happens_after_each_outcome_regardless_of_result(tmp_path, monkeypatch):
    """Пауза (см. _choose_invite_pause_seconds) выполняется после КАЖДОГО
    кандидата — успеха, "уже участник" и обычной ошибки — не пропускается ни
    для одного из этих трёх исходов. Само значение задержки замокано через
    _choose_invite_pause_seconds — его стратегия 80/20 проверяется отдельно
    (см. test_choose_invite_pause_seconds_*), здесь же интересует только факт
    и число вызовов паузы."""
    db_path = _setup_db(tmp_path)
    for user_id in range(1, 4):
        _seed_user(
            db_path, user_id, keywords=["осаго"], access_hash=user_id,
            last_seen_at=_BASE_TIME + timedelta(days=user_id),
        )

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="Основной", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    # По last_seen_at DESC: 3 (успех), 2 (уже участник), 1 (обычная ошибка).
    client_factory = _make_client_factory(
        call_errors={
            "Основной": [
                None,
                UserAlreadyParticipantError(request=GetHistoryRequest),
                ChatAdminRequiredError(request=GetHistoryRequest),
            ],
        },
    )

    import reader.inviter.service as service_module

    monkeypatch.setattr(service_module, "_choose_invite_pause_seconds", lambda: 55.5)
    # "Разогрев" пользуется random.uniform() напрямую (не через
    # _choose_invite_pause_seconds) — задаём ему отдельное фиксированное
    # значение, чтобы отличать его от пауз между кандидатами.
    monkeypatch.setattr(service_module.random, "uniform", lambda a, b: 7.0)

    sleep_calls: list = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    # sleep_calls[0] — разогрев (7.0), остальные три — по одной паузе на
    # каждого из трёх кандидатов, независимо от исхода.
    assert sleep_calls[0] == 7.0
    assert sleep_calls[1:] == [55.5, 55.5, 55.5]


def test_warm_up_account_pauses_once_after_connect_not_per_candidate(tmp_path, monkeypatch):
    """connect() -> случайная пауза 5-15 сек (см. _warm_up_account) -> резолв
    target_chat -> приглашения. Ровно один раз на аккаунт, независимо от
    числа обработанных кандидатов — не перед каждым пользователем."""
    db_path = _setup_db(tmp_path)
    for user_id in range(1, 3):
        _seed_user(
            db_path, user_id, keywords=["осаго"], access_hash=user_id,
            last_seen_at=_BASE_TIME + timedelta(days=user_id),
        )

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="Основной", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    client_factory = _make_client_factory(call_errors={"Основной": [None, None]})

    import reader.inviter.service as service_module

    uniform_calls: list[tuple] = []

    def fake_uniform(a, b):
        uniform_calls.append((a, b))
        return 10.0 if (a, b) == (5, 15) else 30.0

    monkeypatch.setattr(service_module.random, "uniform", fake_uniform)
    monkeypatch.setattr(service_module.random, "random", lambda: 0.9)  # форсируем короткую паузу

    sleep_calls: list = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    # Ровно один вызов "разогрева" random.uniform(5, 15), несмотря на два
    # обработанных кандидата — не перед каждым из них.
    warmup_calls = [call for call in uniform_calls if call == (5, 15)]
    assert len(warmup_calls) == 1

    # Разогрев — самый первый сон, до какой-либо обработки кандидатов;
    # дальше — по одной паузе на каждого из двух кандидатов.
    assert sleep_calls[0] == 10.0
    assert sleep_calls[1:] == [30.0, 30.0]


def test_choose_invite_pause_seconds_returns_short_pause_when_above_probability(monkeypatch):
    """random.random() >= _LONG_PAUSE_PROBABILITY (0.2) -> короткая пауза
    random.uniform(20, 60), не длинная."""
    import reader.inviter.service as service_module

    monkeypatch.setattr(service_module.random, "random", lambda: 0.2)  # ровно на границе — НЕ длинная

    uniform_calls: list[tuple] = []

    def fake_uniform(a, b):
        uniform_calls.append((a, b))
        return 42.0

    monkeypatch.setattr(service_module.random, "uniform", fake_uniform)

    result = service_module._choose_invite_pause_seconds()

    assert uniform_calls == [(20, 60)]
    assert result == 42.0


def test_choose_invite_pause_seconds_returns_long_pause_when_below_probability(monkeypatch):
    """random.random() < _LONG_PAUSE_PROBABILITY (0.2) -> длинная пауза
    random.uniform(90, 180), не короткая — примерно в 20% случаев."""
    import reader.inviter.service as service_module

    monkeypatch.setattr(service_module.random, "random", lambda: 0.199999)

    uniform_calls: list[tuple] = []

    def fake_uniform(a, b):
        uniform_calls.append((a, b))
        return 123.0

    monkeypatch.setattr(service_module.random, "uniform", fake_uniform)

    result = service_module._choose_invite_pause_seconds()

    assert uniform_calls == [(90, 180)]
    assert result == 123.0


def test_execute_peer_flood_stops_account_and_notifies_operator(tmp_path):
    """PeerFloodError на любом кандидате — записать его результат, уведомить
    оператора, немедленно прекратить обработку ОСТАЛЬНЫХ кандидатов этим
    аккаунтом, корректно disconnect() и перейти к следующему аккаунту."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="account_2", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    # По last_seen_at DESC — первый (и единственно тронутый) кандидат: 2.
    client_factory = _make_client_factory(
        call_errors={"account_2": [PeerFloodError(request=GetHistoryRequest)]},
        created=created_clients,
    )
    notifier = _FakeOperatorNotifier()

    asyncio.run(
        _run_service(db_path, client_factory=client_factory, execute=True, notifier=notifier)
    )

    client = created_clients[0]
    # Второй кандидат (1) не тронут вовсе — ни одного запроса на него.
    assert len(client.call_requests) == 1
    assert client.disconnected is True

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        assert len(invites) == 1
        assert invites[0].user_id == 2
        assert invites[0].status == "failed"
    finally:
        invite_repository.close()

    stop_notifications = [m for m in notifier.sent if m.startswith("⚠️")]
    assert len(stop_notifications) == 1
    assert "Аккаунт: account_2" in stop_notifications[0]
    assert "Получен PeerFlood." in stop_notifications[0]
    assert "Работа аккаунта остановлена." in stop_notifications[0]
    assert "Переход к следующему аккаунту." in stop_notifications[0]


def test_execute_flood_wait_at_or_above_threshold_stops_account_and_notifies_operator(
    tmp_path, monkeypatch,
):
    """FloodWaitError с exc.seconds >= 300 — не пережидать, а остановить
    аккаунт (как PeerFlood) и уведомить оператора с указанием времени
    ожидания."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="account_2", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(
        call_errors={"account_2": [FloodWaitError(request=GetHistoryRequest, capture=624)]},
        created=created_clients,
    )
    notifier = _FakeOperatorNotifier()

    import reader.inviter.service as service_module

    sleep_calls: list = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)

    asyncio.run(
        _run_service(db_path, client_factory=client_factory, execute=True, notifier=notifier)
    )

    # Большой FloodWait не пережидается — единственный sleep() — это
    # "разогрев" аккаунта сразу после connect() (см.
    # test_warm_up_account_...), а не ожидание FloodWait.
    assert len(sleep_calls) == 1
    assert 5 <= sleep_calls[0] < 15

    client = created_clients[0]
    assert len(client.call_requests) == 1  # второй кандидат не тронут
    assert client.disconnected is True

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert len(invite_repository.list()) == 1
    finally:
        invite_repository.close()

    stop_notifications = [m for m in notifier.sent if m.startswith("⚠️")]
    assert len(stop_notifications) == 1
    assert "Аккаунт: account_2" in stop_notifications[0]
    assert "FloodWait: 624 сек." in stop_notifications[0]
    assert "Работа аккаунта остановлена." in stop_notifications[0]


def test_execute_flood_wait_below_threshold_waits_and_continues_account(tmp_path, monkeypatch):
    """FloodWaitError с exc.seconds < 300 — поведение как раньше: подождать
    exc.seconds и продолжить ЭТИМ ЖЕ аккаунтом остальных кандидатов, без
    уведомления об остановке."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="account_1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(
        call_errors={"account_1": [FloodWaitError(request=GetHistoryRequest, capture=299), None]},
        created=created_clients,
    )
    notifier = _FakeOperatorNotifier()

    import reader.inviter.service as service_module

    monkeypatch.setattr(service_module.random, "random", lambda: 0.9)  # форсируем короткую паузу

    sleep_calls: list = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)

    asyncio.run(
        _run_service(db_path, client_factory=client_factory, execute=True, notifier=notifier)
    )

    # 299 < 300 — дождались и продолжили ОБА кандидата этим же аккаунтом.
    # sleep_calls[0] — "разогрев" сразу после connect(); [1] — ожидание
    # FloodWait; [2] — случайная пауза после ВТОРОГО (успешного) кандидата
    # (см. _pause_between_invites).
    assert len(sleep_calls) == 3
    assert 5 <= sleep_calls[0] < 15
    assert sleep_calls[1] == 299
    assert 20 <= sleep_calls[2] < 60

    client = created_clients[0]
    assert len(client.call_requests) == 2

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert len(invite_repository.list()) == 2
    finally:
        invite_repository.close()

    # Никакого уведомления об остановке — аккаунт не останавливался.
    stop_notifications = [m for m in notifier.sent if m.startswith("⚠️")]
    assert stop_notifications == []


# ---- отсутствие .session-файла — понятный лог, без исключений, без попыток ----


def test_default_session_checker_reflects_real_file_presence(tmp_path):
    db_path = _setup_db(tmp_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        account = account_repository.create(
            name="@vladimihailov", phone="+995500000001", session_name="vladimihailov",
            session_path=str(tmp_path / "vladimihailov"), daily_limit=1,
        )
    finally:
        account_repository.close()

    import reader.inviter.service as service_module

    assert service_module._default_session_checker(account) is False

    (tmp_path / "vladimihailov.session").write_text("")

    assert service_module._default_session_checker(account) is True


def test_format_missing_session_message_includes_account_and_expected_path(tmp_path):
    db_path = _setup_db(tmp_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        account = account_repository.create(
            name="@vladimihailov", phone="+995500000001", session_name="vladimihailov",
            session_path="data/sessions/vladimihailov", daily_limit=1,
        )
    finally:
        account_repository.close()

    import reader.inviter.service as service_module

    message = service_module._format_missing_session_message(account)
    expected_path = service_module._session_file_path(account)

    assert message == (
        "Session not found:\n\n"
        "Account: @vladimihailov\n\n"
        "Expected session:\n"
        f"{expected_path}\n\n"
        "Please authorize this account first."
    )


def test_execute_missing_session_logs_message_and_skips_account_without_error(tmp_path, caplog):
    """Нет .session-файла — понятный лог, ноль попыток подключения/
    приглашения, никакого необработанного исключения, корректное
    завершение обработки этого аккаунта."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="@vladimihailov", phone="+995500000001", session_name="vladimihailov",
            session_path="data/sessions/vladimihailov", daily_limit=1,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    with caplog.at_level("WARNING", logger="reader.inviter.service"):
        asyncio.run(
            _run_service(
                db_path, client_factory=client_factory, execute=True,
                session_checker=lambda account: False,
            )
        )

    # Ни одного клиента не создано — ни единой попытки подключения.
    assert created_clients == []

    log_text = caplog.text
    assert "Session not found:" in log_text
    assert "Account: @vladimihailov" in log_text
    assert "Expected session:" in log_text
    assert "vladimihailov.session" in log_text
    assert "Please authorize this account first." in log_text

    # Ни одной записи о приглашении — до этого аккаунта дело не дошло.
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert invite_repository.list() == []
    finally:
        invite_repository.close()


def test_dry_run_missing_session_logs_message_and_skips_account_without_error(tmp_path, caplog):
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="@vladimihailov", phone="+995500000001", session_name="vladimihailov",
            session_path="data/sessions/vladimihailov", daily_limit=1,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    with caplog.at_level("WARNING", logger="reader.inviter.service"):
        asyncio.run(
            _run_service(
                db_path, client_factory=client_factory, execute=False,
                session_checker=lambda account: False,
            )
        )

    assert created_clients == []

    log_text = caplog.text
    assert "Session not found:" in log_text
    assert "Account: @vladimihailov" in log_text
    assert "Please authorize this account first." in log_text


def test_missing_session_only_skips_the_affected_account(tmp_path):
    """Один аккаунт без сессии не должен мешать остальным — как и при
    любом другом сбое конкретного аккаунта (см. run())."""
    db_path = _setup_db(tmp_path)
    for user_id in range(1, 3):
        _seed_user(
            db_path, user_id, keywords=["осаго"], access_hash=user_id,
            last_seen_at=_BASE_TIME + timedelta(days=user_id),
        )

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="Без сессии", phone="+995500000001", session_name="no-session",
            session_path="data/sessions/no-session", daily_limit=1,
        )
        account_repository.create(
            name="С сессией", phone="+995500000002", session_name="has-session",
            session_path="data/sessions/has-session", daily_limit=1,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(
        _run_service(
            db_path, client_factory=client_factory, execute=True,
            session_checker=lambda account: account.name == "С сессией",
        )
    )

    # Только "С сессией" реально подключался и пригласил своего кандидата.
    assert len(created_clients) == 1
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        assert len(invites) == 1
    finally:
        invite_repository.close()
