"""
Тесты FineCommand — операторский интерфейс (fine add/list/stop/check/status).
Repository — настоящие (SQLite/tmp_path), FineProvider/NotificationService —
фейковые (без сети/Telegram). Ничего не переопределяет логику FineJob —
использует те же FineCheckService/FineNotificationCoordinator.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError  # noqa: E402
from telethon.tl.types import User as TelethonUser  # noqa: E402

from reader.commands.base import CommandContext, CommandError  # noqa: E402
from reader.commands.fine import FineCommand  # noqa: E402
from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import CarFineStats, ParsedFineRecord  # noqa: E402
from reader.fines.notification_coordinator import FineNotificationCoordinator  # noqa: E402
from reader.fines.provider import FineProvider, FineProviderError  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.jobs.fine_job import FineJob  # noqa: E402
from reader.jobs.scheduler import Scheduler  # noqa: E402
from reader.notifications.base import NotificationResult, NotificationService  # noqa: E402
from reader.time_display import to_tbilisi  # noqa: E402
from reader.users.models import TelegramUserInfo  # noqa: E402

_CHAT_ID = -100999
_USER_ID = 111
_TBILISI = ZoneInfo("Asia/Tbilisi")
_RUN_TIMES = [dt_time(9, 0), dt_time(15, 0), dt_time(21, 0)]


class _FakeProvider(FineProvider):
    def __init__(self, records_by_car=None, error: Exception | None = None):
        self._records_by_car = records_by_car or {}
        self._error = error
        self.requested_plates: list[str] = []

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        self.requested_plates.append(plate)
        if self._error is not None:
            raise self._error
        return self._records_by_car.get(plate, [])


class _SelectiveFailingProvider(FineProvider):
    """Как _FakeProvider, но падает только для номеров из fail_for — нужен,
    чтобы проверить, что ошибка проверки ОДНОГО автомобиля в fine
    update-all не останавливает проверку остальных."""

    def __init__(self, records_by_car=None, fail_for=()):
        self._records_by_car = records_by_car or {}
        self._fail_for = set(fail_for)
        self.requested_plates: list[str] = []

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        self.requested_plates.append(plate)
        if plate in self._fail_for:
            raise FineProviderError(f"police.ge недоступен для {plate}")
        return self._records_by_car.get(plate, [])


class _FakeUserRepository:
    """Реализует ровно FineUserRepositoryLike (см. reader/commands/fine.py) —
    надмножество UserLookupLike (find_by_car_number, для fine check),
    дополнительно find_by_username/add_car_numbers для fine add @username.

    Внутреннее состояние — car_numbers по user_id (как в настоящем
    UserRepository), а не отдельно на car_number, поэтому add_car_numbers()
    корректно отражается в последующих find_by_car_number() в рамках
    одного теста.

    users_by_car_number — старый формат конструктора (car_number ->
    список пользователей), оставлен для существующих тестов "fine check".
    users — список известных пользователей для find_by_username (и чтобы
    add_car_numbers() могло найти, кому дописать номер)."""

    def __init__(
        self,
        users_by_car_number: dict[str, list[TelegramUserInfo]] | None = None,
        users: list[TelegramUserInfo] | None = None,
    ):
        self._users_by_id: dict[int, TelegramUserInfo] = {}
        self._car_numbers_by_user_id: dict[int, set[str]] = {}
        self.upsert_calls: list[TelegramUserInfo] = []

        for user in users or []:
            self._users_by_id[user.user_id] = user

        for car_number, owners in (users_by_car_number or {}).items():
            for owner in owners:
                self._users_by_id[owner.user_id] = owner
                self._car_numbers_by_user_id.setdefault(owner.user_id, set()).add(car_number)

    def find_by_car_number(self, car_number: str) -> list[TelegramUserInfo]:
        return [
            self._users_by_id[user_id]
            for user_id, numbers in self._car_numbers_by_user_id.items()
            if car_number in numbers
        ]

    def find_by_username(self, username: str) -> TelegramUserInfo | None:
        for user in self._users_by_id.values():
            if user.username is not None and user.username.lower() == username.lower():
                return user
        return None

    def add_car_numbers(self, user_id: int, car_numbers: list[str]) -> None:
        self._car_numbers_by_user_id.setdefault(user_id, set()).update(car_numbers)

    def upsert(self, user: TelegramUserInfo) -> None:
        """Как настоящий UserRepository.upsert() — идемпотентно
        перезаписывает запись по user_id, не создавая дублей."""
        self.upsert_calls.append(user)
        self._users_by_id[user.user_id] = user


class _FakeTelegramClient:
    """Ровно то, что нужно FineCommand от TelegramClient — get_entity() (см.
    reader/commands/fine.py::TelegramUsernameResolverLike). entities —
    username (без "@", регистр не важен) -> telethon.tl.types.User;
    not_found_usernames — username'ы, для которых Telegram подтверждённо
    не находит пользователя (UsernameNotOccupiedError); errors — username
    -> произвольное исключение, имитирующее технический сбой (сеть,
    FloodWait и т.п.), не связанный с фактом "юзернейм не существует"."""

    def __init__(self, *, entities=None, not_found_usernames=(), errors=None):
        self._entities = {k.lower(): v for k, v in (entities or {}).items()}
        self._not_found_usernames = {u.lower() for u in not_found_usernames}
        self._errors = {k.lower(): v for k, v in (errors or {}).items()}
        self.get_entity_calls: list[str] = []

    async def get_entity(self, entity):
        username = str(entity).lstrip("@").lower()
        self.get_entity_calls.append(username)

        if username in self._errors:
            raise self._errors[username]
        if username in self._not_found_usernames:
            raise UsernameNotOccupiedError(request=None)
        if username in self._entities:
            return self._entities[username]
        raise UsernameNotOccupiedError(request=None)


class _FakeNotificationService(NotificationService):
    def __init__(self):
        self.notify_calls: list[list] = []

    async def notify(self, events) -> NotificationResult:
        self.notify_calls.append(events)
        return NotificationResult(
            delivered_event_ids=[e.detected_fine_id for e in events], failed_event_ids=[]
        )


def _record(
    car_number="B957MA09",
    external_fine_id="AB123456",
    fingerprint="fp-1",
    penalty_date=date(2026, 8, 6),
    due_date=date(2026, 8, 20),
    delivered_status="Не вручено",
) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number,
        external_fine_id=external_fine_id,
        penalty_date=penalty_date,
        due_date=due_date,
        delivered_status=delivered_status,
        fingerprint=fingerprint,
        raw_data={"protocolNo": external_fine_id},
    )


def _ctx(args: list[str], *, chat_id=_CHAT_ID, user_id=_USER_ID, event=None) -> CommandContext:
    return CommandContext(
        chat_id=chat_id, user_id=user_id, args=args, raw_text="fine " + " ".join(args), event=event
    )


class _FakeEvent:
    """Достаточно Telethon event.respond() для fine update-all — минимум,
    нужный только чтобы проверить, что стартовое сообщение реально
    отправляется через event, а не просто печатается в лог."""

    def __init__(self):
        self.responses: list[str] = []

    async def respond(self, text: str) -> None:
        self.responses.append(text)


def _split_into_args(multiline_text: str) -> list[str]:
    """Имитирует ТОЧНО то же разбиение, что делает CommandDispatcher —
    text.strip().split() (см. reader/commands/dispatcher.py) — на реальном
    многострочном Telegram-сообщении, а не на уже готовом списке токенов:
    перевод строки для str.split() ничем не отличается от пробела."""
    return multiline_text.strip().split()


class _Fixture:
    """Полный набор реальных зависимостей (кроме FineProvider/NotificationService)
    — тот же граф объектов, что собирает reader/main.py."""

    def __init__(
        self, tmp_path, records_by_car=None, provider_error=None, provider=None,
        user_repository=None, telegram_client=None,
    ):
        db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(db_path)
        self.detected_fine_repository = DetectedFineRepository(db_path)
        self.user_repository = user_repository
        self.telegram_client = telegram_client
        self.provider = provider if provider is not None else _FakeProvider(records_by_car, error=provider_error)
        self.check_service = FineCheckService(
            self.provider, self.task_repository, self.detected_fine_repository
        )
        self.notification_service = _FakeNotificationService()
        self.coordinator = FineNotificationCoordinator(
            self.detected_fine_repository, self.task_repository, self.notification_service
        )
        self.fine_job = FineJob(
            task_repository=self.task_repository,
            check_service=self.check_service,
            notification_coordinator=self.coordinator,
            run_times=_RUN_TIMES,
            tz=_TBILISI,
        )
        self.scheduler = Scheduler([self.fine_job])
        self.command = FineCommand(
            task_repository=self.task_repository,
            check_service=self.check_service,
            notification_coordinator=self.coordinator,
            scheduler=self.scheduler,
            fine_job=self.fine_job,
            detected_fine_repository=self.detected_fine_repository,
            run_times=_RUN_TIMES,
            tz=_TBILISI,
            user_repository=self.user_repository,
            telegram_client=self.telegram_client,
        )

    def close(self):
        self.task_repository.close()
        self.detected_fine_repository.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


# ---- fine add ----


async def test_fine_add_with_explicit_dates(fx):
    result = await fx.command.handle(_ctx(["add", "b957ma09", "03.08.2026", "13.08.2026"]))

    assert "✅ Номер добавлен на мониторинг" in result.text
    assert "🚗 B957MA09" in result.text
    assert "📅 Мониторинг: 03.08.2026 — 13.08.2026" in result.text
    assert "🔎 Штрафы проверены: новых штрафов нет" in result.text
    # Внутренний ID задачи — деталь реализации БД, оператору не нужен.
    assert "ID" not in result.text

    [task] = fx.task_repository.list_active()
    assert task.car_number == "B957MA09"
    assert task.start_date == date(2026, 8, 3)
    assert task.end_date == date(2026, 8, 13)
    assert task.telegram_chat_id == _CHAT_ID
    assert task.created_by_user_id == _USER_ID


async def test_fine_add_without_dates_defaults_to_today_plus_30_days(fx):
    result = await fx.command.handle(_ctx(["add", "AA001AA"]))

    assert "✅ Номер добавлен на мониторинг" in result.text

    [task] = fx.task_repository.list_active()
    assert (task.end_date - task.start_date) == timedelta(days=30)


async def test_fine_add_rejects_invalid_car_number(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "AA-001-AA"]))

    assert "❌" in exc_info.value.message
    assert fx.task_repository.list_active() == []


async def test_fine_add_rejects_wrong_argument_count(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "AA001AA", "03.08.2026"]))

    assert "Неверный формат команды" in exc_info.value.message


async def test_fine_add_rejects_invalid_date(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "AA001AA", "2026-08-03", "13.08.2026"]))

    assert "формат даты" in exc_info.value.message


async def test_fine_add_rejects_end_before_start(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "AA001AA", "13.08.2026", "03.08.2026"]))

    assert "END_DATE" in exc_info.value.message


async def test_fine_add_resets_period_instead_of_rejecting_when_active_task_exists(fx):
    """Повторное добавление автомобиля, уже находящегося на активном
    мониторинге, больше НЕ ошибка (см. задачу про изменение бизнес-логики):
    период существующей задачи перезаписывается на today..today+30,
    вторая (дублирующая) задача не создаётся, даже если оператор указал
    свои собственные (пересекающиеся или нет) даты."""
    today = datetime.now(timezone.utc).astimezone(_TBILISI).date()

    await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))
    [task_before] = fx.task_repository.list_active()

    result = await fx.command.handle(_ctx(["add", "AA001AA", "15.08.2026", "20.09.2026"]))

    assert "✅ Номер добавлен на мониторинг" in result.text
    expected_period = f"{today.strftime('%d.%m.%Y')} — {(today + timedelta(days=30)).strftime('%d.%m.%Y')}"
    assert f"📅 Мониторинг: {expected_period}" in result.text
    [task_after] = fx.task_repository.list_active()
    assert task_after.id == task_before.id  # без второй задачи
    assert task_after.start_date == today
    assert task_after.end_date == today + timedelta(days=30)


# ---- fine add NUMBER @username ----


async def test_fine_add_with_username_links_owner_not_creator(tmp_path):
    """Главный regression-тест: created_by_user_id остаётся ID того, кто
    прислал команду, а @username привязывается к car_numbers СОВСЕМ
    другого пользователя."""
    OPERATOR_USER_ID = 111
    OWNER_USER_ID = 222
    owner = TelegramUserInfo(user_id=OWNER_USER_ID, username="owner", first_name=None, last_name=None)
    fx = _Fixture(tmp_path, user_repository=_FakeUserRepository(users=[owner]))
    try:
        result = await fx.command.handle(
            _ctx(["add", "A123AA777", "@owner"], user_id=OPERATOR_USER_ID)
        )

        [task] = fx.task_repository.list_active()
        assert task.car_number == "A123AA777"
        assert task.created_by_user_id == OPERATOR_USER_ID

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert [u.user_id for u in found] == [OWNER_USER_ID]

        assert "✅ Номер добавлен на мониторинг" in result.text
        assert "👤 @owner" in result.text
    finally:
        fx.close()


async def test_fine_add_without_username_keeps_old_behavior(tmp_path):
    """fine add NUMBER (без @username) — старое поведение не меняется:
    номер НЕ присваивается автоматически автору команды."""
    fx = _Fixture(tmp_path, user_repository=_FakeUserRepository())
    try:
        result = await fx.command.handle(_ctx(["add", "A123AA777"]))

        assert "✅ Номер добавлен на мониторинг" in result.text
        assert "👤 не найден" in result.text
        assert fx.user_repository.find_by_car_number("A123AA777") == []

        [task] = fx.task_repository.list_active()
        assert task.created_by_user_id == _USER_ID
    finally:
        fx.close()


async def test_fine_add_with_unknown_username_fails_and_creates_nothing(tmp_path):
    """Без сконфигурированного Telegram-клиента (telegram_client=None, как
    и по умолчанию) резолв через Telegram просто не выполняется — @username,
    которого нет в локальной БД, по-прежнему считается не найденным, но
    БЕЗ формулировки "не найден в базе" (см. задачу про баг: отсутствие в
    нашей БД само по себе больше не должно звучать как причина ошибки)."""
    fx = _Fixture(tmp_path, user_repository=_FakeUserRepository())
    try:
        with pytest.raises(CommandError) as exc_info:
            await fx.command.handle(_ctx(["add", "A123AA777", "@unknown"]))

        assert "@unknown" in exc_info.value.message
        assert "не найден" in exc_info.value.message
        assert "в базе" not in exc_info.value.message
        assert "Автомобиль не добавлен" in exc_info.value.message

        # Задача мониторинга НЕ создана, car_numbers не тронуты.
        assert fx.task_repository.list_active() == []
        assert fx.user_repository.find_by_car_number("A123AA777") == []
    finally:
        fx.close()


# ---- fine add @username: резолв через Telegram, если нет в локальной БД ----


async def test_fine_add_with_username_not_in_db_resolves_via_telegram_and_creates_user(tmp_path):
    """Главный regression-тест бага: @username нет в локальной БД, но
    Telegram успешно резолвит его — пользователь создаётся, и мониторинг
    добавляется сразу, без повторной команды оператора."""
    entity = TelethonUser(
        id=555, username="Santinorussia", first_name="Santino", last_name=None,
        access_hash=999888, bot=False,
    )
    client = _FakeTelegramClient(entities={"santinorussia": entity})
    user_repository = _FakeUserRepository()
    fx = _Fixture(tmp_path, user_repository=user_repository, telegram_client=client)
    try:
        result = await fx.command.handle(
            _ctx(["add", "A123AA777", "@Santinorussia"])
        )

        assert "✅ Номер добавлен на мониторинг" in result.text
        assert "👤 Santino (@Santinorussia)" in result.text

        [task] = fx.task_repository.list_active()
        assert task.car_number == "A123AA777"

        # Пользователь реально создан upsert()'ом — с минимальным набором
        # полей (см. задачу), а не выдуманным user_id/access_hash.
        assert len(user_repository.upsert_calls) == 1
        created = user_repository.upsert_calls[0]
        assert created.user_id == 555
        assert created.username == "Santinorussia"
        assert created.first_name == "Santino"
        assert created.access_hash == 999888
        assert created.is_bot is False

        # Автомобиль сразу добавлен этому пользователю (тем же существующим
        # механизмом add_car_numbers, что и для уже известных пользователей).
        found = fx.user_repository.find_by_car_number("A123AA777")
        assert [u.user_id for u in found] == [555]

        assert client.get_entity_calls == ["santinorussia"]
    finally:
        fx.close()


async def test_fine_add_with_username_not_in_db_saves_available_profile_fields(tmp_path):
    entity = TelethonUser(
        id=777, username="ivan_petrov", first_name="Иван", last_name="Петров",
        access_hash=123123, bot=False,
    )
    client = _FakeTelegramClient(entities={"ivan_petrov": entity})
    user_repository = _FakeUserRepository()
    fx = _Fixture(tmp_path, user_repository=user_repository, telegram_client=client)
    try:
        await fx.command.handle(_ctx(["add", "A123AA777", "@ivan_petrov"]))

        [created] = user_repository.upsert_calls
        assert created.user_id == 777
        assert created.username == "ivan_petrov"
        assert created.first_name == "Иван"
        assert created.last_name == "Петров"
        assert created.access_hash == 123123
        assert created.is_bot is False
        assert created.peer_type == "User"
    finally:
        fx.close()


async def test_fine_add_with_username_telegram_not_found_fails_with_clear_error(tmp_path):
    """Telegram подтверждённо не знает такого username (не "в базе", а
    именно в Telegram) — ошибка, но с новой, точной формулировкой."""
    client = _FakeTelegramClient(not_found_usernames=["santinorussia"])
    user_repository = _FakeUserRepository()
    fx = _Fixture(tmp_path, user_repository=user_repository, telegram_client=client)
    try:
        with pytest.raises(CommandError) as exc_info:
            await fx.command.handle(_ctx(["add", "A123AA777", "@Santinorussia"]))

        assert "@Santinorussia" in exc_info.value.message
        assert "не найден" in exc_info.value.message
        assert "в базе" not in exc_info.value.message
        assert "Автомобиль не добавлен" in exc_info.value.message

        assert fx.task_repository.list_active() == []
        assert user_repository.upsert_calls == []
        assert fx.user_repository.find_by_car_number("A123AA777") == []
    finally:
        fx.close()


async def test_fine_add_with_username_invalid_syntax_fails_with_clear_error(tmp_path):
    client = _FakeTelegramClient(errors={"bad username": UsernameInvalidError(request=None)})
    fx = _Fixture(tmp_path, user_repository=_FakeUserRepository(), telegram_client=client)
    try:
        with pytest.raises(CommandError) as exc_info:
            await fx.command.handle(_ctx(["add", "A123AA777", "@bad username"]))

        assert "не найден" in exc_info.value.message
        assert fx.task_repository.list_active() == []
    finally:
        fx.close()


async def test_fine_add_with_username_telegram_resolve_technical_error_creates_no_partial_state(
    tmp_path,
):
    """Резолв упал технически (не "юзернейм не существует", а, например,
    сеть/FloodWait) — отдельная, явно техническая ошибка, и никакого
    частично созданного состояния (ни task, ни user, ни car_numbers)."""
    client = _FakeTelegramClient(errors={"santinorussia": RuntimeError("connection reset")})
    user_repository = _FakeUserRepository()
    fx = _Fixture(tmp_path, user_repository=user_repository, telegram_client=client)
    try:
        with pytest.raises(CommandError) as exc_info:
            await fx.command.handle(_ctx(["add", "A123AA777", "@Santinorussia"]))

        assert "@Santinorussia" in exc_info.value.message
        assert "техническ" in exc_info.value.message
        assert "Автомобиль не добавлен" in exc_info.value.message

        assert fx.task_repository.list_active() == []
        assert user_repository.upsert_calls == []
        assert fx.user_repository.find_by_car_number("A123AA777") == []
    finally:
        fx.close()


async def test_fine_add_with_username_resolved_entity_not_a_user_is_treated_as_not_found(tmp_path):
    """entity, резолвнутая Telegram, — не обычный User (например, канал) —
    трактуется как "не найден", а не подставляется как владелец автомобиля."""
    class _NotAUser:
        id = 999

    client = _FakeTelegramClient(entities={"somechannel": _NotAUser()})
    user_repository = _FakeUserRepository()
    fx = _Fixture(tmp_path, user_repository=user_repository, telegram_client=client)
    try:
        with pytest.raises(CommandError) as exc_info:
            await fx.command.handle(_ctx(["add", "A123AA777", "@somechannel"]))

        assert "не найден" in exc_info.value.message
        assert user_repository.upsert_calls == []
        assert fx.task_repository.list_active() == []
    finally:
        fx.close()


async def test_fine_add_with_username_repeated_command_does_not_create_duplicate_user(tmp_path):
    """Повтор той же команды (например, оператор случайно отправил её
    дважды) не должен создавать дубликат пользователя — второй раз
    find_by_username() уже находит его в локальной БД, Telegram вообще не
    запрашивается повторно."""
    entity = TelethonUser(
        id=555, username="santinorussia", first_name="Santino", last_name=None,
        access_hash=999888, bot=False,
    )
    client = _FakeTelegramClient(entities={"santinorussia": entity})
    user_repository = _FakeUserRepository()
    fx = _Fixture(tmp_path, user_repository=user_repository, telegram_client=client)
    try:
        await fx.command.handle(_ctx(["add", "A123AA777", "@santinorussia"]))
        # Тот же номер автомобиля уже отслеживается — второй раз с тем же
        # периодом это ожидаемо validate_no_overlap-ошибка, поэтому берём
        # ДРУГОЙ номер для второго вызова той же команды с тем же @username.
        await fx.command.handle(_ctx(["add", "B999BB999", "@santinorussia"]))

        assert len(user_repository.upsert_calls) == 1  # НЕ два
        assert client.get_entity_calls == ["santinorussia"]  # второй раз не резолвился

        found_a = fx.user_repository.find_by_car_number("A123AA777")
        found_b = fx.user_repository.find_by_car_number("B999BB999")
        assert [u.user_id for u in found_a] == [555]
        assert [u.user_id for u in found_b] == [555]
    finally:
        fx.close()


async def test_fine_add_with_username_race_reads_back_authoritative_row_after_upsert(tmp_path):
    """Race: между первым find_by_username() (до резолва — пусто) и
    upsert() внутри _resolve_and_store_new_user тот же user_id мог быть
    записан ПАРАЛЛЕЛЬНО (см. задачу). Проверяем, что код читает владельца
    ЗАНОВО из репозитория ПОСЛЕ upsert(), а не собирает _OwnerLinkResult
    напрямую из Telegram entity — поэтому в результате всегда то, что
    реально в БД (в том числе значения, записанные конкурентно), без
    дублей и без потери гонки."""
    concurrently_written = TelegramUserInfo(
        user_id=555, username="santinorussia", first_name="Записано параллельно", last_name=None,
        access_hash=999888,
    )

    class _RepositoryWithConcurrentWriter(_FakeUserRepository):
        """Первый find_by_username() (до резолва) — пусто. После upsert() —
        как будто в БД уже оказалась запись от параллельного писателя (и
        find_by_username(), и find_by_car_number() читают её), а не
        буквально то, что только что передал наш upsert()."""

        def __init__(self):
            super().__init__()
            self._lookups = 0

        def find_by_username(self, username):
            self._lookups += 1
            if self._lookups == 1:
                return None
            return concurrently_written

        def find_by_car_number(self, car_number):
            if car_number == "A123AA777" and self.upsert_calls:
                return [concurrently_written]
            return super().find_by_car_number(car_number)

    entity = TelethonUser(
        id=555, username="santinorussia", first_name="Santino", last_name=None,
        access_hash=999888, bot=False,
    )
    client = _FakeTelegramClient(entities={"santinorussia": entity})
    user_repository = _RepositoryWithConcurrentWriter()
    fx = _Fixture(tmp_path, user_repository=user_repository, telegram_client=client)
    try:
        result = await fx.command.handle(_ctx(["add", "A123AA777", "@santinorussia"]))

        # upsert() всё равно вызван один раз (идемпотентно на стороне
        # настоящего UserRepository — INSERT ... ON CONFLICT DO UPDATE, без
        # дублей), но итоговый владелец в ответе оператору — из СВЕЖЕГО
        # чтения репозитория, а не собран напрямую из Telegram entity.
        assert len(user_repository.upsert_calls) == 1
        assert "✅ Номер добавлен на мониторинг" in result.text
        # Значение first_name — из СВЕЖЕГО чтения (concurrently_written),
        # а не из Telegram entity ("Santino") — подтверждает read-after-write.
        assert "👤 Записано параллельно (@santinorussia)" in result.text
    finally:
        fx.close()


async def test_fine_add_with_username_fails_when_no_user_repository_wired(fx):
    # user_repository вообще не передан (как у fx по умолчанию) — нельзя
    # проверить username, значит по той же логике, что и "не найден".
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "A123AA777", "@owner"]))

    assert "не найден" in exc_info.value.message
    assert fx.task_repository.list_active() == []


async def test_fine_add_with_username_already_owning_number_is_idempotent(tmp_path):
    owner = TelegramUserInfo(user_id=222, username="owner", first_name=None, last_name=None)
    fx = _Fixture(
        tmp_path,
        user_repository=_FakeUserRepository(users_by_car_number={"A123AA777": [owner]}, users=[owner]),
    )
    try:
        result = await fx.command.handle(_ctx(["add", "A123AA777", "@owner"]))

        assert "👤 @owner" in result.text
        assert "⚠️" not in result.text

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert [u.user_id for u in found] == [222]  # без дублей
    finally:
        fx.close()


async def test_fine_add_with_username_re_adding_existing_owner_among_several_is_idempotent(tmp_path):
    """car_number уже связан с несколькими пользователями (валидное
    состояние, см. задачу) — повторное добавление ОДНОГО ИЗ них не должно
    создавать дубль связи и не должно затрагивать остальных владельцев."""
    user1 = TelegramUserInfo(user_id=1, username="user1", first_name=None, last_name=None)
    user2 = TelegramUserInfo(user_id=2, username="user2", first_name=None, last_name=None)
    fx = _Fixture(
        tmp_path,
        user_repository=_FakeUserRepository(
            users_by_car_number={"A123AA777": [user1, user2]}, users=[user1, user2],
        ),
    )
    try:
        result = await fx.command.handle(_ctx(["add", "A123AA777", "@user2"]))

        assert "👤 @user1, @user2" in result.text  # показаны ВСЕ владельцы
        assert "⚠️" not in result.text

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert sorted(u.user_id for u in found) == [1, 2]  # без дублей, user1 не тронут
    finally:
        fx.close()


async def test_fine_add_with_username_links_second_owner_when_car_already_has_one_owner(tmp_path):
    """Один автомобиль может быть валидно связан сразу с несколькими
    Telegram-пользователями (см. задачу) — car_number уже связан с user1,
    добавление user2 не должно быть ошибкой: user2 связывается
    ДОПОЛНИТЕЛЬНО, user1 не отвязывается, задача мониторинга создаётся как
    обычно (номер ещё не был на активном мониторинге)."""
    user1 = TelegramUserInfo(user_id=1, username="user1", first_name=None, last_name=None)
    user2 = TelegramUserInfo(user_id=2, username="user2", first_name=None, last_name=None)
    fx = _Fixture(
        tmp_path,
        user_repository=_FakeUserRepository(
            users_by_car_number={"A123AA777": [user1]}, users=[user1, user2],
        ),
    )
    try:
        result = await fx.command.handle(_ctx(["add", "A123AA777", "@user2"]))

        assert "✅ Номер добавлен на мониторинг" in result.text
        assert "⚠️" not in result.text

        [task] = fx.task_repository.list_active()
        assert task.car_number == "A123AA777"

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert sorted(u.user_id for u in found) == [1, 2]  # оба владельца, user1 не отвязан
    finally:
        fx.close()


async def test_fine_add_with_username_links_third_owner_when_car_already_has_two_owners(tmp_path):
    """Наличие 2+ пользователей у одного car_number — валидное состояние,
    не конфликт: третий пользователь добавляется точно так же
    дополнительно, без ошибки."""
    user1 = TelegramUserInfo(user_id=1, username="user1", first_name=None, last_name=None)
    user2 = TelegramUserInfo(user_id=2, username="user2", first_name=None, last_name=None)
    user3 = TelegramUserInfo(user_id=3, username="user3", first_name=None, last_name=None)
    fx = _Fixture(
        tmp_path,
        user_repository=_FakeUserRepository(
            users_by_car_number={"A123AA777": [user1, user2]}, users=[user1, user2, user3],
        ),
    )
    try:
        result = await fx.command.handle(_ctx(["add", "A123AA777", "@user3"]))

        assert "✅ Номер добавлен на мониторинг" in result.text
        assert "⚠️" not in result.text

        [task] = fx.task_repository.list_active()
        assert task.car_number == "A123AA777"

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert sorted(u.user_id for u in found) == [1, 2, 3]  # все трое связаны
    finally:
        fx.close()


async def test_fine_add_with_username_linking_additional_owner_does_not_affect_unrelated_tasks(tmp_path):
    """Добавление второго владельца для ОДНОГО car_number не должно
    затрагивать задачи мониторинга других, не связанных с ним номеров."""
    user1 = TelegramUserInfo(user_id=1, username="user1", first_name=None, last_name=None)
    user2 = TelegramUserInfo(user_id=2, username="user2", first_name=None, last_name=None)
    fx = _Fixture(
        tmp_path,
        user_repository=_FakeUserRepository(
            users_by_car_number={"A123AA777": [user1]}, users=[user1, user2],
        ),
    )
    try:
        await fx.command.handle(_ctx(["add", "B999BB999"]))
        [unrelated_task_before] = fx.task_repository.list_active()

        await fx.command.handle(_ctx(["add", "A123AA777", "@user2"]))

        tasks_after = fx.task_repository.list_active()
        assert len(tasks_after) == 2
        unrelated_task_after = next(t for t in tasks_after if t.car_number == "B999BB999")
        assert unrelated_task_after.id == unrelated_task_before.id
    finally:
        fx.close()


async def test_fine_add_with_username_normalizes_car_number(tmp_path):
    owner = TelegramUserInfo(user_id=222, username="owner", first_name=None, last_name=None)
    fx = _Fixture(tmp_path, user_repository=_FakeUserRepository(users=[owner]))
    try:
        await fx.command.handle(_ctx(["add", "a123aa777", "@owner"]))

        [task] = fx.task_repository.list_active()
        assert task.car_number == "A123AA777"

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert [u.user_id for u in found] == [222]
    finally:
        fx.close()


async def test_fine_add_with_explicit_dates_and_username(tmp_path):
    owner = TelegramUserInfo(user_id=222, username="owner", first_name="Иван", last_name="Петров")
    fx = _Fixture(tmp_path, user_repository=_FakeUserRepository(users=[owner]))
    try:
        result = await fx.command.handle(
            _ctx(["add", "A123AA777", "01.08.2026", "31.08.2026", "@owner"])
        )

        assert "📅 Мониторинг: 01.08.2026 — 31.08.2026" in result.text
        assert "👤 Иван Петров (@owner)" in result.text
    finally:
        fx.close()


# ---- fine add @username: номер уже на активном мониторинге -> обогащаем
# существующую запись владельцем, вместо второй (дублирующей) задачи ----


async def test_fine_add_with_username_enriches_existing_monitoring_when_username_known_locally(tmp_path):
    """Номер уже на активном мониторинге БЕЗ владельца; @username уже есть
    в локальной БД — обогащаем существующую запись, вторую НЕ создаём."""
    owner = TelegramUserInfo(user_id=222, username="owner", first_name=None, last_name=None)
    fx = _Fixture(tmp_path, user_repository=_FakeUserRepository(users=[owner]))
    try:
        await fx.command.handle(_ctx(["add", "A123AA777"]))  # без владельца, как раньше
        [task_before] = fx.task_repository.list_active()

        result = await fx.command.handle(_ctx(["add", "A123AA777", "@owner"]))

        assert "✅ Номер добавлен на мониторинг" in result.text
        assert "👤 @owner" in result.text

        # НЕ создана вторая (дублирующая) задача мониторинга того же номера.
        [task_after] = fx.task_repository.list_active()
        assert task_after.id == task_before.id

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert [u.user_id for u in found] == [222]
    finally:
        fx.close()


async def test_fine_add_with_username_not_in_db_resolves_and_enriches_existing_monitoring(tmp_path):
    """Номер уже на активном мониторинге БЕЗ владельца; @username НЕТ в
    локальной БД — резолвим через Telegram (см. 3948738), создаём
    пользователя, и обогащаем существующую запись мониторинга (а не только
    создаём пользователя без применения к уже существующему номеру)."""
    entity = TelethonUser(
        id=555, username="santinorussia", first_name="Santino", last_name=None,
        access_hash=999888, bot=False,
    )
    client = _FakeTelegramClient(entities={"santinorussia": entity})
    user_repository = _FakeUserRepository()
    fx = _Fixture(tmp_path, user_repository=user_repository, telegram_client=client)
    try:
        await fx.command.handle(_ctx(["add", "A123AA777"]))
        [task_before] = fx.task_repository.list_active()

        result = await fx.command.handle(_ctx(["add", "A123AA777", "@santinorussia"]))

        assert "✅ Номер добавлен на мониторинг" in result.text
        assert "👤 Santino (@santinorussia)" in result.text

        [task_after] = fx.task_repository.list_active()
        assert task_after.id == task_before.id  # без дубля задачи
        assert len(user_repository.upsert_calls) == 1  # пользователь создан ровно один раз

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert [u.user_id for u in found] == [555]
    finally:
        fx.close()


async def test_fine_add_with_username_already_linked_to_existing_monitoring_updates_period(tmp_path):
    """Номер уже на мониторинге И уже связан именно с этим @username —
    ни вторая задача, ни дубль пользователя/car_numbers, но период всё
    равно перезаписывается на today..today+30 (см. задачу: "Повторное
    добавление того же user также должно обновлять период")."""
    today = datetime.now(timezone.utc).astimezone(_TBILISI).date()
    owner = TelegramUserInfo(user_id=222, username="owner", first_name=None, last_name=None)
    fx = _Fixture(tmp_path, user_repository=_FakeUserRepository(users=[owner]))
    try:
        await fx.command.handle(_ctx(["add", "A123AA777", "@owner"]))
        [task_before] = fx.task_repository.list_active()

        result = await fx.command.handle(_ctx(["add", "A123AA777", "@owner"]))

        assert "✅ Номер добавлен на мониторинг" in result.text
        assert "👤 @owner" in result.text

        [task_after] = fx.task_repository.list_active()
        assert task_after.id == task_before.id
        assert task_after.start_date == today
        assert task_after.end_date == today + timedelta(days=30)

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert [u.user_id for u in found] == [222]  # без дублей
    finally:
        fx.close()


async def test_fine_add_with_username_links_second_owner_on_existing_monitoring_without_duplicate_task(
    tmp_path,
):
    """Номер уже на активном мониторинге и связан с ОДНИМ пользователем —
    добавление ВТОРОГО @username не конфликт: пользователь связывается
    дополнительно, первый не отвязывается, вторая (дублирующая) задача
    мониторинга НЕ создаётся (см. задачу про требование №1)."""
    old_owner = TelegramUserInfo(user_id=1, username="old_username", first_name=None, last_name=None)
    new_owner = TelegramUserInfo(user_id=2, username="new_username", first_name=None, last_name=None)
    fx = _Fixture(tmp_path, user_repository=_FakeUserRepository(users=[old_owner, new_owner]))
    try:
        await fx.command.handle(_ctx(["add", "A123AA777", "@old_username"]))
        [task_before] = fx.task_repository.list_active()

        result = await fx.command.handle(_ctx(["add", "A123AA777", "@new_username"]))

        assert "✅ Номер добавлен на мониторинг" in result.text
        assert "@new_username" in result.text

        [task_after] = fx.task_repository.list_active()
        assert task_after.id == task_before.id  # без второй задачи

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert sorted(u.user_id for u in found) == [1, 2]  # оба владельца связаны
    finally:
        fx.close()


async def test_fine_add_with_username_existing_owner_without_username_enriched_by_same_telegram_id(
    tmp_path,
):
    """Владелец car_number уже известен по user_id, но БЕЗ username
    (например, попал в БД через forward/историю без ника) — оператор
    передаёт @username, который Telegram резолвит в ТОТ ЖЕ user_id: профиль
    обогащается (username/имя сохраняются на ТУ ЖЕ запись), новый
    пользователь не создаётся, номер не дублируется."""
    owner_without_username = TelegramUserInfo(
        user_id=555, username=None, first_name=None, last_name=None,
    )
    entity = TelethonUser(
        id=555, username="santinorussia", first_name="Santino", last_name=None,
        access_hash=999888, bot=False,
    )
    client = _FakeTelegramClient(entities={"santinorussia": entity})
    user_repository = _FakeUserRepository(
        users_by_car_number={"A123AA777": [owner_without_username]}, users=[owner_without_username],
    )
    fx = _Fixture(tmp_path, user_repository=user_repository, telegram_client=client)
    try:
        await fx.command.handle(_ctx(["add", "B999BB999"]))  # просто чтобы была хоть одна задача
        car_number_tasks_before = fx.task_repository.get_active_by_car_number("A123AA777")
        assert car_number_tasks_before == []  # у A123AA777 задачи мониторинга ещё нет вовсе —
        # обогащается именно СВЯЗЬ пользователь<->номер (users.car_numbers),
        # не запись fine_monitoring_tasks (см. задачу: "изучи модель данных").

        result = await fx.command.handle(_ctx(["add", "A123AA777", "@santinorussia"]))

        assert "✅ Номер добавлен на мониторинг" in result.text
        assert "👤 Santino (@santinorussia)" in result.text

        # upsert() обновил ТУ ЖЕ запись (тот же user_id=555) — не создал новую.
        [updated] = user_repository.upsert_calls
        assert updated.user_id == 555
        assert updated.username == "santinorussia"

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert [u.user_id for u in found] == [555]  # тот же пользователь, без дублей
    finally:
        fx.close()


async def test_fine_add_with_username_existing_owner_without_username_gets_a_second_owner_linked(
    tmp_path,
):
    """Владелец car_number уже известен по user_id (без username), а
    переданный @username Telegram резолвит в ДРУГОЙ user_id — это не
    конфликт: второй пользователь связывается с этим же car_number
    дополнительно, первый (без username) не отвязывается."""
    owner_without_username = TelegramUserInfo(
        user_id=555, username=None, first_name=None, last_name=None,
    )
    different_entity = TelethonUser(
        id=999, username="someone_else", first_name="Someone", last_name=None,
        access_hash=111222, bot=False,
    )
    client = _FakeTelegramClient(entities={"someone_else": different_entity})
    user_repository = _FakeUserRepository(
        users_by_car_number={"A123AA777": [owner_without_username]}, users=[owner_without_username],
    )
    fx = _Fixture(tmp_path, user_repository=user_repository, telegram_client=client)
    try:
        result = await fx.command.handle(_ctx(["add", "A123AA777", "@someone_else"]))

        assert "✅ Номер добавлен на мониторинг" in result.text

        [task] = fx.task_repository.list_active()
        assert task.car_number == "A123AA777"

        # @someone_else реально резолвлен и закэширован в users (как и в
        # 3948738) И связан с этим car_number, первый владелец не тронут.
        [cached] = user_repository.upsert_calls
        assert cached.user_id == 999
        found = fx.user_repository.find_by_car_number("A123AA777")
        assert sorted(u.user_id for u in found) == [555, 999]
    finally:
        fx.close()


async def test_fine_add_with_username_resolve_failure_leaves_existing_monitoring_unchanged(tmp_path):
    """Номер уже на активном мониторинге БЕЗ владельца; Telegram-резолв
    нового @username не удаётся (не найден) — существующая задача
    мониторинга остаётся полностью без изменений (даты/статус), владелец
    не привязывается."""
    client = _FakeTelegramClient(not_found_usernames=["ghost_user"])
    user_repository = _FakeUserRepository()
    fx = _Fixture(tmp_path, user_repository=user_repository, telegram_client=client)
    try:
        await fx.command.handle(_ctx(["add", "A123AA777"]))
        [task_before] = fx.task_repository.list_active()

        with pytest.raises(CommandError) as exc_info:
            await fx.command.handle(_ctx(["add", "A123AA777", "@ghost_user"]))

        assert "не найден" in exc_info.value.message
        assert "в базе" not in exc_info.value.message

        [task_after] = fx.task_repository.list_active()
        assert task_after == task_before  # задача мониторинга не изменилась вовсе
        assert user_repository.upsert_calls == []
        assert fx.user_repository.find_by_car_number("A123AA777") == []
    finally:
        fx.close()


async def test_fine_add_with_username_enrichment_resets_period_but_preserves_other_settings(
    tmp_path,
):
    """Период существующей задачи мониторинга (start_date/end_date)
    ПЕРЕЗАПИСЫВАЕТСЯ на today..today+30 при обогащении владельцем (см.
    задачу про изменение бизнес-логики) — но id/статус/telegram_chat_id/
    created_by_user_id НЕ меняются: единственные изменения — период и
    привязка пользователя к car_number (users.car_numbers)."""
    today = datetime.now(timezone.utc).astimezone(_TBILISI).date()
    owner = TelegramUserInfo(user_id=222, username="owner", first_name=None, last_name=None)
    fx = _Fixture(tmp_path, user_repository=_FakeUserRepository(users=[owner]))
    try:
        await fx.command.handle(
            _ctx(["add", "A123AA777", "01.08.2026", "31.08.2026"], user_id=333)
        )
        [task_before] = fx.task_repository.list_active()

        await fx.command.handle(_ctx(["add", "A123AA777", "15.08.2026", "20.08.2026", "@owner"]))

        [task_after] = fx.task_repository.list_active()
        assert task_after.id == task_before.id
        assert task_after.start_date == today
        assert task_after.end_date == today + timedelta(days=30)
        assert task_after.status == task_before.status
        assert task_after.telegram_chat_id == task_before.telegram_chat_id
        assert task_after.created_by_user_id == task_before.created_by_user_id
    finally:
        fx.close()


async def test_fine_add_rejects_bare_at_symbol(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "A123AA777", "@"]))

    assert "Неверный формат команды" in exc_info.value.message
    assert fx.task_repository.list_active() == []


