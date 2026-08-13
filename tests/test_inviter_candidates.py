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
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerFloodError,
    RPCError,
    UserAlreadyParticipantError,
    UserBotError,
    UserChannelsTooMuchError,
    UserKickedError,
    UserNotParticipantError,
    UserPrivacyRestrictedError,
)
from telethon.tl.functions.channels import InviteToChannelRequest  # noqa: E402
from telethon.tl.functions.messages import AddChatUserRequest, GetHistoryRequest  # noqa: E402
from telethon.tl.types import Channel, ChannelForbidden, Chat, InputPeerUser, User  # noqa: E402

from reader.inviter.repository import (  # noqa: E402
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)
from reader.inviter.models import InviteCandidate  # noqa: E402
from reader.inviter.service import (  # noqa: E402
    TEST_MODE_MAX_SUCCESSFUL_INVITES,
    InviteErrorAction,
    InviterService,
    _CandidateIsBotError,
    _CandidateUnresolvableError,
    _classify_invite_error,
    _format_duration,
    _humanize_error,
)
from reader.inviter.worker import InviterWorker  # noqa: E402
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


_USERNAME_NOT_PASSED = object()


def _seed_user(
    db_path: Path,
    user_id: int,
    *,
    username=_USERNAME_NOT_PASSED,
    keywords: list[str] | None = None,
    access_hash: int | None = None,
    last_seen_at: datetime | None = None,
    is_bot: bool | None = None,
) -> None:
    """Пишет пользователя напрямую в users, минуя UserRepository.upsert()
    (который всегда ставит last_seen_at=CURRENT_TIMESTAMP) — тестам нужен
    полный контроль над last_seen_at, чтобы детерминированно проверить
    сортировку.

    select_candidates() теперь требует непустой username (см. задачу про
    фильтр "username IS NOT NULL AND TRIM(username) <> ''") — большинству
    существующих тестов сам username не важен, поэтому по умолчанию (когда
    он вообще не передан) подставляется "user<id>", а не None. Передайте
    username=None явно, если тест целенаправленно проверяет отсутствие
    username.

    is_bot=None (по умолчанию) — как у строк, записанных без этого
    признака (см. _CANDIDATES_BASE_WHERE: NULL не исключается из
    кандидатов, только is_bot=1); передайте True явно, чтобы смоделировать
    Telegram-бота (см. test_select_candidates_excludes_bots)."""
    if username is _USERNAME_NOT_PASSED:
        username = f"user{user_id}"

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (user_id, username, keywords, access_hash, last_seen_at, is_bot, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                user_id,
                username,
                ", ".join(keywords) if keywords else None,
                access_hash,
                last_seen_at.isoformat() if last_seen_at else None,
                None if is_bot is None else int(is_bot),
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
        get_input_entity_error=None, entity_responses=None,
        participant_check_errors=None,
    ):
        self.account = account
        self._connect_error = connect_error
        self._get_entity_error = get_entity_error
        self._target_entity = target_entity if target_entity is not None else SimpleNamespace(id=999)
        # Список ошибок (или None = успех) — по одной на очередной вызов
        # __call__(), в порядке кандидатов этого аккаунта.
        self._call_errors = list(call_errors) if call_errors is not None else []
        # get_input_entity() по умолчанию "успешна" (кандидат уже известен
        # этому аккаунту) — большинство тестов не про резолв конкретно этим
        # аккаунтом (см. test_execute_resolves_unknown_candidate_via_username_*
        # и соседние, которые явно её переопределяют).
        self._get_input_entity_error = get_input_entity_error
        # {значение entity: результат} — результат либо готовая сущность,
        # либо экземпляр исключения (для get_entity(candidate.username), см.
        # InviterService._resolve_input_peer). Если entity не найден здесь —
        # используется обычное поведение get_entity_error/target_entity
        # (резолв campaign.target_chat — как и раньше).
        self._entity_responses = entity_responses or {}
        # {user_id: исключение} — get_permissions(target_entity, user) для
        # этого user_id бросает исключение (например UserNotParticipantError,
        # см. InviterService._verify_pending_invites), вместо обычного
        # "успешно подтверждён" по умолчанию.
        self._participant_check_errors = participant_check_errors or {}
        self.connected = False
        self.disconnected = False
        self.get_entity_calls: list = []
        self.get_input_entity_calls: list = []
        self.get_permissions_calls: list = []
        self.call_requests: list = []

    async def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    async def get_entity(self, entity):
        self.get_entity_calls.append(entity)
        # Проверка is_bot (см. InviterService._resolve_input_peer) вызывает
        # get_entity(input_peer) — InputPeerUser, а не строка/id, поэтому
        # ключом в entity_responses для такого вызова служит сам user_id.
        lookup_key = entity.user_id if isinstance(entity, InputPeerUser) else entity
        if lookup_key in self._entity_responses:
            response = self._entity_responses[lookup_key]
            if isinstance(response, Exception):
                raise response
            return response
        if self._get_entity_error is not None:
            raise self._get_entity_error
        if isinstance(entity, InputPeerUser):
            # По умолчанию — обычный (не бот) пользователь, если тест не
            # переопределил ответ выше: большинство тестов не про статус
            # бота конкретно (см. test_execute_test_mode_* и соседние про
            # is_bot — они настраивают это явно).
            return User(id=entity.user_id, access_hash=entity.access_hash, bot=False, first_name="Test")
        return self._target_entity

    async def get_input_entity(self, user_id):
        self.get_input_entity_calls.append(user_id)
        if self._get_input_entity_error is not None:
            raise self._get_input_entity_error
        return InputPeerUser(user_id=user_id, access_hash=user_id)

    async def get_permissions(self, entity, user):
        self.get_permissions_calls.append((entity, user))
        user_id = getattr(user, "user_id", user)
        error = self._participant_check_errors.get(user_id)
        if error is not None:
            raise error
        return object()

    async def __call__(self, request):
        self.call_requests.append(request)
        error = self._call_errors.pop(0) if self._call_errors else None
        if error is not None:
            raise error
        return object()

    async def disconnect(self) -> None:
        self.disconnected = True


def _make_client_factory(
    *, connect_errors=None, get_entity_errors=None, call_errors=None,
    target_entities=None, get_input_entity_errors=None, entity_responses=None,
    participant_check_errors=None,
    created=None,
):
    """connect_errors/get_entity_errors/call_errors/get_input_entity_errors —
    {account.name: ...} для аккаунтов, которым нужно смоделировать сбой
    (call_errors — список ошибок, по одной на кандидата, см.
    _FakeTelegramClient). target_entities — {account.name: entity}, чтобы
    задать, что именно "вернул" бы client.get_entity(campaign.target_chat)
    для конкретного аккаунта (по умолчанию — не Channel/Chat, см.
    _FakeTelegramClient). entity_responses — {account.name: {value:
    entity_или_exception}} для get_entity(candidate.username) — резолв
    конкретного кандидата этим аккаунтом (см. _resolve_input_peer).
    participant_check_errors — {account.name: {user_id: exception}} —
    get_permissions() для этого user_id при проверке pending (см.
    _verify_pending_invites) бросает exception вместо обычного успеха
    (по умолчанию — все pending подтверждаются как joined).
    created — список, в который складываются все созданные фейковые клиенты
    (по одному на аккаунт), чтобы тест мог проверить connected/disconnected
    после run()."""
    connect_errors = connect_errors or {}
    get_entity_errors = get_entity_errors or {}
    call_errors = call_errors or {}
    target_entities = target_entities or {}
    get_input_entity_errors = get_input_entity_errors or {}
    entity_responses = entity_responses or {}
    participant_check_errors = participant_check_errors or {}

    def factory(account):
        client = _FakeTelegramClient(
            account,
            connect_error=connect_errors.get(account.name),
            get_entity_error=get_entity_errors.get(account.name),
            call_errors=call_errors.get(account.name),
            target_entity=target_entities.get(account.name),
            get_input_entity_error=get_input_entity_errors.get(account.name),
            entity_responses=entity_responses.get(account.name),
            participant_check_errors=participant_check_errors.get(account.name),
        )
        if created is not None:
            created.append(client)
        return client

    return factory


class _FakeOperatorNotifier:
    """Ровно то, что нужно InviterService от OperatorNotifier —
    notify_text(text) -> bool. raise_error, если задан, имитирует сбой
    отправки (сервис не должен из-за этого падать, см.
    test_notify_failure_does_not_stop_service). deliver=False (без
    raise_error) — как настоящий OperatorNotifier.notify_text() без ни
    одного получателя: не бросает исключение, просто возвращает False
    (см. test_execute_campaign_summary_logged_when_no_recipients_found)."""

    def __init__(self, *, raise_error=None, deliver=True):
        self.sent: list[str] = []
        self._raise_error = raise_error
        self._deliver = deliver

    async def notify_text(self, text: str) -> bool:
        self.sent.append(text)
        if self._raise_error is not None:
            raise self._raise_error
        return self._deliver


class _FakeUserRepository:
    """Ровно то, что нужно InviterService от UserRepository —
    update_access_hash(user_id, access_hash, username=None, is_bot=None)
    и mark_as_bot(user_id) -> bool (см. reader/users/repository.py).
    raise_error, если задан, имитирует сбой записи (сервис не должен из-за
    этого падать, см. test_execute_update_access_hash_failure_does_not_stop_invite)."""

    def __init__(self, *, raise_error=None):
        self.calls: list[tuple] = []
        self.mark_as_bot_calls: list[int] = []
        self._raise_error = raise_error

    def update_access_hash(self, user_id, access_hash, username=None, is_bot=None) -> bool:
        self.calls.append((user_id, access_hash, username, is_bot))
        if self._raise_error is not None:
            raise self._raise_error
        return True

    def mark_as_bot(self, user_id) -> bool:
        self.mark_as_bot_calls.append(user_id)
        if self._raise_error is not None:
            raise self._raise_error
        return True


async def _run_service(
    db_path: Path, client_factory=None, execute: bool = False, notifier=None,
    session_checker=None, user_repository=None, max_successful_invites=None,
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
            user_repository=user_repository,
            # None (по умолчанию) — обычный режим без ограничения; тестовый
            # режим (--test в main.py) передаёт сюда конкретное число, см.
            # test_execute_test_mode_*.
            max_successful_invites=max_successful_invites,
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


def test_select_candidates_excludes_users_without_username(tmp_path):
    """Без username приглашающий аккаунт физически не может резолвить
    кандидата, если он ему не известен (см. InviterService._resolve_input_peer
    и client.get_entity("@username")) — такой candidate не должен попадать
    в выборку вовсе, а не проваливать каждую попытку приглашения."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username=None, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, username="", keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 3, username="  ", keywords=["осаго"], access_hash=3, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 4, username="ivan", keywords=["осаго"], access_hash=4, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")

        candidates = invite_repository.select_candidates(campaign.id, limit=10)
        assert [c.user_id for c in candidates] == [4]
        assert invite_repository.count_candidates(campaign.id) == 1
    finally:
        campaign_repository.close()
        invite_repository.close()


def test_select_candidates_excludes_bots(tmp_path):
    """Приглашение Telegram-бота (is_bot=1) заканчивается
    ChatAdminRequiredError — исключаем таких кандидатов ещё на этапе
    выборки (SQL), а не после ошибки Telegram. is_bot=NULL — как у строк,
    записанных без этого признака вовсе — НЕ исключается (см.
    test_select_candidates_includes_users_with_unknown_bot_flag)."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="vlars_bot", keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME, is_bot=True)
    _seed_user(db_path, 2, username="ivan", keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME, is_bot=False)

    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")

        candidates = invite_repository.select_candidates(campaign.id, limit=10)
        assert [c.user_id for c in candidates] == [2]
        assert invite_repository.count_candidates(campaign.id) == 1
        assert invite_repository.count_found_candidates(campaign.id) == 1
    finally:
        campaign_repository.close()
        invite_repository.close()


def test_select_candidates_includes_users_with_unknown_bot_flag(tmp_path):
    """Строки без известного признака is_bot (NULL — например, записанные
    до появления этого поля) не должны молча выпадать из кандидатов:
    исключаются только те, для кого is_bot=1 достоверно известен."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="ivan", keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME, is_bot=None)

    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")

        candidates = invite_repository.select_candidates(campaign.id, limit=10)
        assert [c.user_id for c in candidates] == [1]
    finally:
        campaign_repository.close()
        invite_repository.close()


def test_select_candidates_includes_users_with_username(tmp_path):
    """Симметричная проверка: пользователь с обычным (непустым) username
    продолжает попадать в выборку — фильтр не отсекает лишнего."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="ivan", keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")

        candidates = invite_repository.select_candidates(campaign.id, limit=10)
        assert [c.user_id for c in candidates] == [1]
        assert candidates[0].username == "ivan"
    finally:
        campaign_repository.close()
        invite_repository.close()


