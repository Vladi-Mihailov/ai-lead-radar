"""«👤 Данные клиента» в @ProtocolGEbot: имя клиента без Telegram-логина.

Имя — подпись менеджера к автомобилю: не user_id, не доступ и не повод
для уведомлений. Сквозные тесты через настоящий ConversationController
поверх SQLite/tmp_path; police.ge — фейк со счётчиком запросов.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import ParsedFineRecord  # noqa: E402
from reader.fines.provider import FineProvider  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.plate_display import format_plate_for_telegram  # noqa: E402
from reader.public_bot import keyboards, texts  # noqa: E402
from reader.public_bot.client_names import (  # noqa: E402
    CLIENT_NAME_MAX_LENGTH,
    CarClientNameRepository,
    ClientNameValidationError,
    normalize_client_name,
)
from reader.public_bot.conversation import (  # noqa: E402
    STEP_AWAITING_ADD_CLIENT_NAME,
    STEP_AWAITING_ADD_CONFIRM,
    ConversationController,
)
from reader.public_bot.conversation_state_repository import BotConversationStateRepository  # noqa: E402
from reader.public_bot.debt_refresh_service import DebtRefreshService  # noqa: E402
from reader.public_bot.delivery_service import _applicable_roles  # noqa: E402
from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402
from reader.public_bot.statistics_service import BotStatisticsService  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import SubscriptionService  # noqa: E402
from reader.users.models import TelegramUserInfo  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_TBILISI = ZoneInfo("Asia/Tbilisi")
_TRUSTED = 5712994689
_CLIENT = 777
_STRANGER = 4242
_CAR = "M295YB196"


async def _instant(_seconds):
    pass


class _Provider(FineProvider):
    def __init__(self):
        self.calls: list[str] = []

    async def search_by_plate(self, plate: str):
        self.calls.append(plate)
        return [ParsedFineRecord(
            car_number=plate, external_fine_id="AB123456", penalty_date=date(2026, 8, 6),
            due_date=date(2099, 12, 31), delivered_status="Не вручено", fingerprint=f"fp-{plate}",
            raw_data={"protocolNo": "AB123456"}, amount=50,
        )]


class _Fixture:
    def __init__(self, tmp_path, *, with_names=True):
        db = tmp_path / "users.db"
        self.tasks = FineMonitoringTaskRepository(db)
        self.fines = DetectedFineRepository(db)
        self.subscriptions = FineSubscriptionRepository(db)
        self.users = UserRepository(db)
        self.states = BotConversationStateRepository(db)
        self.known = BotKnownUsersRepository(db)
        self.names = CarClientNameRepository(db)
        self.provider = _Provider()
        self.check = FineCheckService(self.provider, self.tasks, self.fines, sleep=_instant)
        self.service = SubscriptionService(self.tasks, self.subscriptions, self.users, self.check)
        self.controller = ConversationController(
            self.states, self.service, BotStatisticsService(self.known, self.subscriptions, self.fines), self.known,
            tz=_TBILISI, trusted_operator_user_ids=frozenset({_TRUSTED}),
            debt_refresh_service=DebtRefreshService(self.tasks, self.check, sleep=_instant),
            client_name_repository=self.names if with_names else None,
        )

    async def text(self, value, *, user=_TRUSTED, **kwargs):
        return await self.controller.handle_text(value, chat_id=user, telegram_user_id=user, username=None, **kwargs)

    async def start_trusted_add(self, *, with_client: str | None):
        await self.text(texts.ADD_CAR_LABEL)
        await self.text(_CAR)
        if with_client is None:
            return self.controller.handle_add_client_decision(False, chat_id=_TRUSTED, telegram_user_id=_TRUSTED)
        self.controller.handle_add_client_decision(True, chat_id=_TRUSTED, telegram_user_id=_TRUSTED)
        return await self.text(with_client)

    async def set_name_on_confirm(self, name: str):
        prompt = self.controller.handle_add_car_client_name_prompt(
            chat_id=_TRUSTED, telegram_user_id=_TRUSTED, car_number=_CAR,
        )
        assert prompt.text == texts.CLIENT_NAME_PROMPT
        return await self.text(name)

    async def confirm(self):
        return await self.controller.handle_add_car_confirm(
            chat_id=_TRUSTED, telegram_user_id=_TRUSTED, first_name=None, last_name=None, car_number=_CAR,
        )

    def deliverable(self):
        return self.subscriptions.list_all_deliverable(today=datetime.now(timezone.utc).astimezone(_TBILISI).date())

    def client_recipients(self):
        """Кому уйдут ЛИЧНЫЕ уведомления как владельцу (роль 'owner')."""
        return [s.telegram_user_id for s in self.deliverable() if "owner" in _applicable_roles(s)]

    def close(self):
        for repository in (self.names, self.known, self.states, self.users, self.subscriptions, self.fines, self.tasks):
            repository.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


# ---- Проверка имени ----


@pytest.mark.parametrize("raw, expected", [
    ("Иван Иванов", "Иван Иванов"),
    ("Giorgi123", "Giorgi123"),
    ("გიორგი", "გიორგი"),
    ("Ahmet 007", "Ahmet 007"),
    ("Клиент 12345", "Клиент 12345"),
    ("Александр-2", "Александр-2"),
    ("İsmail Öztürk", "İsmail Öztürk"),
    ("O'Brien Jr.", "O'Brien Jr."),
    ("  Иван \t  Иванов \n", "Иван Иванов"),
    ("Иван​Иванов", "ИванИванов"),  # невидимый символ из копирования убирается
    ("José", "José"),  # NFC
])
def test_client_name_accepts_any_alphabet(raw, expected):
    assert normalize_client_name(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "@ivan123", "Иван*", "_ivan_", "[Иван](tg://user?id=1)", "Иван `x`", "😀", "---",
    "a" * (CLIENT_NAME_MAX_LENGTH + 1),
])
def test_client_name_rejects_markup_symbols_and_overlong_text(raw):
    with pytest.raises(ClientNameValidationError):
        normalize_client_name(raw)


def test_client_name_is_escaped_on_display_even_if_stored_raw():
    lines = texts.format_client_lines("**жирный** _x_", [])
    assert lines == [f"👤 Клиент: {texts.escape_telethon_markdown('**жирный** _x_')}", "🔗 Telegram: не привязан"]
    assert "**жирный**" not in lines[0]


def test_client_lines_with_and_without_telegram():
    assert texts.format_client_lines("Иван Иванов", ["ivan123"]) == ["👤 Клиент: Иван Иванов", "🔗 Telegram: @ivan123"]
    assert texts.format_client_lines("Иван Иванов", []) == ["👤 Клиент: Иван Иванов", "🔗 Telegram: не привязан"]
    assert texts.format_client_lines(None, [], pending_hints=["ivan123"]) == [
        "👤 Клиент: не указан", "🔗 Telegram: не привязан", "⏳ Ждёт подтверждения: @ivan123",
    ]


# ---- Add Car менеджера ----


async def test_manager_adds_a_car_with_a_name_only(fx):
    confirm = await fx.start_trusted_add(with_client=None)
    assert confirm.add_car_client_name_button is True

    with_name = await fx.set_name_on_confirm("Иван Иванов")
    assert with_name.text.splitlines()[:3] == [
        f"🚗 {format_plate_for_telegram(_CAR)}", "👤 Клиент: Иван Иванов", "🔗 Telegram: не привязан",
    ]
    assert fx.states.get(_TRUSTED).step == STEP_AWAITING_ADD_CONFIRM

    summary = await fx.confirm()

    assert summary.text.startswith(texts.ADD_CAR_DONE_LINE)
    assert "👤 Клиент: Иван Иванов\n🔗 Telegram: не привязан" in summary.text
    assert fx.names.get(_CAR) == "Иван Иванов"
    # Имя — не доступ: ни подписки, ни получателей уведомлений.
    assert fx.subscriptions.list_by_car_number(_CAR) == []
    assert fx.deliverable() == []
    assert len(fx.tasks.list_active()) == 1


async def test_manager_adds_a_car_with_telegram_and_name(fx):
    fx.users.upsert(TelegramUserInfo(user_id=_CLIENT, username="real_owner", first_name="Real", last_name="Owner"))
    await fx.start_trusted_add(with_client="@real_owner")
    await fx.set_name_on_confirm("გიორგი")

    summary = await fx.confirm()

    assert "👤 Клиент: გიორგი\n🔗 Telegram: @real_owner" in summary.text
    [subscription] = fx.subscriptions.list_by_user(_CLIENT)
    assert subscription.status == "active" and subscription.telegram_user_id == _CLIENT


async def test_manager_adds_a_car_without_any_client_data_as_before(fx):
    await fx.start_trusted_add(with_client=None)

    summary = await fx.confirm()

    assert summary.text.startswith(texts.ADD_CAR_DONE_LINE)
    assert "Клиент" not in summary.text and fx.names.get(_CAR) is None


async def test_invalid_name_keeps_the_step_and_add_works_without_a_name(fx):
    await fx.start_trusted_add(with_client=None)
    reply = await fx.set_name_on_confirm("😀")

    assert reply.text.startswith("❌ ")
    assert reply.add_car_confirm_plate == _CAR  # ✅/Отмена под вопросом
    assert fx.states.get(_TRUSTED).step == STEP_AWAITING_ADD_CLIENT_NAME

    summary = await fx.confirm()  # ✅ прямо с шага имени
    assert summary.text.startswith(texts.ADD_CAR_DONE_LINE) and fx.names.get(_CAR) is None


async def test_name_button_is_only_for_the_manager_flow(fx):
    self_service = await fx.text(texts.ADD_CAR_LABEL, user=_STRANGER)
    self_service = await fx.text(_CAR, user=_STRANGER)
    assert self_service.add_car_confirm_plate == _CAR
    assert self_service.add_car_client_name_button is False
    assert fx.controller.handle_add_car_client_name_prompt(
        chat_id=_STRANGER, telegram_user_id=_STRANGER, car_number=_CAR,
    ) is None


async def test_without_the_feature_the_confirm_has_no_name_button(tmp_path):
    fixture = _Fixture(tmp_path, with_names=False)
    try:
        confirm = await fixture.start_trusted_add(with_client=None)
        assert confirm.add_car_client_name_button is False
        assert fixture.controller.handle_add_car_client_name_prompt(
            chat_id=_TRUSTED, telegram_user_id=_TRUSTED, car_number=_CAR,
        ) is None
    finally:
        fixture.close()


# ---- Карточка уже добавленной машины ----


async def _added_car(fx, *, name=None):
    await fx.start_trusted_add(with_client=None)
    if name:
        await fx.set_name_on_confirm(name)
    await fx.confirm()
    [task] = fx.tasks.list_active()
    return task


async def test_task_card_shows_the_client_and_its_buttons(fx):
    task = await _added_car(fx, name="Ahmet 007")

    card = fx.controller.handle_trusted_task_open(task.id, 0, telegram_user_id=_TRUSTED)

    assert card.text.splitlines()[1:3] == ["👤 Клиент: Ahmet 007", "🔗 Telegram: не привязан"]
    assert card.trusted_task_client_buttons and card.trusted_task_client_has_name
    assert card.trusted_task_client_can_unlink is False
    labels = [b.text for row in keyboards.trusted_task_detail_keyboard(
        page=0, task_id=task.id, has_client_name=True, can_unlink=False) for b in row]
    assert labels == [texts.CLIENT_NAME_BUTTON_LABEL, texts.TASK_CLIENT_TELEGRAM_BUTTON_LABEL,
                      texts.TASK_CLIENT_NAME_DELETE_BUTTON_LABEL, texts.BACK_BUTTON_LABEL]


async def test_manager_changes_and_deletes_the_name_of_an_existing_car(fx):
    task = await _added_car(fx)

    prompt = fx.controller.handle_task_client_action("name", task.id, 0, chat_id=_TRUSTED, telegram_user_id=_TRUSTED)
    assert prompt.text == texts.CLIENT_NAME_PROMPT
    saved = await fx.text("Клиент 12345")
    assert saved.text.startswith(texts.TASK_CLIENT_NAME_SAVED_TEXT)
    assert "👤 Клиент: Клиент 12345" in saved.text and fx.names.get(_CAR) == "Клиент 12345"

    deleted = fx.controller.handle_task_client_action("delname", task.id, 0, chat_id=_TRUSTED, telegram_user_id=_TRUSTED)
    assert deleted.text.startswith(texts.TASK_CLIENT_NAME_DELETED_TEXT)
    assert "👤 Клиент: не указан" in deleted.text and fx.names.get(_CAR) is None


async def test_telegram_added_later_keeps_the_name_and_reuses_the_task(fx):
    task = await _added_car(fx, name="Иван Иванов")
    calls_before = len(fx.provider.calls)
    fx.users.upsert(TelegramUserInfo(user_id=_CLIENT, username="ivan123", first_name="Ivan", last_name=None))

    fx.controller.handle_task_client_action("tg", task.id, 0, chat_id=_TRUSTED, telegram_user_id=_TRUSTED)
    linked = await fx.text("@ivan123")

    assert linked.text.startswith("✅ Telegram клиента привязан: @ivan123")
    assert "👤 Клиент: Иван Иванов\n🔗 Telegram: @ivan123" in linked.text
    [subscription] = fx.subscriptions.list_by_user(_CLIENT)
    assert subscription.monitoring_task_id == task.id and subscription.status == "active"
    assert subscription.created_by_telegram_user_id == _TRUSTED
    assert len(fx.tasks.list_active()) == 1  # без второй задачи мониторинга
    assert len(fx.provider.calls) == calls_before  # без внеочередного запроса к police.ge
    assert fx.names.get(_CAR) == "Иван Иванов"


async def test_unknown_telegram_gets_a_claim_link_and_no_access_until_confirmed(fx):
    task = await _added_car(fx, name="Иван Иванов")

    fx.controller.handle_task_client_action("tg", task.id, 0, chat_id=_TRUSTED, telegram_user_id=_TRUSTED)
    reply = await fx.text("@unknown_person")

    assert "⏳ Приглашение для @unknown_person создано." in reply.text
    assert "https://t.me/ProtocolGEbot?start=claim_" in reply.text
    assert "⏳ Ждёт подтверждения: @unknown_person" in reply.text
    # До подтверждения клиенту лично — ничего (менеджер, создавший
    # приглашение, получает уведомления как и раньше).
    assert fx.client_recipients() == []

    token = [line for line in reply.text.splitlines() if line.startswith("https://t.me/")][0].rsplit("claim_", 1)[1]
    await fx.controller.handle_text(
        f"/start claim_{token}", chat_id=_CLIENT, telegram_user_id=_CLIENT, username="unknown_person",
        first_name="Real", last_name="Owner",
    )
    card = fx.controller.handle_trusted_task_open(task.id, 0, telegram_user_id=_TRUSTED)
    assert "👤 Клиент: Иван Иванов\n🔗 Telegram: @unknown_person" in card.text
    assert fx.client_recipients() == [_CLIENT]


async def test_unlink_archives_only_manager_links(fx):
    task = await _added_car(fx, name="Иван Иванов")
    fx.users.upsert(TelegramUserInfo(user_id=_CLIENT, username="ivan123", first_name=None, last_name=None))
    fx.controller.handle_task_client_action("tg", task.id, 0, chat_id=_TRUSTED, telegram_user_id=_TRUSTED)
    await fx.text("@ivan123")
    # Клиент, добавивший ту же машину сам, — не привязка менеджера.
    await fx.text(texts.ADD_CAR_LABEL, user=_STRANGER)
    await fx.text(_CAR, user=_STRANGER)
    await fx.controller.handle_add_car_confirm(
        chat_id=_STRANGER, telegram_user_id=_STRANGER, first_name=None, last_name=None, car_number=_CAR,
    )
    fines_before = len(fx.fines.list_by_car_number(_CAR))

    prompt = fx.controller.handle_task_client_action("unlink", task.id, 0, chat_id=_TRUSTED, telegram_user_id=_TRUSTED)
    assert prompt.task_client_unlink_confirm == (task.id, 0) and "@ivan123" in prompt.text
    done = fx.controller.handle_task_client_action("unlinkyes", task.id, 0, chat_id=_TRUSTED, telegram_user_id=_TRUSTED)

    assert done.text.startswith("✅ Telegram клиента отвязан.")
    statuses = {s.telegram_user_id: s.status for s in fx.subscriptions.list_by_car_number(_CAR)}
    assert statuses[_CLIENT] == "archived" and statuses[_STRANGER] == "active"
    assert [t.status for t in fx.tasks.list_active()] == ["active"]
    assert len(fx.fines.list_by_car_number(_CAR)) == fines_before
    assert fx.names.get(_CAR) == "Иван Иванов"


async def test_client_actions_are_manager_only(fx):
    task = await _added_car(fx)
    for action in keyboards.TASK_CLIENT_ACTIONS:
        assert fx.controller.handle_task_client_action(
            action, task.id, 0, chat_id=_STRANGER, telegram_user_id=_STRANGER,
        ) is None


async def test_without_the_feature_the_task_card_is_unchanged(tmp_path):
    fixture = _Fixture(tmp_path, with_names=False)
    try:
        task = await _added_car(fixture)
        card = fixture.controller.handle_trusted_task_open(task.id, 0, telegram_user_id=_TRUSTED)
        assert card.text == texts.format_trusted_task_detail(task_with := fixture.tasks.get(task.id)) and task_with
        assert card.trusted_task_client_buttons is False
    finally:
        fixture.close()


# ---- Статистика ----


async def test_statistics_row_shows_the_client_name(fx):
    await _added_car(fx, name="Александр-2")

    text = fx.controller._format_statistics_reply().text

    row = text[text.index(f"🚗 Госномер: {format_plate_for_telegram(_CAR)}"):]
    assert "👤 Клиент: Александр-2\n🔗 Telegram: не привязан" in row
    assert texts.DEBT_OWNER_NOT_LINKED_LINE not in row


def test_statistics_row_keeps_linked_user_lines_under_the_client_name():
    row = texts.format_debt_row(
        car_number=_CAR, owners=[texts.DebtOwner(name="Ivan", username="ivan123")], total_amount=50.0,
        client_name="Иван Иванов",
    )
    assert row.splitlines()[2:] == ["👤 Клиент: Иван Иванов", "👤 Пользователь: Ivan", "🔗 Telegram: @ivan123"]


# ---- callback_data ----


def test_task_client_callbacks_roundtrip_and_reject_garbage():
    data = keyboards.encode_task_client_callback("unlinkyes", 1234567, 99)
    assert len(data) <= 64
    assert keyboards.decode_task_client_callback(data) == ("unlinkyes", 1234567, 99)
    for garbage in (b"tcl:", b"tcl:drop:1:0", b"tcl:name:x:0", b"tcl:name:1", b"tcl:name:1:0:9", None):
        assert keyboards.decode_task_client_callback(garbage) is None
    name_button = keyboards.add_car_confirm_keyboard(_CAR, client_name_button=True)[1][0]
    assert keyboards.decode_add_car_client_name_callback(name_button.data) == _CAR
    assert keyboards.decode_add_car_confirm_callback(name_button.data) is None
