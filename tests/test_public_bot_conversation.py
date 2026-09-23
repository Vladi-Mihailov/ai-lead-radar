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
    ConversationController,
)
from reader.public_bot.conversation_state_repository import BotConversationStateRepository  # noqa: E402
from reader.public_bot.keyboards import (  # noqa: E402
    encode_trusted_task_open_callback,
    trusted_tasks_page_keyboard,
)
from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402
from reader.public_bot.statistics_service import BotStatisticsService  # noqa: E402
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
        self.known_users_repository = BotKnownUsersRepository(self.db_path)
        self.check_service = FineCheckService(
            _FakeProvider(records_by_car), self.task_repository, self.detected_fine_repository,
        )
        self.service = SubscriptionService(
            self.task_repository, self.subscription_repository,
            self.user_repository, self.check_service,
            owner_resolver_client=owner_resolver_client,
        )
        self.statistics_service = BotStatisticsService(
            self.known_users_repository, self.subscription_repository, self.detected_fine_repository,
        )
        self.controller = ConversationController(
            self.conversation_state_repository, self.service, self.statistics_service, tz=_TBILISI,
            trusted_operator_user_ids=frozenset(trusted_operator_user_ids),
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


async def test_stop_label_for_ordinary_user_is_safe_fallback(fx):
    """Старая кнопка "⛔ Остановить мониторинг" убрана из главного меню
    ОБЫЧНОГО пользователя (см. design report п.8, car-centric ON/OFF её
    заменяет) — текст, отправленный вручную (например, из старого чата),
    получает безопасный отказ (главное меню), а не picker/ошибку."""
    reply = await fx.controller.handle_text(texts.STOP_LABEL, chat_id=1, telegram_user_id=1, username=None)
    assert reply.text == texts.MAIN_MENU_TEXT
    assert reply.show_main_menu is True


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


async def test_add_car_accepts_cyrillic_plate_and_normalizes_to_latin(fx):
    """См. design report "исправить Georgian bot: госномер кириллицей не
    проходит validation" — О687КЕ761 (реальный пример из задачи)
    нормализуется в O687KE761 ДО валидации формата, тем же способом, что и
    в reader/turkey_bot/validation.py::normalize_plate."""
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username="alice")

    reply = await fx.controller.handle_text("О687КЕ761", chat_id=1, telegram_user_id=1, username="alice")

    assert reply.show_period_buttons is True
    state = fx.conversation_state_repository.get(1)
    assert state.payload == {"car_number": "O687KE761", "username": "alice"}


async def test_add_car_invalid_car_number_stays_on_same_step(fx):
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username="alice")

    reply = await fx.controller.handle_text("!!!", chat_id=1, telegram_user_id=1, username="alice")

    assert "❌" in reply.text
    state = fx.conversation_state_repository.get(1)
    assert state.step == STEP_AWAITING_CAR_NUMBER  # диалог не сброшен, можно ввести заново


# ---- Add Car: self-service — username больше НИКОГДА не запрашивается
# вручную (см. задачу "Georgia должен работать с Telegram identity так же,
# как Turkey") ----


async def test_add_car_without_username_skips_prompt_straight_to_period(fx):
    """2. self-service user БЕЗ username: номер авто → сразу period,
    никакого username prompt — username=None штатно сохраняется в payload,
    а не считается ошибкой/поводом что-то запросить."""
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=2, telegram_user_id=2, username=None)

    reply = await fx.controller.handle_text("M295YB196", chat_id=2, telegram_user_id=2, username=None)

    assert reply.text == texts.PERIOD_PROMPT
    assert reply.show_period_buttons is True
    state = fx.conversation_state_repository.get(2)
    assert state.step == STEP_AWAITING_PERIOD
    assert state.payload == {"car_number": "M295YB196", "username": None}


async def test_add_car_without_username_completes_subscription_successfully(fx):
    """3. Отсутствие username не мешает subscription creation/monitoring/
    telegram_user_id binding — период выбирается, подписка создаётся,
    привязка идёт по numeric telegram_user_id как обычно."""
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=2, telegram_user_id=2, username=None)
    await fx.controller.handle_text("M295YB196", chat_id=2, telegram_user_id=2, username=None)

    reply = await fx.controller.handle_period_choice(
        30, chat_id=2, telegram_user_id=2, first_name=None, last_name=None,
    )

    assert reply is not None
    assert "✅ Автомобиль добавлен на мониторинг" in reply.text
    [subscription] = fx.subscription_repository.list_by_user(2)
    assert subscription.telegram_user_id == 2
    assert subscription.telegram_username is None
    assert fx.conversation_state_repository.get(2) is None  # диалог завершён


async def test_add_car_summary_without_username_has_no_none_or_prompt_text(fx):
    """4. self-service summary НЕ содержит "@None"/"None"/запрос придумать
    username — строка "👤 @username" убрана из summary целиком (см.
    texts.format_add_car_summary)."""
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=2, telegram_user_id=2, username=None)
    await fx.controller.handle_text("M295YB196", chat_id=2, telegram_user_id=2, username=None)

    reply = await fx.controller.handle_period_choice(
        30, chat_id=2, telegram_user_id=2, first_name=None, last_name=None,
    )

    assert "@None" not in reply.text
    assert "None" not in reply.text
    assert "Telegram-логин" not in reply.text
    assert "👤" not in reply.text
    assert "🚗 M295YB196" in reply.text
    assert "📅 Мониторинг:" in reply.text


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
    # Строка "👤 @username" убрана из self-service summary целиком (см.
    # задачу "Georgia должен работать с Telegram identity так же, как
    # Turkey") — даже когда username реально есть, он не показывается.
    assert "👤" not in reply.text
    assert "@driver3" not in reply.text
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

    assert reply.text == texts.MY_CARS_HEADER
    [(_subscription_id, label)] = reply.my_cars_page_options
    assert "AA001AA" in label
    assert "BB002BB" not in label
    assert "🟢" in label
    assert "ON" in label


