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

from telethon.errors import UsernameNotOccupiedError  # noqa: E402
from telethon.tl.types import User as TelethonUser  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import ParsedFineRecord  # noqa: E402
from reader.fines.provider import FineProvider  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot import texts  # noqa: E402
from reader.public_bot.conversation import (  # noqa: E402
    STEP_AWAITING_CAR_NUMBER,
    STEP_AWAITING_CLIENT_DECISION,
    STEP_AWAITING_OWNER_USERNAME,
    STEP_AWAITING_PERIOD,
    STEP_AWAITING_TRUSTED_CHECK_NOW_CAR_NUMBER,
    STEP_AWAITING_USERNAME,
    ConversationController,
)
from reader.public_bot.conversation_state_repository import BotConversationStateRepository  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import SubscriptionService  # noqa: E402
from reader.users.models import TelegramUserInfo  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_TBILISI = ZoneInfo("Asia/Tbilisi")
_TRUSTED_ID = 5712994689
_TRUSTED_CHAT_ID = 5712994689


class _FakeProvider(FineProvider):
    def __init__(self, records_by_car=None):
        self._records_by_car = records_by_car or {}

    async def search_by_plate(self, plate: str):
        return self._records_by_car.get(plate, [])


class _FakeTelegramClient:
    def __init__(self, *, entities=None):
        self._entities = {k.lower(): v for k, v in (entities or {}).items()}
        self.get_entity_calls: list[str] = []

    async def get_entity(self, entity):
        username = str(entity).lstrip("@").lower()
        self.get_entity_calls.append(username)
        if username in self._entities:
            return self._entities[username]
        raise UsernameNotOccupiedError(request=None)


def _telethon_user(user_id: int, username: str) -> TelethonUser:
    return TelethonUser(
        id=user_id, is_self=False, contact=False, mutual_contact=False, deleted=False,
        bot=False, bot_chat_history=False, bot_nochats=False, verified=False, restricted=False,
        min=False, bot_inline_geo=False, support=False, scam=False, apply_min_photo=False,
        fake=False, bot_attach_menu=False, premium=False, attach_menu_enabled=False,
        bot_can_edit=False, close_friend=False, stories_hidden=False, stories_unavailable=False,
        access_hash=999,
        first_name="Real", last_name="Owner", username=username, phone=None, photo=None,
        status=None, bot_info_version=None, restriction_reason=None, bot_inline_placeholder=None,
        lang_code=None,
    )


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
    def __init__(
        self, tmp_path, records_by_car=None,
        trusted_operator_user_ids=frozenset(), owner_resolver_client=None,
    ):
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
            owner_resolver_client=owner_resolver_client,
        )
        self.controller = ConversationController(
            self.conversation_state_repository, self.service, tz=_TBILISI,
            trusted_operator_user_ids=frozenset(trusted_operator_user_ids),
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


@pytest.fixture
def trusted_fx(tmp_path):
    fixture = _Fixture(tmp_path, trusted_operator_user_ids={_TRUSTED_ID})
    yield fixture
    fixture.close()


# ---- /start / главное меню ----


async def test_start_shows_main_menu_and_clears_state(fx):
    fx.conversation_state_repository.set(chat_id=1, telegram_user_id=1, step=STEP_AWAITING_CAR_NUMBER)

    reply = await fx.controller.handle_text("/start", chat_id=1, telegram_user_id=1, username=None)

    assert reply.text == texts.MAIN_MENU_TEXT
    assert reply.show_main_menu is True
    assert fx.conversation_state_repository.get(1) is None


async def test_my_cars_button_with_no_subscriptions(fx):
    reply = await fx.controller.handle_text(texts.MY_CARS_LABEL, chat_id=1, telegram_user_id=1, username=None)

    assert reply.text == texts.NO_CARS_TEXT


async def test_check_now_with_no_cars_shows_empty_message(fx):
    reply = await fx.controller.handle_text(texts.CHECK_NOW_LABEL, chat_id=1, telegram_user_id=1, username=None)
    assert reply.text == texts.NO_ACTIONABLE_CARS_TEXT


async def test_stop_with_no_cars_shows_empty_message(fx):
    reply = await fx.controller.handle_text(texts.STOP_LABEL, chat_id=1, telegram_user_id=1, username=None)
    assert reply.text == texts.NO_ACTIONABLE_CARS_TEXT


# ---- Add Car: username уже известен Telegram'у ----


async def test_add_car_with_known_username_skips_username_step(fx):
    reply = await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username="alice")
    assert reply.text == texts.CAR_NUMBER_PROMPT

    reply = await fx.controller.handle_text("M295YB196", chat_id=1, telegram_user_id=1, username="alice")

    assert reply.show_period_buttons is True
    assert reply.text == texts.PERIOD_PROMPT

    state = fx.conversation_state_repository.get(1)
    assert state.step == STEP_AWAITING_PERIOD
    assert state.payload == {"car_number": "M295YB196", "username": "alice"}


async def test_add_car_invalid_car_number_stays_on_same_step(fx):
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username="alice")

    reply = await fx.controller.handle_text("!!!", chat_id=1, telegram_user_id=1, username="alice")

    assert "❌" in reply.text
    state = fx.conversation_state_repository.get(1)
    assert state.step == STEP_AWAITING_CAR_NUMBER  # диалог не сброшен, можно ввести заново


# ---- Add Car: username отсутствует ----


async def test_add_car_without_username_asks_for_it(fx):
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=2, telegram_user_id=2, username=None)

    reply = await fx.controller.handle_text("M295YB196", chat_id=2, telegram_user_id=2, username=None)

    assert reply.text == texts.USERNAME_PROMPT
    state = fx.conversation_state_repository.get(2)
    assert state.step == STEP_AWAITING_USERNAME
    assert state.payload == {"car_number": "M295YB196"}