def test_count_found_candidates_ignores_username_filter(tmp_path):
    """count_found_candidates() — те же условия, что и count_candidates()
    (keyword/access_hash/ещё-не-приглашён), но БЕЗ фильтра по username —
    "всего найдено" для операторского отчёта (см.
    InviterService._notify_campaign_result)."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username=None, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, username="", keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 3, username="ivan", keywords=["осаго"], access_hash=3, last_seen_at=_BASE_TIME)
    # Не подходит ни под один из счётчиков — нет access_hash вовсе.
    _seed_user(db_path, 4, username="petr", keywords=["осаго"], access_hash=None, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")

        # 1, 2, 3 подходят по keyword/access_hash (4 — без access_hash, не
        # подходит вообще ни при каких условиях).
        assert invite_repository.count_found_candidates(campaign.id) == 3
        # Из них только 3 — с username.
        assert invite_repository.count_candidates(campaign.id) == 1
    finally:
        campaign_repository.close()
        invite_repository.close()


def test_count_found_candidates_respects_keyword_and_already_invited_filters(tmp_path):
    """count_found_candidates() — не просто "все пользователи с
    access_hash", а те же keyword/ещё-не-приглашён условия, что и у
    count_candidates()/select_candidates()."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username=None, keywords=["каско"], access_hash=1, last_seen_at=_BASE_TIME)  # другой keyword
    _seed_user(db_path, 2, username=None, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")

        assert invite_repository.count_found_candidates(campaign.id) == 1  # только user 2

        invite_repository.create(user_id=2, campaign_id=campaign.id, status="invited")
        assert invite_repository.count_found_candidates(campaign.id) == 0  # уже приглашён
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


def _setup_single_candidate_campaign(db_path, *, daily_limit=5, is_bot=None, verify_membership=True):
    _seed_user(
        db_path, 1, username="ivan", keywords=["осаго"], access_hash=11,
        last_seen_at=_BASE_TIME, is_bot=is_bot,
    )

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account = account_repository.create(
            name="Основной", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=daily_limit,
            verify_membership=verify_membership,
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
        # pending -> joined: отправлено, подтверждено при проверке (см.
        # _verify_pending_invites, get_permissions успешна по умолчанию).
        assert invite.status == "joined"
        assert invite.error is None
        assert invite.invited_at is not None
        assert invite.verified_at is not None
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


# ---- проверка возможности приглашать ДО выборки кандидатов (execute=True) ----


def test_execute_broken_connect_does_not_select_candidates_for_that_account(tmp_path, monkeypatch):
    """Аккаунт, который не может подключиться, не должен выбирать
    кандидатов вовсе (см. задачу про бесполезную выборку для
    неработающих аккаунтов) — рабочий аккаунт получает ВСЕХ реальных
    кандидатов, ни один не "зарезервирован" впустую за неработающим."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="broken", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
        account_repository.create(
            name="works", phone="+995500000002", session_name="acc2",
            session_path="acc2.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    client_factory = _make_client_factory(
        connect_errors={"broken": ConnectionError("не удалось подключиться")},
    )

    import reader.inviter.service as service_module

    all_logs: list[str] = []
    monkeypatch.setattr(service_module.logger, "info", all_logs.append)
    monkeypatch.setattr(service_module.logger, "warning", all_logs.append)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    # Ни одного "Campaign: ..." блока (=вызова select_candidates(), см.
    # _format_candidates_block) для неработающего аккаунта — только для
    # того, который реально подключился (волна №1 и волна добора, см.
    # _execute_account — обе выбирают кандидатов у одного и того же
    # рабочего аккаунта, "broken" среди них нет вовсе).
    campaign_blocks = [b for b in all_logs if b.startswith("Campaign:")]
    assert campaign_blocks
    assert all("Account: works" in block for block in campaign_blocks)

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        # Оба кандидата ушли работающему аккаунту — ни один не пропал
        # впустую из-за неработающего.
        assert len(invites) == 2
        assert all(i.account_id == 2 for i in invites)
        assert all(i.status == "joined" for i in invites)
    finally:
        invite_repository.close()


def test_execute_target_chat_not_found_does_not_select_candidates_for_that_account(tmp_path, monkeypatch):
    """Аналогично test_execute_broken_connect_does_not_select_candidates_
    for_that_account, но аккаунт подключается успешно, а резолв
    target_chat падает — тоже без единого запроса выборки кандидатов."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="broken", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
        account_repository.create(
            name="works", phone="+995500000002", session_name="acc2",
            session_path="acc2.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    client_factory = _make_client_factory(
        get_entity_errors={"broken": ValueError("target_chat не найден")},
    )

    import reader.inviter.service as service_module

    all_logs: list[str] = []
    monkeypatch.setattr(service_module.logger, "info", all_logs.append)
    monkeypatch.setattr(service_module.logger, "warning", all_logs.append)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    campaign_blocks = [b for b in all_logs if b.startswith("Campaign:")]
    assert campaign_blocks
    assert all("Account: works" in block for block in campaign_blocks)

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        assert len(invites) == 2
        assert all(i.account_id == 2 for i in invites)
        assert all(i.status == "joined" for i in invites)
    finally:
        invite_repository.close()


def test_execute_missing_session_does_not_select_candidates(tmp_path, monkeypatch):
    """Симметрично: аккаунт вовсе без .session-файла тоже не должен
    выбирать кандидатов — самая дешёвая из всех проверок, до единого RPC."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="no_session", phone="+995500000001", session_name="acc1",
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

    asyncio.run(
        _run_service(
            db_path, client_factory=client_factory, execute=True,
            session_checker=lambda account: False,
        )
    )

    assert created_clients == []
    campaign_blocks = [b for b in all_logs if b.startswith("Campaign:")]
    assert campaign_blocks == []

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert invite_repository.list() == []
    finally:
        invite_repository.close()


# ---- daily_limit учитывает уже выполненные СЕГОДНЯ приглашения (execute=True) ----


def test_execute_daily_limit_subtracts_already_successful_invites_today(tmp_path):
    """daily_limit=5, но 3 подтверждённых участника этим аккаунтом уже
    получены СЕГОДНЯ (в user_campaign_invites, а не в памяти) — остаток 2,
    и именно 2 (а не 5) должно использоваться как LIMIT при выборе НОВЫХ
    кандидатов (см. задачу про daily_limit-баг)."""
    db_path = _setup_db(tmp_path)
    # 5 свежих кандидатов, ещё не приглашённых.
    for user_id in range(101, 106):
        _seed_user(
            db_path, user_id, keywords=["осаго"], access_hash=user_id,
            last_seen_at=_BASE_TIME + timedelta(days=user_id),
        )

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

    # 3 "уже подтверждённых сегодня участника" этим же аккаунтом — как
    # будто более ранний запуск --execute сегодня же. user_id этих записей
    # не пересекается с 5 свежими кандидатами выше; user_campaign_invites
    # не требует, чтобы такой user_id существовал в users (нет внешнего
    # ключа).
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        now = datetime.now(timezone.utc)
        for user_id in (901, 902, 903):
            invite_repository.create(
                user_id=user_id, campaign_id=campaign.id, account_id=account.id,
                status="joined", invited_at=now, verified_at=now,
            )
    finally:
        invite_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    client = created_clients[0]
    # Остаток лимита = 5 - 3 = 2, а не 5 — и после того, как обе новые
    # заявки подтвердятся (get_permissions успешна по умолчанию), остаток
    # становится 0, поэтому волны добора не происходит.
    assert len(client.call_requests) == 2

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        # 3 старые записи + 2 новые.
        assert len(invites) == 5
        new_invites = [i for i in invites if i.user_id in range(101, 106)]
        assert len(new_invites) == 2
        assert all(i.status == "joined" for i in new_invites)
    finally:
        invite_repository.close()


def test_execute_skips_account_entirely_when_daily_limit_already_reached(tmp_path):
    """Остаток лимита <= 0 — аккаунт полностью пропускается: ни клиента,
    ни единого запроса выборки кандидатов."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account = account_repository.create(
            name="Основной", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=3,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        now = datetime.now(timezone.utc)
        for user_id in (901, 902, 903):
            invite_repository.create(
                user_id=user_id, campaign_id=campaign.id, account_id=account.id,
                status="joined", invited_at=now, verified_at=now,
            )
    finally:
        invite_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    assert created_clients == []

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        # Ровно 3 старые записи — ничего нового не добавилось.
        assert len(invite_repository.list()) == 3
    finally:
        invite_repository.close()


def test_execute_daily_limit_is_global_per_account_across_campaigns(tmp_path):
    """daily_limit — свойство самого аккаунта, а не пары (аккаунт,
    кампания): успешное приглашение ЭТИМ аккаунтом в одной кампании
    должно уменьшать остаток лимита и для ДРУГОЙ кампании того же
    аккаунта в рамках одного запуска run() (см. count_today_joined —
    без фильтра по campaign_id)."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="u1", keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, username="u2", keywords=["каско"], access_hash=2, last_seen_at=_BASE_TIME)

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        campaign_repository.create(name="Каско", keyword="каско", target_chat="@target_chat")
        account_repository.create(
            name="Основной", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=1,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    client_factory = _make_client_factory()

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        # daily_limit=1 на аккаунт ЦЕЛИКОМ — только ОДНО приглашение
        # суммарно по обеим кампаниям, а не по одному на каждую.
        assert len(invites) == 1
        assert invites[0].status == "joined"
        assert invites[0].user_id == 1  # кампания ОСАГО обработана первой
    finally:
        invite_repository.close()


# ---- подтверждение вступления: pending -> joined, ожидание, волна добора ----


# campaign_id, которого не существует в invite_campaigns — user_campaign_invites
# не требует внешнего ключа (см. схему), поэтому можно смоделировать "уже
# сегодня отправленные/подтверждённые где-то" записи без создания отдельной
# настоящей кампании (иначе run() пытался бы обработать и её тоже).
_UNRELATED_CAMPAIGN_ID = 999999


@pytest.mark.parametrize(
    "joined_today,pending_today,daily_limit,expected_new",
    [
        (10, 14, 24, 0),
        (19, 0, 24, 5),
        (19, 3, 24, 2),
        (24, 0, 24, 0),
        (0, 24, 24, 0),
    ],
)
def test_execute_remaining_budget_accounts_for_pending_reservations(
    tmp_path, joined_today, pending_today, daily_limit, expected_new,
):
    """remaining = daily_limit - joined_today - pending_today, а НЕ
    только daily_limit - joined_today (см. задачу про перелив лимита) —
    pending временно резервирует место, пока не подтверждён (joined) или
    не опровергнут (not_joined)."""
    db_path = _setup_db(tmp_path)
    # Кандидатов для НОВОЙ волны с запасом — больше, чем может понадобиться
    # в любом из сценариев.
    for user_id in range(1, 31):
        _seed_user(
            db_path, user_id, keywords=["осаго"], access_hash=user_id,
            last_seen_at=_BASE_TIME + timedelta(days=user_id),
        )

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account = account_repository.create(
            name="account_1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=daily_limit,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    # "Уже сегодня" joined/pending — синтетические записи в НЕсвязанной
    # кампании (см. _UNRELATED_CAMPAIGN_ID), чтобы count_today_joined()/
    # count_today_pending() (глобальные по аккаунту, без campaign_id) их
    # учли, а _verify_pending_invites() (per-campaign) — не тронул.
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        now = datetime.now(timezone.utc)
        for i in range(joined_today):
            invite_repository.create(
                user_id=900 + i, campaign_id=_UNRELATED_CAMPAIGN_ID, account_id=account.id,
                status="joined", invited_at=now, verified_at=now,
            )
        for i in range(pending_today):
            invite_repository.create(
                user_id=800 + i, campaign_id=_UNRELATED_CAMPAIGN_ID, account_id=account.id,
                status="pending", invited_at=now,
            )
    finally:
        invite_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    if expected_new == 0:
        # remaining <= 0 сразу — аккаунт пропущен полностью, ни один
        # клиент не создаётся (см. _execute_account).
        assert created_clients == []
    else:
        client = created_clients[0]
        assert len(client.call_requests) == expected_new


def test_execute_top_up_remaining_also_accounts_for_pending_after_verification(tmp_path):
    """Важный сценарий: отправили 24 pending -> верификация -> 19 joined,
    5 НЕ подтвердились, но проверка САМА не смогла определить их статус
    (не UserNotParticipantError — например сбой сети) -> они остаются
    'pending', а не 'not_joined' -> их резерв в лимите НЕ освобождается ->
    волна добора не должна отправлять новые 5, иначе daily_limit был бы
    превышен (19 joined + 5 старых pending + 5 новых pending = 29 > 24)."""
    db_path = _setup_db(tmp_path)
    total_users = 29
    for user_id in range(1, total_users + 1):
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
            session_path="acc1.session", daily_limit=24,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    # По last_seen_at DESC волна №1 (limit=24) выбирает user_id 29..6. Для
    # 5 из них (10..14) сама проверка не может определить статус (сбой,
    # не UserNotParticipantError) — они остаются 'pending'.
    unresolvable = {10, 11, 12, 13, 14}
    client_factory = _make_client_factory(
        participant_check_errors={
            "account_1": {
                user_id: ConnectionError("сеть моргнула") for user_id in unresolvable
            },
        },
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        # Волны добора НЕ было вовсе — 24 (волна №1) и ни одной новой
        # записи сверху: 19 joined + 5 pending = 24, лимит уже исчерпан
        # (0 свободных мест), кандидаты 1..5 не тронуты.
        assert len(invites) == 24
        joined_ids = {i.user_id for i in invites if i.status == "joined"}
        pending_ids = {i.user_id for i in invites if i.status == "pending"}
        assert len(joined_ids) == 19
        assert pending_ids == unresolvable
    finally:
        invite_repository.close()


def test_execute_verify_pending_confirms_joined_when_participant(tmp_path):
    """get_permissions() успешна для pending-кандидата (см.
    _verify_pending_invites) — status='joined', verified_at задан."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    client_factory = _make_client_factory()

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invite = invite_repository.list()[0]
        assert invite.status == "joined"
        assert invite.verified_at is not None
        assert invite.verified_at >= invite.invited_at
    finally:
        invite_repository.close()


def test_execute_verify_pending_marks_not_joined_when_confirmed_not_participant(tmp_path):
    """get_permissions() бросает UserNotParticipantError — Telegram явно
    говорит "не участник", это ДОСТОВЕРНЫЙ ответ: status='not_joined' (не
    'pending' — иначе занятое место в дневном лимите никогда бы не
    освободилось, см. задачу про перелив лимита), verified_at задан, и
    это не считается ошибкой (см. _verify_pending_invites)."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    client_factory = _make_client_factory(
        participant_check_errors={
            "Основной": {1: UserNotParticipantError(request=GetHistoryRequest)},
        },
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invite = invite_repository.list()[0]
        assert invite.status == "not_joined"
        assert invite.verified_at is not None
    finally:
        invite_repository.close()


def test_execute_verify_pending_leaves_pending_on_unexpected_check_error(tmp_path, caplog):
    """Любая другая ошибка при самой проверке (не UserNotParticipantError)
    — тоже остаётся 'pending' (не считаем это подтверждением ИЛИ провалом
    кандидата — просто не удалось проверить сейчас), с предупреждением в
    лог."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    client_factory = _make_client_factory(
        participant_check_errors={"Основной": {1: ConnectionError("сеть моргнула")}},
    )

    with caplog.at_level("WARNING", logger="reader.inviter.service"):
        asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    assert "Не удалось проверить участие в группе" in caplog.text

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invite = invite_repository.list()[0]
        assert invite.status == "pending"
        assert invite.verified_at is None
    finally:
        invite_repository.close()


# ---- InviterService: account.verify_membership ----
# Обычные (не admin) аккаунты инвайтера могут не иметь прав на
# GetParticipantRequest в конкретном target_chat — раньше это означало
# десятки заведомо неработающих запросов на каждый цикл ("Chat admin
# privileges are required...", см. задачу). verify_membership=False
# отключает именно эту проверку для конкретного аккаунта, не трогая
# отправку приглашений/daily_limit/hourly worker limit.


def test_execute_verifies_pending_when_verify_membership_true(tmp_path):
    """Базовое поведение по умолчанию (verify_membership=True) — не
    изменилось: get_permissions() вызывается, pending подтверждается."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path, verify_membership=True)

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    assert len(created_clients[0].get_permissions_calls) == 1
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert invite_repository.list()[0].status == "joined"
    finally:
        invite_repository.close()


def test_execute_skips_verification_when_verify_membership_false(tmp_path):
    """verify_membership=False — ни одного client.get_permissions()
    (GetParticipantRequest) вообще, новый успешный InviteToChannelRequest/
    AddChatUserRequest по-прежнему выполняется и остаётся status='pending'
    (НЕ искусственно переводится в joined — нет данных Telegram, чтобы это
    утверждать, см. задачу)."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path, verify_membership=False)

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    client = created_clients[0]
    assert len(client.call_requests) == 1  # InviteToChannelRequest/AddChatUserRequest всё равно выполнен
    assert client.get_permissions_calls == []  # но НИ ОДНОГО GetParticipantRequest

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invite = invite_repository.list()[0]
        assert invite.status == "pending"
        assert invite.verified_at is None
    finally:
        invite_repository.close()


def test_execute_verify_membership_false_does_not_check_old_pending_from_previous_run(tmp_path):
    """pending, оставшийся с ПРЕДЫДУЩЕГО прогона (до того, как оператор
    выключил verify_membership) — тоже не проверяется ни при каких
    обстоятельствах, а не только новые из этого запуска."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(
        db_path, daily_limit=5, verify_membership=False,
    )

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        old_pending = invite_repository.create(
            user_id=999, campaign_id=campaign.id, account_id=account.id,
            status="pending", invited_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    finally:
        invite_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    assert created_clients[0].get_permissions_calls == []

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        untouched = invite_repository.get(old_pending.id)
        assert untouched.status == "pending"
        assert untouched.verified_at is None
    finally:
        invite_repository.close()


def test_execute_verify_membership_false_avoids_admin_privileges_warning(tmp_path, caplog):
    """Продакшен-сценарий из задачи: get_permissions() падал бы с
    ChatAdminRequiredError ("Chat admin privileges are required...") на
    каждом pending — verify_membership=False должен исключить это
    полностью (никакого запроса — никакой ошибки и предупреждения в лог),
    а не просто подавить сообщение об ошибке."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path, verify_membership=False)

    client_factory = _make_client_factory(
        participant_check_errors={"Основной": {1: ChatAdminRequiredError(request=GetHistoryRequest)}},
    )

    with caplog.at_level("WARNING", logger="reader.inviter.service"):
        asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    assert "Не удалось проверить участие в группе" not in caplog.text
    assert "Chat admin privileges" not in caplog.text


def test_execute_verify_membership_false_still_reports_pending_in_summary(tmp_path):
    """Summary/operator-уведомление по-прежнему показывает pending (см.
    задачу: "В summary допустимо по-прежнему показывать pending") — просто
    без попытки его подтвердить."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path, verify_membership=False)

    notifier = _FakeOperatorNotifier()
    asyncio.run(
        _run_service(
            db_path, client_factory=_make_client_factory(), execute=True, notifier=notifier,
        )
    )

    account_report = next(t for t in notifier.sent if "Аккаунт:" in t)
    assert "Ожидают подтверждения: 1" in account_report


def test_execute_enabled_false_overrides_regardless_of_verify_membership(tmp_path):
    """enabled=False по-прежнему полностью выключает аккаунт из инвайтера
    — verify_membership (в любую сторону) не может это компенсировать
    (см. задачу: это независимые флаги, но enabled остаётся главным)."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path, verify_membership=True)

    account_repository = TelegramAccountRepository(db_path)
    try:
        account_repository.update(account.id, enabled=False)
    finally:
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    assert created_clients == []  # аккаунт вообще не подключался
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert invite_repository.list() == []
    finally:
        invite_repository.close()


def test_worker_picks_up_verify_membership_change_without_restart(tmp_path):
    """reader/inviter/worker.py::InviterWorker перечитывает аккаунты из БД
    на каждом тике (см. InviterWorker._enabled_pairs) — оператор,
    выключивший verify_membership МЕЖДУ тиками (без перезапуска процесса,
    тот же InviterWorker/InviterService), должен увидеть эффект уже на
    следующем тике (см. задачу: "Worker должен автоматически подхватывать
    изменение флага из БД без перезапуска")."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="ivan1", keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(
        db_path, 2, username="ivan2", keywords=["осаго"], access_hash=2,
        last_seen_at=_BASE_TIME + timedelta(days=1),
    )

    created_clients: list = []
    service, account_repository, campaign_repository, invite_repository = _build_service(
        db_path, client_factory=_make_client_factory(created=created_clients),
    )
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")
        account = account_repository.create(
            name="acc1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=24, verify_membership=True,
        )

        worker = InviterWorker(
            service, campaign_repository, account_repository,
            invitations_per_account_per_hour=2, poll_interval_seconds=600,
            shutdown_event=asyncio.Event(),
        )

        asyncio.run(worker.run_one_tick())
        assert len(created_clients) == 1
        # verify_membership=True — проверка pending прошла как обычно.
        assert len(created_clients[0].get_permissions_calls) == 1

        # Оператор выключает verify_membership МЕЖДУ тиками — тот же
        # процесс, тот же InviterWorker/InviterService, никакого
        # перезапуска.
        account_repository.update(account.id, verify_membership=False)

        asyncio.run(worker.run_one_tick())

        assert len(created_clients) == 2
        # Второй тик отправил новое приглашение (второй кандидат), но НЕ
        # проверил его — изменение флага подхватилось немедленно.
        assert len(created_clients[1].call_requests) == 1
        assert created_clients[1].get_permissions_calls == []
    finally:
        account_repository.close()
        campaign_repository.close()
        invite_repository.close()


def test_execute_waits_short_interval_before_verifying_when_sent_below_threshold(tmp_path, monkeypatch):
    """Отправлено < 20 приглашений этой волной — ждать 60 сек. перед
    проверкой pending (см. _wait_before_verifying_pending/
    _PENDING_CHECK_BATCH_THRESHOLD)."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path, daily_limit=1)

    import reader.inviter.service as service_module

    sleep_calls: list = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)

    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(), execute=True))

    # [0] — разогрев, [1] — пауза после кандидата, [2] — ожидание перед
    # проверкой pending: ровно 1 отправлен (< 20) -> короткое, 60 сек.
    assert sleep_calls[2] == 60