# ---- fine add: повторное добавление автомобиля с активной задачей -------
# ---- всегда сбрасывает период на today..today+30 (см. задачу про --------
# ---- изменение бизнес-логики повторного добавления) ----------------------


async def test_fine_add_shortens_period_when_old_end_date_is_later_than_today_plus_30(tmp_path):
    """Старый end_date позже today+30 — период всё равно заменяется на
    today..today+30, а не сохраняется/продлевается (см. задачу)."""
    today = datetime.now(timezone.utc).astimezone(_TBILISI).date()
    fx = _Fixture(tmp_path, records_by_car={"AA001AA": []})
    try:
        far_future_end = (today + timedelta(days=90)).strftime("%d.%m.%Y")
        await fx.command.handle(
            _ctx(["add", "AA001AA", today.strftime("%d.%m.%Y"), far_future_end])
        )
        [task_before] = fx.task_repository.list_active()
        assert task_before.end_date == today + timedelta(days=90)

        result = await fx.command.handle(_ctx(["add", "AA001AA"]))

        [task_after] = fx.task_repository.list_active()
        assert task_after.id == task_before.id
        assert task_after.start_date == today
        assert task_after.end_date == today + timedelta(days=30)
        assert "✅ Номер добавлен на мониторинг" in result.text
    finally:
        fx.close()


