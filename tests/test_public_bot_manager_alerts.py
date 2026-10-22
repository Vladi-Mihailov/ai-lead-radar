"""
Тесты "NEW USER EMERGENCY WORKFLOW" (см. задачу "back off when police.ge
is unavailable" / "IMPORTANT PRODUCT DECISION — CHANGE PREVIOUS
REQUIREMENT", production incident 2026-10-21): менеджер получает
проактивное уведомление о новом пользователе @GEShtrafbot (см. reader/
public_bot/manager_alerts.py), а когда этот же пользователь впервые
добавляет автомобиль — короткое дополнение с номером, вместо того чтобы
ждать, пока он сам его найдёт в чате. ВСЕГДА включено (не завязано на
состояние circuit breaker — см. design report про manager_alerts.py), для
exactly-once используется персистентная bot_known_users (та же таблица,
что и у самого design report про "единственный способ узнать, что боту
можно что-либо доставить").

_FakeTelethonClient/_FakeMessageEvent/_FakeCallbackEvent — тот же приём,
что и в tests/test_public_bot_handlers_check_now_ack.py, с добавленным
.client/.send_message (нужно для manager-алертов, которых та линейка
тестов не касается).
"""

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from telethon import events  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.provider import FineProvider  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot import manager_alerts, texts  # noqa: E402
from reader.public_bot.conversation import ConversationController  # noqa: E402
from reader.public_bot.conversation_state_repository import (  # noqa: E402
    BotConversationStateRepository,
)
from reader.public_bot.handlers import register  # noqa: E402
from reader.public_bot.keyboards import encode_period_callback  # noqa: E402
from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402
from reader.public_bot.statistics_service import BotStatisticsService  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import SubscriptionService  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

pytestmark = pytest.mark.asyncio

_TBILISI = ZoneInfo("Asia/Tbilisi")
_MANAGER_CHAT_ID = -1009988776655
_USER_ID = 42


class _FakeProvider(FineProvider):
    async def search_by_plate(self, plate: str):
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


class _FakeSender:
    def __init__(self, *, username=None, first_name=None, last_name=None):
        self.username = username
        self.first_name = first_name
        self.last_name = last_name


class _FakeMessageEvent:
    def __init__(self, *, client, text: str, sender_id: int, chat_id: int, sender=None):
        self.client = client
        self.raw_text = text
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.is_private = True
        self._sender = sender
        self.calls: list[tuple] = []

    async def get_sender(self):
        return self._sender

    async def respond(self, text, *, buttons=None):
        self.calls.append(("respond", text, buttons))


class _FakeCallbackEvent:
    def __init__(self, *, client, data: bytes, sender_id: int, chat_id: int, sender=None):
        self.client = client
        self.data = data
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.is_private = True
        self._sender = sender
        self.calls: list[tuple] = []

    async def get_sender(self):
        return self._sender

    async def answer(self, *args, **kwargs):
        self.calls.append(("answer", args, kwargs))

    async def edit(self, text, *, buttons=None):
        self.calls.append(("edit", text, buttons))

    async def respond(self, text, *, buttons=None):
        self.calls.append(("respond", text, buttons))


class _Fixture:
    def __init__(self, tmp_path):
        self.db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(self.db_path)
        self.detected_fine_repository = DetectedFineRepository(self.db_path)
        self.subscription_repository = FineSubscriptionRepository(self.db_path)
        self.user_repository = UserRepository(self.db_path)
        self.conversation_state_repository = BotConversationStateRepository(self.db_path)
        self.known_users_repository = BotKnownUsersRepository(self.db_path)
        self.check_service = FineCheckService(
            _FakeProvider(), self.task_repository, self.detected_fine_repository,
        )
        self.subscription_service = SubscriptionService(
            self.task_repository, self.subscription_repository,
            self.user_repository, self.check_service,
        )
        self.statistics_service = BotStatisticsService(
            self.known_users_repository, self.subscription_repository, self.detected_fine_repository,
        )
        self.controller = ConversationController(
            self.conversation_state_repository, self.subscription_service, self.statistics_service,
            self.known_users_repository, tz=_TBILISI,
        )

    def close(self):
        self.task_repository.close()
        self.detected_fine_repository.close()
        self.subscription_repository.close()
        self.user_repository.close()
        self.conversation_state_repository.close()
        self.known_users_repository.close()


def _register(fx: _Fixture, *, manager_chat_id=_MANAGER_CHAT_ID) -> _FakeTelethonClient:
    client = _FakeTelethonClient()
    register(
        client, fx.controller, known_users_repository=fx.known_users_repository,
        manager_chat_id=manager_chat_id,
    )
    return client


async def _send_text(client, *, text, sender_id, sender=None):
    event = _FakeMessageEvent(client=client, text=text, sender_id=sender_id, chat_id=sender_id, sender=sender)
    await client.message_handler(event)
    return event


async def _send_callback(client, *, data, sender_id, sender=None):
    event = _FakeCallbackEvent(client=client, data=data, sender_id=sender_id, chat_id=sender_id, sender=sender)
    await client.callback_handler(event)
    return event


# ==== Pure formatting (manager_alerts.py) ====


def test_format_new_user_alert_shows_identity_fields():
    text = manager_alerts.format_new_user_alert(
        telegram_user_id=12345, username="ivan_petrov", first_name="Иван", last_name="Петров",
    )
    assert "Новый пользователь" in text
    assert "Иван Петров" in text
    assert "@ivan_petrov" in text
    assert "12345" in text


