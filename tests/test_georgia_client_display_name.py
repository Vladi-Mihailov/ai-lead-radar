"""«👤 Данные клиента» в @ProtocolGEbot: имя клиента без Telegram-логина.

Имя — подпись менеджера к автомобилю: не user_id, не доступ и не повод
для уведомлений. Сквозные тесты через настоящий ConversationController
(и handlers.register для навигации) поверх SQLite/tmp_path; police.ge —
фейк со счётчиком запросов, доставка — настоящий ClientDeliveryService.
"""

import functools
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from telethon import events  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import ParsedFineRecord  # noqa: E402
from reader.fines.notification_control import EXCLUSION_LATE_CLIENT_LINK  # noqa: E402
from reader.fines.provider import FineProvider  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.plate_display import format_plate_for_telegram  # noqa: E402
from reader.public_bot import keyboards, texts  # noqa: E402
from reader.public_bot.client_names import (  # noqa: E402
    CLIENT_NAME_MAX_LENGTH,
    CarClientNameRepository,
    ClientNameValidationError,
    normalize_client_name,
)
from reader.public_bot.conversation import (  # noqa: E402
    STEP_AWAITING_ADD_CLIENT_NAME,
    STEP_AWAITING_ADD_CONFIRM,
    STEP_AWAITING_TASK_CLIENT_NAME,
    ConversationController,
)
from reader.public_bot.conversation_state_repository import BotConversationStateRepository  # noqa: E402
from reader.public_bot.debt_refresh_service import DebtRefreshService  # noqa: E402
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository  # noqa: E402
from reader.public_bot.delivery_service import (  # noqa: E402
    ClientDeliveryService,
    _applicable_roles,
    exclude_known_fines_for_subscription,
)
from reader.public_bot.handlers import register  # noqa: E402
from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402
from reader.public_bot.statistics_service import BotStatisticsService  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import SubscriptionService  # noqa: E402
from reader.users.models import TelegramUserInfo  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_TBILISI = ZoneInfo("Asia/Tbilisi")
_TRUSTED = 5712994689
_CLIENT = 777
_STRANGER = 4242
_CAR = "M295YB196"
_OTHER_CAR = "AA123BB"


async def _instant(_seconds):
    pass


class _Provider(FineProvider):
    def __init__(self):
        self.calls: list[str] = []

    async def search_by_plate(self, plate: str):
        self.calls.append(plate)
        return [ParsedFineRecord(
            car_number=plate, external_fine_id=f"AB-{plate}", penalty_date=date(2026, 8, 6),
            due_date=date(2099, 12, 31), delivered_status="Не вручено", fingerprint=f"fp-{plate}",
            raw_data={"protocolNo": f"AB-{plate}"}, amount=50,
        )]


class _Sender:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, *, buttons=None) -> None:
        self.sent.append((chat_id, text))