def test_execute_waits_long_interval_before_verifying_when_sent_at_threshold(tmp_path, monkeypatch):
    """Отправлено >= 20 приглашений этой волной — ждать 5 минут перед
    проверкой pending: Telegram может обрабатывать большие пачки не
    мгновенно (см. задачу)."""
    db_path = _setup_db(tmp_path)
    total_users = 20
    for user_id in range(1, total_users + 1):
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
            session_path="acc1.session", daily_limit=20,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    import reader.inviter.service as service_module

    sleep_calls: list = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)

    asyncio.run(_run_service(db_path, client_factory=_make_client_factory(), execute=True))

    # [0] — разогрев, [1..20] — по паузе на каждого из 20 кандидатов,
    # [21] — ожидание перед проверкой pending: отправлено ровно 20 (>= 20)
    # -> длинное, 300 сек.
    assert sleep_calls[21] == 300


def test_execute_top_up_wave_matches_daily_limit_example_from_task(tmp_path):
    """Пример из задачи: daily_limit=24, из первой волны подтверждается 19
    (5 кандидатов "не успевают" вступить к моменту проверки), остаток 5
    добирается ОДНОЙ дополнительной волной — и только ею, без повторной
    проверки после неё."""
    db_path = _setup_db(tmp_path)
    total_users = 29
    for user_id in range(1, total_users + 1):
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
            session_path="acc1.session", daily_limit=24,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    # По last_seen_at DESC волна №1 (limit=24) выбирает user_id 29..6 (24
    # штук). 5 из них "не успевают" вступить к моменту проверки.
    not_yet_joined = {10, 11, 12, 13, 14}
    client_factory = _make_client_factory(
        participant_check_errors={
            "account_1": {
                user_id: UserNotParticipantError(request=GetHistoryRequest)
                for user_id in not_yet_joined
            },
        },
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        # 24 (волна №1) + 5 (волна добора — единственные оставшиеся
        # кандидаты, user_id 5..1, раз not_yet_joined уже "заняты" этим
        # прогоном через attempted_user_ids) = 29.
        assert len(invites) == 29
        joined_ids = {i.user_id for i in invites if i.status == "joined"}
        not_joined_ids = {i.user_id for i in invites if i.status == "not_joined"}
        pending_ids = {i.user_id for i in invites if i.status == "pending"}
        assert len(joined_ids) == 19
        # not_yet_joined — ДОСТОВЕРНО не вступили (UserNotParticipantError,
        # см. _verify_pending_invites) — их резерв в лимите освободился,
        # это и позволило волне добора отправить именно 5 новых (1..5).
        assert not_joined_ids == not_yet_joined
        # Волна добора (5 штук, user_id 1..5) остаётся неподтверждённой —
        # после неё проверка НЕ выполняется (см. задачу).
        assert pending_ids == {1, 2, 3, 4, 5}
        assert joined_ids == (set(range(6, 30)) - not_yet_joined)
    finally:
        invite_repository.close()


def test_execute_top_up_wave_does_not_repeat_if_still_short_after_it(tmp_path):
    """Даже если и после волны добора остаток лимита всё ещё > 0 (мало
    кандидатов) — вторая волна добора НЕ выполняется: максимум две волны
    приглашений за запуск (см. задачу)."""
    db_path = _setup_db(tmp_path)
    # Всего 2 кандидата, daily_limit=10 — после волны №1 (оба сразу
    # выбираются, поскольку это всё, что есть) и волны добора (которая не
    # найдёт вообще никого, раз кандидаты закончились) лимит всё ещё не
    # исчерпан, но третьей волны быть не должно.
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="account_1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=10,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    client = created_clients[0]
    # Волна №1 отправила оба существующих кандидата; волна добора не нашла
    # никого (пул пуст) — ровно 2 запроса всего, не больше.
    assert len(client.call_requests) == 2

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        assert len(invites) == 2
        assert all(i.status == "joined" for i in invites)
    finally:
        invite_repository.close()


def test_execute_peer_flood_during_main_wave_skips_verification_and_top_up(tmp_path, monkeypatch):
    """STOP_ACCOUNT (например PeerFlood) во время основной волны — ни
    ожидания, ни проверки pending, ни волны добора: аккаунт сразу
    завершается (см. _execute_account: stopped=True)."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="account_1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=10,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    client_factory = _make_client_factory(
        call_errors={"account_1": [PeerFloodError(request=GetHistoryRequest)]},
    )

    import reader.inviter.service as service_module

    sleep_calls: list = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    # Только "разогрев" — никакого ожидания перед проверкой pending
    # (60/300 сек.) не было вовсе.
    assert sleep_calls == [sleep_calls[0]]
    assert sleep_calls[0] not in (60, 300)

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        # Только первый (провалившийся) кандидат — второй не тронут
        # (обычная остановка аккаунта), волны добора не было.
        assert len(invites) == 1
        assert invites[0].status == "failed"
    finally:
        invite_repository.close()


# ---- выбор InviteToChannelRequest/AddChatUserRequest по типу target_entity ----


def test_execute_public_supergroup_uses_invite_to_channel_request(tmp_path):
    """client.get_entity('@username') для публичной супергруппы
    возвращает telethon.tl.types.Channel (megagroup=True) — именно для него
    должен строиться InviteToChannelRequest, а не AddChatUserRequest (баг из
    отчёта: AddChatUserRequest, отправленный для реальной супергруппы
    @tplgee, вызвал 'Invalid object ID for a user')."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    supergroup = Channel(
        id=1234567890, title="Test supergroup", photo=None, date=None,
        megagroup=True, access_hash=987654321,
    )
    created_clients: list = []
    client_factory = _make_client_factory(
        target_entities={account.name: supergroup}, created=created_clients,
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    client = created_clients[0]
    assert len(client.call_requests) == 1
    request = client.call_requests[0]
    assert isinstance(request, InviteToChannelRequest)
    assert not isinstance(request, AddChatUserRequest)
    assert request.channel is supergroup


def test_execute_channel_forbidden_still_uses_invite_to_channel_request(tmp_path):
    """ChannelForbidden — тоже Channel-семейство (не Chat): get_entity()
    может вернуть его, если у аккаунта нет полного доступа к каналу/
    супергруппе. Должен вести к тому же InviteToChannelRequest, а не
    AddChatUserRequest, который для Channel-семейства в принципе не
    подходит (см. _build_invite_request)."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    forbidden = ChannelForbidden(id=555, access_hash=111, title="Forbidden", megagroup=True)
    created_clients: list = []
    client_factory = _make_client_factory(
        target_entities={account.name: forbidden}, created=created_clients,
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    client = created_clients[0]
    assert len(client.call_requests) == 1
    assert isinstance(client.call_requests[0], InviteToChannelRequest)


def test_execute_basic_group_chat_uses_add_chat_user_request(tmp_path):
    """Обычный (не супергруппа) small group chat — telethon.tl.types.Chat —
    должен вести к AddChatUserRequest, не к InviteToChannelRequest."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    basic_group = Chat(id=42, title="Small group", photo=None, participants_count=5, date=None, version=0)
    created_clients: list = []
    client_factory = _make_client_factory(
        target_entities={account.name: basic_group}, created=created_clients,
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    client = created_clients[0]
    assert len(client.call_requests) == 1
    request = client.call_requests[0]
    assert isinstance(request, AddChatUserRequest)
    assert request.chat_id == 42


# ---- резолв кандидата ИМЕННО текущим аккаунтом (access_hash не переносится
# между разными Telegram-аккаунтами — см. отчёт "Invalid object ID for a user") ----


def test_execute_skips_username_resolution_when_candidate_already_known(tmp_path):
    """get_input_entity() успешна (кандидат уже известен этому аккаунту) и
    is_bot=False уже подтверждён при последней синхронизации (см.
    users.is_bot) — ни get_entity(username), ни дополнительная проверка
    is_bot (get_entity(input_peer), см. test_execute_verifies_bot_status_*)
    для НЕГО вызываться не должны, только get_entity для campaign.target_chat."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path, is_bot=False)  # access_hash=11, username="ivan"

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)  # get_input_entity успешна по умолчанию

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    client = created_clients[0]
    # Второй [1] — это get_input_entity() из проверки pending (см.
    # _verify_pending_invites), не из резолва перед отправкой.
    assert client.get_input_entity_calls == [1, 1]
    # get_entity вызывался только для target_chat, не для "@ivan" и не для
    # проверки is_bot (InputPeerUser).
    assert client.get_entity_calls == ["@target_chat"]
    assert len(client.call_requests) == 1

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        # pending -> joined: get_permissions() успешна по умолчанию (см.
        # _verify_pending_invites).
        assert invite_repository.list()[0].status == "joined"
    finally:
        invite_repository.close()


def test_execute_resolves_unknown_candidate_via_username_with_fresh_access_hash(tmp_path):
    """Кандидат не известен этому аккаунту (get_input_entity падает) — резолв
    через client.get_entity(username), и в реальный запрос идёт СВЕЖИЙ
    access_hash из этого резолва, а не устаревший из users.db (см. отчёт про
    "Invalid object ID for a user": access_hash получен ЧИТАЮЩИМ аккаунтом,
    не этим инвайтящим)."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)  # user_id=1, username="ivan", access_hash=11 (устаревший)

    fresh_entity = SimpleNamespace(id=1, access_hash=999999)  # то, что реально знает ЭТОТ аккаунт
    created_clients: list = []
    client_factory = _make_client_factory(
        get_input_entity_errors={"Основной": ValueError("не известен этому аккаунту")},
        entity_responses={"Основной": {"@ivan": fresh_entity}},
        created=created_clients,
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    client = created_clients[0]
    # Второй вызов — из проверки pending (см. _verify_pending_invites),
    # и падает по той же смоделированной причине (get_input_entity_errors
    # действует на КАЖДЫЙ вызов в этом фейке) — кандидат остаётся 'pending',
    # что не противоречит цели теста (он про СВЕЖИЙ access_hash в самом
    # запросе приглашения, а не про подтверждение вступления).
    assert client.get_input_entity_calls == [1, 1]
    assert "@ivan" in client.get_entity_calls  # резолвился именно по username

    assert len(client.call_requests) == 1
    request = client.call_requests[0]
    # AddChatUserRequest.user_id — InputPeerUser, построенный из СВЕЖЕГО
    # резолва (access_hash=999999), а не из устаревшего users.access_hash=11.
    assert request.user_id.access_hash == 999999
    assert request.user_id.access_hash != 11

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert invite_repository.list()[0].status == "pending"
    finally:
        invite_repository.close()


def test_execute_resolving_unknown_candidate_updates_users_db_access_hash(tmp_path):
    """Успешный get_entity(username) — сразу же обновить access_hash (и
    username, если он реально изменился) в users.db через
    UserRepository.update_access_hash(), не написав ни строчки SQL в
    service.py."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)  # user_id=1, username="ivan", access_hash=11 (устаревший)

    fresh_user = User(id=1, access_hash=999999, username="ivan")
    client_factory = _make_client_factory(
        get_input_entity_errors={"Основной": ValueError("не известен этому аккаунту")},
        entity_responses={"Основной": {"@ivan": fresh_user}},
    )
    user_repository = _FakeUserRepository()

    asyncio.run(
        _run_service(
            db_path, client_factory=client_factory, execute=True, user_repository=user_repository,
        )
    )

    assert user_repository.calls == [(1, 999999, "ivan", False)]


def test_execute_update_access_hash_failure_does_not_stop_invite(tmp_path, caplog):
    """Сбой записи в users.db (update_access_hash бросает исключение) —
    только warning в лог, само приглашение всё равно должно пройти."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    fresh_user = User(id=1, access_hash=999999, username="ivan")
    client_factory = _make_client_factory(
        get_input_entity_errors={"Основной": ValueError("не известен этому аккаунту")},
        entity_responses={"Основной": {"@ivan": fresh_user}},
    )
    user_repository = _FakeUserRepository(raise_error=RuntimeError("диск переполнен"))

    with caplog.at_level("WARNING", logger="reader.inviter.service"):
        asyncio.run(
            _run_service(
                db_path, client_factory=client_factory, execute=True, user_repository=user_repository,
            )
        )

    assert "Не удалось обновить access_hash" in caplog.text

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        # Приглашение всё равно прошло, несмотря на сбой записи access_hash
        # (get_input_entity всегда падает в этом фейке, поэтому проверка
        # pending тоже не резолвит кандидата — статус остаётся 'pending',
        # что не противоречит цели теста: она про сам факт отправки).
        assert invite_repository.list()[0].status == "pending"
    finally:
        invite_repository.close()


def test_execute_does_not_update_access_hash_when_candidate_already_known(tmp_path):
    """Кандидат уже известен этому аккаунту (get_input_entity успешна) и
    is_bot=False уже подтверждён (см. users.is_bot) — ни get_entity(username),
    ни проверка is_bot не вызываются вовсе, а значит и update_access_hash не
    должен вызываться (свежей сущности просто нет)."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path, is_bot=False)

    user_repository = _FakeUserRepository()

    asyncio.run(
        _run_service(
            db_path, client_factory=_make_client_factory(), execute=True,
            user_repository=user_repository,
        )
    )

    assert user_repository.calls == []


def test_execute_does_not_update_access_hash_for_non_user_entity(tmp_path):
    """get_entity(username) в принципе резолвит пользователя (User), но
    защита isinstance(entity, User) — на случай, если это когда-либо
    вернёт что-то другое: update_access_hash не должен вызываться."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    not_a_user = SimpleNamespace(id=1, access_hash=999999, username="ivan")
    client_factory = _make_client_factory(
        get_input_entity_errors={"Основной": ValueError("не известен этому аккаунту")},
        entity_responses={"Основной": {"@ivan": not_a_user}},
    )
    user_repository = _FakeUserRepository()

    asyncio.run(
        _run_service(
            db_path, client_factory=client_factory, execute=True, user_repository=user_repository,
        )
    )

    assert user_repository.calls == []


async def test_resolve_input_peer_raises_for_candidate_without_username(tmp_path):
    """select_candidates() теперь никогда не отдаёт кандидата без username
    (см. тесты test_select_candidates_excludes_users_without_username в
    tests/test_inviter_repository.py), поэтому эта ветка _resolve_input_peer
    недостижима через обычный run() — но остаётся защитой на случай, если
    InviteCandidate придёт сюда как-то иначе. Проверяем её напрямую, минуя
    выборку."""
    account_repository = TelegramAccountRepository(tmp_path / "users.db")
    campaign_repository = InviteCampaignRepository(tmp_path / "users.db")
    invite_repository = UserCampaignInviteRepository(tmp_path / "users.db")
    try:
        service = InviterService(
            account_repository, campaign_repository, invite_repository,
            client_factory=_make_client_factory(),
        )
        client = _FakeTelegramClient(
            SimpleNamespace(name="Основной"),
            get_input_entity_error=ValueError("не известен этому аккаунту"),
        )
        candidate = InviteCandidate(
            user_id=1, username=None, keywords=["осаго"], access_hash=11, last_seen_at=None,
        )

        import reader.inviter.service as service_module

        with pytest.raises(service_module._CandidateUnresolvableError):
            await service._resolve_input_peer(client, candidate)

        assert client.call_requests == []
    finally:
        account_repository.close()
        campaign_repository.close()
        invite_repository.close()


# ---- защита от приглашения Telegram-ботов (см. задачу об инциденте с ----
# ---- приглашением @Vlars_Bot и 3-дневным ограничением аккаунта) --------


async def _resolve_with_fake_service(candidate, client, user_repository=None):
    """Вызывает InviterService._resolve_input_peer() напрямую, минуя
    выборку кандидатов и полноценный run() — для точечной проверки самой
    проверки is_bot, без лишней инфраструктуры БД кампаний/аккаунтов."""
    import reader.inviter.service as service_module

    service = service_module.InviterService(
        account_repository=None, campaign_repository=None, invite_repository=None,
        client_factory=_make_client_factory(), user_repository=user_repository,
    )
    return await service._resolve_input_peer(client, candidate)


def test_resolve_input_peer_raises_immediately_for_known_bot_without_any_rpc(tmp_path):
    """candidate.is_bot=True (не ожидается — уже отсекается на этапе SQL,
    см. _CANDIDATES_BASE_WHERE — но на случай гонки/устаревшей выборки) —
    приглашение отменяется без единого RPC-вызова вовсе."""
    client = _FakeTelegramClient(SimpleNamespace(name="Основной"))
    candidate = InviteCandidate(
        user_id=1, username="ivan", keywords=["осаго"], access_hash=11,
        last_seen_at=None, is_bot=True,
    )

    with pytest.raises(_CandidateIsBotError):
        asyncio.run(_resolve_with_fake_service(candidate, client))

    assert client.get_input_entity_calls == []
    assert client.get_entity_calls == []


def test_resolve_input_peer_skips_bot_check_when_already_confirmed_not_bot(tmp_path):
    """candidate.is_bot=False (уже подтверждён Telethon при последней
    синхронизации) — дополнительный get_entity(input_peer) не нужен."""
    client = _FakeTelegramClient(SimpleNamespace(name="Основной"))
    candidate = InviteCandidate(
        user_id=1, username="ivan", keywords=["осаго"], access_hash=11,
        last_seen_at=None, is_bot=False,
    )

    input_peer = asyncio.run(_resolve_with_fake_service(candidate, client))

    assert input_peer.user_id == 1
    assert client.get_entity_calls == []


def test_resolve_input_peer_verifies_unknown_status_and_raises_for_confirmed_bot(tmp_path):
    """candidate.is_bot=None (статус неизвестен) и get_input_entity успешна
    (кандидат известен этому аккаунту локально) — обязательная живая
    проверка get_entity(input_peer) должна произойти, и, если она
    подтверждает User.bot=True, приглашение отменяется ДО единого
    InviteToChannelRequest/AddChatUserRequest, а is_bot=1 сохраняется в
    users.db (через update_access_hash, у которого уже есть access_hash)."""
    client = _FakeTelegramClient(
        SimpleNamespace(name="Основной"),
        entity_responses={1: User(id=1, access_hash=11, username="vlars_bot", bot=True)},
    )
    candidate = InviteCandidate(
        user_id=1, username="vlars_bot", keywords=["осаго"], access_hash=11,
        last_seen_at=None, is_bot=None,
    )
    user_repository = _FakeUserRepository()

    with pytest.raises(_CandidateIsBotError):
        asyncio.run(_resolve_with_fake_service(candidate, client, user_repository=user_repository))

    assert client.get_input_entity_calls == [1]
    assert len(client.get_entity_calls) == 1  # проверка бота, не username-резолв
    assert user_repository.calls == [(1, 11, "vlars_bot", True)]


def test_resolve_input_peer_persists_confirmed_non_bot_status(tmp_path):
    """candidate.is_bot=None — после подтверждения User.bot=False
    приглашение продолжается как обычно, а свежий статус (False, а не
    только True) сохраняется в users.db, чтобы со временем NULL исчезали и
    повторная проверка больше не требовалась (см. test_resolve_input_peer_
    skips_bot_check_when_already_confirmed_not_bot)."""
    client = _FakeTelegramClient(
        SimpleNamespace(name="Основной"),
        entity_responses={1: User(id=1, access_hash=11, username="ivan", bot=False)},
    )
    candidate = InviteCandidate(
        user_id=1, username="ivan", keywords=["осаго"], access_hash=11,
        last_seen_at=None, is_bot=None,
    )
    user_repository = _FakeUserRepository()

    input_peer = asyncio.run(
        _resolve_with_fake_service(candidate, client, user_repository=user_repository)
    )

    assert input_peer.user_id == 1
    assert user_repository.calls == [(1, 11, "ivan", False)]


def test_execute_skips_confirmed_bot_before_sending_invite_request(tmp_path):
    """Кандидат с is_bot=NULL в users.db, который Telegram подтверждает
    ботом при живой проверке (см. _resolve_input_peer) — НИ ОДНОГО
    InviteToChannelRequest/AddChatUserRequest не отправляется вовсе,
    записывается status='invalid', is_bot=1 сохраняется в users.db, и
    аккаунт продолжает обработку ОСТАЛЬНЫХ кандидатов (это не PeerFlood/
    ChatAdminRequired — риска для аккаунта здесь нет). Настоящий
    UserRepository (не фейк) — чтобы is_bot=1 реально попал в users.db и
    не дал волне добора (см. _execute_account) повторно выбрать того же
    "бота", раз status='invalid' сам по себе не исключается из будущей
    выборки."""
    db_path = _setup_db(tmp_path)
    # По last_seen_at DESC: 2 (бот, is_bot=NULL), 1 (обычный, is_bot=False
    # — уже подтверждён, чтобы не отвлекать проверку лишним вызовом).
    _seed_user(db_path, 2, username="vlars_bot", keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))
    _seed_user(db_path, 1, username="ivan", keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME, is_bot=False)

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
    client_factory = _make_client_factory(
        entity_responses={"Основной": {2: User(id=2, access_hash=2, username="vlars_bot", bot=True)}},
        created=created_clients,
    )
    user_repository = UserRepository(db_path)

    try:
        asyncio.run(
            _run_service(
                db_path, client_factory=client_factory, execute=True, user_repository=user_repository,
            )
        )

        client = created_clients[0]
        # Ровно один InviteToChannelRequest/AddChatUserRequest — для
        # кандидата 1, не для бота 2 (и волна добора не переигрывает его,
        # раз is_bot=1 реально сохранён).
        assert len(client.call_requests) == 1

        invite_repository = UserCampaignInviteRepository(db_path)
        try:
            invites = {i.user_id: i for i in invite_repository.list()}
            assert invites[2].status == "invalid"
            assert invites[1].status == "joined"
        finally:
            invite_repository.close()

        assert user_repository.get(2).is_bot is True
    finally:
        user_repository.close()


def test_execute_marks_bot_via_rpc_error_as_defense_in_depth(tmp_path):
    """Даже если проактивная проверка почему-то не сработала (кандидат уже
    известен ЭТОМУ аккаунту и is_bot=False был неверно подтверждён раньше
    — здесь смоделировано напрямую через RPC-ошибку при самой отправке) —
    Telegram-специфичная RPC-ошибка (UserBotError) при самой отправке
    приглашения тоже должна сохранить is_bot=1 в users.db (через
    mark_as_bot — полноценного entity здесь уже нет) и не останавливать
    аккаунт (это SKIP_USER, а не STOP_ACCOUNT). Настоящий UserRepository
    (не фейк) — чтобы is_bot=1 реально попал в users.db и не дал волне
    добора (см. _execute_account) повторно выбрать того же "бота", раз
    status='invalid' сам по себе не исключается из будущей выборки."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 2, username="vlars_bot", keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1), is_bot=False)
    _seed_user(db_path, 1, username="ivan", keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME, is_bot=False)

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

    client_factory = _make_client_factory(
        call_errors={"Основной": [UserBotError(request=GetHistoryRequest), None]},
    )
    user_repository = UserRepository(db_path)

    try:
        asyncio.run(
            _run_service(
                db_path, client_factory=client_factory, execute=True, user_repository=user_repository,
            )
        )

        invite_repository = UserCampaignInviteRepository(db_path)
        try:
            invites = {i.user_id: i for i in invite_repository.list()}
            assert invites[2].status == "invalid"
            # Аккаунт НЕ остановился — второй кандидат тоже обработан и
            # подтверждён (get_permissions успешна по умолчанию).
            assert invites[1].status == "joined"
        finally:
            invite_repository.close()

        assert user_repository.get(2).is_bot is True
    finally:
        user_repository.close()



def test_execute_flood_wait_during_resolution_handled_like_during_send(tmp_path, monkeypatch):
    """FloodWaitError, полученный при резолве кандидата (get_entity(username)),
    обрабатывается точно так же, как при самой отправке приглашения:
    запись failed, ожидание exc.seconds, продолжение работы (< 300 сек)."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)  # user_id=1, username="ivan"

    client_factory = _make_client_factory(
        get_input_entity_errors={"Основной": ValueError("не известен этому аккаунту")},
        entity_responses={"Основной": {"@ivan": FloodWaitError(request=GetHistoryRequest, capture=42)}},
    )

    import reader.inviter.service as service_module

    sleep_calls: list = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    assert 42 in sleep_calls  # дождались ровно как при FloodWait на отправке

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invite = invite_repository.list()[0]
        assert invite.status == "failed"
        assert invite.error == str(FloodWaitError(request=GetHistoryRequest, capture=42))
    finally:
        invite_repository.close()


def test_execute_peer_flood_during_resolution_stops_account(tmp_path):
    """PeerFloodError, полученный при резолве кандидата (get_entity(username)),
    останавливает аккаунт точно так же, как при самой отправке приглашения."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="ivan", keywords=["осаго"], access_hash=11, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, username="petr", keywords=["осаго"], access_hash=22, last_seen_at=_BASE_TIME + timedelta(days=1))

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

    notifier = _FakeOperatorNotifier()
    created_clients: list = []
    client_factory = _make_client_factory(
        get_input_entity_errors={"account_2": ValueError("не известен этому аккаунту")},
        entity_responses={"account_2": {"@petr": PeerFloodError(request=GetHistoryRequest)}},
        created=created_clients,
    )

    asyncio.run(
        _run_service(db_path, client_factory=client_factory, execute=True, notifier=notifier)
    )

    client = created_clients[0]
    # petr (первый по last_seen_at DESC) вызвал PeerFlood при резолве —
    # ivan вообще не тронут.
    assert client.call_requests == []
    assert client.disconnected is True

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = {i.user_id: i for i in invite_repository.list()}
        assert invites[2].status == "failed"
        assert 1 not in invites
    finally:
        invite_repository.close()

    stop_notifications = [m for m in notifier.sent if m.startswith("⚠️")]
    assert len(stop_notifications) == 1
    assert "Telegram временно ограничил приглашения (Too many requests)." in stop_notifications[0]


def test_execute_creates_failed_record_on_rpc_error(tmp_path):
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    # UserPrivacyRestrictedError — пример SKIP_USER-ошибки, касающейся
    # только этого кандидата (см. _classify_invite_error). PeerFloodError/
    # ChatAdminRequiredError и т.п. останавливают аккаунт — см.
    # test_execute_peer_flood_stops_account_and_notifies_operator/
    # test_execute_chat_admin_required_stops_account_and_notifies_operator.
    client_factory = _make_client_factory(
        call_errors={"Основной": [UserPrivacyRestrictedError(request=GetHistoryRequest)]},
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
        assert invite.error == str(UserPrivacyRestrictedError(request=GetHistoryRequest))
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
    # (см. _pause_between_invites) — в диапазоне [20, 60), [3] — ожидание
    # перед проверкой pending (см. _wait_before_verifying_pending) — 60
    # сек., отправлен всего 1 (< 20). Волна добора после проверки не находит
    # новых кандидатов (user 2 уже обработан этим прогоном, user 1 —
    # подтверждён), поэтому дополнительных sleep() нет.
    assert len(sleep_calls) == 4
    assert 5 <= sleep_calls[0] < 15
    assert sleep_calls[1] == 7
    assert 20 <= sleep_calls[2] < 60
    assert sleep_calls[3] == 60

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = {i.user_id: i for i in invite_repository.list()}
        assert invites[2].status == "failed"
        assert invites[2].error == str(FloodWaitError(request=GetHistoryRequest, capture=7))
        assert invites[2].invited_at is None

        assert invites[1].status == "joined"
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
        # достигнута: status='joined' (подтверждённый участник — Telegram
        # сказал это прямо, см. _classify_invite_error), а не 'failed'/'pending'.
        assert invites[0].status == "joined"
        assert invites[0].verified_at is not None
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

    # По last_seen_at DESC: 1 (успех), 2 (уже участник), 3 (обычная ошибка,
    # касающаяся только этого кандидата — SKIP_USER, см.
    # _classify_invite_error — не PeerFlood/FloodWait/ChatAdminRequired и
    # т.п., у которых теперь особое поведение остановки аккаунта, см.
    # test_execute_peer_flood_stops_account_and_notifies_operator).
    client_factory = _make_client_factory(
        call_errors={"account_1": [None, UserAlreadyParticipantError(request=GetHistoryRequest), UserPrivacyRestrictedError(request=GetHistoryRequest)]},
    )
    notifier = _FakeOperatorNotifier()

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True, notifier=notifier))

    # Ровно два уведомления: по аккаунту и итоговое по кампании, в этом порядке.
    assert len(notifier.sent) == 2
    account_message, campaign_message = notifier.sent

    # user 1 отправлен и подтверждён (get_permissions успешна по
    # умолчанию), user 2 подтверждён немедленно (UserAlreadyParticipantError),
    # итого "Подтверждено" = 2, "Отправлено" = 1 (только реальная отправка,
    # UserAlreadyParticipantError в неё не считается).
    assert "📨 Кампания: ОСАГО" in account_message
    assert "👤 Аккаунт: account_1" in account_message
    assert "📤 Отправлено приглашений: 1" in account_message
    assert "✅ Подтверждено участников: 2" in account_message
    assert "⏳ Ожидают подтверждения: 0" in account_message
    assert "🚫 Недоступны (invalid): 0" in account_message
    assert "❌ Ошибок: 1" in account_message
    # 3-й кандидат получил UserPrivacyRestrictedError (status='failed') —
    # не исключается из будущей выборки, поэтому остаётся кандидатом.
    assert "Осталось кандидатов: 1" in account_message

    assert '📊 Итоги кампании "ОСАГО"' in campaign_message
    assert "Аккаунтов обработано: 1" in campaign_message
    assert "📤 Отправлено приглашений: 1" in campaign_message
    assert "✅ Подтверждено участников: 2" in campaign_message
    assert "⏳ Ожидают подтверждения: 0" in campaign_message
    assert "🚫 Недоступны: 0" in campaign_message
    assert "❌ Ошибок: 1" in campaign_message
    assert "Осталось кандидатов: 1" in campaign_message


def test_execute_campaign_notification_includes_found_vs_processable_summary(tmp_path):
    """Итоговое уведомление по кампании должно показывать "Всего найдено"
    (без фильтра по username), "Будет обработано" (с ним, как раньше
    count_candidates()) и "без username" — разницу между ними."""
    db_path = _setup_db(tmp_path)
    # 2 без username (не подходят для приглашения вовсе), 1 с username.
    _seed_user(db_path, 1, username=None, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, username="", keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))
    _seed_user(db_path, 3, username="ivan", keywords=["осаго"], access_hash=3, last_seen_at=_BASE_TIME + timedelta(days=2))

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

    notifier = _FakeOperatorNotifier()
    client_factory = _make_client_factory()

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True, notifier=notifier))

    assert len(notifier.sent) == 2
    _account_message, campaign_message = notifier.sent

    assert "📋 Кампания: ОСАГО" in campaign_message
    # 3 подходят по keyword/access_hash, но только 1 (ivan) — с username.
    assert "👥 Всего найдено: 3" in campaign_message
    assert "✅ Будет обработано: 1" in campaign_message
    assert "🚫 Пропущено:" in campaign_message
    assert "• без username: 2" in campaign_message

    # Существующие поля отчёта остаются на месте, не заменены новыми.
    assert '📊 Итоги кампании "ОСАГО"' in campaign_message
    assert "✅ Подтверждено участников: 1" in campaign_message


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
    assert "✅ Подтверждено участников: 1" in account_message
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
    # account_2 (кандидаты 2,1): один SKIP_USER-ошибка (не про сам аккаунт,
    # см. _classify_invite_error), один успешен.
    client_factory = _make_client_factory(
        call_errors={
            "account_1": [None, None],
            "account_2": [UserPrivacyRestrictedError(request=GetHistoryRequest), None],
        },
    )
    notifier = _FakeOperatorNotifier()

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True, notifier=notifier))

    # 2 уведомления по аккаунтам + 1 итоговое по кампании.
    assert len(notifier.sent) == 3
    campaign_message = notifier.sent[-1]

    assert "Аккаунтов обработано: 2" in campaign_message
    assert "📤 Отправлено приглашений: 3" in campaign_message  # 2 (account_1) + 1 (account_2)
    assert "✅ Подтверждено участников: 3" in campaign_message
    assert "⏳ Ожидают подтверждения: 0" in campaign_message
    assert "❌ Ошибок: 1" in campaign_message
    # Кандидат с UserPrivacyRestrictedError (status='failed') не
    # исключается из будущей выборки — остаётся ровно один "оставшийся"
    # кандидат.
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
        assert invites[0].status == "joined"
    finally:
        invite_repository.close()

    # Уведомление было ПОПЫТАНО (иначе сбой было бы неоткуда взять).
    assert len(notifier.sent) >= 1


# ---- баг: итоговая статистика по кампании пропадала целиком, если у -----
# ---- OperatorNotifier не было получателей (или он вообще отсутствовал) --


def test_execute_campaign_summary_logged_and_sent_when_notifier_succeeds(tmp_path, caplog):
    """Уведомитель успешно отправляет — итоговый отчёт по кампании и
    попадает в лог (logger.info), и реально уходит оператору, и это ровно
    один и тот же текст (см. _safe_notify)."""
    db_path = _setup_db(tmp_path)
    _setup_single_candidate_campaign(db_path)

    notifier = _FakeOperatorNotifier()

    with caplog.at_level("INFO", logger="reader.inviter.service"):
        asyncio.run(
            _run_service(
                db_path, client_factory=_make_client_factory(), execute=True, notifier=notifier,
            )
        )

    campaign_notifications = [m for m in notifier.sent if "Итоги кампании" in m]
    assert len(campaign_notifications) == 1
    assert campaign_notifications[0] in caplog.text


def test_execute_campaign_summary_logged_when_no_recipients_found(tmp_path, caplog):
    """Уведомитель поднят, но получателей нет — OperatorNotifier.notify_text()
    возвращает False (не бросает исключение), как настоящий "Нет ни
    одного получателя уведомлений оператора" (см. задачу про баг) —
    итоговый отчёт по кампании всё равно должен появиться в логе, а не
    потеряться целиком."""
    db_path = _setup_db(tmp_path)
    _setup_single_candidate_campaign(db_path)

    notifier = _FakeOperatorNotifier(deliver=False)

    with caplog.at_level("INFO", logger="reader.inviter.service"):
        asyncio.run(
            _run_service(
                db_path, client_factory=_make_client_factory(), execute=True, notifier=notifier,
            )
        )

    # Отправка была ПОПЫТАНА (иначе не узнали бы, что доставки не было) —
    # и по аккаунту, и по кампании.
    assert len(notifier.sent) == 2
    assert "Итоги кампании" in caplog.text
    assert "не доставлено" in caplog.text


def test_execute_campaign_summary_logged_when_notifier_absent(tmp_path, caplog):
    """notifier=None (main.py не смог поднять OperatorNotifier) — итоговый
    отчёт по кампании всё равно должен появиться в логе."""
    db_path = _setup_db(tmp_path)
    _setup_single_candidate_campaign(db_path)

    with caplog.at_level("INFO", logger="reader.inviter.service"):
        asyncio.run(_run_service(db_path, client_factory=_make_client_factory(), execute=True))

    assert "Итоги кампании" in caplog.text


def test_execute_campaign_summary_logged_when_notifier_raises(tmp_path, caplog):
    """Уведомитель бросает исключение при отправке — процесс успешно
    завершается (без падения и без потери уже созданных записей), а
    итоговый отчёт по кампании всё равно остаётся в логе."""
    db_path = _setup_db(tmp_path)
    _setup_single_candidate_campaign(db_path)

    notifier = _FakeOperatorNotifier(raise_error=RuntimeError("сеть недоступна"))

    with caplog.at_level("INFO", logger="reader.inviter.service"):
        asyncio.run(
            _run_service(
                db_path, client_factory=_make_client_factory(), execute=True, notifier=notifier,
            )
        )

    assert "Итоги кампании" in caplog.text

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert len(invite_repository.list()) == 1
    finally:
        invite_repository.close()


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


@pytest.mark.parametrize(
    "exc,expected",
    [
        (
            FloodWaitError(request=GetHistoryRequest, capture=624),
            "Telegram требует подождать 624 секунд.",
        ),
        (
            UserChannelsTooMuchError(request=GetHistoryRequest),
            "Пользователь состоит в слишком большом количестве групп.",
        ),
        (
            UserPrivacyRestrictedError(request=GetHistoryRequest),
            "Пользователь запретил приглашения.",
        ),
        (
            UserAlreadyParticipantError(request=GetHistoryRequest),
            "Уже состоит в группе.",
        ),
        (
            ChatAdminRequiredError(request=GetHistoryRequest),
            "У аккаунта нет прав приглашать участников.",
        ),
        (
            PeerFloodError(request=GetHistoryRequest),
            "Telegram временно ограничил приглашения (Too many requests).",
        ),
    ],
)
def test_humanize_error_maps_known_rpc_errors_to_readable_text(exc, expected):
    assert _humanize_error(exc) == expected


def test_humanize_error_falls_back_to_str_for_unmapped_exception():
    exc = ValueError("что-то неожиданное")
    assert _humanize_error(exc) == str(exc)


# ---- единый классификатор ошибок приглашения (_classify_invite_error) ----


@pytest.mark.parametrize(
    "exc,expected_action,expected_db_status,expected_stat_field",
    [
        (
            UserAlreadyParticipantError(request=GetHistoryRequest),
            InviteErrorAction.SKIP_USER, "joined", "joined",
        ),
        (
            _CandidateIsBotError("42 — известный Telegram-бот"),
            InviteErrorAction.SKIP_USER, "invalid", "invalid",
        ),
        (
            _CandidateUnresolvableError("42 не известен и без username"),
            InviteErrorAction.SKIP_USER, "failed", "errors",
        ),
        (
            UserPrivacyRestrictedError(request=GetHistoryRequest),
            InviteErrorAction.SKIP_USER, "failed", "errors",
        ),
        (
            UserChannelsTooMuchError(request=GetHistoryRequest),
            InviteErrorAction.SKIP_USER, "failed", "errors",
        ),
        (
            UserKickedError(request=GetHistoryRequest),
            InviteErrorAction.SKIP_USER, "failed", "errors",
        ),
        (
            UserBotError(request=GetHistoryRequest),
            InviteErrorAction.SKIP_USER, "invalid", "invalid",
        ),
        (
            FloodWaitError(request=GetHistoryRequest, capture=7),
            InviteErrorAction.RETRY_LATER, "failed", None,
        ),
        (
            FloodWaitError(request=GetHistoryRequest, capture=624),
            InviteErrorAction.STOP_ACCOUNT, "failed", None,
        ),
        (
            PeerFloodError(request=GetHistoryRequest),
            InviteErrorAction.STOP_ACCOUNT, "failed", "errors",
        ),
        (
            ChatAdminRequiredError(request=GetHistoryRequest),
            InviteErrorAction.STOP_ACCOUNT, "failed", "errors",
        ),
        (
            ChatWriteForbiddenError(request=GetHistoryRequest),
            InviteErrorAction.STOP_ACCOUNT, "failed", "errors",
        ),
        (
            # Никогда явно не классифицированный RPCError — по умолчанию
            # STOP_ACCOUNT ("если есть сомнения — лучше остановить").
            RPCError(request=GetHistoryRequest, message="SOME_UNKNOWN_ERROR", code=400),
            InviteErrorAction.STOP_ACCOUNT, "failed", "errors",
        ),
        (
            ValueError("совсем не Telegram RPC-ошибка"),
            InviteErrorAction.FATAL, "failed", "errors",
        ),
    ],
)
def test_classify_invite_error_returns_expected_action_and_bookkeeping(
    exc, expected_action, expected_db_status, expected_stat_field,
):
    classification = _classify_invite_error(exc)
    assert classification.action == expected_action
    assert classification.db_status == expected_db_status
    assert classification.stat_field == expected_stat_field


def test_classify_invite_error_flood_wait_carries_wait_seconds():
    classification = _classify_invite_error(FloodWaitError(request=GetHistoryRequest, capture=42))
    assert classification.wait_seconds == 42


def test_classify_invite_error_marks_bot_error_types_for_persisting():
    """UserBotError (RPC, "живой" ответ Telegram уже ПОСЛЕ попытки
    приглашения) должен запомнить is_bot=1 — в отличие от
    _CandidateIsBotError, который срабатывает ДО отправки (см.
    InviterService._resolve_input_peer) и статус уже сохранён там."""
    assert _classify_invite_error(UserBotError(request=GetHistoryRequest)).mark_as_bot is True
    assert _classify_invite_error(_CandidateIsBotError("бот")).mark_as_bot is False


def test_classify_invite_error_stop_account_has_operator_message():
    """STOP_ACCOUNT/FATAL — единственные случаи, где нужен человекочитаемый
    operator_message (см. _format_account_stopped_notification); для
    SKIP_USER/RETRY_LATER он не используется и остаётся пустым."""
    stop = _classify_invite_error(PeerFloodError(request=GetHistoryRequest))
    assert stop.operator_message == "Telegram временно ограничил приглашения (Too many requests)."

    skip = _classify_invite_error(UserPrivacyRestrictedError(request=GetHistoryRequest))
    assert skip.operator_message == ""


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
    assert "📤 Отправлено приглашений: 1" in account_message
    assert "✅ Подтверждено участников: 1" in account_message
    assert "⏳ Ожидают подтверждения: 0" in account_message
    assert "🚫 Недоступны (invalid): 0" in account_message
    assert "❌ Ошибок: 0" in account_message
    assert "Осталось кандидатов: 0" in account_message

    assert '📊 Итоги кампании "ОСАГО"' in campaign_message
    assert "Аккаунтов обработано: 1" in campaign_message
    assert "📤 Отправлено приглашений: 1" in campaign_message
    assert "✅ Подтверждено участников: 1" in campaign_message
    assert "⏳ Ожидают подтверждения: 0" in campaign_message
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

    # По last_seen_at DESC: 3 (успех), 2 (уже участник), 1 (обычная
    # SKIP_USER-ошибка, не про сам аккаунт — см. _classify_invite_error).
    client_factory = _make_client_factory(
        call_errors={
            "Основной": [
                None,
                UserAlreadyParticipantError(request=GetHistoryRequest),
                UserPrivacyRestrictedError(request=GetHistoryRequest),
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

    # sleep_calls[0] — разогрев (7.0), следующие три — по одной паузе на
    # каждого из трёх кандидатов, независимо от исхода, [4] — ожидание
    # перед проверкой pending (см. _wait_before_verifying_pending) — 60
    # сек., отправлен всего 1 (< 20).
    assert sleep_calls[0] == 7.0
    assert sleep_calls[1:4] == [55.5, 55.5, 55.5]
    assert sleep_calls[4] == 60
    assert len(sleep_calls) == 5


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
    # дальше — по одной паузе на каждого из двух кандидатов, и в конце —
    # ожидание перед проверкой pending (см. _wait_before_verifying_pending) —
    # 60 сек., отправлено 2 (< 20).
    assert sleep_calls[0] == 10.0
    assert sleep_calls[1:3] == [30.0, 30.0]
    assert sleep_calls[3] == 60
    assert len(sleep_calls) == 4


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
        # В БД — оригинальный текст исключения, а не человекочитаемый: он
        # предназначен для диагностики, а не для оператора.
        assert invites[0].error == str(PeerFloodError(request=GetHistoryRequest))
        assert invites[0].error != "Telegram временно ограничил приглашения (Too many requests)."
    finally:
        invite_repository.close()

    stop_notifications = [m for m in notifier.sent if m.startswith("⚠️")]
    assert len(stop_notifications) == 1
    assert "Аккаунт: account_2" in stop_notifications[0]
    assert "Telegram временно ограничил приглашения (Too many requests)." in stop_notifications[0]
    assert "Работа аккаунта остановлена." in stop_notifications[0]
    assert "Переход к следующему аккаунту." in stop_notifications[0]


def test_execute_chat_admin_required_stops_account_and_notifies_operator(tmp_path):
    """ChatAdminRequiredError теперь останавливает аккаунт (как PeerFlood),
    а не просто пропускает кандидата — именно эта ошибка на попытке
    пригласить Telegram-бота предшествовала реальному 3-дневному
    ограничению приглашений у аккаунта (см. задачу об инциденте с
    @Vlars_Bot и единый классификатор _classify_invite_error)."""
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
        call_errors={"account_2": [ChatAdminRequiredError(request=GetHistoryRequest)]},
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
        assert invites[0].error == str(ChatAdminRequiredError(request=GetHistoryRequest))
    finally:
        invite_repository.close()

    stop_notifications = [m for m in notifier.sent if m.startswith("⚠️")]
    assert len(stop_notifications) == 1
    assert "Аккаунт: account_2" in stop_notifications[0]
    assert "У аккаунта нет прав приглашать участников." in stop_notifications[0]
    assert "Работа аккаунта остановлена." in stop_notifications[0]
    assert "Переход к следующему аккаунту." in stop_notifications[0]


def test_execute_unrecognized_rpc_error_stops_account_by_default(tmp_path):
    """Ошибка Telethon (RPCError), которую _classify_invite_error не
    распознал явно (не входит ни в один из известных типов) — по
    умолчанию STOP_ACCOUNT, а не "пропустить и продолжить" — таково
    условие задачи: если есть сомнения, лучше остановить аккаунт."""
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
        call_errors={
            "account_2": [RPCError(request=GetHistoryRequest, message="SOME_UNKNOWN_ERROR", code=400)],
        },
        created=created_clients,
    )
    notifier = _FakeOperatorNotifier()

    asyncio.run(
        _run_service(db_path, client_factory=client_factory, execute=True, notifier=notifier)
    )

    client = created_clients[0]
    assert len(client.call_requests) == 1  # второй кандидат не тронут

    stop_notifications = [m for m in notifier.sent if m.startswith("⚠️")]
    assert len(stop_notifications) == 1
    assert "Аккаунт: account_2" in stop_notifications[0]


def test_execute_unexpected_non_rpc_exception_is_fatal_and_stops_account(tmp_path, caplog):
    """Исключение, которое даже не RPCError (например, ошибка внутри самого
    Telethon/сети, никак не относящаяся к известным Telegram-ошибкам) —
    FATAL: тоже останавливает аккаунт (см. STOP_ACCOUNT), но логируется с
    трассировкой (exc_info), чтобы отличать "непонятно что случилось" от
    распознанного риска."""
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
        call_errors={"account_2": [RuntimeError("совсем не Telegram RPC-ошибка")]},
        created=created_clients,
    )
    notifier = _FakeOperatorNotifier()

    with caplog.at_level("WARNING"):
        asyncio.run(
            _run_service(db_path, client_factory=client_factory, execute=True, notifier=notifier)
        )

    client = created_clients[0]
    assert len(client.call_requests) == 1  # второй кандидат не тронут

    stop_notifications = [m for m in notifier.sent if m.startswith("⚠️")]
    assert len(stop_notifications) == 1
    assert "Аккаунт: account_2" in stop_notifications[0]

    fatal_records = [r for r in caplog.records if r.exc_info is not None]
    assert len(fatal_records) == 1


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
        invites = invite_repository.list()
        assert len(invites) == 1
        # В БД — оригинальный (технический) текст, а не человекочитаемый.
        assert invites[0].error == str(FloodWaitError(request=GetHistoryRequest, capture=624))
        assert invites[0].error != "Telegram требует подождать 624 секунд."
    finally:
        invite_repository.close()

    stop_notifications = [m for m in notifier.sent if m.startswith("⚠️")]
    assert len(stop_notifications) == 1
    assert "Аккаунт: account_2" in stop_notifications[0]
    assert "Telegram требует подождать 624 секунд." in stop_notifications[0]
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
    # (см. _pause_between_invites); [3] — ожидание перед проверкой pending
    # (см. _wait_before_verifying_pending) — 60 сек., отправлен всего 1
    # (< 20).
    assert len(sleep_calls) == 4
    assert 5 <= sleep_calls[0] < 15
    assert sleep_calls[1] == 299
    assert 20 <= sleep_calls[2] < 60
    assert sleep_calls[3] == 60

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


# ---- blocked_until: активный FloodWait самого Telegram блокирует аккаунт ----


def test_execute_flood_wait_persists_blocked_until_for_five_hours(tmp_path):
    """FloodWaitError(seconds=5*3600) — account.blocked_until сохраняется
    как now + 5 часов (см. задачу про повторный FloodWait у @Mihailov_vm),
    blocked_reason='flood_wait'."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    five_hours = 5 * 3600
    client_factory = _make_client_factory(
        call_errors={account.name: [FloodWaitError(request=GetHistoryRequest, capture=five_hours)]},
    )

    before = datetime.now(timezone.utc)
    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))
    after = datetime.now(timezone.utc)

    account_repository = TelegramAccountRepository(db_path)
    try:
        updated = account_repository.get(account.id)
        assert updated.blocked_reason == "flood_wait"
        assert updated.blocked_until is not None
        assert before + timedelta(seconds=five_hours) <= updated.blocked_until
        assert updated.blocked_until <= after + timedelta(seconds=five_hours)
    finally:
        account_repository.close()


