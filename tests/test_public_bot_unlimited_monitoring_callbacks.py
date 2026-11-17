"""
Бессрочный мониторинг Грузии (задача "убрать выбор срока мониторинга") —
сквозные тесты через НАСТОЯЩИЙ reader/public_bot/handlers.py::register():
подтверждение Add Car вместо выбора срока и обратная совместимость
callback_data кнопок, которые бот больше не отправляет, но которые
остались в истории чатов (выбор срока 30/90/180/365 и 15/30/90, карточка
"срок истёк"). Telethon — фейковый, provider — фейковый (без сети).
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from telethon import events  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import UNLIMITED_END_DATE  # noqa: E402
from reader.fines.provider import FineProvider  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot import texts  # noqa: E402
from reader.public_bot.conversation import ConversationController  # noqa: E402
from reader.public_bot.conversation_state_repository import BotConversationStateRepository  # noqa: E402
from reader.public_bot.handlers import register  # noqa: E402
from reader.public_bot.keyboards import add_car_confirm_keyboard  # noqa: E402
from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402
from reader.public_bot.statistics_service import BotStatisticsService  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import SubscriptionService  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

pytestmark = pytest.mark.asyncio

_TBILISI = ZoneInfo("Asia/Tbilisi")
_USER_ID = 42
_OTHER_USER_ID = 43
_TRUSTED_ID = 5712994689
_PLATE = "M295YB196"
_CONFIRM_DATA, _CANCEL_DATA = (button.data for button in add_car_confirm_keyboard()[0])


class _FakeProvider(FineProvider):
    def __init__(self):
        self.requested_plates: list[str] = []

    async def search_by_plate(self, plate: str):
        self.requested_plates.append(plate)
        return []


class _FakeTelethonClient:
    def __init__(self):
        self.callback_handler = None
        self.message_handler = None
        self.sent_messages: list[tuple] = []

    def on(self, event_filter):
        def decorator(func):
            if isinstance(event_filter, events.CallbackQuery):
                self.callback_handler = func
            elif isinstance(event_filter, events.NewMessage):
                self.message_handler = func
            return func

        return decorator

    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text, kwargs))


class _FakeEvent:
    def __init__(self, *, client, sender_id: int, text: str | None = None, data: bytes | None = None):
        self.client = client
        self.raw_text = text
        self.data = data
        self.sender_id = sender_id
        self.chat_id = sender_id
        self.is_private = True
        self.calls: list[tuple] = []

    async def get_sender(self):
        return None

    async def answer(self, *args, **kwargs):
        self.calls.append(("answer", args, kwargs))

    async def edit(self, text, *, buttons=None):
        self.calls.append(("edit", text, buttons))

    async def respond(self, text, *, buttons=None):
        self.calls.append(("respond", text, buttons))

    def shown(self) -> list[tuple[str, list[str]]]:
        """(text, inline-button labels) каждого показанного экрана."""
        screens = []
        for call in self.calls:
            if call[0] in ("edit", "respond"):
                _, text, buttons = call
                labels = [
                    getattr(button, "text", None) or getattr(getattr(button, "button", None), "text", "")
                    for row in (buttons or []) for button in row
                ]
                screens.append((text, labels))
        return screens


class _Fixture:
    def __init__(self, tmp_path):
        db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(db_path)
        self.detected_fine_repository = DetectedFineRepository(db_path)
        self.subscription_repository = FineSubscriptionRepository(db_path)
        self.user_repository = UserRepository(db_path)
        self.conversation_state_repository = BotConversationStateRepository(db_path)
        self.known_users_repository = BotKnownUsersRepository(db_path)
        self.provider = _FakeProvider()
        check_service = FineCheckService(self.provider, self.task_repository, self.detected_fine_repository)
        self.subscription_service = SubscriptionService(
            self.task_repository, self.subscription_repository, self.user_repository, check_service,
        )
        controller = ConversationController(
            self.conversation_state_repository, self.subscription_service,
            BotStatisticsService(
                self.known_users_repository, self.subscription_repository, self.detected_fine_repository,
            ),
            self.known_users_repository, tz=_TBILISI,
            trusted_operator_user_ids=frozenset({_TRUSTED_ID}),
        )
        self.client = _FakeTelethonClient()
        register(self.client, controller, known_users_repository=self.known_users_repository)

    async def text(self, text: str, *, sender_id=_USER_ID) -> _FakeEvent:
        event = _FakeEvent(client=self.client, sender_id=sender_id, text=text)
        await self.client.message_handler(event)
        return event

    async def press(self, data: bytes, *, sender_id=_USER_ID) -> _FakeEvent:
        event = _FakeEvent(client=self.client, sender_id=sender_id, data=data)
        await self.client.callback_handler(event)
        return event

    def legacy_subscription(self, *, status: str) -> int:
        """Подписка со сроком, оставшаяся с периода до бессрочного
        мониторинга; срок уже прошёл."""
        today = date.today()
        task = self.task_repository.create(
            car_number=_PLATE, label=None, start_date=today - timedelta(days=60),
            end_date=today - timedelta(days=30), telegram_chat_id=_USER_ID, created_by_user_id=_USER_ID,
            monitoring_scope="client_bot",
        )
        subscription = self.subscription_repository.create(
            monitoring_task_id=task.id, car_number=_PLATE, telegram_user_id=_USER_ID,
            telegram_chat_id=_USER_ID, telegram_username=None,
            start_date=today - timedelta(days=60), end_date=today - timedelta(days=30),
        )
        if status == "expired":
            self.subscription_repository.expire_elapsed(today=today)
        return subscription.id

    def close(self):
        for repository in (
            self.task_repository, self.detected_fine_repository, self.subscription_repository,
            self.user_repository, self.conversation_state_repository, self.known_users_repository,
        ):
            repository.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


def _all_labels(*events_) -> list[str]:
    return [label for event in events_ for _text, labels in event.shown() for label in labels]


# ---- Add Car: номер → подтверждение → бессрочная проверка ----


async def test_add_car_flow_shows_confirmation_instead_of_period_choice(fx):
    menu = await fx.text(texts.ADD_CAR_LABEL)
    plate = await fx.text(_PLATE)

    [(prompt_text, prompt_labels)] = plate.shown()
    assert prompt_text == texts.format_add_car_confirm_prompt(_PLATE)
    assert prompt_labels == ["✅ Добавить", "Отмена"]
    assert not any("дней" in label for label in _all_labels(menu, plate))

    done = await fx.press(_CONFIRM_DATA)

    [(done_text, _labels)] = done.shown()
    assert done_text.startswith("✅ Автомобиль добавлен. Проверка штрафов включена")
    [subscription] = fx.subscription_repository.list_by_user(_USER_ID)
    assert subscription.status == "active"
    assert subscription.end_date == UNLIMITED_END_DATE
    [task] = fx.task_repository.list_active()
    assert task.end_date == UNLIMITED_END_DATE


async def test_add_car_cancel_creates_nothing(fx):
    await fx.text(_PLATE)

    cancel = await fx.press(_CANCEL_DATA)

    [(text, _labels)] = cancel.shown()
    assert text == texts.MAIN_MENU_TEXT
    assert fx.subscription_repository.list_by_user(_USER_ID) == []
    assert fx.task_repository.list_active() == []
    assert fx.provider.requested_plates == []
    assert fx.conversation_state_repository.get(_USER_ID) is None


async def test_my_car_card_shows_monitoring_active_instead_of_end_date(fx):
    await fx.text(_PLATE)
    await fx.press(_CONFIRM_DATA)
    [subscription] = fx.subscription_repository.list_by_user(_USER_ID)

    card = await fx.press(f"mycaropen:{subscription.id}:0".encode("ascii"))

    [(text, labels)] = card.shown()
    assert texts.MONITORING_ACTIVE_LINE in text
    assert "📅" not in text
    assert texts.TURN_OFF_BUTTON_LABEL in labels


# ---- обратная совместимость старых callback_data ----


@pytest.mark.parametrize("step", ["awaiting_period", "awaiting_add_confirm"])
@pytest.mark.parametrize("days", [30, 365])
async def test_legacy_period_button_confirms_add_car_unlimited(fx, step, days):
    """Старая кнопка "N дней" (бот её больше не отправляет) работает как
    "✅ Добавить" — и для диалога, остановленного на прежнем шаге выбора
    срока до обновления, и для нового шага подтверждения. Выбранные дни
    игнорируются — мониторинг бессрочный."""
    fx.conversation_state_repository.set(
        _USER_ID, telegram_user_id=_USER_ID, step=step, payload={"car_number": _PLATE, "username": None},
    )

    done = await fx.press(f"period:{days}".encode("ascii"))

    [(done_text, _labels)] = done.shown()
    assert done_text.startswith(texts.ADD_CAR_DONE_LINE)
    [subscription] = fx.subscription_repository.list_by_user(_USER_ID)
    assert subscription.end_date == UNLIMITED_END_DATE


async def test_legacy_period_button_without_dialog_creates_nothing(fx):
    event = await fx.press(b"period:30")

    assert event.calls  # ответили (alert/текст), не упали
    assert fx.subscription_repository.list_by_user(_USER_ID) == []
    assert fx.task_repository.list_active() == []


async def test_legacy_trusted_period_button_resumes_task_unlimited(fx):
    task = fx.task_repository.create(
        car_number=_PLATE, label=None, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        telegram_chat_id=-100999, created_by_user_id=111,
    )
    fx.task_repository.set_status(task.id, "stopped")

    event = await fx.press(f"ttaskperiod:{task.id}:30:0".encode("ascii"), sender_id=_TRUSTED_ID)

    resumed = fx.task_repository.get(task.id)
    assert resumed.status == "active"
    assert resumed.end_date == UNLIMITED_END_DATE
    [(text, _labels)] = event.shown()
    assert text.startswith("✅ Мониторинг")
    assert "дней" not in text


async def test_legacy_trusted_period_button_is_ignored_for_non_trusted_user(fx):
    task = fx.task_repository.create(
        car_number=_PLATE, label=None, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        telegram_chat_id=-100999, created_by_user_id=111,
    )
    fx.task_repository.set_status(task.id, "stopped")

    await fx.press(f"ttaskperiod:{task.id}:30:0".encode("ascii"), sender_id=_USER_ID)

    unchanged = fx.task_repository.get(task.id)
    assert unchanged.status == "stopped"
    assert unchanged.end_date == date(2026, 1, 31)


async def test_trusted_resume_button_turns_on_without_period_screen(fx):
    task = fx.task_repository.create(
        car_number=_PLATE, label=None, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        telegram_chat_id=-100999, created_by_user_id=111,
    )
    fx.task_repository.set_status(task.id, "stopped")

    off_screen = await fx.press(f"ttasktoggle:{task.id}:0".encode("ascii"), sender_id=_TRUSTED_ID)
    resumed = await fx.press(f"ttaskcontinue:{task.id}:0".encode("ascii"), sender_id=_TRUSTED_ID)

    assert not any("дней" in label for label in _all_labels(off_screen, resumed))
    assert fx.task_repository.get(task.id).status == "active"
    assert fx.task_repository.get(task.id).end_date == UNLIMITED_END_DATE


@pytest.mark.parametrize("status", ["active", "expired"])
async def test_legacy_resume_button_from_expired_card_turns_notifications_on(fx, status):
    """"▶️ Продолжить мониторинг" с прежней карточки "срок истёк" — то же,
    что "🔔 Включить уведомления": та же подписка, бессрочно."""
    subscription_id = fx.legacy_subscription(status=status)

    event = await fx.press(f"mycarresume:{subscription_id}".encode("ascii"))

    resumed = fx.subscription_repository.get(subscription_id)
    assert resumed.status == "active"
    assert resumed.end_date == UNLIMITED_END_DATE
    [(text, labels)] = event.shown()
    assert texts.MONITORING_ACTIVE_LINE in text
    assert texts.TURN_OFF_BUTTON_LABEL in labels
    assert len(fx.subscription_repository.list_by_user(_USER_ID)) == 1


async def test_legacy_resume_button_rejects_other_users_subscription(fx):
    subscription_id = fx.legacy_subscription(status="expired")

    await fx.press(f"mycarresume:{subscription_id}".encode("ascii"), sender_id=_OTHER_USER_ID)

    assert fx.subscription_repository.get(subscription_id).status == "expired"


async def test_legacy_check_once_button_still_checks_without_reactivating(fx):
    subscription_id = fx.legacy_subscription(status="expired")

    await fx.press(f"mycarchecknowonce:{subscription_id}".encode("ascii"))

    assert fx.provider.requested_plates == [_PLATE]
    assert fx.subscription_repository.get(subscription_id).status == "expired"