class _Fixture:
    def __init__(self, tmp_path, *, with_names=True):
        db = tmp_path / "users.db"
        self.db = db
        self.tasks = FineMonitoringTaskRepository(db)
        self.fines = DetectedFineRepository(db)
        self.subscriptions = FineSubscriptionRepository(db)
        self.users = UserRepository(db)
        self.states = BotConversationStateRepository(db)
        self.known = BotKnownUsersRepository(db)
        self.names = CarClientNameRepository(db)
        self.deliveries = ClientFineDeliveryRepository(db)
        self.provider = _Provider()
        self.check = FineCheckService(self.provider, self.tasks, self.fines, sleep=_instant)
        self.service = SubscriptionService(
            self.tasks, self.subscriptions, self.users, self.check,
            late_link_history_excluder=functools.partial(
                exclude_known_fines_for_subscription, detected_fine_repository=self.fines,
                delivery_repository=self.deliveries, reason=EXCLUSION_LATE_CLIENT_LINK,
            ),
        )
        self.controller = ConversationController(
            self.states, self.service, BotStatisticsService(self.known, self.subscriptions, self.fines), self.known,
            tz=_TBILISI, trusted_operator_user_ids=frozenset({_TRUSTED}),
            debt_refresh_service=DebtRefreshService(self.tasks, self.check, sleep=_instant),
            client_name_repository=self.names if with_names else None,
        )
        self.sender = _Sender()
        self.delivery = ClientDeliveryService(self.fines, self.subscriptions, self.deliveries, self.sender, tz=_TBILISI)

    async def text(self, value, *, user=_TRUSTED, **kwargs):
        return await self.controller.handle_text(value, chat_id=user, telegram_user_id=user, username=None, **kwargs)

    async def start_trusted_add(self, *, with_client: str | None, car=_CAR):
        await self.text(texts.ADD_CAR_LABEL)
        await self.text(car)
        if with_client is None:
            return self.controller.handle_add_client_decision(False, chat_id=_TRUSTED, telegram_user_id=_TRUSTED)
        self.controller.handle_add_client_decision(True, chat_id=_TRUSTED, telegram_user_id=_TRUSTED)
        return await self.text(with_client)

    async def set_name_on_confirm(self, name: str, car=_CAR):
        prompt = self.controller.handle_add_car_client_name_prompt(
            chat_id=_TRUSTED, telegram_user_id=_TRUSTED, car_number=car,
        )
        assert prompt.text == texts.CLIENT_NAME_PROMPT
        return await self.text(name)

    async def confirm(self, car=_CAR):
        return await self.controller.handle_add_car_confirm(
            chat_id=_TRUSTED, telegram_user_id=_TRUSTED, first_name=None, last_name=None, car_number=car,
        )

    async def added_car(self, *, name=None, car=_CAR):
        await self.start_trusted_add(with_client=None, car=car)
        if name:
            await self.set_name_on_confirm(name, car=car)
        await self.confirm(car=car)
        return self.tasks.get_active_by_car_number(car)[0]

    def action(self, action, task, *, user=_TRUSTED, token=None):
        return self.controller.handle_task_client_action(
            action, task.id, 0, chat_id=user, telegram_user_id=user, token=token,
        )

    async def ask_link(self, task, username):
        """«🔗 Telegram клиента» → логин → экран подтверждения (ничего не создано)."""
        self.action("tg", task)
        return await self.text(username)

    async def link_later(self, task, username):
        confirm = await self.ask_link(task, username)
        action, task_id, page, token = confirm.task_client_confirm
        assert (action, task_id, page) == ("linkyes", task.id, 0)
        return await self.controller.handle_task_client_link_confirm(
            task.id, 0, chat_id=_TRUSTED, telegram_user_id=_TRUSTED, token=token,
        )

    def known_user(self, user_id, username):
        self.users.upsert(TelegramUserInfo(user_id=user_id, username=username, first_name=None, last_name=None))

    def deliverable(self):
        return self.subscriptions.list_all_deliverable(today=datetime.now(timezone.utc).astimezone(_TBILISI).date())

    def client_recipients(self):
        """Кому уйдут ЛИЧНЫЕ уведомления как владельцу (роль 'owner')."""
        return [s.telegram_user_id for s in self.deliverable() if "owner" in _applicable_roles(s)]

    async def delivered_to(self) -> list[int]:
        self.sender.sent.clear()
        await self.delivery.run_once()
        return sorted(chat for chat, _ in self.sender.sent)

    def age_state(self, chat_id, minutes):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE bot_conversation_state SET updated_at = datetime('now', ?) WHERE chat_id = ?",
            (f"-{minutes} minutes", chat_id),
        )
        conn.commit()
        conn.close()

    def close(self):
        for repository in (
            self.deliveries, self.names, self.known, self.states, self.users, self.subscriptions, self.fines,
            self.tasks,
        ):
            repository.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


# ---- Проверка имени ----


@pytest.mark.parametrize("raw, expected", [
    ("Иван Иванов", "Иван Иванов"),
    ("Giorgi123", "Giorgi123"),
    ("გიორგი", "გიორგი"),
    ("Ahmet 007", "Ahmet 007"),
    ("Клиент 12345", "Клиент 12345"),
    ("Александр-2", "Александр-2"),
    ("İsmail Öztürk", "İsmail Öztürk"),
    ("O'Brien Jr.", "O'Brien Jr."),
    ("Nguyễn Thị Ệ", "Nguyễn Thị Ệ"),
    ("हिन्दी", "हिन्दी"),
    ("  Иван \t  Иванов \n", "Иван Иванов"),
    ("Иван​Иванов", "ИванИванов"),  # невидимый символ из копирования убирается
    ("Ivanㅤ", "Ivan"),  # Hangul filler — тоже невидимый
    ("Ivan\u17b5", "Ivan"),  # кхмерская «немая» гласная — тоже
    ("\u1780\u17b4\u17b4\u17b4", "\u1780"),
    ("José", "José"),  # NFC
    ("a" + "́" * 3, "á́́"),  # до трёх знаков на букву
])
def test_client_name_accepts_any_alphabet(raw, expected):
    assert normalize_client_name(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "@ivan123", "Иван*", "_ivan_", "[Иван](tg://user?id=1)", "Иван `x`", "😀", "---",
    "a" * (CLIENT_NAME_MAX_LENGTH + 1),
    "ㅤ", "ﾠﾠ", "ㅤ ᅟᅠ",  # только «пустые» буквы
    "a" + "́" * 4, "Иван" + "̀" * 20,  # стопка знаков
    "\u0301a", "Ivan \u0301", "Ivan-\u0301x",  # знак без буквы под ним
    "x\u20dd", "x\u20dd\u20dd\u20dd",  # охватывающие знаки
])
def test_client_name_rejects_markup_blank_and_stacked_text(raw):
    with pytest.raises(ClientNameValidationError):
        normalize_client_name(raw)