async def test_add_car_valid_username_after_missing_proceeds_to_period(fx):
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=2, telegram_user_id=2, username=None)
    await fx.controller.handle_text("M295YB196", chat_id=2, telegram_user_id=2, username=None)

    reply = await fx.controller.handle_text("@VeronaWarm", chat_id=2, telegram_user_id=2, username=None)

    assert reply.show_period_buttons is True
    state = fx.conversation_state_repository.get(2)
    assert state.step == STEP_AWAITING_PERIOD
    assert state.payload == {"car_number": "M295YB196", "username": "VeronaWarm"}


async def test_add_car_invalid_username_stays_on_same_step(fx):
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=2, telegram_user_id=2, username=None)
    await fx.controller.handle_text("M295YB196", chat_id=2, telegram_user_id=2, username=None)

    reply = await fx.controller.handle_text("!!", chat_id=2, telegram_user_id=2, username=None)

    assert "❌" in reply.text
    state = fx.conversation_state_repository.get(2)
    assert state.step == STEP_AWAITING_USERNAME


# ---- выбор периода: 30/90/180/365 ----


@pytest.mark.parametrize("days", [30, 90, 180, 365])
async def test_period_choice_creates_subscription_with_expected_dates(fx, days):
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=3, telegram_user_id=3, username="driver3")
    await fx.controller.handle_text("M295YB196", chat_id=3, telegram_user_id=3, username="driver3")

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
        await fixture.controller.handle_text(
            texts.ADD_CAR_LABEL, chat_id=4, telegram_user_id=4, username="driver4",
        )
        await fixture.controller.handle_text("M295YB196", chat_id=4, telegram_user_id=4, username="driver4")

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
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=5, telegram_user_id=5, username="owner5")
    await fx.controller.handle_text("M295YB196", chat_id=5, telegram_user_id=5, username="owner5")

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
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username="alice")
    await fx.controller.handle_text("M295YB196", chat_id=1, telegram_user_id=1, username="alice")

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

    reply = await fx.controller.handle_text(texts.MY_CARS_LABEL, chat_id=10, telegram_user_id=10, username=None)

    assert "AA001AA" in reply.text
    assert "BB002BB" not in reply.text
    assert "✅ Активен" in reply.text


async def test_my_cars_shows_expired_label_for_past_end_date(fx):
    task = fx.task_repository.create(
        car_number="AA001AA", label=None, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        telegram_chat_id=10, created_by_user_id=10, monitoring_scope="client_bot",
    )
    fx.subscription_repository.create(
        monitoring_task_id=task.id, car_number="AA001AA", telegram_user_id=10,
        telegram_chat_id=10, telegram_username="user10",
        start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
    )

    reply = await fx.controller.handle_text(texts.MY_CARS_LABEL, chat_id=10, telegram_user_id=10, username=None)

    assert "⏱ Истёк" in reply.text


# ---- переживание рестарта (переоткрытие БД) ----


async def test_conversation_state_survives_restart_simulated_reopen(tmp_path):
    fixture1 = _Fixture(tmp_path)
    try:
        await fixture1.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=7, telegram_user_id=7, username=None)
        await fixture1.controller.handle_text("M295YB196", chat_id=7, telegram_user_id=7, username=None)
        # состояние теперь STEP_AWAITING_USERNAME — "процесс" останавливается.
    finally:
        fixture1.close()

    fixture2 = _Fixture(tmp_path)
    try:
        state = fixture2.conversation_state_repository.get(7)
        assert state.step == STEP_AWAITING_USERNAME
        assert state.payload == {"car_number": "M295YB196"}

        reply = await fixture2.controller.handle_text("@VeronaWarm", chat_id=7, telegram_user_id=7, username=None)

        assert reply.show_period_buttons is True
        assert fixture2.conversation_state_repository.get(7).step == STEP_AWAITING_PERIOD
    finally:
        fixture2.close()


# ==== trusted-operator delegated flow (см. design report) ====


async def test_ordinary_user_never_sees_owner_username_prompt(fx):
    """Регресс: обычный пользователь (не в trusted_operator_user_ids) —
    поведение self-service flow не должно отличаться от Stage 2, даже
    если у него самого нет username (авто-детект/обычный USERNAME_PROMPT)."""
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username=None)

    reply = await fx.controller.handle_text("M295YB196", chat_id=1, telegram_user_id=1, username=None)

    assert reply.text == texts.USERNAME_PROMPT
    assert fx.conversation_state_repository.get(1).step == STEP_AWAITING_USERNAME


async def test_trusted_user_car_number_shows_add_client_decision_first(trusted_fx):
    """Trusted-оператор — ПЕРВЫМ делом видит "Добавить клиента?", а не
    сразу OWNER_USERNAME_PROMPT (см. design: username клиента больше не
    обязателен для постановки на мониторинг)."""
    await trusted_fx.controller.handle_text(
        texts.ADD_CAR_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username="trusted_own_username",
    )

    reply = await trusted_fx.controller.handle_text(
        "M295YB196", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username="trusted_own_username",
    )

    assert reply.text == texts.ADD_CLIENT_DECISION_PROMPT
    assert reply.show_add_client_decision_buttons is True
    state = trusted_fx.conversation_state_repository.get(_TRUSTED_ID)
    assert state.step == STEP_AWAITING_CLIENT_DECISION
    assert state.payload == {"car_number": "M295YB196"}


async def test_trusted_user_ok_on_client_decision_shows_owner_username_prompt(trusted_fx):
    """"OK" на "Добавить клиента?" — ВСЕГДА OWNER_USERNAME_PROMPT, даже
    если у самого trusted-пользователя есть свой Telegram username (авто-
    детект self-service здесь не применяется, см. design report об
    упрощении: нет отдельного экрана "Для себя/Для другого")."""
    await trusted_fx.controller.handle_text(
        texts.ADD_CAR_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username="trusted_own_username",
    )
    await trusted_fx.controller.handle_text(
        "M295YB196", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username="trusted_own_username",
    )

    reply = trusted_fx.controller.handle_add_client_decision(
        True, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID,
    )

    assert reply.text == texts.OWNER_USERNAME_PROMPT
    state = trusted_fx.conversation_state_repository.get(_TRUSTED_ID)
    assert state.step == STEP_AWAITING_OWNER_USERNAME
    assert state.payload == {"car_number": "M295YB196"}