async def test_fine_add_extends_period_when_old_end_date_is_earlier_than_today_plus_30(tmp_path):
    """Старый end_date раньше today+30 (но ещё не истёк) — период тоже
    заменяется на today..today+30, а не остаётся коротким (см. задачу)."""
    today = datetime.now(timezone.utc).astimezone(_TBILISI).date()
    fx = _Fixture(tmp_path, records_by_car={"AA001AA": []})
    try:
        near_end = (today + timedelta(days=5)).strftime("%d.%m.%Y")
        await fx.command.handle(
            _ctx(["add", "AA001AA", today.strftime("%d.%m.%Y"), near_end])
        )
        [task_before] = fx.task_repository.list_active()
        assert task_before.end_date == today + timedelta(days=5)

        result = await fx.command.handle(_ctx(["add", "AA001AA"]))

        [task_after] = fx.task_repository.list_active()
        assert task_after.id == task_before.id
        assert task_after.start_date == today
        assert task_after.end_date == today + timedelta(days=30)
        assert "✅ Номер добавлен на мониторинг" in result.text
    finally:
        fx.close()


async def test_fine_add_links_third_owner_on_existing_monitoring_and_updates_period(tmp_path):
    """Добавление ТРЕТЬЕГО Telegram-пользователя на автомобиль, уже
    связанный с двумя, — не конфликт: связь добавляется дополнительно, и
    период всё равно обновляется (см. задачу: несколько владельцев —
    валидное состояние)."""
    today = datetime.now(timezone.utc).astimezone(_TBILISI).date()
    user1 = TelegramUserInfo(user_id=1, username="user1", first_name=None, last_name=None)
    user2 = TelegramUserInfo(user_id=2, username="user2", first_name=None, last_name=None)
    user3 = TelegramUserInfo(user_id=3, username="user3", first_name=None, last_name=None)
    fx = _Fixture(tmp_path, user_repository=_FakeUserRepository(users=[user1, user2, user3]))
    try:
        await fx.command.handle(_ctx(["add", "A123AA777", "@user1"]))
        await fx.command.handle(_ctx(["add", "A123AA777", "@user2"]))
        [task_before] = fx.task_repository.list_active()

        result = await fx.command.handle(_ctx(["add", "A123AA777", "@user3"]))

        assert "✅ Номер добавлен на мониторинг" in result.text
        [task_after] = fx.task_repository.list_active()
        assert task_after.id == task_before.id
        assert task_after.start_date == today
        assert task_after.end_date == today + timedelta(days=30)

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert sorted(u.user_id for u in found) == [1, 2, 3]
    finally:
        fx.close()