def test_client_name_is_escaped_on_display_even_if_stored_raw():
    lines = texts.format_client_lines("**жирный** _x_", [])
    assert lines == [f"👤 Клиент: {texts.escape_telethon_markdown('**жирный** _x_')}", "🔗 Telegram: не привязан"]
    assert "**жирный**" not in lines[0]


def test_client_lines_with_and_without_telegram():
    assert texts.format_client_lines("Иван Иванов", ["ivan123"]) == ["👤 Клиент: Иван Иванов", "🔗 Telegram: @ivan123"]
    assert texts.format_client_lines("Иван Иванов", []) == ["👤 Клиент: Иван Иванов", "🔗 Telegram: не привязан"]
    assert texts.format_client_lines(None, [], pending_hints=["ivan123"]) == [
        "👤 Клиент: не указан", "🔗 Telegram: не привязан", "⏳ Ждёт подтверждения: @ivan123",
    ]


# ---- Add Car менеджера ----


async def test_manager_adds_a_car_with_a_name_only(fx):
    confirm = await fx.start_trusted_add(with_client=None)
    assert confirm.add_car_client_name_button is True

    with_name = await fx.set_name_on_confirm("Иван Иванов")
    assert with_name.text.splitlines()[:3] == [
        f"🚗 {format_plate_for_telegram(_CAR)}", "👤 Клиент: Иван Иванов", "🔗 Telegram: не привязан",
    ]
    assert fx.states.get(_TRUSTED).step == STEP_AWAITING_ADD_CONFIRM

    summary = await fx.confirm()

    assert summary.text.startswith(texts.ADD_CAR_DONE_LINE)
    assert "👤 Клиент: Иван Иванов\n🔗 Telegram: не привязан" in summary.text
    assert fx.names.get(_CAR) == "Иван Иванов"
    # Имя — не доступ: ни подписки, ни получателей уведомлений.
    assert fx.subscriptions.list_by_car_number(_CAR) == []
    assert fx.deliverable() == []
    assert len(fx.tasks.list_active()) == 1


async def test_manager_adds_a_car_with_a_known_telegram_and_name(fx):
    fx.known_user(_CLIENT, "real_owner")
    confirm = await fx.start_trusted_add(with_client="@real_owner")
    with_name = await fx.set_name_on_confirm("გიორგი")
    # До добавления ничего ещё не создано — ни привязки, ни приглашения.
    assert "👤 Клиент: გიორგი\n🔗 Telegram: @real_owner — будет привязан после добавления" in with_name.text
    assert "Ждёт подтверждения" not in with_name.text
    assert confirm.text == texts.format_add_car_confirm_prompt(_CAR, owner_username="real_owner")  # без имени — как раньше

    summary = await fx.confirm()

    assert "👤 Клиент: გიორგი\n🔗 Telegram: @real_owner" in summary.text
    [subscription] = fx.subscriptions.list_by_user(_CLIENT)
    assert subscription.status == "active" and subscription.telegram_user_id == _CLIENT


async def test_add_car_summary_with_an_unconfirmed_invite_says_not_linked(fx):
    await fx.start_trusted_add(with_client="@unknown_person")
    await fx.set_name_on_confirm("Иван Иванов")

    summary = await fx.confirm()

    assert "👤 Клиент: Иван Иванов\n🔗 Telegram: не привязан\n⏳ Ждёт подтверждения: @unknown_person" in summary.text
    assert "🔗 Telegram: @unknown_person" not in summary.text


async def test_add_car_with_client_keeps_sending_current_fines_as_before(fx):
    """Add Car с клиентом — прежнее поведение: текущие штрафы приходят."""
    fx.known_user(_CLIENT, "real_owner")
    await fx.start_trusted_add(with_client="@real_owner")
    await fx.confirm()

    assert await fx.delivered_to() == [_CLIENT, _TRUSTED]