async def test_trusted_user_cancel_on_client_decision_skips_straight_to_period(trusted_fx):
    """"Отмена" — НЕ запрашивает username вовсе, сразу период (см. design:
    "username НЕ должен быть обязательным условием постановки машины на
    мониторинг")."""
    await trusted_fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
    await trusted_fx.controller.handle_text("M295YB196", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)

    reply = trusted_fx.controller.handle_add_client_decision(
        False, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID,
    )

    assert reply.text == texts.PERIOD_PROMPT
    assert reply.show_period_buttons is True
    state = trusted_fx.conversation_state_repository.get(_TRUSTED_ID)
    assert state.step == STEP_AWAITING_PERIOD
    assert state.payload == {"car_number": "M295YB196", "no_client": True}


async def test_client_decision_rejects_stranger_in_this_chat(trusted_fx):
    """None — server-side проверка владения диалогом: тот же chat_id, но
    ДРУГОЙ telegram_user_id — ничего не должно произойти (тот же принцип,
    что и у handle_period_choice)."""
    await trusted_fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
    await trusted_fx.controller.handle_text("M295YB196", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)

    reply = trusted_fx.controller.handle_add_client_decision(
        True, chat_id=_TRUSTED_ID, telegram_user_id=999999,
    )

    assert reply is None