def test_execute_flood_wait_mid_wave_stops_account_and_persists_block(tmp_path):
    """FloodWaitError, полученный ПРИ отправке приглашения (основная
    волна) — останавливает текущий аккаунт немедленно (второй кандидат не
    трогается) и сохраняет blocked_until, как и любой другой FloodWaitError."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account = account_repository.create(
            name="account_flood", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(
        call_errors={"account_flood": [FloodWaitError(request=GetHistoryRequest, capture=624)]},
        created=created_clients,
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    client = created_clients[0]
    assert len(client.call_requests) == 1  # второй кандидат не тронут

    account_repository = TelegramAccountRepository(db_path)
    try:
        updated = account_repository.get(account.id)
        assert updated.blocked_until is not None
        assert updated.blocked_until > datetime.now(timezone.utc)
    finally:
        account_repository.close()


# ---- PeerFloodError ("Too many requests"): fallback-кулдаун (см. задачу
# про account.blocked_until, оставшийся в прошлом после "Too many requests") ----


def test_execute_peer_flood_sets_future_blocked_until_with_peer_flood_reason(tmp_path):
    """До исправления PeerFloodError вообще не обновлял blocked_until
    (wait_seconds было None) — аккаунт снова считался свободным уже на
    следующем тике. Теперь — конкретное будущее время и отдельная причина
    ('peer_flood', не 'flood_wait' — Telegram не сообщал длительность,
    это fallback, см. _PEER_FLOOD_COOLDOWN_SECONDS), и это сохраняется
    именно в БД (перечитано ОТДЕЛЬНЫМ репозиторием, не тот же объект)."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    client_factory = _make_client_factory(
        call_errors={account.name: [PeerFloodError(request=GetHistoryRequest)]},
    )

    before = datetime.now(timezone.utc)
    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    account_repository = TelegramAccountRepository(db_path)
    try:
        updated = account_repository.get(account.id)
        assert updated.blocked_reason == "peer_flood"
        assert updated.blocked_until is not None
        assert updated.blocked_until > before  # строго в будущем, а не в прошлом
    finally:
        account_repository.close()