async def test_manager_adds_a_car_without_any_client_data_as_before(fx):
    await fx.start_trusted_add(with_client=None)

    summary = await fx.confirm()

    assert summary.text.startswith(texts.ADD_CAR_DONE_LINE)
    assert "Клиент" not in summary.text and fx.names.get(_CAR) is None


async def test_invalid_name_keeps_the_step_and_add_works_without_a_name(fx):
    await fx.start_trusted_add(with_client=None)
    reply = await fx.set_name_on_confirm("😀")

    assert reply.text.startswith("❌ ")
    assert reply.add_car_confirm_plate == _CAR  # ✅/Отмена под вопросом
    assert fx.states.get(_TRUSTED).step == STEP_AWAITING_ADD_CLIENT_NAME

    summary = await fx.confirm()  # ✅ прямо с шага имени
    assert summary.text.startswith(texts.ADD_CAR_DONE_LINE) and fx.names.get(_CAR) is None


async def test_name_button_is_only_for_the_manager_flow(fx):
    await fx.text(texts.ADD_CAR_LABEL, user=_STRANGER)
    self_service = await fx.text(_CAR, user=_STRANGER)
    assert self_service.add_car_confirm_plate == _CAR
    assert self_service.add_car_client_name_button is False
    assert fx.controller.handle_add_car_client_name_prompt(
        chat_id=_STRANGER, telegram_user_id=_STRANGER, car_number=_CAR,
    ) is None


async def test_without_the_feature_the_confirm_has_no_name_button(tmp_path):
    fixture = _Fixture(tmp_path, with_names=False)
    try:
        confirm = await fixture.start_trusted_add(with_client=None)
        assert confirm.add_car_client_name_button is False
        assert fixture.controller.handle_add_car_client_name_prompt(
            chat_id=_TRUSTED, telegram_user_id=_TRUSTED, car_number=_CAR,
        ) is None
    finally:
        fixture.close()


# ---- Карточка уже добавленной машины ----


async def test_task_card_shows_the_client_and_its_buttons(fx):
    task = await fx.added_car(name="Ahmet 007")

    card = fx.controller.handle_trusted_task_open(task.id, 0, telegram_user_id=_TRUSTED)

    assert card.text.splitlines()[1:3] == ["👤 Клиент: Ahmet 007", "🔗 Telegram: не привязан"]
    assert card.trusted_task_client_buttons and card.trusted_task_client_has_name
    assert card.trusted_task_client_can_unlink is False
    labels = [b.text for row in keyboards.trusted_task_detail_keyboard(
        page=0, task_id=task.id, has_client_name=True, can_unlink=False) for b in row]
    assert labels == [texts.CLIENT_NAME_BUTTON_LABEL, texts.TASK_CLIENT_TELEGRAM_BUTTON_LABEL,
                      texts.TASK_CLIENT_NAME_DELETE_BUTTON_LABEL, texts.BACK_BUTTON_LABEL]


async def test_manager_changes_and_deletes_the_name_of_an_existing_car(fx):
    task = await fx.added_car()

    prompt = fx.action("name", task)
    assert prompt.text == texts.format_task_client_input_prompt(_CAR, texts.CLIENT_NAME_PROMPT)
    assert prompt.text.startswith(f"🚗 {format_plate_for_telegram(_CAR)}")
    assert prompt.task_client_cancel == (task.id, 0)
    saved = await fx.text("Клиент 12345")
    assert saved.text.startswith(texts.TASK_CLIENT_NAME_SAVED_TEXT)
    assert "👤 Клиент: Клиент 12345" in saved.text and fx.names.get(_CAR) == "Клиент 12345"

    deleted = fx.action("delname", task)
    assert deleted.text.startswith(texts.TASK_CLIENT_NAME_DELETED_TEXT)
    assert "👤 Клиент: не указан" in deleted.text and fx.names.get(_CAR) is None


async def test_cancel_closes_the_input_step(fx):
    task = await fx.added_car()
    fx.action("name", task)

    card = fx.action("cancel", task)
    reply = await fx.text("Клиент 12345")

    assert card.trusted_task_detail_id == task.id
    assert fx.names.get(_CAR) is None and reply.text != texts.TASK_CLIENT_NAME_SAVED_TEXT


