"""Тесты 📋 Мои авто / 🔎 Проверить сейчас / 📜 История / 🗑 Удалить (см.
design report "Перестроить UX Turkey test bot" п.3/п.4/п.7) через полный
ConversationController — ручная unified-проверка сохраняет run и
обновляет кэш в "Мои авто" (см. design report п.12: manual check
использует ТОТ ЖЕ UnifiedTurkeyCheckService)."""

from decimal import Decimal

import pytest

from reader.turkey_bot import texts
from reader.turkey_bot.conversation import ConversationController
from reader.turkey_bot.conversation_state_repository import (
    TurkeyConversationStateRepository,
)
from reader.turkey_bot.keyboards import (
    car_card_keyboard,
    manager_car_detail_keyboard,
    manager_cars_page_keyboard,
    my_cars_list_keyboard,
)
from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.statistics_service import TurkeyStatisticsService
from reader.turkey_bot.texts import (
    DISABLE_MONITORING_LABEL,
    ENABLE_MONITORING_LABEL,
)
from reader.turkey_bot.unified.models import (
    OverallStatus,
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
)
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository

pytestmark = pytest.mark.asyncio

_CHAT_ID = 111
_USER_ID = 222


def _button_texts(rows):
    return [[btn.text for btn in row] for row in rows]


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

    async def check(self, plate: str, **kwargs) -> UnifiedCheckResult:
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


_TRUSTED_ID = 5712994689


def _make_manager_controller(check_service, *, trusted_operator_user_ids=frozenset({_TRUSTED_ID})):
    """Как _make_controller(), но с доступом к known_users (для
    record_seen в тестах) и с trusted_operator_user_ids — см. задачу
    "Реализуем manager/trusted 'Мои автомобили' для Turkey bot"."""
    states = TurkeyConversationStateRepository(":memory:")
    garage = TurkeyUserCarsRepository(":memory:")
    runs = TurkeyCheckRunRepository(":memory:")
    subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
    known_users = TurkeyBotKnownUsersRepository(":memory:")
    statistics = TurkeyStatisticsService(known_users, runs, subscriptions)
    controller = ConversationController(
        states, garage, runs, subscriptions, statistics, check_service,
        trusted_operator_user_ids=frozenset(trusted_operator_user_ids),
    )
    return controller, garage, runs, subscriptions, known_users


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