def test_execute_peer_flood_replaces_stale_past_blocked_until(tmp_path):
    """Воспроизводит production-сценарий буквально: у аккаунта УЖЕ есть
    blocked_until в прошлом (оставшийся от давнего, давно истёкшего
    FloodWait — именно так и получались "странные" прошлые даты в логе).
    Новый PeerFloodError должен заменить это старое прошлое значение
    новым будущим, а не оставить БД нетронутой."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    stale_blocked_until = datetime.now(timezone.utc) - timedelta(days=2)
    account_repository = TelegramAccountRepository(db_path)
    try:
        account_repository.update(
            account.id, blocked_until=stale_blocked_until, blocked_reason="flood_wait",
        )
    finally:
        account_repository.close()

    client_factory = _make_client_factory(
        call_errors={account.name: [PeerFloodError(request=GetHistoryRequest)]},
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    account_repository = TelegramAccountRepository(db_path)
    try:
        updated = account_repository.get(account.id)
        assert updated.blocked_until > datetime.now(timezone.utc)
        assert updated.blocked_until != stale_blocked_until
        assert updated.blocked_reason == "peer_flood"
    finally:
        account_repository.close()


def test_worker_skips_account_on_next_tick_after_peer_flood(tmp_path):
    """reader/inviter/worker.py::InviterWorker — после PeerFloodError на
    одном тике, следующий тик должен пропустить аккаунт целиком (см.
    _is_blocked_by_flood_wait) — ни новой попытки connect(), ни нового
    InviteToChannelRequest. hourly_limit/daily_limit не должны считать
    неудачную попытку как "отправленную" (status='failed', invited_at не
    задан — не попадает ни в count_recent_sent, ни в daily-бюджет)."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, username="ivan1", keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(
        db_path, 2, username="ivan2", keywords=["осаго"], access_hash=2,
        last_seen_at=_BASE_TIME + timedelta(days=1),
    )

    created_clients: list = []
    service, account_repository, campaign_repository, invite_repository = _build_service(
        db_path,
        client_factory=_make_client_factory(
            call_errors={"acc1": [PeerFloodError(request=GetHistoryRequest)]},
            created=created_clients,
        ),
    )
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")
        account = account_repository.create(
            name="acc1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=24,
        )

        worker = InviterWorker(
            service, campaign_repository, account_repository,
            invitations_per_account_per_hour=2, poll_interval_seconds=600,
            shutdown_event=asyncio.Event(),
        )

        asyncio.run(worker.run_one_tick())
        assert len(created_clients) == 1  # первая попытка — connect() был

        asyncio.run(worker.run_one_tick())
        # Второй тик: аккаунт заблокирован (см. _is_blocked_by_flood_wait) —
        # НИ ОДНОГО нового клиента/подключения не создано.
        assert len(created_clients) == 1

        # Неудачная попытка не расходует hourly/daily лимит.
        assert invite_repository.count_recent_sent(account.id, datetime.now(timezone.utc) - timedelta(hours=1)) == 0
    finally:
        account_repository.close()
        campaign_repository.close()
        invite_repository.close()