async def test_telegram_link_needs_a_confirmation_and_creates_nothing_before_it(fx):
    task = await fx.added_car(name="Иван Иванов")
    fx.known_user(_CLIENT, "ivan123")

    confirm = await fx.ask_link(task, "@ivan123")

    assert confirm.text == texts.format_task_client_link_confirm_prompt(_CAR, "ivan123")
    assert confirm.task_client_confirm[:3] == ("linkyes", task.id, 0)
    assert fx.subscriptions.list_by_car_number(_CAR) == []


async def test_telegram_added_later_keeps_the_name_and_reuses_the_task(fx):
    task = await fx.added_car(name="Иван Иванов")
    calls_before = len(fx.provider.calls)
    fx.known_user(_CLIENT, "ivan123")

    linked = await fx.link_later(task, "@ivan123")

    assert linked.text.startswith("✅ Telegram клиента привязан: @ivan123")
    assert "👤 Клиент: Иван Иванов\n🔗 Telegram: @ivan123" in linked.text
    [subscription] = fx.subscriptions.list_by_user(_CLIENT)
    assert subscription.monitoring_task_id == task.id and subscription.status == "active"
    assert subscription.created_by_telegram_user_id == _TRUSTED
    assert len(fx.tasks.list_active()) == 1  # без второй задачи мониторинга
    assert len(fx.provider.calls) == calls_before  # без внеочередного запроса к police.ge
    assert fx.names.get(_CAR) == "Иван Иванов"


async def test_link_confirm_button_is_single_use_and_bound_to_its_screen(fx):
    task = await fx.added_car()
    other = await fx.added_car(car=_OTHER_CAR)
    fx.known_user(_CLIENT, "ivan123")
    confirm = await fx.ask_link(task, "@ivan123")
    token = confirm.task_client_confirm[3]

    wrong_token = await fx.controller.handle_task_client_link_confirm(
        task.id, 0, chat_id=_TRUSTED, telegram_user_id=_TRUSTED, token="deadbeef",
    )
    assert wrong_token.text.startswith(texts.TASK_CLIENT_STALE_TEXT)
    confirm = await fx.ask_link(task, "@ivan123")
    token = confirm.task_client_confirm[3]
    other_car = await fx.controller.handle_task_client_link_confirm(
        other.id, 0, chat_id=_TRUSTED, telegram_user_id=_TRUSTED, token=token,
    )
    assert other_car.text.startswith(texts.TASK_CLIENT_STALE_TEXT)
    assert fx.subscriptions.list_by_car_number(_OTHER_CAR) == []

    confirm = await fx.ask_link(task, "@ivan123")
    token = confirm.task_client_confirm[3]
    first = await fx.controller.handle_task_client_link_confirm(task.id, 0, chat_id=_TRUSTED, telegram_user_id=_TRUSTED, token=token)
    again = await fx.controller.handle_task_client_link_confirm(task.id, 0, chat_id=_TRUSTED, telegram_user_id=_TRUSTED, token=token)

    assert first.text.startswith("✅ Telegram клиента привязан")
    assert again.text.startswith(texts.TASK_CLIENT_STALE_TEXT)
    assert len(fx.subscriptions.list_by_user(_CLIENT)) == 1


async def test_link_is_refused_on_a_stopped_car(fx):
    task = await fx.added_car()
    fx.known_user(_CLIENT, "ivan123")
    fx.service.stop_task_for_trusted_admin(task.id, stopped_by=_TRUSTED)

    card = fx.action("tg", task)

    assert card.text.startswith(texts.TASK_CLIENT_TASK_NOT_ACTIVE_TEXT)
    assert fx.states.get(_TRUSTED) is None
    assert fx.subscriptions.list_by_car_number(_CAR) == []


async def test_link_is_refused_if_the_car_was_stopped_before_the_confirmation(fx):
    task = await fx.added_car()
    fx.known_user(_CLIENT, "ivan123")
    confirm = await fx.ask_link(task, "@ivan123")
    fx.service.stop_task_for_trusted_admin(task.id, stopped_by=_TRUSTED)

    reply = await fx.controller.handle_task_client_link_confirm(
        task.id, 0, chat_id=_TRUSTED, telegram_user_id=_TRUSTED, token=confirm.task_client_confirm[3],
    )

    assert reply.text.startswith(texts.TASK_CLIENT_TASK_NOT_ACTIVE_TEXT)
    assert fx.subscriptions.list_by_car_number(_CAR) == []


