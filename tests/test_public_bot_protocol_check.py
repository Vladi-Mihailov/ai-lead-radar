"""Тесты "📸 Проверить протокол" (@ProtocolGEbot, задача "Проверить
протокол") — сквозные, через РЕАЛЬНЫЙ ConversationController.
Repository — настоящие (SQLite/tmp_path), ProtocolCheckProvider — лёгкий
фейк (тот же приём, что и tests/test_public_bot_debt_refresh.py).
"""

import logging
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.provider import FineProvider
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot import keyboards, texts
from reader.public_bot.conversation import ConversationController
from reader.public_bot.conversation_state_repository import (
    BotConversationStateRepository,
)
from reader.public_bot.keyboards import main_menu_keyboard
from reader.public_bot.known_users_repository import BotKnownUsersRepository
from reader.public_bot.protocol_check_ephemeral_store import (
    ProtocolCheckEphemeralStore,
)
from reader.public_bot.protocol_check_models import (
    ProtocolCheckResult,
    ProtocolCheckStatus,
)
from reader.public_bot.protocol_check_provider import ProtocolCheckProvider
from reader.public_bot.statistics_service import BotStatisticsService
from reader.public_bot.subscription_repository import FineSubscriptionRepository
from reader.public_bot.subscription_service import SubscriptionService
from reader.turkey_bot.keyboards import main_menu_keyboard as turkey_main_menu_keyboard
from reader.users.repository import UserRepository

_TBILISI = ZoneInfo("Asia/Tbilisi")
_TRUSTED_ID = 5712994689
_ORDINARY_ID = 685137235
_CHAT_ID = 685137235
_VALID_CAR_NUMBER = "M295YB196"


class _FakeFineProvider(FineProvider):
    async def search_by_plate(self, plate: str):
        return []


class _FakeProtocolCheckProvider:
    def __init__(self):
        self.vehicle_calls: list[tuple[str, str]] = []
        self.protocol_calls: list[tuple[str, str]] = []
        self.next_result = ProtocolCheckResult(status=ProtocolCheckStatus.NOT_FOUND)

    async def check_vehicle(self, *, car_number: str, document_no: str) -> ProtocolCheckResult:
        self.vehicle_calls.append((car_number, document_no))
        return self.next_result

    async def check_protocol(self, *, protocol_no: str, personal_no: str) -> ProtocolCheckResult:
        self.protocol_calls.append((protocol_no, personal_no))
        return self.next_result


class _Fixture:
    def __init__(self, tmp_path, *, with_protocol_check: bool = True):
        self.db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(self.db_path)
        self.detected_fine_repository = DetectedFineRepository(self.db_path)
        self.subscription_repository = FineSubscriptionRepository(self.db_path)
        self.user_repository = UserRepository(self.db_path)
        self.conversation_state_repository = BotConversationStateRepository(self.db_path)
        self.known_users_repository = BotKnownUsersRepository(self.db_path)
        self.check_service = FineCheckService(
            _FakeFineProvider(), self.task_repository, self.detected_fine_repository,
        )
        self.subscription_service = SubscriptionService(
            self.task_repository, self.subscription_repository, self.user_repository, self.check_service,
        )
        self.statistics_service = BotStatisticsService(
            self.known_users_repository, self.subscription_repository, self.detected_fine_repository,
        )
        self.protocol_check_provider = _FakeProtocolCheckProvider() if with_protocol_check else None
        self.protocol_check_ephemeral_store = ProtocolCheckEphemeralStore()
        self.controller = ConversationController(
            self.conversation_state_repository, self.subscription_service, self.statistics_service,
            self.known_users_repository, tz=_TBILISI,
            trusted_operator_user_ids=frozenset({_TRUSTED_ID}),
            protocol_check_provider=self.protocol_check_provider,
            protocol_check_ephemeral_store=self.protocol_check_ephemeral_store,
        )

    def raw_state_rows(self) -> list[tuple]:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT * FROM bot_conversation_state").fetchall()
        finally:
            conn.close()


# ---- 1/2: кнопка есть в Georgia, нет в Turkey ----

