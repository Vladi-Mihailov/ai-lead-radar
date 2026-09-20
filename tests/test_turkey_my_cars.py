"""Тесты 📋 Мои авто / 🔎 Проверить сейчас / 📜 История / 🗑 Удалить (см.
design report "Перестроить UX Turkey test bot" п.3/п.4/п.7) через полный
ConversationController — ручная unified-проверка сохраняет run и
обновляет кэш в "Мои авто" (см. design report п.12: manual check
использует ТОТ ЖЕ UnifiedTurkeyCheckService)."""

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
from reader.turkey_bot_test.unified.models import (
    OverallStatus,
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
)
from reader.turkey_bot_test.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot_test.user_cars_repository import TurkeyUserCarsRepository

pytestmark = pytest.mark.asyncio

_CHAT_ID = 111
_USER_ID = 222


def _make_result(plate: str, *, overall: OverallStatus, total: Decimal) -> UnifiedCheckResult:
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    providers = tuple(
        ProviderCheckResult(
            provider=name, status=ProviderStatus.HAS_DEBT if overall == OverallStatus.HAS_DEBT else ProviderStatus.NO_DEBT,
            debt_count=1 if overall == OverallStatus.HAS_DEBT else 0,
            principal_amount=total if overall == OverallStatus.HAS_DEBT else Decimal(0),
            penalty_amount=Decimal(0), total_amount=total if overall == OverallStatus.HAS_DEBT else Decimal(0),
            items=(), error_type=None, checked_at=now,
        )
        for name in ("gib", "avrasya", "kgm")
    )
    return UnifiedCheckResult(
        plate=plate, started_at=now, finished_at=now, overall_status=overall,
        total_amount=total if overall == OverallStatus.HAS_DEBT else Decimal(0), providers=providers,
    )


class _FakeCheckService:
    def __init__(self, result: UnifiedCheckResult):
        self._result = result
        self.calls: list[str] = []

    async def check(self, plate: str) -> UnifiedCheckResult:
        self.calls.append(plate)
        return self._result


def _make_controller(check_service):
    states = TurkeyConversationStateRepository(":memory:")
    garage = TurkeyUserCarsRepository(":memory:")
    runs = TurkeyCheckRunRepository(":memory:")
    subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
    known_users = TurkeyBotKnownUsersRepository(":memory:")
    statistics = TurkeyStatisticsService(known_users, runs, subscriptions)
    controller = ConversationController(states, garage, runs, subscriptions, statistics, check_service)
    return controller, garage, runs


async def _add_car(controller, plate="A123AA123"):
    controller.handle_add_car_start(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text(plate, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    return reply.check_now_confirmation_car_id


async def test_empty_my_cars():
    controller, _, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    reply = controller.handle_my_cars(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert "нет добавленных" in reply.text


async def test_my_cars_lists_added_car():
    controller, _, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    await _add_car(controller)

    reply = controller.handle_my_cars(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert reply.my_cars is not None
    assert len(reply.my_cars) == 1
    assert reply.my_cars[0].car_number == "A123AA123"


async def test_unified_manual_check_saves_run_and_updates_cache():
    result = _make_result("A123AA123", overall=OverallStatus.HAS_DEBT, total=Decimal(5477))
    check_service = _FakeCheckService(result)
    controller, garage, runs = _make_controller(check_service)
    car_id = await _add_car(controller)

    reply = await controller.handle_car_action("check", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert check_service.calls == ["A123AA123"]
    assert "5 477" in reply.text or "5477" in reply.text
    assert reply.cta_buttons is not None  # has_debt -> CTA кнопки показаны

    cars = garage.list_cars(_USER_ID)
    assert cars[0].last_overall_status == "has_debt"
    assert cars[0].last_total_amount == Decimal(5477)

    history = runs.list_by_plate("A123AA123")
    assert len(history) == 1
    assert history[0].overall_status == OverallStatus.HAS_DEBT


async def test_no_debt_result_has_no_cta_buttons():
    result = _make_result("A123AA123", overall=OverallStatus.NO_DEBT, total=Decimal(0))
    controller, garage, _ = _make_controller(_FakeCheckService(result))
    car_id = await _add_car(controller)

    reply = await controller.handle_car_action("check", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.cta_buttons is None
    assert garage.list_cars(_USER_ID)[0].last_overall_status == "no_debt"


async def test_car_action_on_foreign_car_returns_none():
    controller, _garage, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    car_id = await _add_car(controller)

    reply = await controller.handle_car_action("check", car_id, chat_id=999, telegram_user_id=999)
    assert reply is None


async def test_delete_car_removes_it_from_my_cars():
    controller, garage, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    car_id = await _add_car(controller)

    reply = await controller.handle_car_action("delete", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert "удалён" in reply.text
    assert garage.list_cars(_USER_ID) == []


async def test_history_empty_before_any_check():
    controller, _, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    car_id = await _add_car(controller)

    reply = await controller.handle_car_action("history", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert "пока не было" in reply.text


async def test_history_shows_past_checks_after_manual_check():
    result = _make_result("A123AA123", overall=OverallStatus.HAS_DEBT, total=Decimal(500))
    controller, _, _ = _make_controller(_FakeCheckService(result))
    car_id = await _add_car(controller)

    await controller.handle_car_action("check", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_car_action("history", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert "500" in reply.text