async def test_trusted_user_invalid_owner_username_stays_on_same_step(trusted_fx):
    await trusted_fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
    await trusted_fx.controller.handle_text("M295YB196", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
    trusted_fx.controller.handle_add_client_decision(True, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    reply = await trusted_fx.controller.handle_text("!!", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)

    assert "❌" in reply.text
    assert trusted_fx.conversation_state_repository.get(_TRUSTED_ID).step == STEP_AWAITING_OWNER_USERNAME


async def test_trusted_delegate_flow_resolves_immediately_via_local_db(trusted_fx):
    trusted_fx.user_repository.upsert(
        TelegramUserInfo(
            user_id=777, username="real_owner", first_name="Real", last_name="Owner",
        )
    )
    await trusted_fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
    await trusted_fx.controller.handle_text("M295YB196", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
    trusted_fx.controller.handle_add_client_decision(True, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)
    await trusted_fx.controller.handle_text("@real_owner", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)

    reply = await trusted_fx.controller.handle_period_choice(
        90, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, first_name=None, last_name=None,
    )

    assert reply is not None
    assert "✅ Автомобиль добавлен на мониторинг" in reply.text
    assert "👤 Владелец: @real_owner" in reply.text
    assert "https://t.me/" not in reply.text  # резолвлено сразу — ссылка не нужна

    [subscription] = trusted_fx.subscription_repository.list_by_user(777)
    assert subscription.status == "active"
    assert subscription.created_by_telegram_user_id == _TRUSTED_ID
    assert trusted_fx.conversation_state_repository.get(_TRUSTED_ID) is None


async def test_trusted_delegate_flow_pending_claim_when_owner_unresolved(trusted_fx):
    await trusted_fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
    await trusted_fx.controller.handle_text("M295YB196", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
    trusted_fx.controller.handle_add_client_decision(True, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)
    await trusted_fx.controller.handle_text("@unknown_person", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)

    reply = await trusted_fx.controller.handle_period_choice(
        30, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, first_name=None, last_name=None,
    )

    assert reply is not None
    assert "✅ Автомобиль добавлен на мониторинг" in reply.text
    assert "👤 Владелец: @unknown_person" in reply.text
    assert "https://t.me/ProtocolGEbot?start=claim_" in reply.text

    [subscription] = trusted_fx.subscription_repository.list_managed_by_creator(_TRUSTED_ID)
    assert subscription.status == "pending_claim"
    assert subscription.telegram_user_id is None
    # Мониторинг уже идёт, несмотря на pending_claim.
    [task] = trusted_fx.task_repository.list_active()
    assert task.car_number == "M295YB196"


async def test_claim_deep_link_start_binds_owner_and_confirms(trusted_fx):
    await trusted_fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
    await trusted_fx.controller.handle_text("M295YB196", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
    trusted_fx.controller.handle_add_client_decision(True, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)
    await trusted_fx.controller.handle_text("@unknown_person", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
    reply = await trusted_fx.controller.handle_period_choice(
        30, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, first_name=None, last_name=None,
    )
    link = [line for line in reply.text.splitlines() if line.startswith("https://t.me/")][0]
    token = link.rsplit("claim_", 1)[1]

    claim_reply = await trusted_fx.controller.handle_text(
        f"/start claim_{token}", chat_id=777, telegram_user_id=777,
        username="unknown_person", first_name="Real", last_name="Owner",
    )

    assert "✅" in claim_reply.text
    assert claim_reply.show_main_menu is True

    [subscription] = trusted_fx.subscription_repository.list_by_user(777)
    assert subscription.status == "active"
    assert subscription.telegram_user_id == 777


async def test_claim_deep_link_start_rejects_unknown_token(fx):
    reply = await fx.controller.handle_text(
        "/start claim_does-not-exist", chat_id=42, telegram_user_id=42, username="someone",
    )

    assert reply.text == texts.CLAIM_INVALID_TEXT


async def test_trusted_my_cars_shows_all_active_tasks_not_subscriptions(trusted_fx):
    """См. design report: пересмотр архитектуры — "Мои авто" для trusted
    оператора теперь task-based (ВСЕ активные fine_monitoring_tasks), а не
    subscription-based (own+managed) — старый MANAGED_CARS_HEADER-раздел
    здесь больше не показывается."""
    trusted_fx.user_repository.upsert(
        TelegramUserInfo(
            user_id=777, username="real_owner", first_name=None, last_name=None,
        )
    )
    await trusted_fx.service.add_delegated_car(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        owner_username="real_owner", car_number="M295YB196", period_days=30, today=_today(),
    )

    reply = await trusted_fx.controller.handle_text(
        texts.MY_CARS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert reply.text.startswith(texts.TRUSTED_TASKS_HEADER)
    assert "M295YB196" in reply.text
    assert texts.MANAGED_CARS_HEADER not in reply.text


async def test_ordinary_user_my_cars_has_no_managed_section(fx):
    reply = await fx.controller.handle_text(texts.MY_CARS_LABEL, chat_id=1, telegram_user_id=1, username=None)

    assert texts.MANAGED_CARS_HEADER not in reply.text


# ==== 🔎 Проверить сейчас / ⛔ Остановить мониторинг (см. design report Stage 4) ====


async def test_check_now_lists_own_car_and_returns_result(fx):
    await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
    )

    pick_reply = await fx.controller.handle_text(texts.CHECK_NOW_LABEL, chat_id=1, telegram_user_id=1, username="alice")
    assert pick_reply.text == texts.CHECK_NOW_PICK_PROMPT
    assert pick_reply.check_now_options is not None
    [(subscription_id, car_number)] = pick_reply.check_now_options
    assert car_number == "M295YB196"

    result_reply = await fx.controller.handle_check_now_choice(subscription_id, telegram_user_id=1)
    assert result_reply is not None
    assert "M295YB196" in result_reply.text
    assert "новых штрафов нет" in result_reply.text


async def test_check_now_rejects_subscription_belonging_to_another_user(fx):
    await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
    )
    [subscription] = fx.subscription_repository.list_by_user(1)

    # Пользователь 999 подделывает/подбирает чужой subscription_id.
    result_reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=999)

    assert result_reply is None


async def test_check_now_reports_new_fines(tmp_path):
    fixture = _Fixture(tmp_path, records_by_car={"M295YB196": [_record()]})
    try:
        await fixture.service.add_car(
            telegram_user_id=1, telegram_chat_id=1, username="alice",
            first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
        )
        [subscription] = fixture.subscription_repository.list_by_user(1)
        # Первая проверка (внутри add_car) уже нашла штраф — второй ручной
        # запуск не должен найти его СНОВА как новый (дедуп общий).
        reply = await fixture.controller.handle_check_now_choice(subscription.id, telegram_user_id=1)
        assert "новых штрафов нет" in reply.text
    finally:
        fixture.close()


async def test_stop_flow_pick_confirm_and_cancel(fx):
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username="alice")
    await fx.controller.handle_text("M295YB196", chat_id=1, telegram_user_id=1, username="alice")
    await fx.controller.handle_period_choice(
        30, chat_id=1, telegram_user_id=1, first_name=None, last_name=None,
    )

    pick_reply = await fx.controller.handle_text(texts.STOP_LABEL, chat_id=1, telegram_user_id=1, username="alice")
    assert pick_reply.stop_options is not None
    [(subscription_id, car_number)] = pick_reply.stop_options
    assert car_number == "M295YB196"

    confirm_reply = fx.controller.handle_stop_pick(subscription_id, telegram_user_id=1)
    assert confirm_reply is not None
    assert confirm_reply.stop_confirm_subscription_id == subscription_id
    assert "M295YB196" in confirm_reply.text

    # Подписка ещё активна — отмена ничего не останавливает.
    cancel_reply = fx.controller.handle_stop_cancel()
    assert cancel_reply.show_main_menu is True
    assert fx.subscription_repository.get(subscription_id).status == "active"

    final_reply = fx.controller.handle_stop_confirm(subscription_id, telegram_user_id=1)
    assert texts.format_stop_success("M295YB196") == final_reply.text
    assert fx.subscription_repository.get(subscription_id).status == "stopped"


async def test_stop_pick_rejects_subscription_belonging_to_another_user(fx):
    await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
    )
    [subscription] = fx.subscription_repository.list_by_user(1)

    reply = fx.controller.handle_stop_pick(subscription.id, telegram_user_id=999)

    assert reply is None
    assert fx.subscription_repository.get(subscription.id).status == "active"


async def test_stop_confirm_rechecks_ownership_even_if_pick_step_was_skipped(fx):
    """Финальный шаг ЗАНОВО проверяет владение, а не доверяет тому, что
    пользователь как-то дошёл до этого экрана (см. security-инвариант)."""
    await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
    )
    [subscription] = fx.subscription_repository.list_by_user(1)

    reply = fx.controller.handle_stop_confirm(subscription.id, telegram_user_id=999)

    assert reply.text == texts.STOP_FAILED_TEXT
    assert fx.subscription_repository.get(subscription.id).status == "active"


async def test_trusted_creator_can_check_and_stop_delegated_subscription(trusted_fx):
    trusted_fx.user_repository.upsert(
        TelegramUserInfo(user_id=777, username="real_owner", first_name=None, last_name=None)
    )
    await trusted_fx.service.add_delegated_car(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        owner_username="real_owner", car_number="M295YB196", period_days=30, today=_today(),
    )
    [subscription] = trusted_fx.subscription_repository.list_managed_by_creator(_TRUSTED_ID)

    check_reply = await trusted_fx.controller.handle_check_now_choice(
        subscription.id, telegram_user_id=_TRUSTED_ID,
    )
    assert check_reply is not None

    stop_reply = trusted_fx.controller.handle_stop_confirm(subscription.id, telegram_user_id=_TRUSTED_ID)
    assert "остановлен" in stop_reply.text
    assert trusted_fx.subscription_repository.get(subscription.id).status == "stopped"


async def test_unrelated_user_cannot_check_or_stop_delegated_subscription(trusted_fx):
    trusted_fx.user_repository.upsert(
        TelegramUserInfo(user_id=777, username="real_owner", first_name=None, last_name=None)
    )
    await trusted_fx.service.add_delegated_car(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        owner_username="real_owner", car_number="M295YB196", period_days=30, today=_today(),
    )
    [subscription] = trusted_fx.subscription_repository.list_managed_by_creator(_TRUSTED_ID)

    check_reply = await trusted_fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=999999)
    assert check_reply is None

    stop_reply = trusted_fx.controller.handle_stop_pick(subscription.id, telegram_user_id=999999)
    assert stop_reply is None


# ==== trusted-operator task-level admin (см. design report: пересмотр
# архитектуры — fine_monitoring_tasks = source of truth, subscription для
# этих трёх пунктов меню trusted-оператору НЕ требуется) ====