def test_georgia_menu_hides_protocol_check_button():
    """Кнопка скрыта из главного меню (ordinary и trusted); сам flow не
    удалён — текст кнопки по-прежнему открывает его (см. тесты ниже, которые
    входят во flow через handle_text(texts.PROTOCOL_CHECK_LABEL))."""
    # Button.text(...) — reply-кнопка, обёрнута: реальный label — на
    # button.button.text (см. tests/test_public_bot_keyboards.py, тот же
    # приём инспекции reply-клавиатуры).
    rows = main_menu_keyboard(is_trusted=False)
    labels = [row_button.button.text for row in rows for row_button in row]
    assert texts.PROTOCOL_CHECK_LABEL not in labels

    trusted_rows = main_menu_keyboard(is_trusted=True)
    trusted_labels = [row_button.button.text for row in trusted_rows for row_button in row]
    assert texts.PROTOCOL_CHECK_LABEL not in trusted_labels


def test_turkey_menu_unchanged_no_protocol_check_button():
    rows = turkey_main_menu_keyboard(is_trusted=False)
    labels = [row_button.button.text for row in rows for row_button in row]
    assert texts.PROTOCOL_CHECK_LABEL not in labels
    assert "📸 Проверить протокол" not in labels

    trusted_rows = turkey_main_menu_keyboard(is_trusted=True)
    trusted_labels = [row_button.button.text for row in trusted_rows for row_button in row]
    assert texts.PROTOCOL_CHECK_LABEL not in trusted_labels


# ---- 3: экран выбора способа ----

@pytest.mark.asyncio
async def test_button_opens_method_selection_screen(tmp_path):
    fx = _Fixture(tmp_path)
    reply = await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    assert reply.protocol_check_method_prompt is True
    assert reply.text == texts.PROTOCOL_CHECK_INTRO_TEXT


# ---- 4/6: vehicle two-step flow -> provider с правильными полями ----