async def test_fine_add_does_not_touch_completed_task_creates_new_one_instead(tmp_path):
    """Завершённая (completed) задача НЕ считается активной (см.
    get_active_by_car_number) — повторное добавление того же номера
    создаёт НОВУЮ задачу обычным путём, а completed-задача остаётся как
    есть, ни один её атрибут не меняется (см. задачу: "Исторические
    completed tasks не менять")."""
    fx = _Fixture(tmp_path)
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.01.2026", "31.01.2026"]))
        [old_task] = fx.task_repository.list_active()
        fx.task_repository.set_status(old_task.id, "completed")
        completed_before = fx.task_repository.get(old_task.id)
        assert completed_before.status == "completed"

        result = await fx.command.handle(_ctx(["add", "AA001AA"]))

        # Обычное создание — активной задачи не было (completed не считается).
        assert "✅ Номер добавлен на мониторинг" in result.text
        [new_task] = fx.task_repository.list_active()
        assert new_task.id != old_task.id

        completed_after = fx.task_repository.get(old_task.id)
        assert completed_after == completed_before  # завершённая задача не изменилась вовсе
    finally:
        fx.close()


async def test_fine_add_runs_immediate_check_on_both_new_creation_and_reset(tmp_path):
    """Немедленная проверка штрафов выполняется И для нового автомобиля, И
    для сброса периода уже отслеживаемого (см. задачу) — тот же самый
    механизм (FineCheckService.check_task() +
    FineNotificationCoordinator.flush_pending()), что и "fine check"/"fine
    update-all", без параллельной реализации. Новый штраф, найденный при
    первом создании, реально отправляется через notification_service, а
    не только упоминается в тексте ответа; повторное добавление того же
    номера видит тот же штраф уже не новым."""
    fx = _Fixture(tmp_path, records_by_car={"AA001AA": [_record(car_number="AA001AA")]})
    try:
        first_result = await fx.command.handle(_ctx(["add", "AA001AA"]))

        assert "🔎 Штрафы проверены: найдено новых — 1" in first_result.text
        assert len(fx.notification_service.notify_calls) == 1  # реально отправлено

        second_result = await fx.command.handle(_ctx(["add", "AA001AA"]))

        assert "🔎 Штрафы проверены: новых штрафов нет" in second_result.text
        assert len(fx.notification_service.notify_calls) == 1  # не задублировано
    finally:
        fx.close()