def _make_operator_task(fx, car_number="E911EE95", *, status="active") -> int:
    """Задача БЕЗ единой fine_monitoring_subscriptions строки — как
    исторические операторские автомобили на production (см. design
    report diagnosis: 1245 из 1248 задач без единой подписки)."""
    task = fx.task_repository.create(
        car_number=car_number, label=None,
        start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
        telegram_chat_id=-100999, created_by_user_id=111,
    )
    if status != "active":
        fx.task_repository.set_status(task.id, status)
    return task.id


async def test_trusted_my_cars_shows_task_without_any_subscription(trusted_fx):
    """Явное требование: trusted видит task без subscription — 📋 Мои
    авто показывает ВСЕ активные fine_monitoring_tasks, subscription не
    требуется вовсе."""
    _make_operator_task(trusted_fx, "E911EE95")

    reply = await trusted_fx.controller.handle_text(
        texts.MY_CARS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "E911EE95" in reply.text
    assert trusted_fx.subscription_repository.list_by_user(_TRUSTED_ID) == []


async def test_trusted_my_cars_shows_all_active_tasks_operator_and_client_bot(trusted_fx):
    """Явное требование: trusted видит ВСЕ active tasks — операторские И
    клиентские, независимо от scope."""
    _make_operator_task(trusted_fx, "E911EE95")
    client_task = trusted_fx.task_repository.create(
        car_number="M398YK763", label=None,
        start_date=date(2026, 9, 1), end_date=date(2027, 9, 1),
        telegram_chat_id=_TRUSTED_ID, created_by_user_id=_TRUSTED_ID,
        monitoring_scope="client_bot",
    )
    _make_operator_task(trusted_fx, "COMPLETED1", status="completed")

    reply = await trusted_fx.controller.handle_text(
        texts.MY_CARS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "E911EE95" in reply.text
    assert client_task.car_number in reply.text
    assert "COMPLETED1" not in reply.text  # completed — не активна, не должна попасть в список


async def test_ordinary_user_my_cars_never_shows_task_only_cars(fx):
    """Явное требование: ordinary user НЕ видит чужие/task-only cars —
    "Мои авто" для обычного пользователя остаётся строго
    subscription-based, задачи без подписки на него в принципе не влияют."""
    _make_operator_task(fx, "E911EE95")

    reply = await fx.controller.handle_text(
        texts.MY_CARS_LABEL, chat_id=1, telegram_user_id=1, username=None,
    )

    assert "E911EE95" not in reply.text
    assert reply.text == texts.NO_CARS_TEXT


# ==== 📋 Мои авто — trusted-operator pagination (см. design report:
# hard cap "первые 50 из N" убран, доступны ВСЕ active tasks, 10 на
# страницу) ====


def _make_many_tasks(fx, count: int) -> list[str]:
    """count последовательных активных задач с уникальными номерами —
    порядок car_number соответствует порядку создания (и id ASC, см.
    design report: "стабильная сортировка")."""
    car_numbers = [f"CAR{i:04d}" for i in range(count)]
    for car_number in car_numbers:
        _make_operator_task(fx, car_number)
    return car_numbers


async def test_trusted_my_cars_paginates_250_active_tasks_into_25_pages(trusted_fx):
    """Явное требование: 250 active tasks → 25 страниц; hard cap "первые
    50" убран — доступны ВСЕ 250."""
    _make_many_tasks(trusted_fx, 250)

    reply = await trusted_fx.controller.handle_text(
        texts.MY_CARS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert reply.trusted_tasks_page == 0
    assert reply.trusted_tasks_total_pages == 25
    assert "Страница 1 из 25" in reply.text


def test_trusted_my_cars_shows_10_tasks_per_page(trusted_fx):
    car_numbers = _make_many_tasks(trusted_fx, 250)

    reply = trusted_fx.controller.handle_trusted_tasks_page(0, telegram_user_id=_TRUSTED_ID)

    shown = [c for c in car_numbers if c in reply.text]
    assert len(shown) == 10
    assert shown == car_numbers[:10]


def test_trusted_my_cars_first_page_content(trusted_fx):
    car_numbers = _make_many_tasks(trusted_fx, 250)

    reply = trusted_fx.controller.handle_trusted_tasks_page(0, telegram_user_id=_TRUSTED_ID)

    assert "Страница 1 из 25" in reply.text
    for c in car_numbers[:10]:
        assert c in reply.text
    assert car_numbers[10] not in reply.text


def test_trusted_my_cars_middle_page_content(trusted_fx):
    car_numbers = _make_many_tasks(trusted_fx, 250)

    reply = trusted_fx.controller.handle_trusted_tasks_page(12, telegram_user_id=_TRUSTED_ID)

    assert "Страница 13 из 25" in reply.text
    for c in car_numbers[120:130]:
        assert c in reply.text
    assert car_numbers[119] not in reply.text
    assert car_numbers[130] not in reply.text


def test_trusted_my_cars_last_page_content_exact_multiple(trusted_fx):
    """250 = 25 * 10 — последняя страница ровно полная (проверяется
    отдельно от неполной последней страницы ниже)."""
    car_numbers = _make_many_tasks(trusted_fx, 250)

    reply = trusted_fx.controller.handle_trusted_tasks_page(24, telegram_user_id=_TRUSTED_ID)

    assert "Страница 25 из 25" in reply.text
    for c in car_numbers[240:250]:
        assert c in reply.text


def test_trusted_my_cars_last_incomplete_page_content(trusted_fx):
    """205 задач, 10 на страницу — 21 страница, последняя неполная (5)."""
    car_numbers = _make_many_tasks(trusted_fx, 205)

    reply = trusted_fx.controller.handle_trusted_tasks_page(20, telegram_user_id=_TRUSTED_ID)

    assert reply.trusted_tasks_total_pages == 21
    assert "Страница 21 из 21" in reply.text
    for c in car_numbers[200:205]:
        assert c in reply.text


def test_trusted_my_cars_next_moves_forward_one_page(trusted_fx):
    _make_many_tasks(trusted_fx, 250)

    reply = trusted_fx.controller.handle_trusted_tasks_page(3, telegram_user_id=_TRUSTED_ID)

    assert reply.trusted_tasks_page == 3
    # Кнопка "Вперёд" в keyboards.trusted_tasks_page_keyboard кодирует page+1 —
    # здесь проверяем через сам ConversationController, что page+1 валиден.
    next_reply = trusted_fx.controller.handle_trusted_tasks_page(4, telegram_user_id=_TRUSTED_ID)
    assert next_reply.trusted_tasks_page == 4


def test_trusted_my_cars_back_moves_to_previous_page(trusted_fx):
    _make_many_tasks(trusted_fx, 250)

    reply = trusted_fx.controller.handle_trusted_tasks_page(3, telegram_user_id=_TRUSTED_ID)
    assert reply.trusted_tasks_page == 3

    back_reply = trusted_fx.controller.handle_trusted_tasks_page(2, telegram_user_id=_TRUSTED_ID)
    assert back_reply.trusted_tasks_page == 2


def test_trusted_my_cars_back_on_first_page_is_noop(trusted_fx):
    """Явное требование: первая страница — Back disabled/no-op (кламп к
    той же странице, а не отрицательная страница/ошибка)."""
    _make_many_tasks(trusted_fx, 250)

    reply = trusted_fx.controller.handle_trusted_tasks_page(-1, telegram_user_id=_TRUSTED_ID)

    assert reply.trusted_tasks_page == 0
    assert "Страница 1 из 25" in reply.text


def test_trusted_my_cars_next_on_last_page_is_noop(trusted_fx):
    """Явное требование: последняя страница — Next disabled/no-op (кламп
    к той же последней странице, а не IndexError/пустой список)."""
    _make_many_tasks(trusted_fx, 250)

    reply = trusted_fx.controller.handle_trusted_tasks_page(24, telegram_user_id=_TRUSTED_ID)
    next_reply = trusted_fx.controller.handle_trusted_tasks_page(25, telegram_user_id=_TRUSTED_ID)

    assert reply.trusted_tasks_page == next_reply.trusted_tasks_page == 24
    assert next_reply.text == reply.text


def test_trusted_my_cars_forged_wildly_out_of_range_page_clamps_safely(trusted_fx):
    """forged/out-of-range page — не ошибка, не пустая страница, просто
    ближайшая валидная (см. design report: "page из callback нельзя
    считать authorization")."""
    _make_many_tasks(trusted_fx, 250)

    huge = trusted_fx.controller.handle_trusted_tasks_page(999999, telegram_user_id=_TRUSTED_ID)
    very_negative = trusted_fx.controller.handle_trusted_tasks_page(-999999, telegram_user_id=_TRUSTED_ID)

    assert huge.trusted_tasks_page == 24
    assert very_negative.trusted_tasks_page == 0


def test_trusted_my_cars_single_page_when_few_tasks(trusted_fx):
    """Явное требование: корректно работать при количестве страниц 1."""
    _make_many_tasks(trusted_fx, 3)

    reply = trusted_fx.controller.handle_trusted_tasks_page(0, telegram_user_id=_TRUSTED_ID)

    assert reply.trusted_tasks_total_pages == 1
    assert "Страница 1 из 1" in reply.text

    # Back и Next на единственной странице — оба no-op.
    back = trusted_fx.controller.handle_trusted_tasks_page(-1, telegram_user_id=_TRUSTED_ID)
    forward = trusted_fx.controller.handle_trusted_tasks_page(1, telegram_user_id=_TRUSTED_ID)
    assert back.trusted_tasks_page == forward.trusted_tasks_page == 0


def test_ordinary_user_does_not_get_trusted_tasks_pagination(fx):
    """Явное требование: ordinary users этот task-level список не
    получают — forged page callback с их telegram_user_id отклоняется."""
    _make_many_tasks(fx, 250)

    reply = fx.controller.handle_trusted_tasks_page(0, telegram_user_id=1)

    assert reply is None


async def test_ordinary_user_my_cars_menu_label_never_paginates(fx):
    """Регрессия: обычный пользователь при нажатии "Мои авто" не получает
    ни одного из trusted-полей ответа."""
    reply = await fx.controller.handle_text(
        texts.MY_CARS_LABEL, chat_id=1, telegram_user_id=1, username=None,
    )

    assert reply.trusted_tasks_page is None
    assert reply.trusted_tasks_total_pages is None


async def test_trusted_check_now_asks_for_car_number_not_a_list(trusted_fx):
    """Явное требование: trusted 🔎 Проверить сейчас сразу просит ввести
    номер — НИКАКОГО списка автомобилей вообще, даже если активные задачи
    существуют."""
    _make_operator_task(trusted_fx, "E911EE95")

    reply = await trusted_fx.controller.handle_text(
        texts.CHECK_NOW_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert reply.text == texts.CAR_NUMBER_PROMPT
    assert reply.trusted_stop_options is None
    assert reply.check_now_options is None
    state = trusted_fx.conversation_state_repository.get(_TRUSTED_ID)
    assert state.step == STEP_AWAITING_TRUSTED_CHECK_NOW_CAR_NUMBER


async def test_trusted_check_now_works_for_task_without_subscription(trusted_fx):
    """Явное требование: trusted Check Now по номеру работает для task
    без subscription — реальный FineCheckService.check_task()."""
    _make_operator_task(trusted_fx, "E911EE95")

    await trusted_fx.controller.handle_text(
        texts.CHECK_NOW_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )
    check_reply = await trusted_fx.controller.handle_text(
        "E911EE95", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "E911EE95" in check_reply.text
    assert "новых штрафов нет" in check_reply.text
    assert trusted_fx.subscription_repository.list_by_user(_TRUSTED_ID) == []
    # Диалог завершён — state очищен.
    assert trusted_fx.conversation_state_repository.get(_TRUSTED_ID) is None


async def test_trusted_check_now_normalizes_car_number_input(trusted_fx):
    """Явное требование: номер нормализуется существующим механизмом —
    разные написания одного и того же номера находят ту же задачу."""
    _make_operator_task(trusted_fx, "E911EE95")

    await trusted_fx.controller.handle_text(
        texts.CHECK_NOW_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )
    check_reply = await trusted_fx.controller.handle_text(
        "e911ee95", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "E911EE95" in check_reply.text


async def test_trusted_check_now_can_find_car_beyond_old_first_50_limit(trusted_fx):
    """Регрессия на прежний hard cap "первые 50" — Check Now по номеру
    находит машину независимо от её позиции/id среди активных задач
    (60-я созданная задача — заведомо за пределами прежнего лимита в 50)."""
    for i in range(59):
        _make_operator_task(trusted_fx, f"CAR{i:04d}")
    _make_operator_task(trusted_fx, "E911EE95")  # 60-я по счёту задача

    await trusted_fx.controller.handle_text(
        texts.CHECK_NOW_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )
    check_reply = await trusted_fx.controller.handle_text(
        "E911EE95", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "E911EE95" in check_reply.text
    assert "не найден" not in check_reply.text


async def test_trusted_check_now_rejects_unknown_plate_without_creating_anything(trusted_fx):
    """Явное требование: если машины нет среди active tasks — явная
    ошибка, ничего автоматически не добавляется."""
    await trusted_fx.controller.handle_text(
        texts.CHECK_NOW_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    reply = await trusted_fx.controller.handle_text(
        "E911EE95", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert reply.text == "❌ Автомобиль E911EE95 не найден в активном мониторинге."
    assert trusted_fx.task_repository.get_active_by_car_number("E911EE95") == []


async def test_trusted_check_now_rejects_inactive_task_plate(trusted_fx):
    """Машина существовала, но задача уже completed/stopped — та же
    ошибка "не найден", а не случайная проверка неактивной задачи."""
    _make_operator_task(trusted_fx, "E911EE95", status="completed")

    await trusted_fx.controller.handle_text(
        texts.CHECK_NOW_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )
    reply = await trusted_fx.controller.handle_text(
        "E911EE95", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert reply.text == "❌ Автомобиль E911EE95 не найден в активном мониторинге."


async def test_trusted_check_now_uses_existing_dedup(tmp_path):
    """Тот же дедуп/detected_fines, что и везде — второй "Проверить
    сейчас" по тому же номеру без новых штрафов от провайдера не находит
    уже виденный штраф повторно как новый."""
    fixture = _Fixture(
        tmp_path, records_by_car={"E911EE95": [_record(car_number="E911EE95")]},
        trusted_operator_user_ids={_TRUSTED_ID},
    )
    try:
        _make_operator_task(fixture, "E911EE95")

        await fixture.controller.handle_text(
            texts.CHECK_NOW_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        first = await fixture.controller.handle_text(
            "E911EE95", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        assert "найдено новых штрафов — 1" in first.text

        await fixture.controller.handle_text(
            texts.CHECK_NOW_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        second = await fixture.controller.handle_text(
            "E911EE95", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        assert "новых штрафов нет" in second.text
    finally:
        fixture.close()


async def test_trusted_check_now_invalid_format_stays_on_same_step(trusted_fx):
    """Невалидный формат номера — остаёмся на этом же шаге (тот же UX,
    что и у self-service Add Car), не показывается "не найден"."""
    await trusted_fx.controller.handle_text(
        texts.CHECK_NOW_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    reply = await trusted_fx.controller.handle_text(
        "!!", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "❌" in reply.text
    assert "не найден" not in reply.text
    state = trusted_fx.conversation_state_repository.get(_TRUSTED_ID)
    assert state.step == STEP_AWAITING_TRUSTED_CHECK_NOW_CAR_NUMBER


async def test_ordinary_user_check_now_still_subscription_scoped(fx):
    """Регрессия: обычный пользователь при 🔎 Проверить сейчас по-прежнему
    видит список СВОИХ подписок (а не просьбу ввести номер) — Check Now
    для ordinary user не менялся вовсе."""
    await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
    )

    reply = await fx.controller.handle_text(
        texts.CHECK_NOW_LABEL, chat_id=1, telegram_user_id=1, username="alice",
    )

    assert reply.text == texts.CHECK_NOW_PICK_PROMPT
    assert reply.check_now_options is not None
    [(subscription_id, car_number)] = reply.check_now_options
    assert car_number == "M295YB196"


async def test_ordinary_user_cannot_reach_trusted_check_now_step_by_forging_state(fx):
    """Явное требование: ordinary user не может получить task-level Check
    Now даже если бы каким-то образом оказался на этом шаге состояния —
    is_trusted() перепроверяется по РЕАЛЬНОМУ telegram_user_id независимо
    от того, что записано в conversation_state."""
    _make_operator_task(fx, "E911EE95")
    fx.conversation_state_repository.set(
        chat_id=1, telegram_user_id=1, step=STEP_AWAITING_TRUSTED_CHECK_NOW_CAR_NUMBER,
    )

    reply = await fx.controller.handle_text("E911EE95", chat_id=1, telegram_user_id=1, username=None)

    assert reply.text == texts.CALLBACK_NOT_AUTHORIZED_TEXT
    assert reply.show_main_menu is True


async def test_trusted_stop_task_without_subscribers_shows_plain_confirm(trusted_fx):
    """Явное требование: trusted Stop task без subscribers — обычное
    подтверждение, без предупреждения про клиентов."""
    task_id = _make_operator_task(trusted_fx, "E911EE95")

    pick_reply = trusted_fx.controller.handle_trusted_stop_pick(task_id, telegram_user_id=_TRUSTED_ID)

    assert pick_reply is not None
    assert pick_reply.text == "Остановить мониторинг для E911EE95?"
    assert pick_reply.trusted_stop_confirm_task_id == task_id
    assert pick_reply.trusted_stop_confirm_button_label == "⛔ Остановить"


async def test_trusted_stop_task_with_one_subscriber_shows_singular_warning(trusted_fx):
    """Явное требование: warning при active/pending subscribers —
    единственное число ("клиентом")."""
    task = trusted_fx.task_repository.create(
        car_number="M398YK763", label=None,
        start_date=date(2026, 9, 1), end_date=date(2027, 9, 1),
        telegram_chat_id=_TRUSTED_ID, created_by_user_id=_TRUSTED_ID,
        monitoring_scope="client_bot",
    )
    trusted_fx.subscription_repository.create(
        monitoring_task_id=task.id, car_number="M398YK763",
        telegram_user_id=777, telegram_chat_id=777, telegram_username="client_one",
        start_date=date(2026, 9, 1), end_date=date(2027, 9, 1),
    )

    pick_reply = trusted_fx.controller.handle_trusted_stop_pick(task.id, telegram_user_id=_TRUSTED_ID)

    assert pick_reply is not None
    assert "также отслеживается клиентом." in pick_reply.text
    assert "клиентами" not in pick_reply.text
    assert pick_reply.trusted_stop_confirm_button_label == "⛔ Остановить для всех"


async def test_trusted_stop_task_with_several_subscribers_shows_plural_warning(trusted_fx):
    """Явное требование: если клиентов несколько — текст должен корректно
    отражать это (множественное число)."""
    task = trusted_fx.task_repository.create(
        car_number="M398YK763", label=None,
        start_date=date(2026, 9, 1), end_date=date(2027, 9, 1),
        telegram_chat_id=_TRUSTED_ID, created_by_user_id=_TRUSTED_ID,
        monitoring_scope="client_bot",
    )
    trusted_fx.subscription_repository.create(
        monitoring_task_id=task.id, car_number="M398YK763",
        telegram_user_id=777, telegram_chat_id=777, telegram_username="client_one",
        start_date=date(2026, 9, 1), end_date=date(2027, 9, 1),
    )
    trusted_fx.subscription_repository.create_pending_claim(
        monitoring_task_id=task.id, car_number="M398YK763",
        owner_username_hint="client_two",
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        start_date=date(2026, 9, 1), end_date=date(2027, 9, 1),
        claim_token="tok-1", claim_token_expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )

    pick_reply = trusted_fx.controller.handle_trusted_stop_pick(task.id, telegram_user_id=_TRUSTED_ID)

    assert pick_reply is not None
    assert "также отслеживается клиентами." in pick_reply.text
    assert pick_reply.trusted_stop_confirm_button_label == "⛔ Остановить для всех"


def test_trusted_stop_confirm_rechecks_authorization_and_task_state(trusted_fx):
    """Явное требование: final Stop повторно проверяет authorization — не
    только на pick-шаге, но и на самом confirm-callback (тот же принцип,
    что и у обычного stop_confirm)."""
    task_id = _make_operator_task(trusted_fx, "E911EE95")

    # Не-trusted (даже если он как-то узнал task_id) — отклонён.
    forged = trusted_fx.controller.handle_trusted_stop_confirm(task_id, telegram_user_id=999999)
    assert forged.text == texts.CALLBACK_NOT_AUTHORIZED_TEXT
    assert trusted_fx.task_repository.get(task_id).status == "active"  # ничего не изменилось

    stop_reply = trusted_fx.controller.handle_trusted_stop_confirm(task_id, telegram_user_id=_TRUSTED_ID)

    assert "остановлен" in stop_reply.text
    assert trusted_fx.task_repository.get(task_id).status == "stopped"


def test_trusted_stop_confirm_rejects_already_inactive_task(trusted_fx):
    """Задача уже не active (кто-то другой остановил её между pick и
    confirm, либо истёк период) — final Stop отклоняет, а не пытается
    остановить повторно."""
    task_id = _make_operator_task(trusted_fx, "E911EE95", status="completed")

    reply = trusted_fx.controller.handle_trusted_stop_confirm(task_id, telegram_user_id=_TRUSTED_ID)

    assert reply.text == texts.TRUSTED_STOP_FAILED_TEXT


async def test_forced_stop_does_not_leave_client_with_misleading_active_state(trusted_fx):
    """Явное требование: forced Stop не оставляет клиенту ложное состояние
    "мониторинг активен" — client-подписка, привязанная к принудительно
    остановленной задаче, должна перестать показываться клиенту как
    активная в его собственном "Мои авто"."""
    task = trusted_fx.task_repository.create(
        car_number="M398YK763", label=None,
        start_date=date(2026, 9, 1), end_date=date(2027, 9, 1),
        telegram_chat_id=_TRUSTED_ID, created_by_user_id=_TRUSTED_ID,
        monitoring_scope="client_bot",
    )
    trusted_fx.subscription_repository.create(
        monitoring_task_id=task.id, car_number="M398YK763",
        telegram_user_id=777, telegram_chat_id=777, telegram_username="client_one",
        start_date=date(2026, 9, 1), end_date=date(2027, 9, 1),
    )

    trusted_fx.controller.handle_trusted_stop_confirm(task.id, telegram_user_id=_TRUSTED_ID)

    client_reply = await trusted_fx.controller.handle_text(
        texts.MY_CARS_LABEL, chat_id=777, telegram_user_id=777, username="client_one",
    )
    assert "✅ Активен" not in client_reply.text
    assert "⛔ Остановлен" in client_reply.text
    # Клиент также больше не может действовать через 🔎/⛔ этой подпиской.
    [subscription] = trusted_fx.subscription_repository.list_by_user(777)
    assert (
        trusted_fx.service.get_actionable_subscription(subscription.id, telegram_user_id=777) is not None
    )  # get_actionable_subscription не фильтрует по статусу — это ожидаемо
    actionable = trusted_fx.service.list_actionable_subscriptions(777, today=_today())
    assert actionable == []  # но список для действий её больше не покажет


def test_unrelated_user_forged_task_id_rejected_for_stop_pick(trusted_fx):
    """Явное требование: unrelated user forged task_id rejected — не-
    trusted telegram_user_id, даже зная реальный task_id, получает None
    на stop-pick."""
    task_id = _make_operator_task(trusted_fx, "E911EE95")

    reply = trusted_fx.controller.handle_trusted_stop_pick(task_id, telegram_user_id=1)

    assert reply is None
    assert trusted_fx.task_repository.get(task_id).status == "active"
