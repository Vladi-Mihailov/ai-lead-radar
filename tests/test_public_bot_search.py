"""Тесты reader/public_bot/conversation.py — manager/trusted Search (см.
задачу "manager/trusted Search") — ТОЛЬКО trusted/manager, self-service не
затронут.

Live check per result (см. задачу, подтверждено пользователем): Search
для Georgia выполняет ТУ ЖЕ live-проверку police.ge, что и "🔎 Проверить
сейчас" (SubscriptionService.check_now_task_for_search) — _FakeProvider
ниже позволяет управлять, что "вернул бы police.ge ПРЯМО СЕЙЧАС" для
конкретного car_number, включая ERROR (FineProviderError) и
неопределённую сумму (amount=None).
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.provider import FineProvider, FineProviderError
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot import texts
from reader.public_bot.conversation import ConversationController
from reader.public_bot.conversation_state_repository import (
    BotConversationStateRepository,
)
from reader.public_bot.known_users_repository import (
    BotKnownUsersRepository,
)
from reader.public_bot.statistics_service import BotStatisticsService
from reader.public_bot.subscription_repository import (
    FineSubscriptionRepository,
)
from reader.public_bot.subscription_service import SubscriptionService
from reader.users.repository import UserRepository

pytestmark = pytest.mark.asyncio

_TBILISI = ZoneInfo("Asia/Tbilisi")
_TRUSTED_ID = 5712994689
_ORDINARY_ID = 1


class _FakeProvider(FineProvider):
    """records_by_car — {car_number: [ParsedFineRecord, ...]} (может
    мутироваться между вызовами теста, см. класс docstring про "что
    вернул бы police.ge ПРЯМО СЕЙЧАС"). error_cars — car_number, для
    которых search_by_plate() бросает FineProviderError (см. задачу:
    "ERROR/UNKNOWN != 0")."""

    def __init__(self, records_by_car=None, error_cars=None):
        self.records_by_car = records_by_car if records_by_car is not None else {}
        self.error_cars = error_cars if error_cars is not None else set()

    async def search_by_plate(self, plate: str):
        if plate in self.error_cars:
            raise FineProviderError("provider unavailable")
        return self.records_by_car.get(plate, [])


def _record(car_number: str, *, amount: float | None, fingerprint: str) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number, external_fine_id=fingerprint,
        penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
        delivered_status="Не вручено", fingerprint=fingerprint,
        raw_data={"protocolNo": fingerprint}, amount=amount,
    )


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(_TBILISI).date()


class _Fixture:
    def __init__(self, tmp_path, *, trusted_operator_user_ids=frozenset()):
        self.db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(self.db_path)
        self.detected_fine_repository = DetectedFineRepository(self.db_path)
        self.subscription_repository = FineSubscriptionRepository(self.db_path)
        self.user_repository = UserRepository(self.db_path)
        self.conversation_state_repository = BotConversationStateRepository(self.db_path)
        self.known_users_repository = BotKnownUsersRepository(self.db_path)
        self.provider = _FakeProvider()
        self.check_service = FineCheckService(
            self.provider, self.task_repository, self.detected_fine_repository,
        )
        self.service = SubscriptionService(
            self.task_repository, self.subscription_repository,
            self.user_repository, self.check_service,
        )
        self.statistics_service = BotStatisticsService(
            self.known_users_repository, self.subscription_repository, self.detected_fine_repository,
        )
        self.controller = ConversationController(
            self.conversation_state_repository, self.service, self.statistics_service,
            self.known_users_repository, tz=_TBILISI,
            trusted_operator_user_ids=frozenset(trusted_operator_user_ids),
        )

    async def add_car(self, *, telegram_user_id: int, car_number: str, username: str | None = None) -> None:
        await self.service.add_car(
            telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id,
            username=username, first_name=None, last_name=None,
            car_number=car_number, period_days=30, today=_today(),
        )

    def record_known(self, *, telegram_user_id: int, username: str) -> None:
        self.known_users_repository.record_seen(
            telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, telegram_username=username,
        )

    def close(self):
        self.task_repository.close()
        self.detected_fine_repository.close()
        self.subscription_repository.close()
        self.user_repository.close()
        self.conversation_state_repository.close()
        self.known_users_repository.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


@pytest.fixture
def trusted_fx(tmp_path):
    fixture = _Fixture(tmp_path, trusted_operator_user_ids={_TRUSTED_ID})
    yield fixture
    fixture.close()


async def _open_search(fx) -> None:
    reply = await fx.controller.handle_text(
        texts.SEARCH_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )
    assert reply.search_prompt is True


async def _search(fx, query: str, *, telegram_user_id: int = _TRUSTED_ID):
    await _open_search(fx)
    return await fx.controller.handle_text(
        query, chat_id=telegram_user_id, telegram_user_id=telegram_user_id, username=None,
    )


# ---- 1/2. trusted menu содержит 🔎 Поиск / не содержит ⛔ Остановить мониторинг ----
# (см. tests/test_public_bot_keyboards.py::test_main_menu_keyboard_includes_trusted_buttons_for_trusted_operator/
# test_main_menu_keyboard_manager_puts_statistics_and_search_in_one_row — keyboard-level,
# не дублируем здесь).


# ---- 3. ordinary menu unchanged (см. test_public_bot_keyboards.py::
# test_main_menu_keyboard_does_not_change_ordinary_user_buttons) ----


# ---- 4. trusted opens Search ----


async def test_trusted_opens_search(trusted_fx):
    reply = await trusted_fx.controller.handle_text(
        texts.SEARCH_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert reply.search_prompt is True
    assert reply.text == texts.SEARCH_ENTRY_TEXT
    state = trusted_fx.conversation_state_repository.get(_TRUSTED_ID)
    assert state is not None


# ---- 5. ordinary cannot open Search ----


async def test_ordinary_cannot_open_search(fx):
    reply = await fx.controller.handle_text(
        texts.SEARCH_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )

    assert reply.search_prompt is False
    assert reply.text == texts.MAIN_MENU_TEXT
    assert fx.conversation_state_repository.get(_ORDINARY_ID) is None


# ---- 6/7/8. @username / username / case-insensitive ----


async def test_search_by_username_with_at(trusted_fx):
    await trusted_fx.add_car(telegram_user_id=777, car_number="M295YB196")
    trusted_fx.record_known(telegram_user_id=777, username="alenaogir")

    reply = await _search(trusted_fx, "@alenaogir")

    assert reply.search_result_shown is True
    assert "M295YB196" in reply.text
    assert "@alenaogir" in reply.text


async def test_search_by_username_without_at(trusted_fx):
    """"mihailov_vm" (с подчёркиванием, реальный пример из production) —
    НЕ проходит normalize_car_number (только [A-Z0-9]), поэтому
    однозначно резолвится как username, а не car (см. задачу п.3 и
    _parse_search_query докстрок про порядок "car-first")."""
    await trusted_fx.add_car(telegram_user_id=777, car_number="M295YB196")
    trusted_fx.record_known(telegram_user_id=777, username="mihailov_vm")

    reply = await _search(trusted_fx, "mihailov_vm")

    assert reply.search_result_shown is True
    assert "M295YB196" in reply.text


async def test_search_by_username_is_case_insensitive(trusted_fx):
    await trusted_fx.add_car(telegram_user_id=777, car_number="M295YB196")
    trusted_fx.record_known(telegram_user_id=777, username="AlenaOgir")

    reply = await _search(trusted_fx, "@ALENAOGIR")

    assert reply.search_result_shown is True
    assert "M295YB196" in reply.text
    # Отображается АКТУАЛЬНЫЙ known username (корректный регистр), не
    # введённый пользователем запрос as-is.
    assert "@AlenaOgir" in reply.text


# ---- 9. normalized car search ----


async def test_search_by_car_number_normalizes_input(trusted_fx):
    """Кириллица/пробелы/регистр — та же normalize_car_number, что и
    Add Car flow (см. задачу: "переиспользовать существующую
    normalization")."""
    await trusted_fx.add_car(telegram_user_id=777, car_number="M295YB196")

    reply = await _search(trusted_fx, "m295 yb196")

    assert reply.search_result_shown is True
    assert "M295YB196" in reply.text
    assert reply.text != texts.format_search_not_found("M295YB196")


# ---- 10. username -> multiple cars ----


async def test_search_by_username_shows_all_cars(trusted_fx):
    await trusted_fx.add_car(telegram_user_id=777, car_number="AA001AA")
    await trusted_fx.add_car(telegram_user_id=777, car_number="BB002BB")
    trusted_fx.record_known(telegram_user_id=777, username="alenaogir")

    reply = await _search(trusted_fx, "@alenaogir")

    assert "AA001AA" in reply.text
    assert "BB002BB" in reply.text
    assert "🔎 Результаты поиска" in reply.text


# ---- 11. car -> correct owner ----


async def test_search_by_car_number_shows_correct_owner(trusted_fx):
    await trusted_fx.add_car(telegram_user_id=777, car_number="AA001AA")
    await trusted_fx.add_car(telegram_user_id=888, car_number="BB002BB")
    trusted_fx.record_known(telegram_user_id=777, username="owner_one")
    trusted_fx.record_known(telegram_user_id=888, username="owner_two")

    reply = await _search(trusted_fx, "AA001AA")

    assert "@owner_one" in reply.text
    assert "@owner_two" not in reply.text


# ---- 12. duplicate car -> multiple owner records ----


async def test_search_by_duplicate_car_number_shows_every_owner_separately(trusted_fx):
    """Один и тот же car_number, добавленный ДВУМЯ разными пользователями
    (общая fine_monitoring_task, см. design: "несколько подписок могут
    указывать на одну и ту же monitoring_task_id") — Search показывает
    ОБЕИХ владельцев отдельно, без dedup (см. задачу п.6)."""
    await trusted_fx.add_car(telegram_user_id=111, car_number="A123AA123")
    await trusted_fx.add_car(telegram_user_id=222, car_number="A123AA123")
    trusted_fx.record_known(telegram_user_id=111, username="owner_one")
    trusted_fx.record_known(telegram_user_id=222, username="owner_two")

    reply = await _search(trusted_fx, "A123AA123")

    assert "@owner_one" in reply.text
    assert "@owner_two" in reply.text
    assert "🔎 Результаты поиска" in reply.text  # 2 записи -> множественное число


# ---- 13. missing username -> — ----


async def test_search_missing_username_shows_dash(trusted_fx):
    await trusted_fx.add_car(telegram_user_id=777, car_number="M295YB196")
    # record_known НЕ вызывается — владелец никогда не писал боту.

    reply = await _search(trusted_fx, "M295YB196")

    assert "👤 —" in reply.text
    assert "@None" not in reply.text
    assert "None" not in reply.text


# ---- 14. confirmed no fines -> 0 ₾ ----


async def test_search_confirmed_no_fines_shows_zero(trusted_fx):
    await trusted_fx.add_car(telegram_user_id=777, car_number="M295YB196")
    trusted_fx.provider.records_by_car["M295YB196"] = []  # live check: подтверждённо пусто

    reply = await _search(trusted_fx, "M295YB196")

    assert "💰 Штрафы: 0 ₾" in reply.text


# ---- 15. fines > 0 -> correct amount ----


async def test_search_shows_correct_fine_amount(trusted_fx):
    await trusted_fx.add_car(telegram_user_id=777, car_number="M295YB196")
    trusted_fx.provider.records_by_car["M295YB196"] = [
        _record("M295YB196", amount=25.0, fingerprint="fp-a"),
        _record("M295YB196", amount=15.0, fingerprint="fp-b"),
    ]

    reply = await _search(trusted_fx, "M295YB196")

    assert "💰 Штрафы: 40 ₾" in reply.text


# ---- 16. ERROR/UNKNOWN != 0 ----


async def test_search_provider_error_shows_unknown_not_zero(trusted_fx):
    await trusted_fx.add_car(telegram_user_id=777, car_number="M295YB196")
    trusted_fx.provider.error_cars.add("M295YB196")

    reply = await _search(trusted_fx, "M295YB196")

    assert "💰 Штрафы: неизвестно" in reply.text
    assert "💰 Штрафы: 0 ₾" not in reply.text


async def test_search_fine_with_unknown_amount_shows_unknown_not_zero(trusted_fx):
    """Штраф РЕАЛЬНО найден (police.ge вернул запись), но сумма не
    распознана (amount=None) — НИКОГДА не занижать до 0 ₾ молчаливым
    пропуском (см. задачу п.8: "КРИТИЧНО")."""
    await trusted_fx.add_car(telegram_user_id=777, car_number="M295YB196")
    trusted_fx.provider.records_by_car["M295YB196"] = [
        _record("M295YB196", amount=None, fingerprint="fp-c"),
    ]

    reply = await _search(trusted_fx, "M295YB196")

    assert "💰 Штрафы: неизвестно" in reply.text
    assert "💰 Штрафы: 0 ₾" not in reply.text


# ---- 17. not found ----


async def test_search_not_found(trusted_fx):
    reply = await _search(trusted_fx, "ZZZ999ZZ")

    assert reply.text == texts.format_search_not_found("ZZZ999ZZ")
    assert reply.search_result_shown is True


async def test_search_username_not_found(trusted_fx):
    reply = await _search(trusted_fx, "@nobody_at_all")

    assert reply.text == texts.format_search_not_found("@nobody_at_all")


# ---- 18. pagination ----


async def test_search_pagination_across_many_cars(trusted_fx):
    """list_my_cars()/list_by_user() сортирует created_at DESC, id DESC
    (та же, уже существующая конвенция self-service "Мои авто") — самая
    НЕДАВНО добавленная машина (P014) на первой странице, самая старая
    (P000) — на последней."""
    for i in range(15):
        await trusted_fx.add_car(telegram_user_id=777, car_number=f"P{i:03d}AA01")
    trusted_fx.record_known(telegram_user_id=777, username="alenaogir")

    await _open_search(trusted_fx)
    page0 = await trusted_fx.controller.handle_text(
        "@alenaogir", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )
    assert page0.search_total_pages == 2
    assert page0.search_page == 0
    assert "P014AA01" in page0.text
    assert "P004AA01" not in page0.text

    page1 = await trusted_fx.controller.handle_search_page(
        "username", "alenaogir", 1, telegram_user_id=_TRUSTED_ID,
    )
    assert page1.search_page == 1
    assert "P000AA01" in page1.text
    assert "P014AA01" not in page1.text

    clamped_high = await trusted_fx.controller.handle_search_page(
        "username", "alenaogir", 999, telegram_user_id=_TRUSTED_ID,
    )
    assert clamped_high.search_page == 1


# ---- 19. forged callbacks denied ----


async def test_search_callbacks_reject_ordinary_user(trusted_fx):
    await trusted_fx.add_car(telegram_user_id=777, car_number="M295YB196")

    assert await trusted_fx.controller.handle_search_page(
        "car", "M295YB196", 0, telegram_user_id=_ORDINARY_ID,
    ) is None
    assert trusted_fx.controller.handle_search_new(
        chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID,
    ) is None
    assert trusted_fx.controller.handle_search_back(
        chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID,
    ) is None


async def test_search_state_input_rejects_ordinary_user_with_hijacked_state(trusted_fx):
    """Forged/устаревшее conversation_state (STEP_AWAITING_SEARCH_QUERY)
    для НЕ-trusted telegram_user_id — defense-in-depth (см. задачу п.14:
    "проверка не только при входе через menu")."""
    from reader.public_bot.conversation import STEP_AWAITING_SEARCH_QUERY

    trusted_fx.conversation_state_repository.set(
        chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID, step=STEP_AWAITING_SEARCH_QUERY,
    )

    reply = await trusted_fx.controller.handle_text(
        "@anyone", chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )

    assert reply.search_result_shown is False
    assert reply.show_main_menu is True


# ---- 20. Back/New Search ----


async def test_search_back_returns_to_main_menu(trusted_fx):
    await _open_search(trusted_fx)

    reply = trusted_fx.controller.handle_search_back(chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    assert reply.show_main_menu is True
    assert trusted_fx.conversation_state_repository.get(_TRUSTED_ID) is None


async def test_search_new_returns_to_entry_prompt(trusted_fx):
    await trusted_fx.add_car(telegram_user_id=777, car_number="M295YB196")
    await _search(trusted_fx, "M295YB196")

    reply = trusted_fx.controller.handle_search_new(chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    assert reply.search_prompt is True
    assert reply.text == texts.SEARCH_ENTRY_TEXT


# ---- doubly-verified: STOP_LABEL underlying functionality NOT removed ----


async def test_stop_label_underlying_flow_still_works_for_trusted(trusted_fx):
    """Задача явно требует "underlying functionality не удалять" — только
    кнопка убрана из меню, сам текстовый flow остаётся рабочим, если
    отправлен вручную."""
    await trusted_fx.add_car(telegram_user_id=777, car_number="M295YB196")

    reply = await trusted_fx.controller.handle_text(
        texts.STOP_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert reply.trusted_stop_options is not None