# ---- fine add: единое итоговое сообщение оператору (см. задачу) ---------


async def test_fine_add_summary_no_fines_found_shows_all_required_fields(tmp_path):
    """Успешное добавление без штрафов — итоговое сообщение содержит все
    обязательные поля: номер, всех связанных Telegram-пользователей,
    фактические start_date/end_date, результат немедленной проверки."""
    owner1 = TelegramUserInfo(user_id=1, username="MarysuaZ", first_name=None, last_name=None)
    owner2 = TelegramUserInfo(user_id=2, username="VeronaWarm", first_name=None, last_name=None)
    today = datetime.now(timezone.utc).astimezone(_TBILISI).date()
    fx = _Fixture(
        tmp_path,
        records_by_car={"M295YB196": []},
        user_repository=_FakeUserRepository(users=[owner1, owner2]),
    )
    try:
        await fx.command.handle(_ctx(["add", "M295YB196", "@MarysuaZ"]))
        result = await fx.command.handle(_ctx(["add", "M295YB196", "@VeronaWarm"]))

        expected_period = (
            f"{today.strftime('%d.%m.%Y')} — {(today + timedelta(days=30)).strftime('%d.%m.%Y')}"
        )
        assert result.text == (
            "✅ Номер добавлен на мониторинг\n\n"
            "🚗 M295YB196\n"
            "👤 @MarysuaZ, @VeronaWarm\n"
            f"📅 Мониторинг: {expected_period}\n"
            "🔎 Штрафы проверены: новых штрафов нет"
        )
    finally:
        fx.close()


async def test_fine_add_summary_reports_fines_found_without_duplicating_detailed_notification(
    tmp_path,
):
    """Если немедленная проверка находит штраф — сообщение отражает
    фактический результат (не "новых штрафов нет"), а подробное
    уведомление о самом штрафе доставляется отдельно, тем же
    FineNotificationCoordinator, без дублирования в итоговом сообщении."""
    fx = _Fixture(tmp_path, records_by_car={"M295YB196": [_record(car_number="M295YB196")]})
    try:
        result = await fx.command.handle(_ctx(["add", "M295YB196"]))

        assert "✅ Номер добавлен на мониторинг" in result.text
        assert "🔎 Штрафы проверены: найдено новых — 1" in result.text
        assert "новых штрафов нет" not in result.text
        # Итоговое сообщение короткое — не пересказывает сам штраф (номер
        # протокола/даты и т.п.), это уже задача отдельного уведомления.
        assert "AB123456" not in result.text

        # Подробное уведомление реально отправлено — ровно один раз, тем
        # же координатором, не второй параллельной реализацией.
        assert len(fx.notification_service.notify_calls) == 1
        [sent_event] = fx.notification_service.notify_calls[0]
        assert sent_event.external_fine_id == "AB123456"
    finally:
        fx.close()


async def test_fine_add_summary_shows_warning_when_immediate_check_fails(tmp_path):
    """Немедленная проверка завершилась ошибкой — задача мониторинга и
    связи с Telegram-пользователями всё равно сохранены (см. задачу), но
    сообщение явно предупреждает, а не заявляет "Штрафы проверены"."""
    owner = TelegramUserInfo(user_id=222, username="owner", first_name=None, last_name=None)
    fx = _Fixture(
        tmp_path,
        provider_error=FineProviderError("police.ge недоступен"),
        user_repository=_FakeUserRepository(users=[owner]),
    )
    try:
        result = await fx.command.handle(_ctx(["add", "M295YB196", "@owner"]))

        assert result.text.startswith(
            "⚠️ Номер добавлен на мониторинг, но проверить штрафы сейчас не удалось"
        )
        assert "Штрафы проверены" not in result.text
        assert "🔎 Проверка не выполнена: police.ge недоступен" in result.text
        assert "🚗 M295YB196" in result.text
        assert "👤 @owner" in result.text

        # Задача и связь с владельцем всё равно сохранены.
        [task] = fx.task_repository.list_active()
        assert task.car_number == "M295YB196"
        found = fx.user_repository.find_by_car_number("M295YB196")
        assert [u.user_id for u in found] == [222]

        # Ни одного уведомления не отправлено — проверка не завершилась успешно.
        assert fx.notification_service.notify_calls == []
    finally:
        fx.close()


async def test_fine_add_summary_shows_all_telegram_users_for_reused_car(tmp_path):
    """Реальный случай из задачи: M295YB196 уже связан с @MarysuaZ и
    @VeronaWarm — повторное добавление (без нового @username) должно
    показать ОБОИХ в итоговом сообщении, а не одного/никого."""
    owner1 = TelegramUserInfo(user_id=1, username="MarysuaZ", first_name=None, last_name=None)
    owner2 = TelegramUserInfo(user_id=2, username="VeronaWarm", first_name=None, last_name=None)
    fx = _Fixture(
        tmp_path,
        records_by_car={"M295YB196": []},
        user_repository=_FakeUserRepository(users_by_car_number={"M295YB196": [owner1, owner2]}),
    )
    try:
        result = await fx.command.handle(_ctx(["add", "M295YB196"]))

        assert "👤 @MarysuaZ, @VeronaWarm" in result.text
    finally:
        fx.close()


# ---- fine add bulk ----


async def test_fine_add_bulk_with_explicit_dates(fx):
    result = await fx.command.handle(
        _ctx(
            [
                "add", "bulk", "04.08.2026", "04.09.2026",
                "H663KH702", "C072H0977", "M012KT193", "P701XY126",
            ]
        )
    )

    assert result.text == "✅ Добавлено: 4\n⚠️ Уже отслеживаются: 0\n❌ Ошибок: 0"

    tasks = {task.car_number: task for task in fx.task_repository.list_active()}
    assert set(tasks) == {"H663KH702", "C072H0977", "M012KT193", "P701XY126"}
    for task in tasks.values():
        assert task.start_date == date(2026, 8, 4)
        assert task.end_date == date(2026, 9, 4)
        assert task.telegram_chat_id == _CHAT_ID
        assert task.created_by_user_id == _USER_ID


async def test_fine_add_bulk_without_dates_defaults_to_today_plus_30_days(fx):
    result = await fx.command.handle(_ctx(["add", "bulk", "H663KH702", "C072H0977"]))

    assert result.text == "✅ Добавлено: 2\n⚠️ Уже отслеживаются: 0\n❌ Ошибок: 0"

    tasks = fx.task_repository.list_active()
    assert len(tasks) == 2
    for task in tasks:
        assert (task.end_date - task.start_date) == timedelta(days=30)


async def test_fine_add_bulk_normalizes_car_numbers(fx):
    await fx.command.handle(_ctx(["add", "bulk", "h663kh702", " c072h0977 "]))

    car_numbers = {task.car_number for task in fx.task_repository.list_active()}
    assert car_numbers == {"H663KH702", "C072H0977"}


async def test_fine_add_bulk_deduplicates_within_message_preserving_order(fx):
    result = await fx.command.handle(
        _ctx(["add", "bulk", "H663KH702", "C072H0977", "H663KH702", "h663kh702"])
    )

    assert result.text == "✅ Добавлено: 2\n⚠️ Уже отслеживаются: 0\n❌ Ошибок: 0"

    car_numbers = sorted(task.car_number for task in fx.task_repository.list_active())
    assert car_numbers == ["C072H0977", "H663KH702"]