async def test_my_cars_shows_expired_state_for_past_end_date(fx):
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

    [(_subscription_id, label)] = reply.my_cars_page_options
    assert "⏱" in label
    assert "ИСТЁК" in label


# ---- переживание рестарта (переоткрытие БД) ----


async def test_conversation_state_survives_restart_simulated_reopen(tmp_path):
    """Self-service БЕЗ username — состояние STEP_AWAITING_PERIOD (с
    payload["username"]=None) переживает "рестарт" (переоткрытие БД) точно
    так же, как и раньше промежуточный STEP_AWAITING_USERNAME — сам факт,
    что username больше не запрашивается, не должен ломать persistence."""
    fixture1 = _Fixture(tmp_path)
    try:
        await fixture1.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=7, telegram_user_id=7, username=None)
        await fixture1.controller.handle_text("M295YB196", chat_id=7, telegram_user_id=7, username=None)
        # состояние теперь STEP_AWAITING_PERIOD (username=None) — "процесс" останавливается.
    finally:
        fixture1.close()

    fixture2 = _Fixture(tmp_path)
    try:
        state = fixture2.conversation_state_repository.get(7)
        assert state.step == STEP_AWAITING_PERIOD
        assert state.payload == {"car_number": "M295YB196", "username": None}

        reply = await fixture2.controller.handle_period_choice(
            30, chat_id=7, telegram_user_id=7, first_name=None, last_name=None,
        )

        assert reply is not None
        assert "✅ Автомобиль добавлен на мониторинг" in reply.text
        assert fixture2.conversation_state_repository.get(7) is None
    finally:
        fixture2.close()


# ==== trusted-operator delegated flow (см. design report) ====


async def test_ordinary_user_without_username_never_sees_owner_username_prompt(fx):
    """6. Регресс: обычный пользователь (не в trusted_operator_user_ids)
    без username никогда не видит OWNER_USERNAME_PROMPT (тот относится
    ИСКЛЮЧИТЕЛЬНО к delegated flow) — и, после задачи "убрать username
    requirement", вообще никакого username prompt: сразу период."""
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username=None)

    reply = await fx.controller.handle_text("M295YB196", chat_id=1, telegram_user_id=1, username=None)

    assert reply.text != texts.OWNER_USERNAME_PROMPT
    assert reply.text == texts.PERIOD_PROMPT
    assert reply.show_period_buttons is True
    assert fx.conversation_state_repository.get(1).step == STEP_AWAITING_PERIOD


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
    assert any(car == "M295YB196" for _tid, car, _is_on, _end in reply.trusted_tasks_page_options)
    assert reply.my_cars_page_options is None  # task-level, не car-centric


async def test_ordinary_user_my_cars_uses_car_buttons_not_trusted_task_list(fx):
    """Обычный (не-trusted) пользователь получает car-centric список (см.
    design report про переработку UX), а не trusted task-level admin —
    старый MANAGED_CARS_HEADER-раздел упразднён вместе с текстовым
    "Мои авто" (см. п.8: он относился к УЖЕ неиспользуемой ветке)."""
    await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
    )

    reply = await fx.controller.handle_text(texts.MY_CARS_LABEL, chat_id=1, telegram_user_id=1, username=None)

    assert reply.my_cars_page_options is not None
    assert reply.trusted_tasks_page is None


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
    assert "штрафов не найдено" in result_reply.text
    assert "новых штрафов нет" not in result_reply.text
    assert result_reply.cta_buttons is None  # штрафов нет — CTA нечего показывать


async def test_check_now_rejects_subscription_belonging_to_another_user(fx):
    await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
    )
    [subscription] = fx.subscription_repository.list_by_user(1)

    # Пользователь 999 подделывает/подбирает чужой subscription_id.
    result_reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=999)

    assert result_reply is None


async def test_check_now_shows_existing_fine_found_by_earlier_check(tmp_path):
    """Явное требование задачи про UX manual check: 🔎 Проверить сейчас
    показывает ВСЕ штрафы текущего ответа police.ge, включая уже
    известные — штраф, найденный первой проверкой (внутри add_car),
    по-прежнему показывается ручным запуском, а не "новых штрафов нет"
    (дедуп/detection semantics при этом не меняются — см. отдельный тест
    на отсутствие повторного уведомления оператора)."""
    fixture = _Fixture(tmp_path, records_by_car={"M295YB196": [_record()]})
    try:
        await fixture.service.add_car(
            telegram_user_id=1, telegram_chat_id=1, username="alice",
            first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
        )
        [subscription] = fixture.subscription_repository.list_by_user(1)

        reply = await fixture.controller.handle_check_now_choice(subscription.id, telegram_user_id=1)

        assert "новых штрафов нет" not in reply.text
        assert "штрафов не найдено" not in reply.text
        assert "🚨 Обнаружен штраф" in reply.text
        assert "🚗 Автомобиль: M295YB196" in reply.text
        assert "📄 Штраф №: AB123456" in reply.text
        # Реальный владелец (не trusted-creator) — CTA-кнопки присутствуют.
        assert reply.cta_buttons is not None
        assert len(reply.cta_buttons) == 1
        assert len(reply.cta_buttons[0]) == 2
        labels = [label for label, _url in reply.cta_buttons[0]]
        assert labels == ["💳 Оплатить в рублях", "🚗 ОСАГО Грузии"]
        urls = {url for _label, url in reply.cta_buttons[0]}
        assert urls == {"https://t.me/tplgee"}
    finally:
        fixture.close()


