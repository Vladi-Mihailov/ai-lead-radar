"""
Тесты FineCommand — операторский интерфейс (fine add/list/stop/check/status).
Repository — настоящие (SQLite/tmp_path), FineProvider/NotificationService —
фейковые (без сети/Telegram). Ничего не переопределяет логику FineJob —
использует те же FineCheckService/FineNotificationCoordinator.
"""

import sys
from datetime import date, timedelta
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

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
        user_repository=None,
    ):
        db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(db_path)
        self.detected_fine_repository = DetectedFineRepository(db_path)
        self.user_repository = user_repository
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

    assert "✅ Мониторинг штрафов добавлен" in result.text
    assert "Автомобиль: B957MA09" in result.text
    assert "Период: 03.08.2026–13.08.2026" in result.text
    assert "09:00, 15:00 и 21:00" in result.text
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

    assert "✅ Мониторинг штрафов добавлен" in result.text

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


async def test_fine_add_rejects_overlapping_active_task(fx):
    await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "AA001AA", "15.08.2026", "20.09.2026"]))

    assert "уже есть активная задача" in exc_info.value.message
    assert len(fx.task_repository.list_active()) == 1


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

        assert "✅ Мониторинг штрафов добавлен" in result.text
        assert "Telegram: @owner" in result.text
    finally:
        fx.close()


async def test_fine_add_without_username_keeps_old_behavior(tmp_path):
    """fine add NUMBER (без @username) — старое поведение не меняется:
    номер НЕ присваивается автоматически автору команды."""
    fx = _Fixture(tmp_path, user_repository=_FakeUserRepository())
    try:
        result = await fx.command.handle(_ctx(["add", "A123AA777"]))

        assert "✅ Мониторинг штрафов добавлен" in result.text
        assert "Telegram:" not in result.text
        assert fx.user_repository.find_by_car_number("A123AA777") == []

        [task] = fx.task_repository.list_active()
        assert task.created_by_user_id == _USER_ID
    finally:
        fx.close()


async def test_fine_add_with_unknown_username_fails_and_creates_nothing(tmp_path):
    fx = _Fixture(tmp_path, user_repository=_FakeUserRepository())
    try:
        with pytest.raises(CommandError) as exc_info:
            await fx.command.handle(_ctx(["add", "A123AA777", "@unknown"]))

        assert "@unknown" in exc_info.value.message
        assert "не найден" in exc_info.value.message
        assert "Автомобиль не добавлен" in exc_info.value.message

        # Задача мониторинга НЕ создана, car_numbers не тронуты.
        assert fx.task_repository.list_active() == []
        assert fx.user_repository.find_by_car_number("A123AA777") == []
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

        assert "Telegram: @owner" in result.text
        assert "⚠️" not in result.text

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert [u.user_id for u in found] == [222]  # без дублей
    finally:
        fx.close()


async def test_fine_add_with_username_conflicting_owner_creates_no_task(tmp_path):
    """Вся pre-validation владельца — ДО task_repository.create(): конфликт
    (номер уже у ДРУГОГО пользователя) должен целиком блокировать команду,
    а не создавать задачу с последующим предупреждением."""
    user1 = TelegramUserInfo(user_id=1, username="user1", first_name=None, last_name=None)
    user2 = TelegramUserInfo(user_id=2, username="user2", first_name=None, last_name=None)
    fx = _Fixture(
        tmp_path,
        user_repository=_FakeUserRepository(
            users_by_car_number={"A123AA777": [user1]}, users=[user1, user2],
        ),
    )
    try:
        tasks_before = fx.task_repository.list_active()
        assert tasks_before == []

        with pytest.raises(CommandError) as exc_info:
            await fx.command.handle(_ctx(["add", "A123AA777", "@user2"]))

        assert "⚠️" in exc_info.value.message
        assert "@user1" in exc_info.value.message
        assert "Автомобиль не добавлен" in exc_info.value.message

        # Regression: количество задач НЕ изменилось.
        assert fx.task_repository.list_active() == tasks_before
        assert len(fx.task_repository.list_active()) == 0

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert [u.user_id for u in found] == [1]  # user2 НЕ добавлен
    finally:
        fx.close()