async def test_fine_add_bulk_reports_already_tracked_for_existing_active_task(fx):
    await fx.command.handle(_ctx(["add", "H663KH702", "01.08.2026", "31.08.2026"]))

    result = await fx.command.handle(
        _ctx(["add", "bulk", "15.08.2026", "20.09.2026", "H663KH702", "C072H0977"])
    )

    assert result.text == "✅ Добавлено: 1\n⚠️ Уже отслеживаются: 1\n❌ Ошибок: 0"
    assert len(fx.task_repository.list_active()) == 2


async def test_fine_add_bulk_reports_invalid_car_number_among_valid_ones(fx):
    result = await fx.command.handle(
        _ctx(["add", "bulk", "H663KH702", "AA-001-AA", "C072H0977"])
    )

    assert "✅ Добавлено: 2" in result.text
    assert "⚠️ Уже отслеживаются: 0" in result.text
    assert "❌ Ошибок: 1" in result.text
    assert "Ошибки:" in result.text
    assert "• AA-001-AA — " in result.text

    car_numbers = {task.car_number for task in fx.task_repository.list_active()}
    assert car_numbers == {"H663KH702", "C072H0977"}


async def test_fine_add_bulk_error_in_one_number_does_not_block_others(fx):
    # Невалидный номер посередине списка — оба соседних валидных всё равно
    # должны быть добавлены, ошибка одного не откатывает остальные.
    result = await fx.command.handle(
        _ctx(["add", "bulk", "H663KH702", "AA-001-AA", "C072H0977", "###", "M012KT193"])
    )

    assert "✅ Добавлено: 3" in result.text
    assert "❌ Ошибок: 2" in result.text

    car_numbers = {task.car_number for task in fx.task_repository.list_active()}
    assert car_numbers == {"H663KH702", "C072H0977", "M012KT193"}


async def test_fine_add_bulk_all_succeed_omits_errors_block(fx):
    result = await fx.command.handle(_ctx(["add", "bulk", "H663KH702", "C072H0977"]))

    assert "Ошибки:" not in result.text


async def test_fine_add_bulk_with_no_car_numbers_shows_format_example(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "bulk"]))

    assert "Неверный формат команды" in exc_info.value.message
    assert "fine add bulk" in exc_info.value.message
    assert fx.task_repository.list_active() == []


async def test_fine_add_bulk_with_dates_but_no_car_numbers_shows_format_example(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "bulk", "04.08.2026", "04.09.2026"]))

    assert "Неверный формат команды" in exc_info.value.message
    assert fx.task_repository.list_active() == []


async def test_fine_add_bulk_rejects_over_limit(fx):
    car_numbers = [f"AA{i:03d}AA" for i in range(101)]

    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "bulk", *car_numbers]))

    assert "Слишком много номеров" in exc_info.value.message
    # Превышение лимита проверяется до обработки — ни один номер не добавлен.
    assert fx.task_repository.list_active() == []


async def test_fine_add_bulk_accepts_exactly_the_limit(fx):
    car_numbers = [f"AA{i:03d}AA" for i in range(100)]

    result = await fx.command.handle(_ctx(["add", "bulk", *car_numbers]))

    assert "✅ Добавлено: 100" in result.text
    assert len(fx.task_repository.list_active()) == 100


# ---- fine add-bulk (многострочное Telegram-сообщение) ----


async def test_fine_add_bulk_command_from_real_multiline_telegram_message(fx):
    # Именно так текст выглядел бы в реальном Telegram-сообщении — одна
    # команда "fine add-bulk", затем каждый номер на отдельной строке.
    raw_text = "fine add-bulk\nA111AA111\nB222BB222\nC333CC333"
    args = _split_into_args(raw_text)[1:]  # диспетчер отдаёт "fine" отдельно

    result = await fx.command.handle(_ctx(args))

    assert "Добавлено: 3" in result.text
    assert "Уже в мониторинге: 0" in result.text
    assert "Некорректных: 0" in result.text
    assert "Ошибок: 0" in result.text

    car_numbers = {task.car_number for task in fx.task_repository.list_active()}
    assert car_numbers == {"A111AA111", "B222BB222", "C333CC333"}


async def test_fine_add_bulk_command_produces_identical_task_to_sequential_fine_add(fx):
    # "fine add-bulk" в одном сообщении должно давать ИДЕНТИЧНЫЙ результат
    # последовательным fine add NUMBER — сравниваем реально созданные
    # задачи (все поля, кроме car_number/id/created_at), а не только счётчики.
    await fx.command.handle(_ctx(["add", "A111AA111"]))
    [via_single_add] = fx.task_repository.list_active()

    await fx.command.handle(_ctx(["add-bulk", "B222BB222"]))
    tasks = {t.car_number: t for t in fx.task_repository.list_active()}
    via_bulk = tasks["B222BB222"]

    assert via_bulk.start_date == via_single_add.start_date
    assert via_bulk.end_date == via_single_add.end_date
    assert via_bulk.status == via_single_add.status == "active"
    assert via_bulk.telegram_chat_id == via_single_add.telegram_chat_id == _CHAT_ID
    assert via_bulk.created_by_user_id == via_single_add.created_by_user_id == _USER_ID
    assert via_bulk.label == via_single_add.label is None


async def test_fine_add_bulk_command_accepts_comma_separated_numbers(fx):
    result = await fx.command.handle(_ctx(["add-bulk", "H663KH702,C072H0977", "M012KT193"]))

    assert "Добавлено: 3" in result.text
    car_numbers = {task.car_number for task in fx.task_repository.list_active()}
    assert car_numbers == {"H663KH702", "C072H0977", "M012KT193"}


async def test_fine_add_bulk_command_accepts_space_separated_numbers(fx):
    result = await fx.command.handle(_ctx(["add-bulk", "H663KH702", "C072H0977"]))

    assert "Добавлено: 2" in result.text
    car_numbers = {task.car_number for task in fx.task_repository.list_active()}
    assert car_numbers == {"H663KH702", "C072H0977"}


async def test_fine_add_bulk_command_resets_period_for_already_tracked_number(fx):
    """С новой бизнес-логикой (см. задачу про изменение поведения при
    повторном добавлении) _handle_add() больше не бросает ошибку для уже
    отслеживаемого номера — вместо этого обновляет период существующей
    задачи. add-bulk делегирует туда же (см. _handle_add_bulk_command), поэтому
    такой номер тоже считается "Добавлено", а не "Уже в мониторинге", и
    задача НЕ дублируется."""
    await fx.command.handle(_ctx(["add", "H663KH702"]))
    [task_before] = fx.task_repository.get_active_by_car_number("H663KH702")

    result = await fx.command.handle(_ctx(["add-bulk", "H663KH702", "C072H0977"]))

    assert "Добавлено: 2" in result.text
    assert "Уже в мониторинге: 0" in result.text
    assert len(fx.task_repository.list_active()) == 2
    [task_after] = fx.task_repository.get_active_by_car_number("H663KH702")
    assert task_after.id == task_before.id  # не дублирована, только период обновлён


async def test_fine_add_bulk_command_deduplicates_within_message(fx):
    # Дубль внутри самого сообщения — второе появление того же номера
    # теперь тоже успешно обрабатывается _handle_add_bulk_command() (см.
    # _handle_add): второе появление находит задачу, созданную первым, и
    # обновляет её период вместо ошибки — задача по-прежнему одна.
    result = await fx.command.handle(
        _ctx(["add-bulk", "H663KH702", "h663kh702", "C072H0977"])
    )

    assert "Добавлено: 3" in result.text
    assert "Уже в мониторинге: 0" in result.text
    car_numbers = sorted(task.car_number for task in fx.task_repository.list_active())
    assert car_numbers == ["C072H0977", "H663KH702"]


async def test_fine_add_bulk_command_reports_invalid_number_among_valid_ones(fx):
    result = await fx.command.handle(
        _ctx(["add-bulk", "H663KH702", "AA-001-AA", "C072H0977"])
    )

    assert "Добавлено: 2" in result.text
    assert "Некорректных: 1" in result.text
    assert "Некорректные номера:" in result.text
    assert "• AA-001-AA — " in result.text

    car_numbers = {task.car_number for task in fx.task_repository.list_active()}
    assert car_numbers == {"H663KH702", "C072H0977"}


async def test_fine_add_bulk_command_error_on_one_number_does_not_block_others(fx, monkeypatch):
    original_create = fx.task_repository.create

    def failing_create(*, car_number, **kwargs):
        if car_number == "C072H0977":
            raise RuntimeError("simulated db failure")
        return original_create(car_number=car_number, **kwargs)

    monkeypatch.setattr(fx.task_repository, "create", failing_create)

    result = await fx.command.handle(
        _ctx(["add-bulk", "H663KH702", "C072H0977", "M012KT193"])
    )

    assert "Добавлено: 2" in result.text
    assert "Ошибок: 1" in result.text
    assert "Ошибки:" in result.text
    assert "• C072H0977 — simulated db failure" in result.text

    car_numbers = {task.car_number for task in fx.task_repository.list_active()}
    assert car_numbers == {"H663KH702", "M012KT193"}


async def test_fine_add_bulk_command_with_no_car_numbers_shows_format_example(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add-bulk"]))

    assert "Неверный формат команды" in exc_info.value.message
    assert "fine add-bulk" in exc_info.value.message
    assert fx.task_repository.list_active() == []


async def test_fine_add_bulk_command_uses_default_period_like_single_add(fx):
    await fx.command.handle(_ctx(["add-bulk", "H663KH702"]))

    [task] = fx.task_repository.list_active()
    assert (task.end_date - task.start_date) == timedelta(days=30)


async def test_fine_add_bulk_command_does_not_affect_existing_add_bulk_command(fx):
    # fine add bulk (пробелом) — старая, отдельная от add-bulk команда,
    # должна продолжать работать буквально без изменений.
    result = await fx.command.handle(_ctx(["add", "bulk", "H663KH702", "C072H0977"]))

    assert result.text == "✅ Добавлено: 2\n⚠️ Уже отслеживаются: 0\n❌ Ошибок: 0"


# ---- fine list ----


async def test_fine_list_with_tasks(fx):
    await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))
    await fx.command.handle(_ctx(["add", "BB002BB", "01.08.2026", "31.08.2026"]))

    result = await fx.command.handle(_ctx(["list"]))

    assert "AA001AA" in result.text
    assert "BB002BB" in result.text
    assert "01.08.2026–31.08.2026" in result.text
    # Внутренний ID задачи — деталь реализации БД, оператору не нужен.
    assert "ID" not in result.text


async def test_fine_list_with_no_tasks(fx):
    result = await fx.command.handle(_ctx(["list"]))

    assert result.text == "Активных задач мониторинга нет."