async def test_link_is_refused_when_the_user_already_has_the_car(fx):
    task = await fx.added_car()
    # Клиент добавил машину сам и выключил уведомления.
    await fx.text(texts.ADD_CAR_LABEL, user=_CLIENT)
    await fx.text(_CAR, user=_CLIENT)
    await fx.controller.handle_add_car_confirm(
        chat_id=_CLIENT, telegram_user_id=_CLIENT, first_name=None, last_name=None, car_number=_CAR,
    )
    [own] = fx.subscriptions.list_by_user(_CLIENT)
    fx.subscriptions.stop_by_owner_or_creator(own.id, telegram_user_id=_CLIENT)
    fx.known_user(_CLIENT, "ivan123")

    reply = await fx.link_later(task, "@ivan123")

    assert reply.text.startswith(texts.format_task_client_already_has_car("ivan123"))
    [still_own] = fx.subscriptions.list_by_user(_CLIENT)
    assert still_own.id == own.id and still_own.status == "stopped"


async def test_unknown_telegram_gets_a_claim_link_and_no_access_until_confirmed(fx):
    task = await fx.added_car(name="Иван Иванов")

    reply = await fx.link_later(task, "@unknown_person")

    assert "⏳ Приглашение для @unknown_person создано." in reply.text
    assert "https://t.me/ProtocolGEbot?start=claim_" in reply.text
    assert "⏳ Ждёт подтверждения: @unknown_person" in reply.text
    assert fx.client_recipients() == []  # до подтверждения клиенту лично — ничего

    token = [line for line in reply.text.splitlines() if line.startswith("https://t.me/")][0].rsplit("claim_", 1)[1]
    await fx.controller.handle_text(
        f"/start claim_{token}", chat_id=_CLIENT, telegram_user_id=_CLIENT, username="unknown_person",
        first_name="Real", last_name="Owner",
    )
    card = fx.controller.handle_trusted_task_open(task.id, 0, telegram_user_id=_TRUSTED)
    assert "👤 Клиент: Иван Иванов\n🔗 Telegram: @unknown_person" in card.text
    assert fx.client_recipients() == [_CLIENT]


# ---- Доставка: поздняя привязка не шлёт известные штрафы ----


async def test_late_link_sends_no_known_fines_to_client_or_manager(fx):
    task = await fx.added_car()
    assert fx.fines.list_by_car_number(_CAR)  # штраф уже известен
    fx.known_user(_CLIENT, "ivan123")

    await fx.link_later(task, "@ivan123")

    assert await fx.delivered_to() == []


async def test_late_invite_claim_sends_no_known_fines(fx):
    task = await fx.added_car()
    reply = await fx.link_later(task, "@unknown_person")
    assert await fx.delivered_to() == []  # менеджеру по приглашению — тоже нет
    token = [line for line in reply.text.splitlines() if line.startswith("https://t.me/")][0].rsplit("claim_", 1)[1]

    await fx.controller.handle_text(
        f"/start claim_{token}", chat_id=_CLIENT, telegram_user_id=_CLIENT, username="unknown_person",
    )

    assert await fx.delivered_to() == []


async def test_unlink_and_relink_does_not_resend_fines(fx):
    task = await fx.added_car()
    fx.known_user(_CLIENT, "ivan123")
    await fx.link_later(task, "@ivan123")
    prompt = fx.action("unlink", task)
    fx.action("unlinkyes", task, token=prompt.task_client_confirm[3])

    await fx.link_later(task, "@ivan123")

    assert await fx.delivered_to() == []
    # Новый штраф после привязки — приходит обоим, как обычно.
    fx.provider.search_by_plate = _two_fines_provider(fx.provider)
    await fx.check.check_task(fx.tasks.get(task.id))
    assert await fx.delivered_to() == [_CLIENT, _TRUSTED]


def _two_fines_provider(provider):
    original = provider.search_by_plate

    async def search(plate):
        records = await original(plate)
        return records + [ParsedFineRecord(
            car_number=plate, external_fine_id=f"NEW-{plate}", penalty_date=date(2026, 11, 27),
            due_date=date(2099, 12, 31), delivered_status="Не вручено", fingerprint=f"fp-new-{plate}",
            raw_data={"protocolNo": f"NEW-{plate}"}, amount=50,
        )]

    return search


# ---- Отвязка ----