async def test_fine_add_with_username_ambiguous_existing_owners_creates_no_task(tmp_path):
    """Тот же принцип для неоднозначной привязки (несколько существующих
    владельцев) — задача НЕ создаётся."""
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
        tasks_before = fx.task_repository.list_active()
        assert tasks_before == []

        with pytest.raises(CommandError) as exc_info:
            await fx.command.handle(_ctx(["add", "A123AA777", "@user3"]))

        assert "⚠️" in exc_info.value.message
        assert "неоднозначна" in exc_info.value.message
        assert "Автомобиль не добавлен" in exc_info.value.message

        # Regression: количество задач НЕ изменилось.
        assert fx.task_repository.list_active() == tasks_before
        assert len(fx.task_repository.list_active()) == 0

        found = fx.user_repository.find_by_car_number("A123AA777")
        assert sorted(u.user_id for u in found) == [1, 2]  # user3 не добавлен
    finally:
        fx.close()


async def test_fine_add_with_username_conflict_does_not_change_task_count_alongside_other_tasks(tmp_path):
    """Более сильная версия regression-теста: конфликт по ОДНОМУ номеру не
    должен влиять на количество задач мониторинга вообще — даже когда в
    базе уже есть другие, не связанные с этим конфликтом задачи."""
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
        count_before = len(fx.task_repository.list_active())
        assert count_before == 1

        with pytest.raises(CommandError):
            await fx.command.handle(_ctx(["add", "A123AA777", "@user2"]))

        assert len(fx.task_repository.list_active()) == count_before
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

        assert "Период: 01.08.2026–31.08.2026" in result.text
        assert "Telegram: Иван Петров (@owner)" in result.text
    finally:
        fx.close()


async def test_fine_add_rejects_bare_at_symbol(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "A123AA777", "@"]))

    assert "Неверный формат команды" in exc_info.value.message
    assert fx.task_repository.list_active() == []


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


async def test_fine_add_bulk_command_reports_already_in_monitoring(fx):
    await fx.command.handle(_ctx(["add", "H663KH702"]))

    result = await fx.command.handle(_ctx(["add-bulk", "H663KH702", "C072H0977"]))

    assert "Добавлено: 1" in result.text
    assert "Уже в мониторинге: 1" in result.text
    assert len(fx.task_repository.list_active()) == 2


async def test_fine_add_bulk_command_deduplicates_within_message(fx):
    # Дубль внутри самого сообщения — второе появление того же номера
    # обрабатывается тем же путём, что и "уже в мониторинге" (см.
    # _handle_add_bulk_command): validate_no_overlap() внутри _handle_add()
    # уже видит задачу, созданную первым появлением этого же номера.
    result = await fx.command.handle(
        _ctx(["add-bulk", "H663KH702", "h663kh702", "C072H0977"])
    )

    assert "Добавлено: 2" in result.text
    assert "Уже в мониторинге: 1" in result.text
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
    # validate_no_overlap не запрещает две непересекающиеся по датам
    # активные задачи для одного номера — fine stop должен остановить обе.
    await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "10.08.2026"]))
    await fx.command.handle(_ctx(["add", "AA001AA", "15.08.2026", "20.08.2026"]))
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
    fx = _Fixture(tmp_path, records_by_car={"AA001AA": [_record(car_number="AA001AA")]})
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

        result = await fx.command.handle(_ctx(["check", "AA001AA"]))

        assert "✅ Проверка завершена" in result.text
        assert "Автомобиль: AA001AA" in result.text
        assert "Найдено штрафов: 1" in result.text
        assert "Новых: 1" in result.text
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
        # Остальное содержимое результата не потеряно.
        assert "✅ Проверка завершена" in result.text
        assert "Автомобиль: AA001AA" in result.text
        assert "Найдено штрафов: 1" in result.text
        assert "Новых: 1" in result.text
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


async def test_fine_check_shows_ambiguous_owner_when_multiple_users_match(tmp_path):
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

        assert "Telegram: найдено несколько пользователей" in result.text
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
    # Две непересекающиеся по датам активные задачи для одного номера —
    # fine check должен проверить обе и просуммировать результат.
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
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "10.08.2026"]))
        await fx.command.handle(_ctx(["add", "AA001AA", "15.08.2026", "20.08.2026"]))
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

        result = await fx.command.handle(_ctx(["update-all"]))

        assert sorted(fx.provider.requested_plates) == ["AA001AA", "BB002BB"]
        assert "✅ Массовая проверка завершена" in result.text
        assert "Всего: 2" in result.text
        assert "Проверено: 2" in result.text
        assert "Новые штрафы: 1" in result.text
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

        result = await fx.command.handle(_ctx(["update-all"]))

        assert sorted(provider.requested_plates) == ["AA001AA", "BB002BB"]
        assert "Всего: 2" in result.text
        assert "Проверено: 1" in result.text
        assert "Новые штрафы: 1" in result.text
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
