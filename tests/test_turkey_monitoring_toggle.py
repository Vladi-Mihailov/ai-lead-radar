"""Тесты 🔔/🔕 Включить/Отключить мониторинг и manager ⛔ Остановить
мониторинг (см. design report "Перестроить UX Turkey test bot" п.8/п.13)."""

import pytest

from reader.turkey_bot_test.conversation import ConversationController
from reader.turkey_bot_test.conversation_state_repository import (
    TurkeyConversationStateRepository,
)
from reader.turkey_bot_test.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot_test.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot_test.statistics_service import TurkeyStatisticsService
from reader.turkey_bot_test.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot_test.user_cars_repository import TurkeyUserCarsRepository

pytestmark = pytest.mark.asyncio

_CHAT_ID = 111
_USER_ID = 222
_TRUSTED_USER_ID = 999


class _UnusedCheckService:
    async def check(self, plate):
        raise AssertionError("не должен вызываться в этих тестах")


def _make_controller():
    states = TurkeyConversationStateRepository(":memory:")
    garage = TurkeyUserCarsRepository(":memory:")
    runs = TurkeyCheckRunRepository(":memory:")
    subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
    known_users = TurkeyBotKnownUsersRepository(":memory:")
    statistics = TurkeyStatisticsService(known_users, runs, subscriptions)
    controller = ConversationController(
        states, garage, runs, subscriptions, statistics, _UnusedCheckService(),
        trusted_operator_user_ids=frozenset({_TRUSTED_USER_ID}),
    )
    return controller, garage, subscriptions


async def _add_car(controller, plate="A123AA123"):
    controller.handle_add_car_start(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text(plate, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    return reply.check_now_confirmation_car_id


async def test_enable_monitoring_creates_active_subscription():
    controller, _, subscriptions = _make_controller()
    car_id = await _add_car(controller)

    reply = await controller.handle_car_action("monitor_on", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert "включён" in reply.text
    sub = subscriptions.get(telegram_user_id=_USER_ID, plate="A123AA123")
    assert sub is not None
    assert sub.active is True
    assert sub.next_check_at is not None


async def test_disable_monitoring_deactivates_subscription():
    controller, _, subscriptions = _make_controller()
    car_id = await _add_car(controller)
    await controller.handle_car_action("monitor_on", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_car_action("monitor_off", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert "отключён" in reply.text
    sub = subscriptions.get(telegram_user_id=_USER_ID, plate="A123AA123")
    assert sub.active is False


async def test_car_card_reflects_monitoring_state():
    controller, _, _ = _make_controller()
    car_id = await _add_car(controller)

    reply = controller.handle_car_open(car_id, telegram_user_id=_USER_ID)
    assert reply.car_card_monitoring_active is False

    await controller.handle_car_action("monitor_on", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = controller.handle_car_open(car_id, telegram_user_id=_USER_ID)
    assert reply.car_card_monitoring_active is True


async def test_delete_car_also_disables_monitoring():
    controller, _garage, subscriptions = _make_controller()
    car_id = await _add_car(controller)
    await controller.handle_car_action("monitor_on", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    await controller.handle_car_action("delete", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    sub = subscriptions.get(telegram_user_id=_USER_ID, plate="A123AA123")
    assert sub.active is False


async def test_stop_monitoring_is_manager_only_and_deactivates_all():
    controller, _, subscriptions = _make_controller()
    car_id = await _add_car(controller)
    await controller.handle_car_action("monitor_on", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    # Обычный пользователь не должен получить доступ через handle_text.
    reply = await controller.handle_text("⛔ Остановить мониторинг", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert subscriptions.get(telegram_user_id=_USER_ID, plate="A123AA123").active is True
    # (роутинг просто не сматчился ни на одну ветку -> трактуется как ввод номера)

    reply = controller.handle_stop_monitoring(chat_id=_CHAT_ID)
    assert "1" in reply.text
    assert subscriptions.get(telegram_user_id=_USER_ID, plate="A123AA123").active is False


async def test_stop_monitoring_only_affects_turkey_test_subscriptions_table():
    """См. design report п.16 — своя изолированная таблица/БД, физически
    не может задеть Georgian production."""
    controller, _, subscriptions = _make_controller()
    car_id = await _add_car(controller)
    await controller.handle_car_action("monitor_on", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert subscriptions.count_active() == 1
    controller.handle_stop_monitoring(chat_id=_CHAT_ID)
    assert subscriptions.count_active() == 0