async def test_fine_list_shows_last_checked_at_in_tbilisi_time(fx):
    """task.last_checked_at приходит из SQLite CURRENT_TIMESTAMP — naive,
    но фактически UTC (см. reader/time_display.py.to_tbilisi) — "Последняя
    проверка" должна показывать время, сдвинутое на +4 часа, а не сырое
    значение из БД (см. задачу про перевод отображения времени)."""
    await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))
    task = fx.task_repository.list_active()[0]

    before = datetime.now(timezone.utc)
    fx.task_repository.record_check_result(task.id, last_check_status="ok", last_error=None)
    after = datetime.now(timezone.utc)

    result = await fx.command.handle(_ctx(["list"]))

    checked_task = fx.task_repository.get(task.id)
    assert checked_task.last_checked_at is not None

    expected = to_tbilisi(checked_task.last_checked_at).strftime("%d.%m.%Y %H:%M")
    assert f"Последняя проверка: {expected}" in result.text
    # Сдвинуто ровно на +4 часа относительно окна [before, after] (UTC).
    assert to_tbilisi(before) - timedelta(minutes=1) <= to_tbilisi(checked_task.last_checked_at)
    assert to_tbilisi(checked_task.last_checked_at) <= to_tbilisi(after) + timedelta(minutes=1)


# ---- fine stop ----


async def test_fine_stop_by_car_number(fx):
    await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))
    task_id = fx.task_repository.list_active()[0].id

    result = await fx.command.handle(_ctx(["stop", "AA001AA"]))

    assert result.text == "✅ Мониторинг для AA001AA остановлен"
    assert fx.task_repository.get(task_id).status == "stopped"
    assert fx.task_repository.list_active() == []


async def test_fine_stop_normalizes_car_number(fx):
    await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

    result = await fx.command.handle(_ctx(["stop", "aa001aa"]))

    assert result.text == "✅ Мониторинг для AA001AA остановлен"
    assert fx.task_repository.list_active() == []


async def test_fine_stop_unknown_car_number_returns_command_error(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["stop", "ZZ999ZZ"]))

    assert "не найдена" in exc_info.value.message


async def test_fine_stop_already_stopped_car_returns_command_error(fx):
    await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))
    await fx.command.handle(_ctx(["stop", "AA001AA"]))

    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["stop", "AA001AA"]))

    assert "не найдена" in exc_info.value.message


async def test_fine_stop_stops_all_active_tasks_for_car_number(fx):
    # С новой бизнес-логикой (см. задачу) через "fine add" у одного номера
    # больше нельзя завести вторую активную задачу — повторное добавление
    # обновляет период существующей. Две активные задачи для одного номера
    # теперь возможны только как унаследованное/историческое состояние —
    # создаём их напрямую через репозиторий, а не через команду, но
    # fine stop всё равно должен останавливать обе.
    fx.task_repository.create(
        car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 10),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
    )
    fx.task_repository.create(
        car_number="AA001AA", label=None, start_date=date(2026, 8, 15), end_date=date(2026, 8, 20),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
    )
    task_ids = [task.id for task in fx.task_repository.list_active()]
    assert len(task_ids) == 2

    result = await fx.command.handle(_ctx(["stop", "AA001AA"]))

    assert result.text == "✅ Мониторинг для AA001AA остановлен"
    assert fx.task_repository.list_active() == []
    assert all(fx.task_repository.get(task_id).status == "stopped" for task_id in task_ids)


async def test_fine_stop_rejects_invalid_car_number(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["stop", "AA-001-AA"]))

    assert "❌" in exc_info.value.message


async def test_fine_stop_rejects_wrong_argument_count(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["stop"]))

    assert "Неверный формат команды" in exc_info.value.message


# ---- fine check ----


async def test_fine_check_by_car_number(tmp_path):
    """fine add теперь тоже запускает немедленную проверку (см. задачу) —
    единственный штраф уже учтён как новый ПРИ добавлении, поэтому
    последующий "fine check" находит его снова (тот же провайдер), но уже
    не как новый (дедуп по fingerprint+task, см. DetectedFineRepository)."""
    fx = _Fixture(tmp_path, records_by_car={"AA001AA": [_record(car_number="AA001AA")]})
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

        result = await fx.command.handle(_ctx(["check", "AA001AA"]))

        assert "✅ Проверка завершена" in result.text
        assert "Автомобиль: AA001AA" in result.text
        assert "Найдено штрафов: 1" in result.text
        assert "Новых: 0" in result.text
        assert "мс" in result.text
    finally:
        fx.close()


async def test_fine_check_shows_owner_by_car_number_not_task_creator(tmp_path):
    """Главный regression-тест против production-бага: car_number
    принадлежит владельцу A (users.car_numbers), а задачу создал (кто
    вызвал fine add, т.е. created_by_user_id) другой пользователь B —
    результат должен показывать именно A, а не B."""
    OWNER_USER_ID = 100
    CREATOR_USER_ID = 200
    fx = _Fixture(
        tmp_path,
        records_by_car={"AA001AA": [_record(car_number="AA001AA")]},
        user_repository=_FakeUserRepository(
            {"AA001AA": [TelegramUserInfo(
                user_id=OWNER_USER_ID, username="owner_ivan", first_name=None, last_name=None,
            )]}
        ),
    )
    try:
        await fx.command.handle(
            _ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"], user_id=CREATOR_USER_ID)
        )

        result = await fx.command.handle(_ctx(["check", "AA001AA"]))

        assert "Telegram: @owner_ivan" in result.text
        # Остальное содержимое результата не потеряно. Штраф уже учтён как
        # новый при "fine add" (см. test_fine_check_by_car_number) — здесь
        # снова найден, но уже не новый.
        assert "✅ Проверка завершена" in result.text
        assert "Автомобиль: AA001AA" in result.text
        assert "Найдено штрафов: 1" in result.text
        assert "Новых: 0" in result.text
        assert "мс" in result.text
    finally:
        fx.close()


async def test_fine_check_shows_username_of_owner_found_by_car_number(tmp_path):
    fx = _Fixture(
        tmp_path,
        records_by_car={"AA001AA": [_record(car_number="AA001AA")]},
        user_repository=_FakeUserRepository(
            {"AA001AA": [TelegramUserInfo(user_id=1, username="ivan_petrov", first_name=None, last_name=None)]}
        ),
    )
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

        result = await fx.command.handle(_ctx(["check", "AA001AA"]))

        assert "Telegram: @ivan_petrov" in result.text
    finally:
        fx.close()


async def test_fine_check_shows_not_found_when_car_number_unknown_to_users(tmp_path):
    fx = _Fixture(
        tmp_path,
        records_by_car={"AA001AA": [_record(car_number="AA001AA")]},
        user_repository=_FakeUserRepository({}),  # AA001AA нет ни у кого в car_numbers
    )
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

        result = await fx.command.handle(_ctx(["check", "AA001AA"]))

        assert "Telegram: не найден" in result.text
    finally:
        fx.close()


async def test_fine_check_shows_not_found_without_user_repository(tmp_path):
    # user_repository вообще не передан — результат всё равно корректен, но
    # НЕ подставляет created_by_user_id как владельца.
    fx = _Fixture(tmp_path, records_by_car={"AA001AA": [_record(car_number="AA001AA")]})
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

        result = await fx.command.handle(_ctx(["check", "AA001AA"]))

        assert "Telegram: не найден" in result.text
    finally:
        fx.close()


async def test_fine_check_shows_full_name_with_username(tmp_path):
    fx = _Fixture(
        tmp_path,
        records_by_car={"AA001AA": [_record(car_number="AA001AA")]},
        user_repository=_FakeUserRepository(
            {"AA001AA": [TelegramUserInfo(
                user_id=1, username="ivan_petrov", first_name="Иван", last_name="Петров",
            )]}
        ),
    )
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

        result = await fx.command.handle(_ctx(["check", "AA001AA"]))

        assert "Telegram: Иван Петров (@ivan_petrov)" in result.text
    finally:
        fx.close()


async def test_fine_check_shows_all_owners_when_multiple_users_match(tmp_path):
    """Один car_number, валидно связанный сразу с несколькими Telegram-
    пользователями (см. задачу) — "fine check" должен показать ВСЕХ, а не
    скрывать их за общей фразой."""
    fx = _Fixture(
        tmp_path,
        records_by_car={"AA001AA": [_record(car_number="AA001AA")]},
        user_repository=_FakeUserRepository(
            {
                "AA001AA": [
                    TelegramUserInfo(user_id=1, username="user_one", first_name=None, last_name=None),
                    TelegramUserInfo(user_id=2, username="user_two", first_name=None, last_name=None),
                ],
            }
        ),
    )
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

        result = await fx.command.handle(_ctx(["check", "AA001AA"]))

        assert "Telegram: @user_one, @user_two" in result.text
    finally:
        fx.close()


async def test_fine_check_normalizes_car_number(tmp_path):
    fx = _Fixture(tmp_path, records_by_car={"AA001AA": [_record(car_number="AA001AA")]})
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

        result = await fx.command.handle(_ctx(["check", "aa001aa"]))

        assert "Автомобиль: AA001AA" in result.text
    finally:
        fx.close()


async def test_fine_check_unknown_car_number_returns_command_error(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["check", "ZZ999ZZ"]))

    assert "не найдена" in exc_info.value.message


async def test_fine_check_rejects_invalid_car_number(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["check", "AA-001-AA"]))

    assert "❌" in exc_info.value.message


async def test_fine_check_rejects_wrong_argument_count(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["check"]))

    assert "Неверный формат команды" in exc_info.value.message


async def test_fine_check_with_provider_error_returns_clean_message(tmp_path):
    fx = _Fixture(tmp_path, provider_error=FineProviderError("police.ge недоступен"))
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

        with pytest.raises(CommandError) as exc_info:
            await fx.command.handle(_ctx(["check", "AA001AA"]))

        assert "police.ge недоступен" in exc_info.value.message
        # Никакого трейсбека оператору — только чистое сообщение.
        assert "Traceback" not in exc_info.value.message
    finally:
        fx.close()


async def test_fine_check_uses_shared_pending_notification_mechanism(tmp_path):
    fx = _Fixture(tmp_path, records_by_car={"AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")]})
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))
        task_id = fx.task_repository.list_active()[0].id

        await fx.command.handle(_ctx(["check", "AA001AA"]))

        # То же самое, что делает FineJob после check_task(): flush_pending()
        # доставил штраф через тот же NotificationService и отметил
        # notification_sent_at, а не оставил его висеть.
        assert len(fx.notification_service.notify_calls) == 1
        fine = fx.detected_fine_repository.get_by_fingerprint(task_id, "fp-1")
        assert fine.notification_sent_at is not None
    finally:
        fx.close()


async def test_fine_check_does_not_duplicate_already_notified_fine_on_repeat(tmp_path):
    fx = _Fixture(tmp_path, records_by_car={"AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")]})
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

        await fx.command.handle(_ctx(["check", "AA001AA"]))
        result = await fx.command.handle(_ctx(["check", "AA001AA"]))

        assert "Новых: 0" in result.text
        # flush_pending() второй раз не находит ничего для отправки.
        assert len(fx.notification_service.notify_calls) == 1
    finally:
        fx.close()


