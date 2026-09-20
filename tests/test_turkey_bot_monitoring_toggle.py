"""Тесты 🔔/🔕 Включить/Отключить мониторинг и manager ⛔ Остановить
мониторинг (см. design report "Перестроить UX Turkey test bot" п.8/п.13)."""

import pytest

from reader.turkey_bot.conversation import ConversationController
from reader.turkey_bot.conversation_state_repository import (
    TurkeyConversationStateRepository,
)
from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.statistics_service import TurkeyStatisticsService
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository

pytestmark = pytest.mark.asyncio

_CHAT_ID = 111
_USER_ID = 222
_TRUSTED_USER_ID = 999


class _UnusedCheckService:
    async def check(self, plate, **kwargs):
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

    # Карточка перерисовывается ОДНИМ сообщением (см. задачу "Адаптация
    # car-centric UX Georgian bot" п.5) — РОВНО тот текст, что задан в
    # задаче: "🚗 {plate}\nМониторинг: 🟢 ON".
    assert reply.text == "🚗 A123AA123\nМониторинг: 🟢 ON"
    assert reply.car_card_monitoring_active is True
    sub = subscriptions.get(telegram_user_id=_USER_ID, plate="A123AA123")
    assert sub is not None
    assert sub.active is True
    assert sub.next_check_at is not None


async def test_disable_monitoring_deactivates_subscription():
    controller, _, subscriptions = _make_controller()
    car_id = await _add_car(controller)
    await controller.handle_car_action("monitor_on", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_car_action("monitor_off", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == "🚗 A123AA123\nМониторинг: ⚪ OFF"
    assert reply.car_card_monitoring_active is False
    sub = subscriptions.get(telegram_user_id=_USER_ID, plate="A123AA123")
    assert sub.active is False


async def test_enable_changes_off_to_on():
    controller, _, _ = _make_controller()
    car_id = await _add_car(controller)

    off_reply = controller.handle_car_open(car_id, telegram_user_id=_USER_ID)
    assert off_reply.car_card_monitoring_active is False

    on_reply = await controller.handle_car_action("monitor_on", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert on_reply.car_card_monitoring_active is True


async def test_disable_changes_on_to_off():
    controller, _, _ = _make_controller()
    car_id = await _add_car(controller)
    await controller.handle_car_action("monitor_on", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    off_reply = await controller.handle_car_action("monitor_off", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert off_reply.car_card_monitoring_active is False


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

    await controller.handle_car_action("delete_confirm", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

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


def _build_controller(db_dir):
    """Файловая (не :memory:) БД — все repository указывают на РЕАЛЬНЫЕ
    файлы в db_dir, чтобы вторая инстанция ConversationController,
    созданная над теми же путями, могла прочитать состояние, записанное
    первой (см. test_on_off_state_survives_restart_reads_from_db_not_memory
    ниже)."""
    states = TurkeyConversationStateRepository(db_dir / "states.sqlite3")
    garage = TurkeyUserCarsRepository(db_dir / "cars.sqlite3")
    runs = TurkeyCheckRunRepository(db_dir / "runs.sqlite3")
    subscriptions = TurkeyMonitoringSubscriptionRepository(db_dir / "subscriptions.sqlite3")
    known_users = TurkeyBotKnownUsersRepository(db_dir / "known_users.sqlite3")
    statistics = TurkeyStatisticsService(known_users, runs, subscriptions)
    controller = ConversationController(states, garage, runs, subscriptions, statistics, _UnusedCheckService())
    return controller, garage, subscriptions


async def test_on_off_state_survives_restart_reads_from_db_not_memory(tmp_path):
    """См. задачу "Адаптация car-centric UX Georgian bot" п.12: "после
    restart ON/OFF берётся из DB, а не из memory-state" — эмулирует
    рестарт процесса: ВТОРОЙ, независимый набор ConversationController/
    repository над ТЕМИ ЖЕ файлами SQLite, без единого общего Python-
    объекта с первым набором."""
    controller_1, _garage_1, _subscriptions_1 = _build_controller(tmp_path)
    controller_1.handle_add_car_start(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller_1.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    car_id = reply.check_now_confirmation_car_id
    await controller_1.handle_car_action("monitor_on", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    # "Рестарт" — ни controller_1, ни его repository здесь больше не
    # используются; controller_2 создан заново с нуля.
    controller_2, _garage_2, _subscriptions_2 = _build_controller(tmp_path)

    on_reply = controller_2.handle_car_open(car_id, telegram_user_id=_USER_ID)
    assert on_reply.car_card_monitoring_active is True
    assert on_reply.text == "🚗 A123AA123\nМониторинг: 🟢 ON"

    await controller_2.handle_car_action("monitor_off", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    controller_3, _garage_3, _subscriptions_3 = _build_controller(tmp_path)
    off_reply = controller_3.handle_car_open(car_id, telegram_user_id=_USER_ID)
    assert off_reply.car_card_monitoring_active is False
    assert off_reply.text == "🚗 A123AA123\nМониторинг: ⚪ OFF"