def test_format_new_user_alert_handles_missing_username_and_name():
    text = manager_alerts.format_new_user_alert(
        telegram_user_id=999, username=None, first_name=None, last_name=None,
    )
    assert "не указан" in text
    assert "None" not in text


def test_format_car_added_alert_shows_car_number():
    text = manager_alerts.format_car_added_alert(
        telegram_user_id=12345, username="ivan_petrov", first_name="Иван", last_name="Петров",
        car_number="O127PX62",
    )
    assert "Пользователь добавил автомобиль" in text
    assert "127" in text and "62" in text  # цифры car_number (буквы — markdown bold run)
    assert "12345" in text


# ==== 6. brand-new user → manager notified exactly once ====


async def test_brand_new_user_triggers_exactly_one_manager_alert(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        client = _register(fx)
        sender = _FakeSender(username="newbie", first_name="Нов", last_name="Юзер")

        await _send_text(client, text="/start", sender_id=_USER_ID, sender=sender)

        assert len(client.sent_messages) == 1
        chat_id, text, _ = client.sent_messages[0]
        assert chat_id == _MANAGER_CHAT_ID
        assert "Новый пользователь" in text
        assert "newbie" in text
    finally:
        fx.close()


# ==== 7. repeated /start → no duplicate ====


async def test_repeated_start_from_same_user_does_not_duplicate_alert(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        client = _register(fx)
        sender = _FakeSender(username="newbie")

        await _send_text(client, text="/start", sender_id=_USER_ID, sender=sender)
        await _send_text(client, text="/start", sender_id=_USER_ID, sender=sender)
        await _send_text(client, text=texts.MY_CARS_LABEL, sender_id=_USER_ID, sender=sender)

        assert len(client.sent_messages) == 1
    finally:
        fx.close()


# ==== 8. restart → no duplicate ====


async def test_known_user_after_process_restart_does_not_duplicate_alert(tmp_path):
    """bot_known_users персистентен (SQLite/tmp_path) — "restart" эмулируется
    ВТОРЫМ независимым BotKnownUsersRepository/register() поверх того же
    файла БД, без переноса in-memory состояния первого процесса."""
    fx = _Fixture(tmp_path)
    try:
        client = _register(fx)
        sender = _FakeSender(username="newbie")
        await _send_text(client, text="/start", sender_id=_USER_ID, sender=sender)
        assert len(client.sent_messages) == 1
    finally:
        fx.close()

    fx2 = _Fixture(tmp_path)
    try:
        client2 = _register(fx2)
        sender = _FakeSender(username="newbie")
        await _send_text(client2, text="/start", sender_id=_USER_ID, sender=sender)

        assert len(client2.sent_messages) == 0
    finally:
        fx2.close()


# ==== manager_chat_id not configured → feature off, never raises ====


async def test_new_user_alert_is_noop_when_manager_chat_id_not_configured(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        client = _register(fx, manager_chat_id=None)
        sender = _FakeSender(username="newbie")

        await _send_text(client, text="/start", sender_id=_USER_ID, sender=sender)

        assert client.sent_messages == []
    finally:
        fx.close()


# ==== 9. new user subsequently adds car → manager gets car-number follow-up ====


async def test_new_user_adding_car_sends_followup_alert_with_car_number(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        client = _register(fx)
        sender = _FakeSender(username="newbie", first_name="Нов")

        await _send_text(client, text="/start", sender_id=_USER_ID, sender=sender)
        assert len(client.sent_messages) == 1  # new-user alert

        await _send_text(client, text=texts.ADD_CAR_LABEL, sender_id=_USER_ID, sender=sender)
        await _send_text(client, text="M295YB196", sender_id=_USER_ID, sender=sender)
        await _send_callback(
            client, data=encode_period_callback(30), sender_id=_USER_ID, sender=sender,
        )

        assert len(client.sent_messages) == 2  # + car-added follow-up
        chat_id, text, _ = client.sent_messages[1]
        assert chat_id == _MANAGER_CHAT_ID
        assert "Пользователь добавил автомобиль" in text
    finally:
        fx.close()


# ==== 10. existing user adds another car → do NOT misclassify as new user ====


async def test_existing_user_adding_second_car_sends_no_followup_alert(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        client = _register(fx)
        sender = _FakeSender(username="regular")

        await _send_text(client, text="/start", sender_id=_USER_ID, sender=sender)
        await _send_text(client, text=texts.ADD_CAR_LABEL, sender_id=_USER_ID, sender=sender)
        await _send_text(client, text="M295YB196", sender_id=_USER_ID, sender=sender)
        await _send_callback(client, data=encode_period_callback(30), sender_id=_USER_ID, sender=sender)
        messages_after_first_car = len(client.sent_messages)
        assert messages_after_first_car == 2  # new-user + first car-added

        # Второй автомобиль того же пользователя.
        await _send_text(client, text=texts.ADD_CAR_LABEL, sender_id=_USER_ID, sender=sender)
        await _send_text(client, text="B111BB11", sender_id=_USER_ID, sender=sender)
        await _send_callback(client, data=encode_period_callback(30), sender_id=_USER_ID, sender=sender)

        # Ни нового "новый пользователь", ни ещё одного "добавил автомобиль".
        assert len(client.sent_messages) == messages_after_first_car
    finally:
        fx.close()