async def test_fine_check_checks_all_active_tasks_for_car_number(tmp_path):
    # Две активные задачи для одного номера — теперь возможны только как
    # унаследованное/историческое состояние (см. задачу: "fine add" больше
    # не создаёт вторую активную задачу для уже отслеживаемого номера) —
    # создаём их напрямую через репозиторий. fine check должен проверить
    # обе и просуммировать результат.
    fx = _Fixture(
        tmp_path,
        records_by_car={
            "AA001AA": [
                _record(car_number="AA001AA", fingerprint="fp-1"),
                _record(car_number="AA001AA", fingerprint="fp-2"),
            ]
        },
    )
    try:
        fx.task_repository.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 10),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        fx.task_repository.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 15), end_date=date(2026, 8, 20),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        assert len(fx.task_repository.list_active()) == 2

        result = await fx.command.handle(_ctx(["check", "AA001AA"]))

        # Провайдер возвращает те же 2 записи для обеих задач — 4 найдено,
        # но каждая уникальна только в рамках своей задачи (fingerprint
        # общий, а monitoring_task_id разный), поэтому все 4 новые.
        assert "Найдено штрафов: 4" in result.text
        assert "Новых: 4" in result.text
    finally:
        fx.close()


# ---- fine update-all ----


async def test_fine_update_all_checks_all_active_car_numbers(tmp_path):
    fx = _Fixture(
        tmp_path,
        records_by_car={
            "AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")],
            "BB002BB": [],
        },
    )
    try:
        await fx.command.handle(_ctx(["add", "AA001AA"]))
        await fx.command.handle(_ctx(["add", "BB002BB"]))
        # fine add теперь тоже запускает немедленную проверку (см. задачу) —
        # считаем здесь только запросы, сделанные именно update-all.
        fx.provider.requested_plates.clear()

        result = await fx.command.handle(_ctx(["update-all"]))

        assert sorted(fx.provider.requested_plates) == ["AA001AA", "BB002BB"]
        assert "✅ Массовая проверка завершена" in result.text
        assert "Всего: 2" in result.text
        assert "Проверено: 2" in result.text
        # fp-1 уже учтён как новый штраф при "fine add AA001AA" — здесь он
        # найден снова, но уже не новый (дедуп по fingerprint+task).
        assert "Новые штрафы: 0" in result.text
        assert "Ошибок: 0" in result.text
    finally:
        fx.close()


async def test_fine_update_all_sends_start_message_via_event_then_final_summary(tmp_path):
    fx = _Fixture(tmp_path, records_by_car={"AA001AA": [_record(car_number="AA001AA")]})
    try:
        await fx.command.handle(_ctx(["add", "AA001AA"]))
        await fx.command.handle(_ctx(["add", "BB002BB"]))

        event = _FakeEvent()
        result = await fx.command.handle(_ctx(["update-all"], event=event))

        # Ровно одно промежуточное сообщение (старт) — не по одному на
        # каждый из активных автомобилей.
        assert event.responses == ["🔄 Запущена проверка 2 автомобилей"]
        assert "✅ Массовая проверка завершена" in result.text
    finally:
        fx.close()


async def test_fine_update_all_without_event_does_not_crash(fx):
    # ctx.event is None (как во всех остальных тестах этого файла) — команда
    # должна просто пропустить стартовое сообщение, а не упасть.
    await fx.command.handle(_ctx(["add", "AA001AA"]))

    result = await fx.command.handle(_ctx(["update-all"]))

    assert "✅ Массовая проверка завершена" in result.text


async def test_fine_update_all_skips_inactive_car_numbers(fx):
    await fx.command.handle(_ctx(["add", "AA001AA"]))
    await fx.command.handle(_ctx(["add", "BB002BB"]))
    await fx.command.handle(_ctx(["stop", "BB002BB"]))
    # fine add теперь тоже запускает немедленную проверку (см. задачу) —
    # считаем здесь только запросы, сделанные именно update-all.
    fx.provider.requested_plates.clear()

    result = await fx.command.handle(_ctx(["update-all"]))

    assert fx.provider.requested_plates == ["AA001AA"]
    assert "Всего: 1" in result.text


async def test_fine_update_all_error_on_one_car_does_not_stop_others(tmp_path):
    provider = _SelectiveFailingProvider(
        records_by_car={"BB002BB": [_record(car_number="BB002BB")]},
        fail_for={"AA001AA"},
    )
    fx = _Fixture(tmp_path, provider=provider)
    try:
        await fx.command.handle(_ctx(["add", "AA001AA"]))
        await fx.command.handle(_ctx(["add", "BB002BB"]))
        # fine add теперь тоже запускает немедленную проверку (см. задачу) —
        # считаем здесь только запросы, сделанные именно update-all.
        provider.requested_plates.clear()

        result = await fx.command.handle(_ctx(["update-all"]))

        assert sorted(provider.requested_plates) == ["AA001AA", "BB002BB"]
        assert "Всего: 2" in result.text
        assert "Проверено: 1" in result.text
        # Штраф BB002BB уже учтён как новый при "fine add BB002BB".
        assert "Новые штрафы: 0" in result.text
        assert "Ошибок: 1" in result.text
        assert "• AA001AA — police.ge недоступен для AA001AA" in result.text
    finally:
        fx.close()


async def test_fine_update_all_uses_same_check_task_mechanism_and_notifies(tmp_path):
    fx = _Fixture(
        tmp_path, records_by_car={"AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")]},
    )
    try:
        await fx.command.handle(_ctx(["add", "AA001AA"]))

        await fx.command.handle(_ctx(["update-all"]))

        # То же самое, что делает FineCheckService.check_task() + flush_pending()
        # для fine check/FineJob — не отдельная логика доставки.
        assert len(fx.notification_service.notify_calls) == 1
        task_id = fx.task_repository.list_active()[0].id
        fine = fx.detected_fine_repository.get_by_fingerprint(task_id, "fp-1")
        assert fine.notification_sent_at is not None

        # Повторный update-all не находит новых штрафов и не шлёт повторно.
        result = await fx.command.handle(_ctx(["update-all"]))
        assert "Новые штрафы: 0" in result.text
        assert len(fx.notification_service.notify_calls) == 1
    finally:
        fx.close()


async def test_fine_update_all_with_no_active_tasks(fx):
    result = await fx.command.handle(_ctx(["update-all"]))

    assert "Всего: 0" in result.text
    assert "Проверено: 0" in result.text
    assert "Ошибок: 0" in result.text


async def test_fine_update_all_does_not_touch_history_or_scheduled_job_state(fx):
    # update-all не должен трогать FineJob (расписание/статус) — это ручной,
    # отдельный вызов того же check_service, а не альтернативный планировщик.
    await fx.command.handle(_ctx(["add", "AA001AA"]))

    await fx.command.handle(_ctx(["update-all"]))

    assert fx.fine_job.status.last_run_at is None
    assert fx.fine_job.status.last_success_at is None


# ---- fine status ----


async def test_fine_status(fx):
    await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

    result = await fx.command.handle(_ctx(["status"]))

    assert "Мониторинг: включён" in result.text
    assert "Активных задач: 1" in result.text
    assert "09:00, 15:00 и 21:00" in result.text
    assert "Asia/Tbilisi" in result.text
    assert "Ошибок: 0" in result.text
    assert "Последняя ошибка: Нет" in result.text
    assert "ещё не запускался" in result.text


async def test_fine_status_shows_last_run_at_in_tbilisi_time_not_utc(fx):
    """FineJobStatus.last_run_at — aware UTC (datetime.now(timezone.utc),
    см. reader/jobs/fine_job.py) — "fine status" должен показать его по
    Asia/Tbilisi (+4 часа), а не сырое UTC-значение (см. задачу про
    перевод отображения времени)."""
    utc_value = datetime(2026, 8, 14, 11, 44, tzinfo=timezone.utc)
    fx.fine_job.status.last_run_at = utc_value

    result = await fx.command.handle(_ctx(["status"]))

    assert "14.08.2026 15:44" in result.text
    assert "11:44" not in result.text


async def test_fine_status_reflects_scheduler_running_state(fx):
    fx.scheduler.is_running = True

    result = await fx.command.handle(_ctx(["status"]))

    assert "Scheduler: работает" in result.text


# ---- fine stats ----


async def test_fine_stats_with_no_fines(fx):
    result = await fx.command.handle(_ctx(["stats"]))

    assert result.text == "📊 Статистика штрафов\n\nПока не найдено ни одного штрафа."


async def test_fine_stats_groups_by_car_and_sorts_by_count_desc(tmp_path):
    records_by_car = {
        "B957MA09": [_record(car_number="B957MA09", fingerprint=f"b-{i}") for i in range(7)],
        "P701XY126": [_record(car_number="P701XY126", fingerprint=f"p-{i}") for i in range(3)],
        "AA123BC77": [_record(car_number="AA123BC77", fingerprint="a-1")],
    }
    fx = _Fixture(tmp_path, records_by_car=records_by_car)
    try:
        for car_number in records_by_car:
            await fx.command.handle(_ctx(["add", car_number, "01.08.2026", "31.08.2026"]))
            await fx.command.handle(_ctx(["check", car_number]))

        result = await fx.command.handle(_ctx(["stats"]))

        assert "📊 Статистика штрафов" in result.text

        lines = result.text.split("\n")
        assert lines[2] == "Автомобиль  Штрафов"
        assert lines[3] == "----------  -------"
        # Порядок строк должен отражать ORDER BY COUNT(*) DESC, счётчик
        # выровнен по правому краю.
        assert lines[4] == "B957MA09          7"
        assert lines[5] == "P701XY126         3"
        assert lines[6] == "AA123BC77         1"

        assert "Всего автомобилей: 3" in result.text
        assert "Всего опубликованных штрафов: 11" in result.text
    finally:
        fx.close()


def test_format_stats_table_aligns_columns_with_separator():
    stats = [
        CarFineStats(car_number="B957MA09", fine_count=7),
        CarFineStats(car_number="P701XY126", fine_count=3),
        CarFineStats(car_number="AA123BC77", fine_count=1),
    ]

    lines = FineCommand._format_stats_table(stats).split("\n")

    assert lines[0] == "Автомобиль  Штрафов"
    assert lines[1] == "----------  -------"
    assert lines[2] == "B957MA09          7"
    assert lines[3] == "P701XY126         3"
    assert lines[4] == "AA123BC77         1"

    # Все строки таблицы одной и той же длины — столбцы выровнены.
    assert len({len(line) for line in lines}) == 1


def test_format_stats_table_column_widths_are_computed_from_data_not_hardcoded():
    # Автомобиль длиннее заголовка "Автомобиль", а счётчик — длиннее
    # заголовка "Штрафов": оба столбца должны расшириться под данные, а не
    # остаться равными длине заголовков.
    stats = [
        CarFineStats(car_number="VERYLONGPLATE123", fine_count=12345678),
        CarFineStats(car_number="AA1", fine_count=1),
    ]

    car_width = len("VERYLONGPLATE123")
    count_width = len("12345678")

    lines = FineCommand._format_stats_table(stats).split("\n")

    assert lines[0] == "Автомобиль".ljust(car_width) + "  " + "Штрафов"
    assert lines[1] == "-" * car_width + "  " + "-" * count_width
    assert lines[2] == "VERYLONGPLATE123  12345678"
    assert lines[3] == "AA1".ljust(car_width) + "  " + "1".rjust(count_width)


# ---- общие ошибки формата ----


async def test_unknown_subcommand_returns_command_error(fx):
    with pytest.raises(CommandError):
        await fx.command.handle(_ctx(["frobnicate"]))


async def test_empty_args_returns_command_error(fx):
    with pytest.raises(CommandError):
        await fx.command.handle(_ctx([]))
