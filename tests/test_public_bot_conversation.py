"""
Тесты ConversationController — /start, главное меню, полный Add Car flow и
"Мои авто" @GEShtrafbot (reader/public_bot/conversation.py). Repository —
настоящие (SQLite/tmp_path), FineProvider — фейковый. Ничего не знает про
реальный Telethon — вызывает controller напрямую с уже "извлечёнными"
значениями (тот же приём, что и CommandContext в tests/test_fine_command.py).
"""

import sys
from datetime import date, datetime, timedelta, timezone
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
from reader.public_bot import texts  # noqa: E402
from reader.public_bot.conversation import (  # noqa: E402
    STEP_AWAITING_CAR_NUMBER,
    STEP_AWAITING_PERIOD,
    STEP_AWAITING_USERNAME,
    ConversationController,
)
from reader.public_bot.conversation_state_repository import BotConversationStateRepository  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import SubscriptionService  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_TBILISI = ZoneInfo("Asia/Tbilisi")


class _FakeProvider(FineProvider):
    def __init__(self, records_by_car=None):
        self._records_by_car = records_by_car or {}

    async def search_by_plate(self, plate: str):
        return self._records_by_car.get(plate, [])


def _record(car_number="M295YB196", fingerprint="fp-1") -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number, external_fine_id="AB123456",
        penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
        delivered_status="Не вручено", fingerprint=fingerprint,
        raw_data={"protocolNo": "AB123456"},
    )


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(_TBILISI).date()


class _Fixture:
    def __init__(self, tmp_path, records_by_car=None):
        self.db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(self.db_path)
        self.detected_fine_repository = DetectedFineRepository(self.db_path)
        self.subscription_repository = FineSubscriptionRepository(self.db_path)
        self.user_repository = UserRepository(self.db_path)
        self.conversation_state_repository = BotConversationStateRepository(self.db_path)
        self.check_service = FineCheckService(
            _FakeProvider(records_by_car), self.task_repository, self.detected_fine_repository,
        )
        self.service = SubscriptionService(
            self.task_repository, self.subscription_repository,
            self.user_repository, self.check_service,
        )
        self.controller = ConversationController(
            self.conversation_state_repository, self.service, tz=_TBILISI,
        )

    def close(self):
        self.task_repository.close()
        self.detected_fine_repository.close()
        self.subscription_repository.close()
        self.user_repository.close()
        self.conversation_state_repository.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


# ---- /start / главное меню ----


def test_start_shows_main_menu_and_clears_state(fx):
    fx.conversation_state_repository.set(chat_id=1, telegram_user_id=1, step=STEP_AWAITING_CAR_NUMBER)

    reply = fx.controller.handle_text("/start", chat_id=1, telegram_user_id=1, username=None)

    assert reply.text == texts.MAIN_MENU_TEXT
    assert reply.show_main_menu is True
    assert fx.conversation_state_repository.get(1) is None


def test_my_cars_button_with_no_subscriptions(fx):
    reply = fx.controller.handle_text(texts.MY_CARS_LABEL, chat_id=1, telegram_user_id=1, username=None)

    assert reply.text == texts.NO_CARS_TEXT


def test_check_now_and_stop_return_coming_soon_placeholder(fx):
    check_reply = fx.controller.handle_text(
        texts.CHECK_NOW_LABEL, chat_id=1, telegram_user_id=1, username=None,
    )
    stop_reply = fx.controller.handle_text(
        texts.STOP_LABEL, chat_id=1, telegram_user_id=1, username=None,
    )

    assert check_reply.text == texts.COMING_SOON_TEXT
    assert stop_reply.text == texts.COMING_SOON_TEXT


# ---- Add Car: username уже известен Telegram'у ----