async def test_car_open_callback_opens_the_correct_car():
    """См. задачу п.1/п.9 — нажатие конкретной inline-кнопки в "🚗 Мои
    автомобили" открывает ИМЕННО этот автомобиль, не первый попавшийся."""
    controller, _garage, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    car_a = await _add_car(controller, plate="A123AA123")
    controller.handle_add_car_start(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    car_b_reply = await controller.handle_text("B456BB456", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    car_b = car_b_reply.check_now_confirmation_car_id

    reply_a = controller.handle_car_open(car_a, telegram_user_id=_USER_ID)
    reply_b = controller.handle_car_open(car_b, telegram_user_id=_USER_ID)

    assert reply_a.car_card.car_number == "A123AA123"
    assert reply_b.car_card.car_number == "B456BB456"


async def test_car_open_on_foreign_car_returns_none():
    """См. задачу п.9 — вручную сформированный callback с чужим car_id не
    должен открывать чужой автомобиль."""
    controller, _garage, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    car_id = await _add_car(controller)

    reply = controller.handle_car_open(car_id, telegram_user_id=999)
    assert reply is None


async def test_off_card_shows_enable_monitoring_button():
    controller, garage, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    car_id = await _add_car(controller)
    car = garage.get_owned_car(car_id, telegram_user_id=_USER_ID)

    keyboard = car_card_keyboard(car, monitoring_active=False)
    flat = [label for row in _button_texts(keyboard) for label in row]

    assert ENABLE_MONITORING_LABEL in flat
    assert DISABLE_MONITORING_LABEL not in flat


async def test_on_card_shows_disable_monitoring_button():
    controller, garage, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    car_id = await _add_car(controller)
    car = garage.get_owned_car(car_id, telegram_user_id=_USER_ID)

    keyboard = car_card_keyboard(car, monitoring_active=True)
    flat = [label for row in _button_texts(keyboard) for label in row]

    assert DISABLE_MONITORING_LABEL in flat
    assert ENABLE_MONITORING_LABEL not in flat


async def test_my_cars_list_shows_on_off_button_labels():
    controller, garage, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    car_a = await _add_car(controller, plate="A123AA123")
    controller.handle_add_car_start(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("B456BB456", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_car_action("monitor_on", car_a, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    cars = garage.list_cars(_USER_ID)

    reply = controller.handle_my_cars(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    keyboard = my_cars_list_keyboard(cars, reply.my_cars_monitoring_active)
    flat = [label for row in _button_texts(keyboard) for label in row]

    assert "🟢 A123AA123 — ON" in flat
    assert "⚪ B456BB456 — OFF" in flat


async def test_my_cars_never_shows_owner_username():
    """Задача "показывать владельца в manager car list" — уточнение
    scope: ТОЛЬКО manager/trusted-operator вариант, self-service UI не
    меняется. Turkey bot вообще не имеет manager/trusted-operator car
    list (см. архитектуру: "📋 Мои авто" здесь ВСЕГДА car-centric,
    car.telegram_user_id всегда равен самому caller'у, единственная
    версия экрана) — self-service список НЕ должен показывать username
    владельца, даже если он известен turkey_bot_known_users, ни сейчас,
    ни как защита от случайного расширения этого экрана в будущем."""
    states = TurkeyConversationStateRepository(":memory:")
    garage = TurkeyUserCarsRepository(":memory:")
    runs = TurkeyCheckRunRepository(":memory:")
    subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
    known_users = TurkeyBotKnownUsersRepository(":memory:")
    statistics = TurkeyStatisticsService(known_users, runs, subscriptions)
    check_service = _FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0)))
    controller = ConversationController(states, garage, runs, subscriptions, statistics, check_service)

    known_users.record_seen(
        telegram_user_id=_USER_ID, telegram_chat_id=_CHAT_ID, telegram_username="known_handle",
    )
    controller.handle_add_car_start(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = controller.handle_my_cars(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    keyboard = my_cars_list_keyboard(garage.list_cars(_USER_ID), reply.my_cars_monitoring_active)
    flat = [label for row in _button_texts(keyboard) for label in row]

    assert flat == ["⚪ A123AA123 — OFF"]
    assert not any("@" in label or "known_handle" in label for label in flat)


async def test_back_returns_to_my_cars_with_up_to_date_state():
    """См. задачу п.8 — "⬅️ Назад" == повторный handle_my_cars, с
    актуальным ON/OFF (не главное меню)."""
    controller, _garage, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    car_id = await _add_car(controller)
    await controller.handle_car_action("monitor_on", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    back_reply = controller.handle_my_cars(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert back_reply.text == "🚗 Мои автомобили"
    assert back_reply.my_cars_monitoring_active[car_id] is True
    assert back_reply.show_main_menu is False


async def test_delete_prompt_shows_confirmation_without_deleting():
    """См. задачу "Адаптация car-centric UX Georgian bot" п.7 — тот же
    Georgian pattern: 🗑 Удалить автомобиль сначала показывает
    подтверждение, НИЧЕГО не удаляя."""
    controller, garage, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    car_id = await _add_car(controller)

    reply = await controller.handle_car_action("delete_prompt", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.car_delete_confirm_car_id == car_id
    assert len(garage.list_cars(_USER_ID)) == 1


async def test_delete_cancel_returns_to_card_without_deleting():
    controller, garage, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    car_id = await _add_car(controller)
    await controller.handle_car_action("delete_prompt", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_car_action("delete_cancel", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.car_card is not None
    assert reply.car_card.id == car_id
    assert len(garage.list_cars(_USER_ID)) == 1


async def test_delete_confirm_removes_it_from_my_cars():
    controller, garage, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    car_id = await _add_car(controller)
    await controller.handle_car_action("delete_prompt", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_car_action("delete_confirm", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert garage.list_cars(_USER_ID) == []
    assert "нет добавленных" in reply.text


async def test_delete_confirm_does_not_affect_other_users_cars():
    """См. задачу п.7/п.9 — удаление не должно затрагивать чужие
    автомобили (даже с тем же номером у другого пользователя)."""
    controller, garage, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    car_id = await _add_car(controller)
    other_user_id = 777
    controller.handle_add_car_start(chat_id=999, telegram_user_id=other_user_id)
    await controller.handle_text("A123AA123", chat_id=999, telegram_user_id=other_user_id)

    await controller.handle_car_action("delete_confirm", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert garage.list_cars(_USER_ID) == []
    assert len(garage.list_cars(other_user_id)) == 1


async def test_delete_confirm_does_not_remove_a_different_car_of_same_user():
    controller, garage, _ = _make_controller(_FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0))))
    car_id = await _add_car(controller, plate="A123AA123")
    controller.handle_add_car_start(chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("B456BB456", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    await controller.handle_car_action("delete_confirm", car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    remaining = garage.list_cars(_USER_ID)
    assert len(remaining) == 1
    assert remaining[0].car_number == "B456BB456"


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


# ==== manager/trusted-operator "🚗 Мои автомобили" (см. задачу "Реализуем
# manager/trusted 'Мои автомобили' для Turkey bot") ====


def _no_debt_check_service():
    return _FakeCheckService(_make_result("X", overall=OverallStatus.NO_DEBT, total=Decimal(0)))


async def test_my_cars_label_routes_trusted_operator_to_manager_flow():
    """A. trusted 5712994689: MY_CARS_LABEL -> manager flow."""
    controller, garage, _runs, _subs, _known = _make_manager_controller(_no_debt_check_service())
    garage.add_car(telegram_user_id=999, car_number="A123AA123")

    reply = await controller.handle_text(texts.MY_CARS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    assert reply.manager_cars_page_options is not None
    assert reply.my_cars is None


async def test_my_cars_label_routes_ordinary_user_to_self_service_flow():
    """B. Обычный пользователь: MY_CARS_LABEL -> старый self-service flow,
    БЕЗ изменений (регресс)."""
    controller, garage, _runs, _subs, _known = _make_manager_controller(_no_debt_check_service())
    garage.add_car(telegram_user_id=_USER_ID, car_number="A123AA123")

    reply = await controller.handle_text(texts.MY_CARS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.my_cars is not None
    assert len(reply.my_cars) == 1
    assert reply.my_cars[0].car_number == "A123AA123"
    assert reply.manager_cars_page_options is None


async def test_manager_cars_empty_when_no_cars_at_all():
    controller, _garage, _runs, _subs, _known = _make_manager_controller(_no_debt_check_service())

    reply = await controller.handle_text(texts.MY_CARS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    assert "Автомобилей пока нет" in reply.text
    assert reply.manager_cars_page_options is None


async def test_manager_cars_shows_cars_from_multiple_owners():
    """C. Manager видит автомобили нескольких telegram_user_id."""
    controller, garage, _runs, _subs, _known = _make_manager_controller(_no_debt_check_service())
    garage.add_car(telegram_user_id=111, car_number="A111AA111")
    garage.add_car(telegram_user_id=222, car_number="B222BB222")

    reply = await controller.handle_text(texts.MY_CARS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    cars = {car for _id, car, _owner, _on in reply.manager_cars_page_options}
    assert "A111AA111" in cars
    assert "B222BB222" in cars


async def test_manager_cars_shows_owner_username_when_known():
    """D. username отображается отдельной кнопкой: [CAR_NUMBER] [@username]
    [STATUS] (см. задачу "унифицировать оба интерфейса Georgia/Turkey")."""
    controller, garage, _runs, _subs, known_users = _make_manager_controller(_no_debt_check_service())
    garage.add_car(telegram_user_id=333, car_number="M295YB196")
    known_users.record_seen(telegram_user_id=333, telegram_chat_id=333, telegram_username="Mihailov_vm")

    reply = await controller.handle_text(texts.MY_CARS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    rows = {(car, owner) for _id, car, owner, _on in reply.manager_cars_page_options}
    assert ("M295YB196", "@Mihailov_vm") in rows


async def test_manager_cars_hides_username_when_unknown():
    """E. username=None: средняя кнопка — "—", без @None/None/пустого @."""
    controller, garage, _runs, _subs, _known = _make_manager_controller(_no_debt_check_service())
    garage.add_car(telegram_user_id=333, car_number="M295YB196")
    # record_seen НЕ вызывается — владелец никогда не писал боту.

    reply = await controller.handle_text(texts.MY_CARS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    rows = {(car, owner) for _id, car, owner, _on in reply.manager_cars_page_options}
    assert ("M295YB196", "—") in rows
    assert not any("@" in car or "None" in car for car, _owner in rows)
    assert not any("None" in owner for _car, owner in rows)


async def test_manager_cars_same_plate_different_owners_are_separate_rows():
    """F. Одинаковый car_number у двух пользователей: две отдельные
    строки, правильные owner usernames, callbacks не конфликтуют (см.
    задачу п.6 — адресация по car.id, а не car_number)."""
    controller, garage, _runs, _subs, known_users = _make_manager_controller(_no_debt_check_service())
    car_a = garage.add_car(telegram_user_id=111, car_number="A123AA123")
    car_b = garage.add_car(telegram_user_id=222, car_number="A123AA123")
    known_users.record_seen(telegram_user_id=111, telegram_chat_id=111, telegram_username="owner_one")
    known_users.record_seen(telegram_user_id=222, telegram_chat_id=222, telegram_username="owner_two")
    assert car_a.id != car_b.id

    reply = await controller.handle_text(texts.MY_CARS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    row_by_id = {car_id: (car, owner) for car_id, car, owner, _on in reply.manager_cars_page_options}
    assert row_by_id[car_a.id] == ("A123AA123", "@owner_one")
    assert row_by_id[car_b.id] == ("A123AA123", "@owner_two")

    detail_a = controller.handle_manager_car_open(car_a.id, 0, telegram_user_id=_TRUSTED_ID)
    detail_b = controller.handle_manager_car_open(car_b.id, 0, telegram_user_id=_TRUSTED_ID)
    assert "owner_one" in detail_a.text
    assert "owner_two" not in detail_a.text
    assert "owner_two" in detail_b.text
    assert "owner_one" not in detail_b.text


async def test_manager_cars_pagination_next_back_and_boundaries():
    """G. Pagination: >10 записей, next/back, границы страниц."""
    controller, garage, _runs, _subs, _known = _make_manager_controller(_no_debt_check_service())
    for i in range(15):
        garage.add_car(telegram_user_id=1000 + i, car_number=f"P{i:03d}AA01")

    page0 = await controller.handle_text(texts.MY_CARS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)
    assert len(page0.manager_cars_page_options) == 10
    assert page0.manager_cars_total_pages == 2
    assert page0.manager_cars_page == 0

    page1 = controller.handle_manager_cars_page(1, telegram_user_id=_TRUSTED_ID)
    assert len(page1.manager_cars_page_options) == 5
    assert page1.manager_cars_page == 1

    back_to_0 = controller.handle_manager_cars_page(0, telegram_user_id=_TRUSTED_ID)
    assert back_to_0.manager_cars_page == 0

    clamped_low = controller.handle_manager_cars_page(-5, telegram_user_id=_TRUSTED_ID)
    assert clamped_low.manager_cars_page == 0

    clamped_high = controller.handle_manager_cars_page(999, telegram_user_id=_TRUSTED_ID)
    assert clamped_high.manager_cars_page == 1

    # страницы не пересекаются (15 машин суммарно, без дублей)
    ids_page0 = {car_id for car_id, _car, _owner, _on in page0.manager_cars_page_options}
    ids_page1 = {car_id for car_id, _car, _owner, _on in page1.manager_cars_page_options}
    assert ids_page0.isdisjoint(ids_page1)
    assert len(ids_page0 | ids_page1) == 15


async def test_manager_toggle_affects_owner_subscription_not_manager():
    """6. Toggle действует от имени ВЛАДЕЛЬЦА, а не менеджера — см. задачу
    п.5 "не подменять owner вызывающим manager user_id"."""
    controller, garage, _runs, subscriptions, _known = _make_manager_controller(_no_debt_check_service())
    car = garage.add_car(telegram_user_id=333, car_number="M295YB196")
    assert subscriptions.get(telegram_user_id=333, plate="M295YB196") is None

    reply = controller.handle_manager_car_toggle(car.id, 0, telegram_user_id=_TRUSTED_ID)

    owner_sub = subscriptions.get(telegram_user_id=333, plate="M295YB196")
    assert owner_sub is not None
    assert owner_sub.active is True
    assert subscriptions.get(telegram_user_id=_TRUSTED_ID, plate="M295YB196") is None
    status_by_id = {car_id: on for car_id, _car, _owner, on in reply.manager_cars_page_options}
    assert status_by_id[car.id] is True

    reply2 = controller.handle_manager_car_toggle(car.id, 0, telegram_user_id=_TRUSTED_ID)
    owner_sub2 = subscriptions.get(telegram_user_id=333, plate="M295YB196")
    assert owner_sub2.active is False
    status_by_id2 = {car_id: on for car_id, _car, _owner, on in reply2.manager_cars_page_options}
    assert status_by_id2[car.id] is False


async def test_manager_callbacks_reject_non_trusted_user():
    """H. Обычный пользователь не может вызвать manager callback вручную
    и получить cross-user данные — trusted проверяется не только при
    входе через меню, но и на pagination/open/toggle callbacks напрямую."""
    controller, garage, _runs, subscriptions, _known = _make_manager_controller(_no_debt_check_service())
    car = garage.add_car(telegram_user_id=333, car_number="M295YB196")

    assert controller.handle_manager_cars_page(0, telegram_user_id=_USER_ID) is None
    assert controller.handle_manager_car_open(car.id, 0, telegram_user_id=_USER_ID) is None
    assert controller.handle_manager_car_toggle(car.id, 0, telegram_user_id=_USER_ID) is None

    # forged car_id (чужой, не self-service владение) + не-trusted -> ничего не изменилось
    assert subscriptions.get(telegram_user_id=333, plate="M295YB196") is None


async def test_manager_cars_page_keyboard_labels_and_callbacks():
    """Прямая проверка keyboard-builder'а (референс —
    reader/public_bot/keyboards.py::trusted_tasks_page_keyboard) — РОВНО
    ТРИ кнопки на строку: car_number, готовый owner_display
    ("@username"/"—"), 🟢/⚪ БЕЗ даты/периода (Turkey monitoring
    бессрочен)."""
    options = [(1, "A123AA123", "@owner", True), (2, "B456BB456", "—", False)]
    rows = manager_cars_page_keyboard(options, page=0, total_pages=1)
    flat = _button_texts(rows)
    assert flat == [["A123AA123", "@owner", "🟢"], ["B456BB456", "—", "⚪"]]


async def test_manager_car_detail_keyboard_has_back_button():
    rows = manager_car_detail_keyboard(3)
    flat = _button_texts(rows)
    assert flat == [["⬅️ Назад"]]