def test_execute_flood_wait_reason_unaffected_by_peer_flood_fallback(tmp_path):
    """Регрессия: настоящий FloodWaitError продолжает использовать
    Telegram-provided длительность и blocked_reason='flood_wait' — новый
    fallback для PeerFloodError не подменяет и не путает это поведение."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    client_factory = _make_client_factory(
        call_errors={account.name: [FloodWaitError(request=GetHistoryRequest, capture=90)]},
    )

    before = datetime.now(timezone.utc)
    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))
    after = datetime.now(timezone.utc)

    account_repository = TelegramAccountRepository(db_path)
    try:
        updated = account_repository.get(account.id)
        assert updated.blocked_reason == "flood_wait"
        assert before + timedelta(seconds=90) <= updated.blocked_until <= after + timedelta(seconds=90)
    finally:
        account_repository.close()


def test_execute_peer_flood_on_one_account_does_not_stop_other_accounts(tmp_path):
    """PeerFloodError у одного аккаунта не должен мешать обработке
    следующего доступного аккаунта в том же прогоне (см. run())."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")
        account_repository.create(
            name="account_flood", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
        account_repository.create(
            name="account_ok", phone="+995500000002", session_name="acc2",
            session_path="acc2.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    client_factory = _make_client_factory(
        call_errors={"account_flood": [PeerFloodError(request=GetHistoryRequest)]},
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        statuses_by_account = {i.account_id: i.status for i in invites}
    finally:
        invite_repository.close()

    account_repository = TelegramAccountRepository(db_path)
    try:
        flood_account = next(a for a in account_repository.list() if a.name == "account_flood")
        ok_account = next(a for a in account_repository.list() if a.name == "account_ok")
    finally:
        account_repository.close()

    assert statuses_by_account[flood_account.id] == "failed"
    # Второй аккаунт всё равно обработал СВОЕГО кандидата, несмотря на
    # PeerFloodError у первого.
    assert statuses_by_account[ok_account.id] in ("pending", "joined")


def test_execute_peer_flood_does_not_affect_verify_membership_false_account(tmp_path):
    """verify_membership=False не связан с обработкой PeerFloodError —
    STOP_ACCOUNT прерывает волну до шага верификации в любом случае,
    blocked_until всё равно корректно устанавливается."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path, verify_membership=False)

    client_factory = _make_client_factory(
        call_errors={account.name: [PeerFloodError(request=GetHistoryRequest)]},
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    account_repository = TelegramAccountRepository(db_path)
    try:
        updated = account_repository.get(account.id)
        assert updated.blocked_reason == "peer_flood"
        assert updated.blocked_until > datetime.now(timezone.utc)
    finally:
        account_repository.close()


def test_execute_flood_wait_stop_skips_verification_and_top_up_wave(tmp_path):
    """FloodWaitError, полученный на ПЕРВОМ кандидате основной волны —
    аккаунт останавливается ДО проверки pending и ДО волны добора (см.
    _execute_account: _run_invite_wave вернул stopped=True) — ни одного
    client.get_permissions(), ни одного дополнительного приглашения сверх
    того, что уже успело отправиться до FloodWait."""
    db_path = _setup_db(tmp_path)
    for user_id in range(1, 6):
        _seed_user(
            db_path, user_id, keywords=["осаго"], access_hash=user_id,
            last_seen_at=_BASE_TIME + timedelta(days=user_id),
        )

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account_repository.create(
            name="account_flood", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(
        call_errors={"account_flood": [FloodWaitError(request=GetHistoryRequest, capture=600)]},
        created=created_clients,
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    client = created_clients[0]
    # Только первый (по last_seen_at DESC) кандидат тронут вообще — ни
    # волны добора, ни повторной попытки основной волны для остальных.
    assert len(client.call_requests) == 1
    assert client.get_permissions_calls == []

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert len(invite_repository.list()) == 1
    finally:
        invite_repository.close()


def test_execute_flood_wait_below_threshold_still_persists_blocked_until(tmp_path):
    """Небольшой FloodWaitError (< 300 сек.) продолжает пережидаться и не
    останавливает аккаунт (см.
    test_execute_flood_wait_below_threshold_waits_and_continues_account),
    но blocked_until всё равно сохраняется — "При получении
    FloodWaitError(seconds=N) сохранить blocked_until" не зависит от
    порога, только от самого факта FloodWaitError (см. задачу)."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        account = account_repository.create(
            name="account_1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    client_factory = _make_client_factory(
        call_errors={"account_1": [FloodWaitError(request=GetHistoryRequest, capture=7), None]},
    )

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    account_repository = TelegramAccountRepository(db_path)
    try:
        updated = account_repository.get(account.id)
        assert updated.blocked_reason == "flood_wait"
        assert updated.blocked_until is not None
        # 7 секунд уже прошли (мок sleep не ждёт реально) — блокировка
        # формально сохранена, но уже истекла к моменту проверки.
        assert updated.blocked_until <= datetime.now(timezone.utc) + timedelta(seconds=7)
    finally:
        account_repository.close()


def test_execute_skips_account_with_active_blocked_until(tmp_path, monkeypatch):
    """Аккаунт с активным (ещё не истёкшим) blocked_until полностью
    пропускается ДО connect()/резолва target_chat/выборки кандидатов — ни
    одного вызова select_candidates() (см. _format_candidates_block/
    "Campaign: ..." в логах), ни одной записи в user_campaign_invites."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    account_repository = TelegramAccountRepository(db_path)
    try:
        account_repository.update(
            account.id,
            blocked_until=datetime.now(timezone.utc) + timedelta(hours=5),
            blocked_reason="flood_wait",
        )
    finally:
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    import reader.inviter.service as service_module

    all_logs: list[str] = []
    monkeypatch.setattr(service_module.logger, "info", all_logs.append)
    monkeypatch.setattr(service_module.logger, "warning", all_logs.append)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    campaign_blocks = [b for b in all_logs if b.startswith("Campaign:")]
    assert campaign_blocks == []

    skip_messages = [m for m in all_logs if m.startswith(f"Account {account.name} пропущен:")]
    assert len(skip_messages) == 1
    assert "Telegram FloodWait действует до" in skip_messages[0]
    assert "Осталось:" in skip_messages[0]

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert invite_repository.list() == []
    finally:
        invite_repository.close()


def test_execute_blocked_account_does_not_create_telegram_client(tmp_path):
    """Симметрично: ни client_factory(), ни connect() не вызываются для
    аккаунта с активным blocked_until — ноль взаимодействия с Telegram."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    account_repository = TelegramAccountRepository(db_path)
    try:
        account_repository.update(
            account.id,
            blocked_until=datetime.now(timezone.utc) + timedelta(hours=5),
            blocked_reason="flood_wait",
        )
    finally:
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    assert created_clients == []


def test_execute_processes_account_again_after_blocked_until_expires(tmp_path):
    """blocked_until в прошлом — считается снятым автоматически, без
    участия оператора (enabled=True изначально и остаётся неизменным) —
    аккаунт обрабатывается как обычно."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    account_repository = TelegramAccountRepository(db_path)
    try:
        account_repository.update(
            account.id,
            blocked_until=datetime.now(timezone.utc) - timedelta(hours=1),
            blocked_reason="flood_wait",
        )
    finally:
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    client = created_clients[0]
    assert client.connected is True
    assert len(client.call_requests) == 1

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert len(invite_repository.list()) == 1
    finally:
        invite_repository.close()


def test_execute_disabled_account_ignored_regardless_of_blocked_until(tmp_path):
    """enabled=False — отключён оператором вручную — не запускается вовсе,
    независимо от blocked_until (даже если блокировка уже истекла/её
    вообще нет): enabled и blocked_until — независимые условия (см.
    TelegramAccount)."""
    db_path = _setup_db(tmp_path)
    campaign, account = _setup_single_candidate_campaign(db_path)

    account_repository = TelegramAccountRepository(db_path)
    try:
        account_repository.update(account.id, enabled=False)
    finally:
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    assert created_clients == []
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert invite_repository.list() == []
    finally:
        invite_repository.close()


def test_execute_one_blocked_account_does_not_block_next_account(tmp_path):
    """Заблокированный аккаунт не должен мешать обработке СЛЕДУЮЩЕГО —
    каждый аккаунт проверяется независимо (см. _execute_account)."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))

    campaign_repository = InviteCampaignRepository(db_path)
    account_repository = TelegramAccountRepository(db_path)
    try:
        campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@target_chat")
        blocked_account = account_repository.create(
            name="blocked", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=5,
        )
        account_repository.create(
            name="works", phone="+995500000002", session_name="acc2",
            session_path="acc2.session", daily_limit=5,
        )
        account_repository.update(
            blocked_account.id,
            blocked_until=datetime.now(timezone.utc) + timedelta(hours=5),
            blocked_reason="flood_wait",
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(_run_service(db_path, client_factory=client_factory, execute=True))

    # Единственный созданный клиент — для "works" (заблокированный вообще
    # не подключается, см. test_execute_blocked_account_does_not_create_
    # telegram_client).
    assert len(created_clients) == 1
    assert created_clients[0].account.name == "works"

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        # Оба реальных кандидата ушли рабочему аккаунту.
        assert len(invites) == 2
        assert all(i.account_id == 2 for i in invites)
    finally:
        invite_repository.close()


# ---- тестовый режим (--test/main.py) — лимит успешных приглашений на весь run() ----


def test_execute_test_mode_stops_after_exactly_n_successful_invites(tmp_path):
    """max_successful_invites (--test в main.py, TEST_MODE_MAX_SUCCESSFUL_INVITES=30) —
    как только текущий вызов run() наберёт ровно это количество успешно
    ОТПРАВЛЕННЫХ приглашений (status='pending', реальный InviteStats.sent —
    RPC-успех, не подтверждённое вступление), дальнейшая обработка
    останавливается: текущий аккаунт заканчивается штатно (не прерывая уже
    выполняющееся приглашение, но и не выполняя проверку pending/волну
    добора для него — см. _execute_account), следующие аккаунты и кампании
    больше не трогаются."""
    db_path = _setup_db(tmp_path)
    total_users = 40
    for user_id in range(1, total_users + 1):
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
            session_path="acc1.session", daily_limit=20,
        )
        account_repository.create(
            name="account_2", phone="+995500000002", session_name="acc2",
            session_path="acc2.session", daily_limit=20,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    # Без call_errors — все приглашения успешны.
    client_factory = _make_client_factory(created=created_clients)

    asyncio.run(
        _run_service(
            db_path, client_factory=client_factory, execute=True,
            max_successful_invites=TEST_MODE_MAX_SUCCESSFUL_INVITES,
        )
    )

    assert TEST_MODE_MAX_SUCCESSFUL_INVITES == 30

    client_1, client_2 = created_clients
    # account_1 (daily_limit=20) отработал полностью — 20 успехов < 30, не
    # останавливается.
    assert len(client_1.call_requests) == 20
    # account_2 останавливается ровно после 10-го своего успеха: 20 + 10 = 30.
    assert len(client_2.call_requests) == 10
    assert client_1.disconnected is True
    assert client_2.disconnected is True

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        assert len(invites) == 30
        # account_1 отработал полностью, включая проверку pending (лимит
        # тестового режима не сработал у него) — все 20 подтверждены.
        # account_2 остановился ИЗ-ЗА лимита ровно на 10-м успехе — это
        # "мягкая" остановка (см. _execute_account: stopped=True пропускает
        # и проверку pending, и волну добора), поэтому его 10 остаются
        # неподтверждёнными.
        assert sum(1 for i in invites if i.status == "joined") == 20
        assert sum(1 for i in invites if i.status == "pending") == 10
    finally:
        invite_repository.close()


def test_execute_test_mode_errors_and_already_participant_do_not_count_toward_limit(tmp_path):
    """UserAlreadyParticipantError и любые ошибки (включая
    UserChannelsTooMuchError/UserPrivacyRestrictedError) не увеличивают
    счётчик тестового режима — лимит реагирует только на реальные успешные
    приглашения."""
    db_path = _setup_db(tmp_path)
    total_users = 6
    for user_id in range(1, total_users + 1):
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
            session_path="acc1.session", daily_limit=6,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(
        call_errors={
            "account_1": [
                UserAlreadyParticipantError(request=GetHistoryRequest),
                UserChannelsTooMuchError(request=GetHistoryRequest),
                UserPrivacyRestrictedError(request=GetHistoryRequest),
                None,
                None,
                None,
            ]
        },
        created=created_clients,
    )

    asyncio.run(
        _run_service(
            db_path, client_factory=client_factory, execute=True,
            max_successful_invites=2,
        )
    )

    client = created_clients[0]
    # 3 "не-успеха" (не в счёт лимита) + ровно 2 успеха до остановки = 5
    # обработанных кандидатов, 6-й не тронут вовсе.
    assert len(client.call_requests) == 5

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        invites = invite_repository.list()
        assert len(invites) == 5
        # UserAlreadyParticipantError — подтверждённый участник немедленно
        # (status='joined', см. _classify_invite_error). Лимит сработал
        # ровно на 2-м реальном успехе — "мягкая" остановка пропускает
        # проверку pending для этого аккаунта (см. _execute_account), т.е.
        # эти 2 успешные отправки остаются 'pending', а не 'joined'.
        assert sum(1 for i in invites if i.status == "joined") == 1
        assert sum(1 for i in invites if i.status == "pending") == 2
        assert sum(1 for i in invites if i.status == "failed") == 2
    finally:
        invite_repository.close()


def test_execute_test_mode_stop_still_sends_correct_final_stats_notifications(tmp_path):
    """После срабатывания лимита тестового режима отчёты оператору (по
    аккаунту и по кампании) формируются как обычно, с корректными
    счётчиками того, что реально произошло до остановки — без отдельного
    уведомления о самой остановке (в отличие от PeerFlood/большого
    FloodWait, см. test_execute_peer_flood_stops_account_and_notifies_operator)."""
    db_path = _setup_db(tmp_path)
    total_users = 10
    for user_id in range(1, total_users + 1):
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
            session_path="acc1.session", daily_limit=10,
        )
    finally:
        campaign_repository.close()
        account_repository.close()

    created_clients: list = []
    client_factory = _make_client_factory(
        call_errors={
            "account_1": [
                None,
                UserAlreadyParticipantError(request=GetHistoryRequest),
                UserChannelsTooMuchError(request=GetHistoryRequest),
                None,
                None,
            ]
        },
        created=created_clients,
    )
    notifier = _FakeOperatorNotifier()

    asyncio.run(
        _run_service(
            db_path, client_factory=client_factory, execute=True, notifier=notifier,
            max_successful_invites=3,
        )
    )

    client = created_clients[0]
    assert len(client.call_requests) == 5  # candidates 6..10 не тронуты

    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        assert len(invite_repository.list()) == 5
    finally:
        invite_repository.close()

    # Никакого уведомления об остановке — это не PeerFlood/FloodWait.
    stop_notifications = [m for m in notifier.sent if m.startswith("⚠️")]
    assert stop_notifications == []

    # "Мягкая" остановка (лимит тестового режима) пропускает проверку
    # pending для этого аккаунта (см. _execute_account) — 3 успешные
    # отправки (candidates 10,7,6) остаются 'pending', 1 подтверждён
    # немедленно (candidate 9, UserAlreadyParticipantError).
    account_notifications = [m for m in notifier.sent if m.startswith("📨")]
    assert len(account_notifications) == 1
    assert "📤 Отправлено приглашений: 3" in account_notifications[0]
    assert "✅ Подтверждено участников: 1" in account_notifications[0]
    assert "⏳ Ожидают подтверждения: 3" in account_notifications[0]
    assert "🚫 Недоступны (invalid): 0" in account_notifications[0]
    assert "❌ Ошибок: 1" in account_notifications[0]
    assert "Осталось кандидатов: 6" in account_notifications[0]

    campaign_notifications = [m for m in notifier.sent if "Итоги кампании" in m]
    assert len(campaign_notifications) == 1
    assert "📤 Отправлено приглашений: 3" in campaign_notifications[0]
    assert "✅ Подтверждено участников: 1" in campaign_notifications[0]
    assert "⏳ Ожидают подтверждения: 3" in campaign_notifications[0]
    assert "❌ Ошибок: 1" in campaign_notifications[0]
    # 10 подходящих кандидатов всего - 4 со статусом 'pending'/'joined' в БД
    # (3 pending + 1 joined) = 6 осталось.
    assert "Осталось кандидатов: 6" in campaign_notifications[0]


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


# ---- InviterService.run_one_worker_attempt() (см. reader/inviter/worker.py) ----


def _build_service(db_path: Path, *, client_factory=None):
    """Как _run_service(), но возвращает сам InviterService и репозитории
    открытыми — тестам run_one_worker_attempt() нужно вызывать метод
    сервиса напрямую (не через run()) и затем проверять состояние БД тем
    же соединением, поэтому закрывать их приходится самому тесту."""
    account_repository = TelegramAccountRepository(db_path)
    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    service = InviterService(
        account_repository, campaign_repository, invite_repository,
        client_factory=client_factory or _make_client_factory(),
        session_checker=lambda account: True,
    )
    return service, account_repository, campaign_repository, invite_repository


def test_worker_attempt_sends_at_most_one_invite_even_with_daily_headroom(tmp_path):
    """daily_limit=24 (реальный прод-лимит аккаунтов, см. задачу) и 5
    подходящих кандидатов — run_one_worker_attempt() должен отправить
    РОВНО одно приглашение, а не всю волну добора вплоть до остатка
    дневного лимита (см. _execute_account max_sent_this_call=1) — иначе
    почасовое распределение не имело бы смысла."""
    db_path = _setup_db(tmp_path)
    for user_id in range(1, 6):
        _seed_user(
            db_path, user_id, keywords=["осаго"], access_hash=user_id,
            last_seen_at=_BASE_TIME + timedelta(days=user_id),
        )

    service, account_repository, campaign_repository, invite_repository = _build_service(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")
        account = account_repository.create(
            name="acc1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=24,
        )

        stats = asyncio.run(service.run_one_worker_attempt(campaign, account, hourly_limit=2))

        assert stats is not None
        assert stats.sent == 1
        assert len(invite_repository.list()) == 1
    finally:
        account_repository.close()
        campaign_repository.close()
        invite_repository.close()


def test_worker_attempt_skips_account_when_hourly_limit_already_reached(tmp_path):
    """Скользящий часовой лимит (см. UserCampaignInviteRepository.
    count_recent_sent) уже исчерпан — аккаунт пропускается ЦЕЛИКОМ, ещё до
    connect() (ни один TelegramClient не создаётся, см. созданный список
    created_clients), а не просто без отправки."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)

    created_clients: list = []
    service, account_repository, campaign_repository, invite_repository = _build_service(
        db_path, client_factory=_make_client_factory(created=created_clients),
    )
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")
        account = account_repository.create(
            name="acc1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=24,
        )
        now = datetime.now(timezone.utc)
        invite_repository.create(
            user_id=901, campaign_id=campaign.id, account_id=account.id,
            status="pending", invited_at=now,
        )
        invite_repository.create(
            user_id=902, campaign_id=campaign.id, account_id=account.id,
            status="joined", invited_at=now, verified_at=now,
        )

        stats = asyncio.run(service.run_one_worker_attempt(campaign, account, hourly_limit=2))

        assert stats is None
        assert created_clients == []
        # Только два "исторических" приглашения — ничего нового не отправлено.
        assert len(invite_repository.list()) == 2
    finally:
        account_repository.close()
        campaign_repository.close()
        invite_repository.close()


def test_worker_attempt_second_call_within_hour_is_skipped(tmp_path):
    """hourly_limit=1: первый вызов отправляет приглашение, второй (в тот
    же скользящий час) — пропускается без единого нового подключения, а не
    пытается "довыполнить" лимит другим кандидатом."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))

    created_clients: list = []
    service, account_repository, campaign_repository, invite_repository = _build_service(
        db_path, client_factory=_make_client_factory(created=created_clients),
    )
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")
        account = account_repository.create(
            name="acc1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=24,
        )

        first = asyncio.run(service.run_one_worker_attempt(campaign, account, hourly_limit=1))
        second = asyncio.run(service.run_one_worker_attempt(campaign, account, hourly_limit=1))

        assert first is not None and first.sent == 1
        assert second is None
        assert len(created_clients) == 1
        assert len(invite_repository.list()) == 1
    finally:
        account_repository.close()
        campaign_repository.close()
        invite_repository.close()


def test_worker_attempt_hourly_limit_persists_across_new_service_instance(tmp_path):
    """Симулирует restart процесса worker'а: часовой лимит вычисляется по
    user_campaign_invites в БД (см. count_recent_sent), а не по счётчику в
    памяти InviterService — совершенно новый экземпляр сервиса (та же БД,
    ни один Python-объект не переиспользуется из "старого процесса")
    должен по-прежнему видеть приглашение, отправленное ДО "перезапуска",
    и пропустить аккаунт, а не начать отсчёт часового лимита заново."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)
    _seed_user(db_path, 2, keywords=["осаго"], access_hash=2, last_seen_at=_BASE_TIME + timedelta(days=1))

    service_before_restart, account_repository, campaign_repository, invite_repository = _build_service(db_path)
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")
        account = account_repository.create(
            name="acc1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=24,
        )

        before = asyncio.run(
            service_before_restart.run_one_worker_attempt(campaign, account, hourly_limit=1)
        )
        assert before is not None and before.sent == 1

        # "Restart" — новый InviterService поверх ТЕХ ЖЕ репозиториев/БД
        # (как и в реальности: новый процесс, тот же users.db на диске).
        created_clients_after_restart: list = []
        service_after_restart = InviterService(
            account_repository, campaign_repository, invite_repository,
            client_factory=_make_client_factory(created=created_clients_after_restart),
            session_checker=lambda a: True,
        )

        after = asyncio.run(
            service_after_restart.run_one_worker_attempt(campaign, account, hourly_limit=1)
        )

        assert after is None
        assert created_clients_after_restart == []
        assert len(invite_repository.list()) == 1
    finally:
        account_repository.close()
        campaign_repository.close()
        invite_repository.close()


def test_worker_attempt_respects_blocked_until(tmp_path):
    """FloodWait (blocked_until в будущем) должен останавливать worker-
    попытку точно так же, как обычный run(execute=True) — логика не
    дублируется, run_one_worker_attempt() делегирует в _execute_account,
    которая уже это проверяет."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)

    created_clients: list = []
    service, account_repository, campaign_repository, invite_repository = _build_service(
        db_path, client_factory=_make_client_factory(created=created_clients),
    )
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")
        account = account_repository.create(
            name="acc1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=24,
        )
        account = account_repository.update(
            account.id,
            blocked_until=datetime.now(timezone.utc) + timedelta(minutes=30),
            blocked_reason="flood_wait",
        )

        stats = asyncio.run(service.run_one_worker_attempt(campaign, account, hourly_limit=2))

        assert stats is None
        assert created_clients == []
    finally:
        account_repository.close()
        campaign_repository.close()
        invite_repository.close()


def test_worker_attempt_respects_daily_limit_even_when_hourly_limit_allows(tmp_path):
    """daily_limit уже полностью исчерпан сегодня — worker-попытка не
    должна отправлять ничего, даже если часовой лимит формально ещё не
    исчерпан (обе проверки независимы, самая строгая побеждает, см. задачу)."""
    db_path = _setup_db(tmp_path)
    _seed_user(db_path, 1, keywords=["осаго"], access_hash=1, last_seen_at=_BASE_TIME)

    created_clients: list = []
    service, account_repository, campaign_repository, invite_repository = _build_service(
        db_path, client_factory=_make_client_factory(created=created_clients),
    )
    try:
        campaign = campaign_repository.create(name="ОСАГО", keyword="осаго", target_chat="@t")
        account = account_repository.create(
            name="acc1", phone="+995500000001", session_name="acc1",
            session_path="acc1.session", daily_limit=1,
        )
        now = datetime.now(timezone.utc)
        invite_repository.create(
            user_id=901, campaign_id=campaign.id, account_id=account.id,
            status="joined", invited_at=now, verified_at=now,
        )

        stats = asyncio.run(service.run_one_worker_attempt(campaign, account, hourly_limit=2))

        assert stats is None
        assert created_clients == []
    finally:
        account_repository.close()
        campaign_repository.close()
        invite_repository.close()