async def test_my_car_turn_off_flow_open_toggle_and_back(fx):
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username="alice")
    await fx.controller.handle_text("M295YB196", chat_id=1, telegram_user_id=1, username="alice")
    await fx.controller.handle_period_choice(
        30, chat_id=1, telegram_user_id=1, first_name=None, last_name=None,
    )

    list_reply = await fx.controller.handle_text(texts.MY_CARS_LABEL, chat_id=1, telegram_user_id=1, username="alice")
    [(subscription_id, label)] = list_reply.my_cars_page_options
    assert "ON" in label

    detail_reply = fx.controller.handle_my_car_open(subscription_id, 0, telegram_user_id=1)
    assert detail_reply is not None
    assert detail_reply.car_detail_monitoring_state == "ON"
    assert "M295YB196" in detail_reply.text

    off_reply = fx.controller.handle_my_car_turn_off(subscription_id, 0, telegram_user_id=1)
    assert off_reply is not None
    assert "выключен" in off_reply.text
    assert off_reply.car_detail_monitoring_state == "OFF"
    assert fx.subscription_repository.get(subscription_id).status == "stopped"

    # "⬅️ Назад" — та же навигация, что и пагинация (см. handlers.py:
    # decode_my_cars_page_callback реиспользуется для Back).
    back_reply = fx.controller.handle_my_cars_page(0, telegram_user_id=1)
    assert back_reply.text == texts.MY_CARS_HEADER
    [(_id, back_label)] = back_reply.my_cars_page_options
    assert "OFF" in back_label


async def test_my_car_turn_on_reactivates_without_duplicate(fx):
    await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
    )
    [subscription] = fx.subscription_repository.list_by_user(1)
    fx.controller.handle_my_car_turn_off(subscription.id, 0, telegram_user_id=1)
    assert fx.subscription_repository.get(subscription.id).status == "stopped"

    on_reply = fx.controller.handle_my_car_turn_on(subscription.id, 0, telegram_user_id=1)

    assert on_reply is not None
    assert "включён" in on_reply.text
    assert on_reply.car_detail_monitoring_state == "ON"
    reactivated = fx.subscription_repository.get(subscription.id)
    assert reactivated.status == "active"
    assert reactivated.id == subscription.id  # та же строка, не дубликат
    assert len(fx.subscription_repository.list_by_user(1)) == 1


async def test_my_car_open_rejects_subscription_belonging_to_another_user(fx):
    await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
    )
    [subscription] = fx.subscription_repository.list_by_user(1)

    reply = fx.controller.handle_my_car_open(subscription.id, 0, telegram_user_id=999)

    assert reply is None
    assert fx.subscription_repository.get(subscription.id).status == "active"


async def test_my_car_turn_off_rechecks_ownership(fx):
    """ON/OFF ЗАНОВО проверяет владение, а не доверяет тому, что
    пользователь как-то дошёл до этого экрана (см. security-инвариант)."""
    await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
    )
    [subscription] = fx.subscription_repository.list_by_user(1)

    reply = fx.controller.handle_my_car_turn_off(subscription.id, 0, telegram_user_id=999)

    assert reply is None
    assert fx.subscription_repository.get(subscription.id).status == "active"


async def test_my_car_delete_requires_confirmation_and_cancel_preserves_car(fx):
    await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
    )
    [subscription] = fx.subscription_repository.list_by_user(1)

    prompt_reply = fx.controller.handle_my_car_delete_prompt(subscription.id, 0, telegram_user_id=1)
    assert prompt_reply is not None
    assert "M295YB196" in prompt_reply.text
    assert prompt_reply.car_delete_confirm_subscription_id == subscription.id
    # Ничего ещё не удалено (см. design report: "Сразу ничего не удалять").
    assert fx.subscription_repository.get(subscription.id).status == "active"

    cancel_reply = fx.controller.handle_my_car_delete_cancel(subscription.id, 0, telegram_user_id=1)
    assert cancel_reply is not None
    assert cancel_reply.car_detail_subscription_id == subscription.id
    assert fx.subscription_repository.get(subscription.id).status == "active"  # Cancel preserves car


async def test_my_car_delete_confirm_removes_car_from_my_cars(fx):
    await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
    )
    [subscription] = fx.subscription_repository.list_by_user(1)

    confirm_reply = fx.controller.handle_my_car_delete_confirm(subscription.id, 0, telegram_user_id=1)

    # Единственный автомобиль удалён — "Мои авто" теперь пуст.
    assert confirm_reply.text == texts.NO_CARS_TEXT
    assert fx.subscription_repository.get(subscription.id).status == "archived"


async def test_my_car_delete_confirm_rechecks_ownership(fx):
    await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
    )
    [subscription] = fx.subscription_repository.list_by_user(1)

    reply = fx.controller.handle_my_car_delete_confirm(subscription.id, 0, telegram_user_id=999)

    assert reply.text == texts.CAR_ACTION_FAILED_TEXT
    assert fx.subscription_repository.get(subscription.id).status == "active"


async def test_my_car_delete_keeps_historical_detected_fines(tmp_path):
    fixture = _Fixture(tmp_path, records_by_car={"M295YB196": [_record()]})
    try:
        await fixture.service.add_car(
            telegram_user_id=1, telegram_chat_id=1, username="alice",
            first_name=None, last_name=None, car_number="M295YB196", period_days=30, today=_today(),
        )
        [subscription] = fixture.subscription_repository.list_by_user(1)
        [fine] = fixture.detected_fine_repository.list_by_car_number("M295YB196")

        fixture.controller.handle_my_car_delete_confirm(subscription.id, 0, telegram_user_id=1)

        still_there = fixture.detected_fine_repository.get_by_fingerprint(
            subscription.monitoring_task_id, fine.fingerprint,
        )
        assert still_there is not None
        assert still_there.id == fine.id
    finally:
        fixture.close()


async def test_readd_car_after_delete_works_through_conversation(fx):
    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username="alice")
    await fx.controller.handle_text("M295YB196", chat_id=1, telegram_user_id=1, username="alice")
    await fx.controller.handle_period_choice(30, chat_id=1, telegram_user_id=1, first_name=None, last_name=None)
    [old_subscription] = fx.subscription_repository.list_by_user(1)
    fx.controller.handle_my_car_delete_confirm(old_subscription.id, 0, telegram_user_id=1)

    await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username="alice")
    await fx.controller.handle_text("M295YB196", chat_id=1, telegram_user_id=1, username="alice")
    await fx.controller.handle_period_choice(30, chat_id=1, telegram_user_id=1, first_name=None, last_name=None)

    list_reply = await fx.controller.handle_text(texts.MY_CARS_LABEL, chat_id=1, telegram_user_id=1, username="alice")
    [(new_subscription_id, label)] = list_reply.my_cars_page_options
    assert new_subscription_id != old_subscription.id
    assert "ON" in label