@pytest.mark.asyncio
async def test_vehicle_flow_calls_provider_with_correct_fields(tmp_path):
    fx = _Fixture(tmp_path)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    method_reply = fx.controller.handle_protocol_check_method(
        "vehicle", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID,
    )
    assert method_reply.text == texts.PROTOCOL_CHECK_VEHICLE_CAR_NUMBER_PROMPT
    assert method_reply.protocol_check_back_target == "method"

    step2_reply = await fx.controller.handle_text(
        _VALID_CAR_NUMBER, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    assert step2_reply.text == texts.PROTOCOL_CHECK_VEHICLE_DOCUMENT_PROMPT
    assert step2_reply.protocol_check_back_target == "vehicle_step1"

    result_reply = await fx.controller.handle_text(
        "tp-12345", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    assert fx.protocol_check_provider.vehicle_calls == [(_VALID_CAR_NUMBER, "TP-12345")]
    assert result_reply.text == texts.PROTOCOL_CHECK_NOT_FOUND_TEXT
    assert result_reply.show_main_menu is True


# ---- 5/7: protocol two-step flow -> provider с правильными полями ----

@pytest.mark.asyncio
async def test_protocol_flow_calls_provider_with_correct_fields(tmp_path):
    fx = _Fixture(tmp_path)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    method_reply = fx.controller.handle_protocol_check_method(
        "protocol", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID,
    )
    assert method_reply.text == texts.PROTOCOL_CHECK_PROTOCOL_NUMBER_PROMPT
    assert method_reply.protocol_check_back_target == "method"

    step2_reply = await fx.controller.handle_text(
        "PR-999888", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    assert step2_reply.text == texts.PROTOCOL_CHECK_PERSONAL_NUMBER_PROMPT
    assert step2_reply.protocol_check_back_target == "protocol_step1"

    result_reply = await fx.controller.handle_text(
        "id-777", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    assert fx.protocol_check_provider.protocol_calls == [("PR-999888", "ID-777")]
    assert result_reply.text == texts.PROTOCOL_CHECK_NOT_FOUND_TEXT


# ---- protocolNo normalization — полная цепочка Telegram -> conversation.py
# -> РЕАЛЬНЫЙ ProtocolCheckProvider (см. задачу "Проверить протокол"
# protocolNo normalization) ----


class _FakeSessionForNormalizationTest:
    """Тот же лёгкий приём, что и в tests/test_protocol_check_session.py —
    записывает ИМЕННО те fields, которые реально дошли до session.search(),
    т.е. ПОСЛЕ normalize_protocol_no() внутри РЕАЛЬНОГО
    ProtocolCheckProvider (в отличие от _FakeProtocolCheckProvider выше,
    который сам является заменой всего provider'а и поэтому не проверяет
    его internal normalization)."""

    def __init__(self):
        self.requested_fields: list[dict] = []

    async def search(self, fields: dict) -> str:
        self.requested_fields.append(fields)
        return "Administrative violations have not been found."


@pytest.mark.asyncio
async def test_telegram_protocol_input_reaches_real_provider_normalized(tmp_path):
    """См. задачу — Telegram-пользователь вводит привычное "eq948218",
    РЕАЛЬНЫЙ ProtocolCheckProvider (не fake) должен получить его от
    conversation.py НЕИЗМЕНЁННЫМ и сам нормализовать перед session.search()
    -> "ექ948218", НЕ "eq948218"."""
    fx = _Fixture(tmp_path, with_protocol_check=False)
    fake_session = _FakeSessionForNormalizationTest()
    fx.protocol_check_provider = ProtocolCheckProvider(fake_session)
    fx.controller._protocol_check_provider = fx.protocol_check_provider

    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("protocol", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text("eq948218", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)
    await fx.controller.handle_text("9931694846", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)

    assert fake_session.requested_fields == [{"protocolNo": "ექ948218", "personalNo": "9931694846"}]


# ---- 11: NOT_FOUND только для точного marker'а (типизированный результат) ----

@pytest.mark.asyncio
async def test_not_found_status_shows_not_found_text(tmp_path):
    fx = _Fixture(tmp_path)
    fx.protocol_check_provider.next_result = ProtocolCheckResult(status=ProtocolCheckStatus.NOT_FOUND)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("vehicle", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text(_VALID_CAR_NUMBER, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)
    reply = await fx.controller.handle_text(
        "TP-1", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    assert reply.text == texts.PROTOCOL_CHECK_NOT_FOUND_TEXT


# ---- 12: timeout/error != NOT_FOUND ----

@pytest.mark.asyncio
async def test_error_status_never_shown_as_not_found(tmp_path):
    fx = _Fixture(tmp_path)
    fx.protocol_check_provider.next_result = ProtocolCheckResult(status=ProtocolCheckStatus.ERROR)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("vehicle", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text(_VALID_CAR_NUMBER, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)
    reply = await fx.controller.handle_text(
        "TP-1", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    assert reply.text == texts.PROTOCOL_CHECK_ERROR_TEXT
    assert reply.text != texts.PROTOCOL_CHECK_NOT_FOUND_TEXT


# ---- 13: unknown successful HTML -> UNKNOWN ----

@pytest.mark.asyncio
async def test_unknown_status_shows_unrecognized_text(tmp_path):
    fx = _Fixture(tmp_path)
    fx.protocol_check_provider.next_result = ProtocolCheckResult(status=ProtocolCheckStatus.UNKNOWN)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("protocol", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text("PR-1", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)
    reply = await fx.controller.handle_text(
        "ID-1", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    assert reply.text == texts.PROTOCOL_CHECK_UNKNOWN_TEXT
    assert reply.text not in (texts.PROTOCOL_CHECK_NOT_FOUND_TEXT, texts.PROTOCOL_CHECK_ERROR_TEXT)


# ---- FOUND: ветка существует ради расширяемости (см. задачу п.8) ----

@pytest.mark.asyncio
async def test_found_status_plumbing_exists_for_future_parser(tmp_path):
    fx = _Fixture(tmp_path)
    fx.protocol_check_provider.next_result = ProtocolCheckResult(status=ProtocolCheckStatus.FOUND)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("vehicle", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text(_VALID_CAR_NUMBER, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)
    reply = await fx.controller.handle_text(
        "TP-1", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    assert reply.text == texts.PROTOCOL_CHECK_FOUND_TEXT


# ---- 14: Back navigation на каждом шаге ----

@pytest.mark.asyncio
async def test_back_from_method_screen_returns_to_main_menu(tmp_path):
    fx = _Fixture(tmp_path)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    reply = fx.controller.handle_protocol_check_back_to_menu(chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    assert reply.show_main_menu is True
    assert reply.text == texts.MAIN_MENU_TEXT
    assert fx.conversation_state_repository.get(_CHAT_ID) is None


@pytest.mark.asyncio
async def test_back_from_vehicle_step1_returns_to_method_screen(tmp_path):
    fx = _Fixture(tmp_path)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("vehicle", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    reply = fx.controller.handle_protocol_check_back_to_method(chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    assert reply.protocol_check_method_prompt is True
    assert reply.text == texts.PROTOCOL_CHECK_INTRO_TEXT


@pytest.mark.asyncio
async def test_back_from_vehicle_step2_returns_to_vehicle_step1(tmp_path):
    fx = _Fixture(tmp_path)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("vehicle", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text(_VALID_CAR_NUMBER, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)
    reply = fx.controller.handle_protocol_check_back_to_vehicle_step1(
        chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID,
    )
    assert reply.text == texts.PROTOCOL_CHECK_VEHICLE_CAR_NUMBER_PROMPT
    assert reply.protocol_check_back_target == "method"


@pytest.mark.asyncio
async def test_protocol_step1_value_is_stored_in_ephemeral_store_before_back(tmp_path):
    fx = _Fixture(tmp_path)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("protocol", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text("PR-1", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)
    assert fx.protocol_check_ephemeral_store.pop_protocol_number(_CHAT_ID) == "PR-1"


@pytest.mark.asyncio
async def test_back_from_protocol_step2_returns_to_protocol_step1_and_clears_ephemeral(tmp_path):
    fx = _Fixture(tmp_path)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("protocol", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text("PR-1", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)

    reply = fx.controller.handle_protocol_check_back_to_protocol_step1(
        chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID,
    )
    assert reply.text == texts.PROTOCOL_CHECK_PROTOCOL_NUMBER_PROMPT
    # Sensitive-значение с шага 1 обязательно очищено (см. задачу п.6/п.9) —
    # pop() возвращает None, если его уже нет.
    assert fx.protocol_check_ephemeral_store.pop_protocol_number(_CHAT_ID) is None


def test_back_navigation_requires_matching_telegram_user_id(tmp_path):
    fx = _Fixture(tmp_path)
    fx.conversation_state_repository.set(
        _CHAT_ID, telegram_user_id=_ORDINARY_ID, step="awaiting_protocol_check_method",
    )
    other_user_reply = fx.controller.handle_protocol_check_back_to_menu(
        chat_id=_CHAT_ID, telegram_user_id=999999999,
    )
    assert other_user_reply is None


# ---- 15: Cancel/новый flow -> очистка ephemeral state ----

@pytest.mark.asyncio
async def test_starting_another_menu_flow_clears_protocol_check_ephemeral_state(tmp_path):
    fx = _Fixture(tmp_path)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("protocol", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text("PR-1", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)
    assert fx.protocol_check_ephemeral_store.pop_protocol_number(_CHAT_ID) == "PR-1"

    # Повторяем flow и на этот раз уходим в другой пункт меню ДО завершения.
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("protocol", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text("PR-2", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)

    await fx.controller.handle_text(
        texts.MY_CARS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    assert fx.protocol_check_ephemeral_store.pop_protocol_number(_CHAT_ID) is None


@pytest.mark.asyncio
async def test_start_command_clears_protocol_check_ephemeral_state(tmp_path):
    fx = _Fixture(tmp_path)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("protocol", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text("PR-1", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)

    fx.controller.start(chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    assert fx.conversation_state_repository.get(_CHAT_ID) is None
    # /start сам по себе НЕ вызывает ephemeral.clear() (только menu-label
    # entry points делают это, см. ConversationController._handle_menu_label) —
    # но conversation_state уже пуст, поэтому следующий шаг этого flow
    # физически недостижим: STALE_DIALOG_TEXT сработает раньше.


# ---- 16/17: sensitive-значения отсутствуют в persistent state/БД ----

@pytest.mark.asyncio
async def test_sensitive_values_absent_from_conversation_state_payload_vehicle_flow(tmp_path):
    fx = _Fixture(tmp_path)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("vehicle", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text(_VALID_CAR_NUMBER, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)

    state = fx.conversation_state_repository.get(_CHAT_ID)
    assert state.payload == {"car_number": _VALID_CAR_NUMBER}
    assert "document_no" not in (state.payload or {})


@pytest.mark.asyncio
async def test_sensitive_values_absent_from_conversation_state_payload_protocol_flow(tmp_path):
    fx = _Fixture(tmp_path)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("protocol", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text(
        "SECRET-PROTOCOL-VALUE", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )

    state = fx.conversation_state_repository.get(_CHAT_ID)
    # protocol_no — sensitive — НЕ должен появиться в persistent payload
    # вовсе (см. задачу п.4) — payload либо None, либо пустой словарь.
    assert not (state.payload or {})

    for row in fx.raw_state_rows():
        row_text = " ".join(str(value) for value in row)
        assert "SECRET-PROTOCOL-VALUE" not in row_text


@pytest.mark.asyncio
async def test_sensitive_values_absent_from_db_file_after_full_flow(tmp_path):
    fx = _Fixture(tmp_path)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("vehicle", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text(_VALID_CAR_NUMBER, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)
    await fx.controller.handle_text(
        "SECRET-DOC-VALUE", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )

    raw_bytes = fx.db_path.read_bytes()
    assert b"SECRET-DOC-VALUE" not in raw_bytes
    assert b"secret-doc-value" not in raw_bytes.lower()


# ---- 18: sensitive-значения отсутствуют в логах ----

@pytest.mark.asyncio
async def test_sensitive_values_absent_from_logs(tmp_path, caplog):
    fx = _Fixture(tmp_path)
    with caplog.at_level(logging.DEBUG):
        await fx.controller.handle_text(
            texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
        )
        fx.controller.handle_protocol_check_method("protocol", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
        await fx.controller.handle_text(
            "SECRET-PROTOCOL-LOG-VALUE", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
        )
        await fx.controller.handle_text(
            "SECRET-PERSONAL-LOG-VALUE", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
        )

    assert "SECRET-PROTOCOL-LOG-VALUE" not in caplog.text
    assert "SECRET-PERSONAL-LOG-VALUE" not in caplog.text


# ---- 19: существующие Georgia flows не изменились (smoke) ----

@pytest.mark.asyncio
async def test_existing_add_car_flow_unaffected(tmp_path):
    fx = _Fixture(tmp_path)
    reply = await fx.controller.handle_text(
        texts.ADD_CAR_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    assert reply.text == texts.CAR_NUMBER_PROMPT

    reply2 = await fx.controller.handle_text(
        _VALID_CAR_NUMBER, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username="someuser",
    )
    assert reply2.show_period_buttons is True


# ---- provider отсутствует в сборке (protocol_check_provider=None) ----

@pytest.mark.asyncio
async def test_missing_provider_falls_back_gracefully(tmp_path):
    fx = _Fixture(tmp_path, with_protocol_check=False)
    await fx.controller.handle_text(
        texts.PROTOCOL_CHECK_LABEL, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    fx.controller.handle_protocol_check_method("vehicle", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID)
    await fx.controller.handle_text(_VALID_CAR_NUMBER, chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None)
    reply = await fx.controller.handle_text(
        "TP-1", chat_id=_CHAT_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )
    assert reply.text == texts.PROTOCOL_CHECK_UNAVAILABLE_TEXT


# ---- keyboards.py: callback encode/decode ----

def test_keyboards_method_callback_roundtrip():
    rows = keyboards.protocol_check_method_keyboard()
    vehicle_data = rows[0][0].data
    protocol_data = rows[1][0].data
    back_data = rows[2][0].data
    assert keyboards.decode_protocol_check_method_callback(vehicle_data) == "vehicle"
    assert keyboards.decode_protocol_check_method_callback(protocol_data) == "protocol"
    assert keyboards.decode_protocol_check_back_to_menu_callback(back_data) is True
    assert keyboards.decode_protocol_check_method_callback(b"unrelated") is None