def test_add_car_with_known_username_skips_username_step(fx):
    reply = fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username="alice")
    assert reply.text == texts.CAR_NUMBER_PROMPT

    reply = fx.controller.handle_text("M295YB196", chat_id=1, telegram_user_id=1, username="alice")

    assert reply.show_period_buttons is True
    assert reply.text == texts.PERIOD_PROMPT

    state = fx.conversation_state_repository.get(1)
    assert state.step == STEP_AWAITING_PERIOD
    assert state.payload == {"car_number": "M295YB196", "username": "alice"}


def test_add_car_invalid_car_number_stays_on_same_step(fx):
    fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username="alice")

    reply = fx.controller.handle_text("!!!", chat_id=1, telegram_user_id=1, username="alice")

    assert "❌" in reply.text
    state = fx.conversation_state_repository.get(1)
    assert state.step == STEP_AWAITING_CAR_NUMBER  # диалог не сброшен, можно ввести заново


# ---- Add Car: username отсутствует ----


def test_add_car_without_username_asks_for_it(fx):
    fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=2, telegram_user_id=2, username=None)

    reply = fx.controller.handle_text("M295YB196", chat_id=2, telegram_user_id=2, username=None)

    assert reply.text == texts.USERNAME_PROMPT
    state = fx.conversation_state_repository.get(2)
    assert state.step == STEP_AWAITING_USERNAME
    assert state.payload == {"car_number": "M295YB196"}


def test_add_car_valid_username_after_missing_proceeds_to_period(fx):
    fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=2, telegram_user_id=2, username=None)
    fx.controller.handle_text("M295YB196", chat_id=2, telegram_user_id=2, username=None)

    reply = fx.controller.handle_text("@VeronaWarm", chat_id=2, telegram_user_id=2, username=None)

    assert reply.show_period_buttons is True
    state = fx.conversation_state_repository.get(2)
    assert state.step == STEP_AWAITING_PERIOD
    assert state.payload == {"car_number": "M295YB196", "username": "VeronaWarm"}


def test_add_car_invalid_username_stays_on_same_step(fx):
    fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=2, telegram_user_id=2, username=None)
    fx.controller.handle_text("M295YB196", chat_id=2, telegram_user_id=2, username=None)

    reply = fx.controller.handle_text("!!", chat_id=2, telegram_user_id=2, username=None)

    assert "❌" in reply.text
    state = fx.conversation_state_repository.get(2)
    assert state.step == STEP_AWAITING_USERNAME


# ---- выбор периода: 30/90/180/365 ----


@pytest.mark.parametrize("days", [30, 90, 180, 365])
async def test_period_choice_creates_subscription_with_expected_dates(fx, days):
    fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=3, telegram_user_id=3, username="driver3")
    fx.controller.handle_text("M295YB196", chat_id=3, telegram_user_id=3, username="driver3")

    reply = await fx.controller.handle_period_choice(
        days, chat_id=3, telegram_user_id=3, first_name="Driver", last_name=None,
    )

    today = _today()
    expected_end = today + timedelta(days=days)
    assert reply is not None
    assert "✅ Автомобиль добавлен на мониторинг" in reply.text
    assert "🚗 M295YB196" in reply.text
    assert "👤 @driver3" in reply.text
    assert f"{today.strftime('%d.%m.%Y')} — {expected_end.strftime('%d.%m.%Y')}" in reply.text
    assert "новых штрафов нет" in reply.text

    [subscription] = fx.subscription_repository.list_by_user(3)
    assert subscription.start_date == today
    assert subscription.end_date == expected_end

    assert fx.conversation_state_repository.get(3) is None  # диалог завершён


async def test_period_choice_reports_new_fines_count(tmp_path):
    fixture = _Fixture(tmp_path, records_by_car={"M295YB196": [_record()]})
    try:
        fixture.controller.handle_text(
            texts.ADD_CAR_LABEL, chat_id=4, telegram_user_id=4, username="driver4",
        )
        fixture.controller.handle_text("M295YB196", chat_id=4, telegram_user_id=4, username="driver4")

        reply = await fixture.controller.handle_period_choice(
            30, chat_id=4, telegram_user_id=4, first_name=None, last_name=None,
        )

        assert "найдено новых — 1" in reply.text
    finally:
        fixture.close()