async def test_my_cars_pagination_mixes_on_and_off_cars(fx):
    """Явное требование: "pagination still works with ON + OFF" — 12
    автомобилей (>10, две страницы), часть ON, часть OFF после ручного
    выключения — все видны, ни один не выпадает из списка."""
    for i in range(12):
        await fx.service.add_car(
            telegram_user_id=1, telegram_chat_id=1, username="alice",
            first_name=None, last_name=None, car_number=f"CAR{i:04d}",
            period_days=30, today=_today(),
        )
    all_subs = fx.subscription_repository.list_by_user(1)
    # Выключаем половину.
    for sub in all_subs[:6]:
        fx.controller.handle_my_car_turn_off(sub.id, 0, telegram_user_id=1)

    page0 = await fx.controller.handle_text(texts.MY_CARS_LABEL, chat_id=1, telegram_user_id=1, username="alice")
    assert page0.my_cars_page == 0
    assert page0.my_cars_total_pages == 2
    assert len(page0.my_cars_page_options) == 10

    page1 = fx.controller.handle_my_cars_page(1, telegram_user_id=1)
    assert page1.my_cars_page == 1
    assert len(page1.my_cars_page_options) == 2

    all_ids = {sid for sid, _label in page0.my_cars_page_options} | {
        sid for sid, _label in page1.my_cars_page_options
    }
    assert all_ids == {s.id for s in all_subs}
    on_count = sum(
        1 for _sid, label in page0.my_cars_page_options + page1.my_cars_page_options if "ON" in label
    )
    off_count = sum(
        1 for _sid, label in page0.my_cars_page_options + page1.my_cars_page_options if "OFF" in label
    )
    assert on_count == 6
    assert off_count == 6


async def test_trusted_creator_can_check_and_toggle_delegated_subscription(trusted_fx):
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

    off_reply = trusted_fx.controller.handle_my_car_turn_off(subscription.id, 0, telegram_user_id=_TRUSTED_ID)
    assert "выключен" in off_reply.text
    assert trusted_fx.subscription_repository.get(subscription.id).status == "stopped"


async def test_unrelated_user_cannot_check_or_toggle_delegated_subscription(trusted_fx):
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

    open_reply = trusted_fx.controller.handle_my_car_open(subscription.id, 0, telegram_user_id=999999)
    assert open_reply is None


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

    # Список — чистая inline keyboard (см. design report про переработку
    # экрана), номер машины теперь в trusted_tasks_page_options, не в тексте.
    assert any(car == "E911EE95" for _tid, car, _is_on, _end in reply.trusted_tasks_page_options)
    assert trusted_fx.subscription_repository.list_by_user(_TRUSTED_ID) == []