async def test_unlink_archives_only_manager_links(fx):
    task = await fx.added_car(name="Иван Иванов")
    fx.known_user(_CLIENT, "ivan123")
    await fx.link_later(task, "@ivan123")
    # Клиент, добавивший ту же машину сам, — не привязка менеджера.
    await fx.text(texts.ADD_CAR_LABEL, user=_STRANGER)
    await fx.text(_CAR, user=_STRANGER)
    await fx.controller.handle_add_car_confirm(
        chat_id=_STRANGER, telegram_user_id=_STRANGER, first_name=None, last_name=None, car_number=_CAR,
    )
    fines_before = len(fx.fines.list_by_car_number(_CAR))

    prompt = fx.action("unlink", task)
    action, task_id, page, token = prompt.task_client_confirm
    assert (action, task_id, page) == ("unlinkyes", task.id, 0) and "@ivan123" in prompt.text
    done = fx.action("unlinkyes", task, token=token)

    assert done.text.startswith("✅ Telegram клиента отвязан.")
    statuses = {s.telegram_user_id: s.status for s in fx.subscriptions.list_by_car_number(_CAR)}
    assert statuses[_CLIENT] == "archived" and statuses[_STRANGER] == "active"
    assert [t.status for t in fx.tasks.list_active()] == ["active"]
    assert len(fx.fines.list_by_car_number(_CAR)) == fines_before
    assert fx.names.get(_CAR) == "Иван Иванов"


async def test_stale_unlink_button_does_not_touch_later_links(fx):
    task = await fx.added_car()
    fx.known_user(_CLIENT, "ivan123")
    fx.known_user(_STRANGER, "second_client")
    await fx.link_later(task, "@ivan123")
    prompt = fx.action("unlink", task)
    token = prompt.task_client_confirm[3]
    # После экрана подтверждения менеджер привязал ещё одного клиента.
    await fx.link_later(task, "@second_client")

    stale = fx.action("unlinkyes", task, token=token)
    assert stale.text.startswith(texts.TASK_CLIENT_STALE_TEXT)  # другой экран закрыл этот

    prompt = fx.action("unlink", task)
    first_ids = {s.id for s in fx.service.manager_client_links(task.id)}
    fx.known_user(4343, "third_client")
    # Привязка в обход экранов (другой менеджер): этот экран её не видел.
    await fx.service.link_client_to_task(
        task.id, created_by_telegram_user_id=_TRUSTED, created_by_telegram_chat_id=_TRUSTED,
        owner_username="third_client", today=date.today(),
    )
    done = fx.action("unlinkyes", task, token=prompt.task_client_confirm[3])
    again = fx.action("unlinkyes", task, token=prompt.task_client_confirm[3])

    assert done.text.startswith("✅ Telegram клиента отвязан.")
    statuses = {s.id: s.status for s in fx.subscriptions.list_by_car_number(_CAR)}
    assert all(statuses[i] == "archived" for i in first_ids)
    [third] = fx.subscriptions.list_by_user(4343)
    assert third.status == "active"
    assert again.text.startswith(texts.TASK_CLIENT_STALE_TEXT)


@pytest.mark.parametrize("stale_action", ["linkyes", "unlinkyes"])
async def test_stale_confirm_button_closes_the_open_input_step(fx, stale_action):
    task_a = await fx.added_car()
    task_b = await fx.added_car(car=_OTHER_CAR)
    fx.action("name", task_a)

    if stale_action == "linkyes":
        stale = await fx.controller.handle_task_client_link_confirm(
            task_b.id, 0, chat_id=_TRUSTED, telegram_user_id=_TRUSTED, token="deadbeef",
        )
    else:
        stale = fx.action("unlinkyes", task_b, token="deadbeef")
    await fx.text("Клиент 12345")

    assert stale.text.startswith(texts.TASK_CLIENT_STALE_TEXT) and stale.trusted_task_detail_id == task_b.id
    assert fx.names.get(_CAR) is None and fx.names.get(_OTHER_CAR) is None



async def test_client_actions_are_manager_only(fx):
    task = await fx.added_car()
    for action in keyboards.TASK_CLIENT_ACTIONS:
        assert fx.controller.handle_task_client_action(
            action, task.id, 0, chat_id=_STRANGER, telegram_user_id=_STRANGER, token="abcd1234",
        ) is None
    assert await fx.controller.handle_task_client_link_confirm(
        task.id, 0, chat_id=_STRANGER, telegram_user_id=_STRANGER, token="abcd1234",
    ) is None


# ---- Устаревшие шаги ввода ----


class _FakeClient:
    def __init__(self):
        self.callback_handler = None

    def on(self, event_filter):
        def decorator(func):
            if isinstance(event_filter, events.CallbackQuery):
                self.callback_handler = func
            return func

        return decorator