async def test_period_choice_rejects_wrong_sender(fx):
    """Security-инвариант: callback периода несёт только days, владение
    проверяется по conversation_state (chat_id -> telegram_user_id) — чужой
    sender_id (например, из пересланного сообщения с кнопкой) не должен
    иметь никакого эффекта."""
    fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=5, telegram_user_id=5, username="owner5")
    fx.controller.handle_text("M295YB196", chat_id=5, telegram_user_id=5, username="owner5")

    reply = await fx.controller.handle_period_choice(
        30, chat_id=5, telegram_user_id=999, first_name=None, last_name=None,
    )

    assert reply is None
    # Диалог владельца НЕ тронут — он всё ещё может завершить свой flow.
    state = fx.conversation_state_repository.get(5)
    assert state is not None
    assert state.step == STEP_AWAITING_PERIOD
    assert fx.subscription_repository.list_by_user(999) == []


async def test_period_choice_without_active_dialog_returns_none(fx):
    reply = await fx.controller.handle_period_choice(
        30, chat_id=6, telegram_user_id=6, first_name=None, last_name=None,
    )
    assert reply is None


async def test_period_choice_with_unknown_value_returns_none(fx):
    fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username="alice")
    fx.controller.handle_text("M295YB196", chat_id=1, telegram_user_id=1, username="alice")

    reply = await fx.controller.handle_period_choice(
        999, chat_id=1, telegram_user_id=1, first_name=None, last_name=None,
    )
    assert reply is None


# ---- "Мои авто" — только свои подписки ----


async def test_my_cars_shows_only_own_subscriptions(fx):
    await fx.service.add_car(
        telegram_user_id=10, telegram_chat_id=10, username="user10",
        first_name=None, last_name=None, car_number="AA001AA",
        period_days=30, today=_today(),
    )
    await fx.service.add_car(
        telegram_user_id=20, telegram_chat_id=20, username="user20",
        first_name=None, last_name=None, car_number="BB002BB",
        period_days=30, today=_today(),
    )

    reply = fx.controller.handle_text(texts.MY_CARS_LABEL, chat_id=10, telegram_user_id=10, username=None)

    assert "AA001AA" in reply.text
    assert "BB002BB" not in reply.text
    assert "✅ Активен" in reply.text


def test_my_cars_shows_expired_label_for_past_end_date(fx):
    task = fx.task_repository.create(
        car_number="AA001AA", label=None, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        telegram_chat_id=10, created_by_user_id=10, monitoring_scope="client_bot",
    )
    fx.subscription_repository.create(
        monitoring_task_id=task.id, car_number="AA001AA", telegram_user_id=10,
        telegram_chat_id=10, telegram_username="user10",
        start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
    )

    reply = fx.controller.handle_text(texts.MY_CARS_LABEL, chat_id=10, telegram_user_id=10, username=None)

    assert "⏱ Истёк" in reply.text


# ---- переживание рестарта (переоткрытие БД) ----


def test_conversation_state_survives_restart_simulated_reopen(tmp_path):
    fixture1 = _Fixture(tmp_path)
    try:
        fixture1.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=7, telegram_user_id=7, username=None)
        fixture1.controller.handle_text("M295YB196", chat_id=7, telegram_user_id=7, username=None)
        # состояние теперь STEP_AWAITING_USERNAME — "процесс" останавливается.
    finally:
        fixture1.close()

    fixture2 = _Fixture(tmp_path)
    try:
        state = fixture2.conversation_state_repository.get(7)
        assert state.step == STEP_AWAITING_USERNAME
        assert state.payload == {"car_number": "M295YB196"}

        reply = fixture2.controller.handle_text("@VeronaWarm", chat_id=7, telegram_user_id=7, username=None)

        assert reply.show_period_buttons is True
        assert fixture2.conversation_state_repository.get(7).step == STEP_AWAITING_PERIOD
    finally:
        fixture2.close()
