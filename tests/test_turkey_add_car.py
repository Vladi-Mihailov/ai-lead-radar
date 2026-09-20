"""Тесты ➕ Добавить авто (см. design report "Перестроить UX Turkey test
bot" п.2) — normalize_plate (вкл. кириллицу), dedupe, "добавляется сразу,
без предварительной проверки"."""

from decimal import Decimal

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


class _UnusedCheckService:
    """Add Car больше не выполняет автоматическую проверку (см. design
    report п.2 — просто добавляет и ПРЕДЛАГАЕТ проверить) — check() не
    должен вызываться вообще в этих тестах."""

    async def check(self, plate):
        raise AssertionError("Add Car не должен сам запускать unified check")


def _make_controller(*, check_service=None):
    states = TurkeyConversationStateRepository(":memory:")
    garage = TurkeyUserCarsRepository(":memory:")
    runs = TurkeyCheckRunRepository(":memory:")
    subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
    known_users = TurkeyBotKnownUsersRepository(":memory:")
    statistics = TurkeyStatisticsService(known_users, runs, subscriptions)
    controller = ConversationController(
        states, garage, runs, subscriptions, statistics, check_service or _UnusedCheckService(),
    )
    return controller, garage


async def test_add_car_flow_creates_car_immediately_without_check():
    controller, garage = _make_controller()

    reply = controller.handle_add_car_start(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert reply.show_cancel_button

    reply = await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert "A123AA123" in reply.text
    assert reply.check_now_confirmation_car_id is not None

    cars = garage.list_cars(_USER_ID)
    assert len(cars) == 1
    assert cars[0].car_number == "A123AA123"
    assert cars[0].last_overall_status is None  # ещё не проверялся


async def test_add_car_normalizes_cyrillic_lookalikes():
    controller, garage = _make_controller()
    controller.handle_add_car_start(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    # А(cyrillic)123АА(cyrillic)123 -> A123AA123 (см. validation.py)
    await controller.handle_text("А123АА123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    cars = garage.list_cars(_USER_ID)
    assert cars[0].car_number == "A123AA123"


async def test_add_car_rejects_invalid_plate_and_stays_in_flow():
    controller, garage = _make_controller()
    controller.handle_add_car_start(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("!!!", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert reply.show_cancel_button
    assert garage.list_cars(_USER_ID) == []

    # Пользователь всё ещё в состоянии "добавляю авто" — второй ввод после
    # ошибки должен сработать нормально.
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert len(garage.list_cars(_USER_ID)) == 1


async def test_duplicate_car_is_not_added_twice():
    controller, garage = _make_controller()

    controller.handle_add_car_start(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    controller.handle_add_car_start(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert "уже есть" in reply.text
    assert len(garage.list_cars(_USER_ID)) == 1


async def test_duplicate_car_preserves_existing_check_history_cache():
    """Повторное "добавление" уже существующего, уже проверенного
    автомобиля не должно стирать last_overall_status/last_total_amount
    (см. reader/turkey_bot_test/user_cars_repository.py::add_car —
    ON CONFLICT DO NOTHING)."""
    controller, garage = _make_controller()
    controller.handle_add_car_start(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    garage.update_last_result(
        telegram_user_id=_USER_ID, car_number="A123AA123",
        overall_status="has_debt", total_amount=Decimal(500),
    )

    controller.handle_add_car_start(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    cars = garage.list_cars(_USER_ID)
    assert cars[0].last_overall_status == "has_debt"
    assert cars[0].last_total_amount == Decimal(500)


async def test_bare_plate_without_pressing_add_car_button_also_works():
    """См. design report: тот же принцип "bare plate still works", что и
    в старом UX — голый ввод номера трактуется как добавление авто."""
    controller, garage = _make_controller()

    reply = await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert "A123AA123" in reply.text
    assert len(garage.list_cars(_USER_ID)) == 1