class _CallbackEvent:
    def __init__(self, data: bytes, sender_id: int):
        self.data = data
        self.sender_id = sender_id
        self.chat_id = sender_id
        self.is_private = True

    async def get_sender(self):
        return None

    async def answer(self, *args, **kwargs):
        pass

    async def edit(self, text, *, buttons=None):
        pass

    async def respond(self, text, *, buttons=None):
        pass


async def test_opening_another_car_closes_the_name_step(fx):
    task_a = await fx.added_car()
    task_b = await fx.added_car(car=_OTHER_CAR)
    client = _FakeClient()
    register(client, fx.controller, known_users_repository=fx.known)
    fx.action("name", task_a)

    await client.callback_handler(_CallbackEvent(keyboards.encode_trusted_task_open_callback(task_b.id, 0), _TRUSTED))
    await fx.text("AA123BB")

    assert fx.names.get(_CAR) is None and fx.names.get(_OTHER_CAR) is None


async def test_paging_closes_the_telegram_step(fx):
    task = await fx.added_car()
    client = _FakeClient()
    register(client, fx.controller, known_users_repository=fx.known)
    fx.action("tg", task)

    await client.callback_handler(_CallbackEvent(keyboards.encode_trusted_tasks_page_callback(0), _TRUSTED))
    reply = await fx.text("@AA123BB")

    assert reply.task_client_confirm is None
    assert fx.subscriptions.list_by_car_number(_CAR) == []


async def test_old_input_step_expires(fx):
    task = await fx.added_car()
    fx.action("name", task)
    fx.age_state(_TRUSTED, 20)

    reply = await fx.text("Клиент 12345")

    assert reply.text == texts.STALE_DIALOG_TEXT and fx.names.get(_CAR) is None


async def test_a_fresh_input_step_still_works(fx):
    task = await fx.added_car()
    fx.action("name", task)
    fx.age_state(_TRUSTED, 5)
    assert fx.states.get(_TRUSTED).step == STEP_AWAITING_TASK_CLIENT_NAME

    reply = await fx.text("Клиент 12345")

    assert reply.text.startswith(texts.TASK_CLIENT_NAME_SAVED_TEXT)


async def test_without_the_feature_the_task_card_is_unchanged(tmp_path):
    fixture = _Fixture(tmp_path, with_names=False)
    try:
        task = await fixture.added_car()
        card = fixture.controller.handle_trusted_task_open(task.id, 0, telegram_user_id=_TRUSTED)
        assert card.text == texts.format_trusted_task_detail(fixture.tasks.get(task.id))
        assert card.trusted_task_client_buttons is False
    finally:
        fixture.close()


# ---- Статистика ----


async def test_statistics_row_shows_the_client_name(fx):
    await fx.added_car(name="Александр-2")

    text = fx.controller._format_statistics_reply().text

    row = text[text.index(f"🚗 Госномер: {format_plate_for_telegram(_CAR)}"):]
    assert "👤 Клиент: Александр-2\n🔗 Telegram: не привязан" in row
    assert texts.DEBT_OWNER_NOT_LINKED_LINE not in row


def test_statistics_row_keeps_linked_user_lines_under_the_client_name():
    row = texts.format_debt_row(
        car_number=_CAR, owners=[texts.DebtOwner(name="Ivan", username="ivan123")], total_amount=50.0,
        client_name="Иван Иванов",
    )
    assert row.splitlines()[2:] == ["👤 Клиент: Иван Иванов", "👤 Пользователь: Ivan", "🔗 Telegram: @ivan123"]


# ---- callback_data ----


def test_task_client_callbacks_roundtrip_and_reject_garbage():
    data = keyboards.encode_task_client_callback("unlinkyes", 1234567, 99, "0a1b2c3d")
    assert len(data) <= 64
    assert keyboards.decode_task_client_callback(data) == ("unlinkyes", 1234567, 99, "0a1b2c3d")
    assert keyboards.decode_task_client_callback(keyboards.encode_task_client_callback("name", 5, 0)) == ("name", 5, 0, None)
    for garbage in (
        b"tcl:", b"tcl:drop:1:0", b"tcl:name:x:0", b"tcl:name:1", b"tcl:name:1:0:9",
        b"tcl:unlinkyes:1:0", b"tcl:linkyes:1:0", b"tcl:linkyes:1:0:!!", None,
    ):
        assert keyboards.decode_task_client_callback(garbage) is None
    name_button = keyboards.add_car_confirm_keyboard(_CAR, client_name_button=True)[1][0]
    assert keyboards.decode_add_car_client_name_callback(name_button.data) == _CAR
    assert keyboards.decode_add_car_confirm_callback(name_button.data) is None