async def test_trusted_my_cars_shows_all_tasks_operator_and_client_bot_and_off(trusted_fx):
    """Явное требование: trusted видит ВСЕ tasks — операторские И
    клиентские, независимо от scope, И независимо от статуса (см. design
    report про per-car ON/OFF toggle — completed/stopped теперь тоже
    попадают в список, просто как ⚪ OFF, а не скрываются вовсе, см.
    handle_trusted_task_toggle/trusted_tasks_page_options)."""
    _make_operator_task(trusted_fx, "E911EE95")
    client_task = trusted_fx.task_repository.create(
        car_number="M398YK763", label=None,
        start_date=date(2026, 9, 1), end_date=date(2027, 9, 1),
        telegram_chat_id=_TRUSTED_ID, created_by_user_id=_TRUSTED_ID,
        monitoring_scope="client_bot",
    )
    completed_id = _make_operator_task(trusted_fx, "COMPLETED1", status="completed")

    reply = await trusted_fx.controller.handle_text(
        texts.MY_CARS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    # Список — чистая inline keyboard (см. design report), номера машин —
    # ТОЛЬКО в trusted_tasks_page_options, не в тексте сообщения.
    cars_shown = {car for _tid, car, _is_on, _end in reply.trusted_tasks_page_options}
    assert "E911EE95" in cars_shown
    assert client_task.car_number in cars_shown
    assert "COMPLETED1" in cars_shown  # completed — теперь показывается как ⚪ OFF, не скрыт

    options_by_id = {
        task_id: (car_number, is_on)
        for task_id, car_number, is_on, _end in reply.trusted_tasks_page_options
    }
    assert options_by_id[completed_id] == ("COMPLETED1", False)


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


def _cars_on_page(reply) -> list[str]:
    """Номера машин, показанные кнопками текущей страницы (см. design
    report про переработку "📋 Мои авто" в чистую inline keyboard —
    список больше НЕ дублируется текстом, единственный источник —
    trusted_tasks_page_options)."""
    return [car for _task_id, car, _is_on, _end in reply.trusted_tasks_page_options]


def test_trusted_my_cars_shows_10_tasks_per_page(trusted_fx):
    car_numbers = _make_many_tasks(trusted_fx, 250)

    reply = trusted_fx.controller.handle_trusted_tasks_page(0, telegram_user_id=_TRUSTED_ID)

    shown = _cars_on_page(reply)
    assert len(shown) == 10
    assert shown == car_numbers[:10]


def test_trusted_my_cars_first_page_content(trusted_fx):
    car_numbers = _make_many_tasks(trusted_fx, 250)

    reply = trusted_fx.controller.handle_trusted_tasks_page(0, telegram_user_id=_TRUSTED_ID)

    assert "Страница 1 из 25" in reply.text
    shown = _cars_on_page(reply)
    assert shown == car_numbers[:10]
    assert car_numbers[10] not in shown


def test_trusted_my_cars_middle_page_content(trusted_fx):
    car_numbers = _make_many_tasks(trusted_fx, 250)

    reply = trusted_fx.controller.handle_trusted_tasks_page(12, telegram_user_id=_TRUSTED_ID)

    assert "Страница 13 из 25" in reply.text
    shown = _cars_on_page(reply)
    assert shown == car_numbers[120:130]
    assert car_numbers[119] not in shown
    assert car_numbers[130] not in shown


def test_trusted_my_cars_last_page_content_exact_multiple(trusted_fx):
    """250 = 25 * 10 — последняя страница ровно полная (проверяется
    отдельно от неполной последней страницы ниже)."""
    car_numbers = _make_many_tasks(trusted_fx, 250)

    reply = trusted_fx.controller.handle_trusted_tasks_page(24, telegram_user_id=_TRUSTED_ID)

    assert "Страница 25 из 25" in reply.text
    assert _cars_on_page(reply) == car_numbers[240:250]


def test_trusted_my_cars_last_incomplete_page_content(trusted_fx):
    """205 задач, 10 на страницу — 21 страница, последняя неполная (5)."""
    car_numbers = _make_many_tasks(trusted_fx, 205)

    reply = trusted_fx.controller.handle_trusted_tasks_page(20, telegram_user_id=_TRUSTED_ID)

    assert reply.trusted_tasks_total_pages == 21
    assert "Страница 21 из 21" in reply.text
    assert _cars_on_page(reply) == car_numbers[200:205]


def test_trusted_my_cars_list_text_has_no_car_details_anymore(trusted_fx):
    """Явное требование задачи: убрать из текста сообщения 📅 период и 🔎
    последнюю проверку каждой машины — теперь эта информация ТОЛЬКО в
    карточке машины (см. handle_trusted_task_open), список — чистый
    заголовок + пагинация."""
    _make_operator_task(trusted_fx, "H331HA763")

    reply = trusted_fx.controller.handle_trusted_tasks_page(0, telegram_user_id=_TRUSTED_ID)

    assert "H331HA763" not in reply.text
    assert "📅" not in reply.text
    assert "🔎" not in reply.text
    assert reply.text == f"{texts.TRUSTED_TASKS_HEADER}\nСтраница 1 из 1"


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


# ==== manager-facing "📋 Мои авто" ON/OFF + "▶️ Продолжить мониторинг"
# 15/30/90 дней (см. design report про per-car monitoring toggle) ====


async def test_trusted_my_cars_shows_on_off_button_next_to_each_car(trusted_fx):
    """1. Manager видит рядом с каждой машиной ON/OFF — trusted_tasks_page_
    options несёт (task_id, car_number, is_on) на каждую задачу страницы."""
    on_id = _make_operator_task(trusted_fx, "M295YB196")
    off_id = _make_operator_task(trusted_fx, "A123AA180", status="stopped")

    reply = trusted_fx.controller.handle_trusted_tasks_page(0, telegram_user_id=_TRUSTED_ID)

    options_by_id = {tid: (car, is_on) for tid, car, is_on, _end in reply.trusted_tasks_page_options}
    assert options_by_id[on_id] == ("M295YB196", True)
    assert options_by_id[off_id] == ("A123AA180", False)


def test_trusted_my_cars_options_carry_end_date_for_the_right_button(trusted_fx):
    """Явное требование задачи: "Добавь период" — end_date каждой задачи
    (ON и OFF одинаково — "последний сохранённый период") доступен через
    trusted_tasks_page_options для построения ПРАВОЙ кнопки "🟢 до ДД.ММ"
    (см. keyboards.py::_format_trusted_task_toggle_label — для OFF дата в
    саму кнопку не идёт, но данные в options есть у обеих)."""
    on_id = _make_operator_task(trusted_fx, "K892AC126")  # end_date по умолчанию 2026-12-31
    off_id = _make_operator_task(trusted_fx, "A123AA180", status="stopped")

    reply = trusted_fx.controller.handle_trusted_tasks_page(0, telegram_user_id=_TRUSTED_ID)

    ends_by_id = {tid: end for tid, _car, _is_on, end in reply.trusted_tasks_page_options}
    assert ends_by_id[on_id] == date(2026, 12, 31)
    assert ends_by_id[off_id] == date(2026, 12, 31)  # OFF: последний сохранённый период


def test_trusted_my_cars_renders_exact_labels_from_the_task(trusted_fx):
    """Явное требование задачи ("Обновить тесты под точные labels"):
    ON:  left = "Y111CA18",  right = "🟢 до 20.12"
    OFF: left = "B641XE89",  right = "⚪"
    Сквозной тест — реальная задача в БД -> controller -> реальная
    keyboards.trusted_tasks_page_keyboard(), а не только сырые данные."""
    on_task = trusted_fx.task_repository.create(
        car_number="Y111CA18", label=None,
        start_date=date(2026, 6, 20), end_date=date(2026, 12, 20),
        telegram_chat_id=-100999, created_by_user_id=111,
    )
    off_task = trusted_fx.task_repository.create(
        car_number="B641XE89", label=None,
        start_date=date(2026, 2, 8), end_date=date(2026, 8, 8),
        telegram_chat_id=-100999, created_by_user_id=111,
    )
    trusted_fx.task_repository.set_status(off_task.id, "stopped")

    reply = trusted_fx.controller.handle_trusted_tasks_page(0, telegram_user_id=_TRUSTED_ID)
    keyboard = trusted_tasks_page_keyboard(
        reply.trusted_tasks_page_options, page=0, total_pages=reply.trusted_tasks_total_pages,
    )
    rows_by_open_callback = {row[0].data: row for row in keyboard}

    on_row = rows_by_open_callback[encode_trusted_task_open_callback(on_task.id, 0)]
    off_row = rows_by_open_callback[encode_trusted_task_open_callback(off_task.id, 0)]
    assert (on_row[0].text, on_row[1].text) == ("Y111CA18", "🟢 до 20.12")
    assert (off_row[0].text, off_row[1].text) == ("B641XE89", "⚪")


def test_trusted_task_state_matches_real_monitoring_status(trusted_fx):
    """2. Состояние ON/OFF соответствует реальному monitoring state —
    'active' -> ON, 'stopped'/'completed' -> OFF (см. texts.task_monitoring_
    state)."""
    active_id = _make_operator_task(trusted_fx, "AA001AA")
    stopped_id = _make_operator_task(trusted_fx, "BB002BB", status="stopped")
    completed_id = _make_operator_task(trusted_fx, "CC003CC", status="completed")

    reply = trusted_fx.controller.handle_trusted_tasks_page(0, telegram_user_id=_TRUSTED_ID)
    options_by_id = {tid: is_on for tid, _car, is_on, _end in reply.trusted_tasks_page_options}

    assert options_by_id[active_id] is True
    assert options_by_id[stopped_id] is False
    assert options_by_id[completed_id] is False


def test_trusted_task_open_shows_car_detail_card(trusted_fx):
    """Кнопка-номер машины в списке открывает карточку с периодом/ON-OFF —
    ровно та информация, что раньше была видна текстом прямо в списке
    (см. design report про переработку экрана)."""
    task_id = _make_operator_task(trusted_fx, "H331HA763")

    reply = trusted_fx.controller.handle_trusted_task_open(task_id, 0, telegram_user_id=_TRUSTED_ID)

    assert reply.trusted_task_detail_id == task_id
    assert reply.trusted_task_detail_page == 0
    assert "🚗 H331HA763" in reply.text
    assert "Мониторинг: 🟢 ON" in reply.text
    assert "📅 01.08.2026 — 31.12.2026" in reply.text
    assert "🔎 Ещё не проверялась" in reply.text


def test_trusted_task_open_shows_off_status_and_last_check(trusted_fx):
    task_id = _make_operator_task(trusted_fx, "A123AA180", status="stopped")
    trusted_fx.task_repository.record_check_result(task_id, last_check_status="ok", last_error=None)

    reply = trusted_fx.controller.handle_trusted_task_open(task_id, 0, telegram_user_id=_TRUSTED_ID)

    assert "Мониторинг: ⚪ OFF" in reply.text
    assert "🔎 Последняя проверка:" in reply.text


def test_trusted_task_open_missing_task_returns_none(trusted_fx):
    reply = trusted_fx.controller.handle_trusted_task_open(999999, 0, telegram_user_id=_TRUSTED_ID)
    assert reply is None


def test_trusted_task_open_rejects_non_trusted_user(fx):
    task_id = _make_operator_task(fx, "H331HA763")
    reply = fx.controller.handle_trusted_task_open(task_id, 0, telegram_user_id=1)
    assert reply is None


def test_trusted_task_open_of_one_car_does_not_affect_another(trusted_fx):
    """Открытие карточки — read-only, не может повлиять ни на эту, ни на
    другую машину."""
    car_a = _make_operator_task(trusted_fx, "M295YB196")
    car_b = _make_operator_task(trusted_fx, "C196BA250", status="stopped")

    trusted_fx.controller.handle_trusted_task_open(car_a, 0, telegram_user_id=_TRUSTED_ID)

    assert trusted_fx.task_repository.get(car_a).status == "active"
    assert trusted_fx.task_repository.get(car_b).status == "stopped"


def test_trusted_task_toggle_on_turns_off_and_shows_continue_screen(trusted_fx):
    """3. ON → нажатие → OFF: handle_trusted_task_toggle на активной задаче
    выключает мониторинг и показывает экран "▶️ Продолжить мониторинг"."""
    task_id = _make_operator_task(trusted_fx, "M295YB196")

    reply = trusted_fx.controller.handle_trusted_task_toggle(task_id, 0, telegram_user_id=_TRUSTED_ID)

    assert trusted_fx.task_repository.get(task_id).status == "stopped"
    assert reply.trusted_task_off_id == task_id
    assert reply.trusted_task_off_page == 0
    assert "M295YB196" in reply.text
    assert "⚪ OFF" in reply.text


def test_trusted_task_toggle_off_opens_continue_screen_without_changing_state(trusted_fx):
    """4. OFF → открывается "Продолжить мониторинг" — нажатие ⚪ OFF (уже
    выключенной задачи) НЕ меняет её статус, просто показывает тот же
    экран."""
    task_id = _make_operator_task(trusted_fx, "A123AA180", status="stopped")

    reply = trusted_fx.controller.handle_trusted_task_toggle(task_id, 0, telegram_user_id=_TRUSTED_ID)

    assert trusted_fx.task_repository.get(task_id).status == "stopped"
    assert reply.trusted_task_off_id == task_id
    assert "Мониторинг: ⚪ OFF" in reply.text
    # "выключен" (turn-off confirmation) НЕ должно появляться — статус не менялся этим вызовом.
    assert "выключен" not in reply.text


def test_trusted_task_continue_shows_15_30_90_choice(trusted_fx):
    """5. Далее появляется выбор 15/30/90 — "▶️ Продолжить мониторинг" на
    OFF-экране открывает TRUSTED_TASK_PERIOD_PROMPT с task_id/page для
    trusted_task_period_choice_keyboard."""
    task_id = _make_operator_task(trusted_fx, "A123AA180", status="stopped")

    reply = trusted_fx.controller.handle_trusted_task_continue(task_id, 0, telegram_user_id=_TRUSTED_ID)

    assert reply.text == texts.TRUSTED_TASK_PERIOD_PROMPT
    assert reply.trusted_task_period_id == task_id
    assert reply.trusted_task_period_page == 0


@pytest.mark.parametrize("days", [15, 30, 90])
def test_trusted_task_period_choice_saves_period_and_turns_on(trusted_fx, days):
    """6/7/8/9. 15/30/90 дней корректно сохраняется, задача становится ON,
    end_date = today + N дней."""
    task_id = _make_operator_task(trusted_fx, "M295YB196", status="stopped")
    today = _today()

    reply = trusted_fx.controller.handle_trusted_task_period_choice(
        task_id, days, 0, telegram_user_id=_TRUSTED_ID,
    )

    task = trusted_fx.task_repository.get(task_id)
    assert task.status == "active"
    assert task.start_date == today
    assert task.end_date == today + timedelta(days=days)
    assert f"включён на {days} дней" in reply.text
    assert f"До: {task.end_date.strftime('%d.%m.%Y')}" in reply.text
    # Явное требование: после подтверждения менеджер возвращается в
    # "📋 Мои авто" — там уже видно новое состояние (ON) И новую дату
    # (пример из задачи: "выбрали 30 дней -> [Y111CA18] [🟢 до 21.10]").
    options_by_id = {tid: (is_on, end) for tid, _car, is_on, end in reply.trusted_tasks_page_options}
    assert options_by_id[task_id] == (True, task.end_date)

    # Явное требование: "Проверить также, что после resume end_date сразу
    # появляется в зелёной кнопке" — рендерим РЕАЛЬНУЮ клавиатуру из тех
    # же options, что вернул controller, а не только сырые данные выше.
    rendered_keyboard = trusted_tasks_page_keyboard(
        reply.trusted_tasks_page_options, page=reply.trusted_tasks_page, total_pages=reply.trusted_tasks_total_pages,
    )
    right_labels = {row[0].data: row[1].text for row in rendered_keyboard if len(row) == 2}
    assert right_labels[encode_trusted_task_open_callback(task_id, 0)] == f"🟢 до {task.end_date.strftime('%d.%m')}"


def test_trusted_task_period_choice_rejects_value_outside_allowlist(trusted_fx):
    """Defensive: days не из TRUSTED_TASK_PERIOD_CHOICES (например,
    подделанный callback) отклоняется, ничего не меняется."""
    task_id = _make_operator_task(trusted_fx, "M295YB196", status="stopped")

    reply = trusted_fx.controller.handle_trusted_task_period_choice(
        task_id, 45, 0, telegram_user_id=_TRUSTED_ID,
    )

    assert reply is None
    assert trusted_fx.task_repository.get(task_id).status == "stopped"


def test_trusted_task_back_from_period_choice_returns_to_continue_screen(trusted_fx):
    """12a. "⬅️ Назад" с экрана 15/30/90 возвращает на экран "▶️
    Продолжить мониторинг" — keyboards.py::trusted_task_period_choice_
    keyboard кодирует его ЧЕРЕЗ encode_trusted_task_toggle_callback (задача
    уже OFF — повторный вызов handle_trusted_task_toggle ничего не меняет,
    просто показывает тот же экран, см. design report)."""
    task_id = _make_operator_task(trusted_fx, "A123AA180", status="stopped")

    back_reply = trusted_fx.controller.handle_trusted_task_toggle(task_id, 0, telegram_user_id=_TRUSTED_ID)

    assert trusted_fx.task_repository.get(task_id).status == "stopped"  # без изменений
    assert back_reply.trusted_task_off_id == task_id
    assert "Мониторинг: ⚪ OFF" in back_reply.text


def test_trusted_task_back_from_continue_screen_returns_to_my_cars_list(trusted_fx):
    """12b. Ещё один "⬅️ Назад" с OFF-экрана возвращает в "📋 Мои авто" —
    keyboards.py::trusted_task_off_keyboard кодирует его через
    encode_trusted_tasks_page_callback(page), т.е. handle_trusted_tasks_page
    на той же странице."""
    task_id = _make_operator_task(trusted_fx, "A123AA180", status="stopped")

    list_reply = trusted_fx.controller.handle_trusted_tasks_page(0, telegram_user_id=_TRUSTED_ID)

    assert list_reply.trusted_tasks_page == 0
    assert any(tid == task_id for tid, _car, _is_on, _end in list_reply.trusted_tasks_page_options)


def test_trusted_task_toggle_of_one_car_does_not_affect_another(trusted_fx):
    """13. Изменение одной машины не влияет на остальные."""
    car_a = _make_operator_task(trusted_fx, "M295YB196")
    car_b = _make_operator_task(trusted_fx, "C196BA250")

    trusted_fx.controller.handle_trusted_task_toggle(car_a, 0, telegram_user_id=_TRUSTED_ID)

    assert trusted_fx.task_repository.get(car_a).status == "stopped"
    assert trusted_fx.task_repository.get(car_b).status == "active"


def test_trusted_task_toggle_missing_task_returns_none(trusted_fx):
    reply = trusted_fx.controller.handle_trusted_task_toggle(999999, 0, telegram_user_id=_TRUSTED_ID)
    assert reply is None


def test_trusted_task_continue_missing_task_returns_none(trusted_fx):
    reply = trusted_fx.controller.handle_trusted_task_continue(999999, 0, telegram_user_id=_TRUSTED_ID)
    assert reply is None


def test_trusted_task_period_choice_missing_task_returns_failure(trusted_fx):
    reply = trusted_fx.controller.handle_trusted_task_period_choice(
        999999, 30, 0, telegram_user_id=_TRUSTED_ID,
    )
    assert reply.text == texts.CAR_ACTION_FAILED_TEXT


def test_ordinary_user_cannot_use_trusted_task_toggle(fx):
    """14a. Обычный пользователь не получает доступ к manager-only ON/OFF
    (is_trusted() перепроверяется заново — не влияет на обычный UX)."""
    task_id = _make_operator_task(fx, "M295YB196")

    assert fx.controller.handle_trusted_task_toggle(task_id, 0, telegram_user_id=1) is None
    assert fx.controller.handle_trusted_task_continue(task_id, 0, telegram_user_id=1) is None
    assert fx.controller.handle_trusted_task_period_choice(task_id, 30, 0, telegram_user_id=1) is None
    # Задача не должна была измениться ни одним из отклонённых вызовов.
    assert fx.task_repository.get(task_id).status == "active"


async def test_ordinary_user_my_cars_ux_unchanged_by_manager_toggle_feature(fx):
    """14b. Регрессия: car-centric "📋 Мои авто" обычного пользователя (ON/
    OFF/Delete через SubscriptionService.turn_on_car/turn_off_car/
    delete_car) не затронут — эти carts/кнопки продолжают работать через
    ПРЕЖНИЙ, subscription-based путь, никак не связанный с task-level
    manager toggle."""
    outcome = await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196",
        period_days=30, today=date(2026, 9, 3),
    )

    reply = await fx.controller.handle_text(
        texts.MY_CARS_LABEL, chat_id=1, telegram_user_id=1, username=None,
    )

    assert reply.trusted_tasks_page is None
    assert reply.trusted_tasks_page_options is None
    assert reply.my_cars_page_options is not None
    assert reply.my_cars_page_options[0][0] == outcome.subscription.id


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
    assert "штрафов не найдено" in check_reply.text
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
    # "не найден в активном мониторинге" (task-level 404) — НЕ "штрафов не
    # найдено" (0 fines, штатный manual-check результат для задачи,
    # которая реально существует и была найдена) — эти два разных смысла
    # умышленно похожи текстуально, поэтому сверяем маркер конкретной
    # not-found ошибки ("❌"), а не расплывчатую подстроку "не найден".
    assert "❌" not in check_reply.text


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
    сейчас" по тому же номеру без новых штрафов от провайдера не создаёт
    дубликат и не считает уже виденный штраф новым, но (см. задачу про
    UX manual check) ВСЁ РАВНО показывает его trusted-оператору — 🔎
    отражает текущее состояние машины, а не только delta."""
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
        assert "🚨 Обнаружен штраф" in first.text
        assert "📄 Штраф №: AB123456" in first.text
        assert first.cta_buttons is None  # trusted-оператор — без коммерческих кнопок

        await fixture.controller.handle_text(
            texts.CHECK_NOW_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        second = await fixture.controller.handle_text(
            "E911EE95", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        assert "новых штрафов нет" not in second.text
        assert "🚨 Обнаружен штраф" in second.text
        assert "📄 Штраф №: AB123456" in second.text
        assert second.cta_buttons is None

        [only_row] = fixture.detected_fine_repository.list_by_car_number("E911EE95")
        assert only_row is not None  # ни одной лишней строки не создано
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
    [(_subscription_id, label)] = client_reply.my_cars_page_options
    assert "ON" not in label
    assert "OFF" in label
    # Клиент также больше не может действовать через 🔎 этой подпиской.
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


# ==== "📊 Статистика" — trusted-operator-only (см. design report) ====


def test_conversation_controller_is_trusted_reflects_configured_ids(trusted_fx, fx):
    """ConversationController.is_trusted() — публичная обёртка, которую
    reader/public_bot/handlers.py использует для
    main_menu_keyboard(include_statistics=...)."""
    assert trusted_fx.controller.is_trusted(_TRUSTED_ID) is True
    assert trusted_fx.controller.is_trusted(1) is False
    assert fx.controller.is_trusted(_TRUSTED_ID) is False  # обычный fx без trusted-списка


async def test_trusted_operator_sees_statistics(trusted_fx):
    """Trusted operator sees statistics — "📊 Статистика" возвращает
    реальную сводку, посчитанную BotStatisticsService."""
    trusted_fx.known_users_repository.record_seen(
        telegram_user_id=1, telegram_chat_id=1, telegram_username="alice",
    )
    trusted_fx.known_users_repository.record_seen(
        telegram_user_id=2, telegram_chat_id=2, telegram_username="bob",
    )
    task_id = _make_operator_task(trusted_fx, "E911EE95")
    sub = trusted_fx.subscription_repository.create(
        monitoring_task_id=task_id, car_number="E911EE95",
        telegram_user_id=3, telegram_chat_id=3, telegram_username="carol",
        start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
    )
    trusted_fx.subscription_repository.stop_by_owner_or_creator(sub.id, telegram_user_id=3)

    reply = await trusted_fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "📊 Статистика бота" in reply.text
    assert "👥 Всего пользователей: 2" in reply.text
    assert "🚗 Активных подписок: 0" in reply.text
    assert "⏸ Остановленных подписок: 1" in reply.text
    assert reply.show_main_menu is True


async def test_normal_user_cannot_invoke_statistics_action_directly(fx):
    """Явное требование задачи: "если обычный пользователь каким-то
    образом вручную вызовет соответствующий callback/action — вернуть
    безопасный отказ и ничего не показывать" — тот же текст, что и у
    кнопки (Telegram неотличим от реального нажатия ЛЮБОЙ reply-кнопки от
    пользователя, вручную напечатавшего тот же текст)."""
    fx.known_users_repository.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username="alice")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=1, telegram_user_id=1, username="alice",
    )

    assert reply.text == texts.MAIN_MENU_TEXT
    assert "Статистика" not in reply.text
    assert "Всего пользователей" not in reply.text
    assert reply.show_main_menu is True


async def test_normal_user_statistics_attempt_does_not_leak_real_numbers(fx):
    """Даже если реальные данные в БД существуют (пользователи/подписки),
    обычный пользователь при попытке вызвать статистику не должен увидеть
    ни одной цифры — полный отказ, а не urезанная/частичная сводка."""
    fx.known_users_repository.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)
    fx.known_users_repository.record_seen(telegram_user_id=2, telegram_chat_id=2, telegram_username=None)

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=1, telegram_user_id=1, username=None,
    )

    assert "2" not in reply.text


# ---- "🇹🇷 Штрафы Турции" (см. design report "унификация UI") ----


async def test_turkey_bot_link_label_returns_reply_with_show_turkey_bot_link(fx):
    """Штатный способ перехода для reply-кнопки — нажатие распознаётся
    как обычный текст (см. reader/public_bot/keyboards.py::
    main_menu_keyboard) и отвечает show_turkey_bot_link=True, ДОСТУПНО
    ВСЕМ пользователям одинаково (не trusted-gated)."""
    reply = await fx.controller.handle_text(
        texts.TURKEY_BOT_LINK_LABEL, chat_id=1, telegram_user_id=1, username="alice",
    )

    assert reply.text == texts.TURKEY_BOT_LINK_TEXT
    assert reply.show_turkey_bot_link is True
    assert reply.show_main_menu is False


async def test_turkey_bot_link_label_works_for_trusted_operator_too(trusted_fx):
    reply = await trusted_fx.controller.handle_text(
        texts.TURKEY_BOT_LINK_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert reply.text == texts.TURKEY_BOT_LINK_TEXT
    assert reply.show_turkey_bot_link is True
