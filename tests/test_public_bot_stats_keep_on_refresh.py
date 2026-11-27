"""📊 Статистика остаётся в чате при "🔄 Проверить авто со штрафами".

Сквозные тесты через настоящие handlers.register + ConversationController
поверх SQLite/tmp_path. Фейковый чат запоминает каждое сообщение и его
кнопки; нажатие кнопки — callback-событие ИМЕННО на этом сообщении (edit
меняет его, respond добавляет новое), так что видно, какое сообщение
поменялось.
"""

import asyncio
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from telethon import events  # noqa: E402
from telethon.errors.rpcerrorlist import MessageNotModifiedError  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import ParsedFineRecord  # noqa: E402
from reader.fines.provider import FineProvider, FineProviderError  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot import keyboards, texts  # noqa: E402
from reader.public_bot.conversation import (  # noqa: E402
    DEBT_REPORT_EXPIRED,
    ConversationController,
    _DebtReport,
    _DebtReportStore,
)
from reader.public_bot.conversation_state_repository import BotConversationStateRepository  # noqa: E402
from reader.public_bot.debt_refresh_service import DebtRefreshService  # noqa: E402
from reader.public_bot.handlers import _send_reply, register  # noqa: E402
from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402
from reader.public_bot.statistics_service import BotStatisticsService  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import SubscriptionService  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_TBILISI = ZoneInfo("Asia/Tbilisi")
_TRUSTED_ID = 5712994689
_ORDINARY_ID = 685137235
_GENERATED_AT_RE = re.compile(r"🕒 Сформировано: \d\d\.\d\d в \d\d:\d\d")


async def _instant_sleep(_seconds: float) -> None:
    pass


class _Provider(FineProvider):
    def __init__(self):
        self.amounts: dict[str, float] = {}
        self.requested: list[str] = []
        self.fail = False
        self.gate: asyncio.Event | None = None

    async def search_by_plate(self, plate: str):
        self.requested.append(plate)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise FineProviderError("police.ge unavailable")
        amount = self.amounts.get(plate, 0)
        if amount <= 0:
            return []
        fingerprint = f"fp-{plate}"
        return [ParsedFineRecord(
            car_number=plate, external_fine_id=fingerprint,
            penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
            delivered_status="Не вручено", fingerprint=fingerprint,
            raw_data={"protocolNo": fingerprint}, amount=amount,
        )]


class _Fixture:
    def __init__(self, tmp_path):
        db = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(db)
        self.detected_fine_repository = DetectedFineRepository(db)
        self.subscription_repository = FineSubscriptionRepository(db)
        self.user_repository = UserRepository(db)
        self.state_repository = BotConversationStateRepository(db)
        self.known_users_repository = BotKnownUsersRepository(db)
        self.provider = _Provider()
        self.check_service = FineCheckService(
            self.provider, self.task_repository, self.detected_fine_repository, sleep=_instant_sleep,
        )
        self.controller = ConversationController(
            self.state_repository,
            SubscriptionService(
                self.task_repository, self.subscription_repository, self.user_repository, self.check_service,
            ),
            BotStatisticsService(
                self.known_users_repository, self.subscription_repository, self.detected_fine_repository,
            ),
            self.known_users_repository, tz=_TBILISI,
            trusted_operator_user_ids=frozenset({_TRUSTED_ID}),
            debt_refresh_service=DebtRefreshService(self.task_repository, self.check_service, sleep=_instant_sleep),
        )
        self.client = _FakeClient()
        register(self.client, self.controller, known_users_repository=self.known_users_repository)
        self.chat = _Chat()

    async def seed(self, amounts: dict[str, float]) -> None:
        for car_number, amount in amounts.items():
            task = self.task_repository.create(
                car_number=car_number, label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
                telegram_chat_id=-100, created_by_user_id=1,
            )
            self.provider.amounts[car_number] = amount
            await self.check_service.check_task(task)
        self.provider.requested.clear()

    async def open_statistics(self) -> "_Message":
        reply = await self.controller.handle_text(
            texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        await _send_reply(_Event(self.chat, None, b"", _TRUSTED_ID), reply, is_trusted=True)
        return self.chat.messages[-1]

    async def press(self, message: "_Message", label: str, *, sender_id: int = _TRUSTED_ID, **event_kwargs) -> "_Event":
        event = _Event(self.chat, message, message.data_of(label), sender_id, **event_kwargs)
        await self.client.callback_handler(event)
        return event

    def close(self):
        for repository in (
            self.task_repository, self.detected_fine_repository, self.subscription_repository,
            self.user_repository, self.state_repository, self.known_users_repository,
        ):
            repository.close()


class _FakeClient:
    def __init__(self):
        self.callback_handler = None

    def on(self, event_filter):
        def decorator(func):
            if isinstance(event_filter, events.CallbackQuery):
                self.callback_handler = func
            return func

        return decorator


class _Message:
    def __init__(self, text: str, buttons):
        self.text = text
        self.buttons = buttons
        self.edits = 0

    def labels(self) -> list[str]:
        return [button.text for row in (self.buttons or []) for button in row]

    def data_of(self, label: str) -> bytes:
        for row in self.buttons or []:
            for button in row:
                if button.text.startswith(label):
                    return button.data
        raise AssertionError(f"нет кнопки {label!r} среди {self.labels()}")


class _Chat:
    def __init__(self):
        self.messages: list[_Message] = []

    def post(self, text: str, buttons) -> _Message:
        message = _Message(text, buttons)
        self.messages.append(message)
        return message


class _Event:
    """Callback-событие на конкретном сообщении чата."""

    def __init__(self, chat: _Chat, message: _Message | None, data: bytes, sender_id: int, *, not_modified=False):
        self.chat = chat
        self.message = message
        self.data = data
        self.sender_id = sender_id
        self.chat_id = sender_id
        self.is_private = True
        self.answers: list[tuple] = []
        self._not_modified = not_modified

    async def get_sender(self):
        return None

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit(self, text, *, buttons=None):
        if self._not_modified:
            raise MessageNotModifiedError(request=None)
        self.message.text = text
        self.message.buttons = buttons
        self.message.edits += 1

    async def respond(self, text, *, buttons=None):
        return self.chat.post(text, buttons)


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


def _snapshot(message: _Message) -> tuple[str, list[str], int]:
    return message.text, message.labels(), message.edits


async def test_statistics_stay_after_pick_confirm_and_result(fx):
    await fx.seed({"AA111AA": 800, "BB222BB": 100})
    statistics = await fx.open_statistics()
    before = _snapshot(statistics)
    assert "800 ₾" in statistics.text
    assert _GENERATED_AT_RE.search(statistics.text)

    await fx.press(statistics, texts.DEBT_REFRESH_BUTTON_LABEL)
    prompt = fx.chat.messages[-1]
    assert prompt is not statistics
    assert prompt.text == texts.format_debt_refresh_prompt(2)
    assert fx.provider.requested == []  # подтверждение ничего не проверяет
    assert _snapshot(statistics) == before

    fx.provider.amounts["AA111AA"] = 500
    await fx.press(prompt, texts.DEBT_REFRESH_CONFIRM_BUTTON_LABEL)
    result = fx.chat.messages[-1]
    assert result is not statistics and result is not prompt
    assert result.text.startswith("🔄 Проверка завершена\n🕒 Сформировано: ")
    assert "500 ₾" in result.text and "800 ₾" not in result.text
    assert texts.DEBT_REFRESH_BUTTON_LABEL in result.labels()

    # Отчёт до проверки — без изменений: те же данные и кнопки.
    assert _snapshot(statistics) == before
    assert len(fx.chat.messages) == 3


async def test_cancel_keeps_statistics_and_checks_nothing(fx):
    await fx.seed({"AA111AA": 250})
    statistics = await fx.open_statistics()
    before = _snapshot(statistics)

    await fx.press(statistics, texts.DEBT_REFRESH_BUTTON_LABEL)
    prompt = fx.chat.messages[-1]
    await fx.press(prompt, "❌ Отмена")

    assert _snapshot(statistics) == before
    assert fx.provider.requested == []
    assert texts.DEBT_REFRESH_CANCELLED_TEXT in [message.text for message in fx.chat.messages]


async def test_failed_check_keeps_statistics(fx):
    await fx.seed({"AA111AA": 250})
    statistics = await fx.open_statistics()
    before = _snapshot(statistics)

    fx.provider.fail = True
    await fx.press(statistics, texts.DEBT_REFRESH_BUTTON_LABEL)
    await fx.press(fx.chat.messages[-1], texts.DEBT_REFRESH_CONFIRM_BUTTON_LABEL)

    result = fx.chat.messages[-1]
    assert "⚠️ Не удалось проверить: 1" in result.text
    assert _snapshot(statistics) == before


async def test_old_report_pages_keep_their_own_data_after_refresh(fx):
    cars = {f"AA{index:03d}AA": 100 for index in range(11)}  # 11 машин — две страницы
    await fx.seed(cars)
    statistics = await fx.open_statistics()
    first_page = statistics.text
    assert "1 / 2" in statistics.labels()

    await fx.press(statistics, "Вперёд")
    second_page_before = statistics.text
    assert second_page_before != first_page
    await fx.press(statistics, "◀️ Назад")
    assert statistics.text == first_page

    for car_number in cars:
        fx.provider.amounts[car_number] = 300
    await fx.press(statistics, texts.DEBT_REFRESH_BUTTON_LABEL)
    await fx.press(fx.chat.messages[-1], texts.DEBT_REFRESH_CONFIRM_BUTTON_LABEL)
    result = fx.chat.messages[-1]
    assert "300 ₾" in result.text

    # Старый отчёт листается своими страницами — данные до проверки.
    await fx.press(statistics, "Вперёд")
    assert statistics.text == second_page_before
    assert "300 ₾" not in statistics.text
    await fx.press(statistics, "◀️ Назад")
    assert statistics.text == first_page

    # Новый отчёт листается своими — данные после проверки.
    await fx.press(result, "Вперёд")
    assert "300 ₾" in result.text and "100 ₾" not in result.text
    assert result.text.startswith("🔄 Проверка завершена\n🕒 Сформировано: ")


async def test_forgotten_report_answers_alert_and_keeps_message(fx):
    await fx.seed({f"AA{index:03d}AA": 100 for index in range(11)})
    statistics = await fx.open_statistics()
    before = _snapshot(statistics)
    fx.controller._debt_reports = _DebtReportStore()  # как после перезапуска бота

    event = await fx.press(statistics, "Вперёд")

    assert event.answers == [((texts.DEBT_REPORT_EXPIRED_TEXT,), {"alert": True})]
    assert _snapshot(statistics) == before
    assert len(fx.chat.messages) == 1


async def test_current_page_button_does_not_post_a_copy(fx):
    await fx.seed({f"AA{index:03d}AA": 100 for index in range(11)})
    statistics = await fx.open_statistics()

    await fx.press(statistics, "1 / 2", not_modified=True)

    assert len(fx.chat.messages) == 1


async def test_double_confirm_runs_one_check(fx):
    await fx.seed({"AA111AA": 250, "BB222BB": 100})
    statistics = await fx.open_statistics()
    await fx.press(statistics, texts.DEBT_REFRESH_BUTTON_LABEL)
    prompt = fx.chat.messages[-1]

    fx.provider.gate = asyncio.Event()
    first = asyncio.create_task(fx.press(prompt, texts.DEBT_REFRESH_CONFIRM_BUTTON_LABEL))
    await asyncio.sleep(0)
    await fx.press(prompt, texts.DEBT_REFRESH_CONFIRM_BUTTON_LABEL)
    fx.provider.gate.set()
    await first

    assert sorted(fx.provider.requested) == ["AA111AA", "BB222BB"]
    texts_posted = [message.text for message in fx.chat.messages]
    assert texts_posted.count(texts.DEBT_REFRESH_IN_PROGRESS_TEXT) == 1
    assert sum(text.startswith("🔄 Проверка завершена") for text in texts_posted) == 1


async def test_legacy_page_button_still_pages(fx):
    await fx.seed({f"AA{index:03d}AA": 100 for index in range(11)})
    message = fx.chat.post("старая статистика", [[
        keyboards.Button.inline("Вперёд ▶️", keyboards.encode_debt_list_page_callback(1)),
    ]])

    await fx.press(message, "Вперёд")

    assert "2 / 2" in message.labels()
    assert keyboards.decode_debt_report_page_callback(message.data_of("◀️ Назад")) is not None


async def test_report_page_is_trusted_only(fx):
    await fx.seed({"AA111AA": 250})
    statistics_reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )
    report_id = statistics_reply.debt_report_id

    assert fx.controller.handle_debt_report_page(report_id, 0, telegram_user_id=_ORDINARY_ID) is None
    assert fx.controller.handle_debt_report_page("0" * 8, 0, telegram_user_id=_TRUSTED_ID) is DEBT_REPORT_EXPIRED
    assert fx.controller.handle_debt_report_page(report_id, 0, telegram_user_id=_TRUSTED_ID).text == statistics_reply.text


def test_generated_at_line_uses_tbilisi_time():
    moment = datetime(2026, 11, 27, 15, 25, tzinfo=timezone.utc)
    assert texts.format_generated_at(moment, tz=_TBILISI) == "🕒 Сформировано: 27.11 в 19:25"


def test_report_page_callback_roundtrip_and_rejects_garbage():
    data = keyboards.encode_debt_report_page_callback("0a1b2c3d", 12)
    assert len(data) <= 64
    assert keyboards.decode_debt_report_page_callback(data) == ("0a1b2c3d", 12)
    assert keyboards.decode_debt_list_page_callback(data) is None
    assert keyboards.decode_debt_report_page_callback(keyboards.encode_debt_list_page_callback(1)) is None
    for garbage in (b"debtpg:", b"debtpg:0a1b2c3d", b"debtpg:0a1b2c3d:-1", b"debtpg:XYZ:1", b"debtpg:0a1b:1", None):
        assert keyboards.decode_debt_report_page_callback(garbage) is None


def test_report_store_keeps_only_the_latest_reports():
    store = _DebtReportStore(limit=2)
    first = store.add(_DebtReport(header="a", summary="", pages=()))
    second = store.add(_DebtReport(header="b", summary="", pages=()))
    third = store.add(_DebtReport(header="c", summary="", pages=()))

    assert store.get(first) is None
    assert store.get(second).header == "b"
    assert store.get(third).header == "c"


def test_report_render_clamps_page():
    report = _DebtReport(header="H", summary="S", pages=("p1", "p2"))
    assert report.render(5) == ("H\n\nS\n\np2", 1, 2)
    assert report.render(-1) == ("H\n\nS\n\np1", 0, 2)
    assert _DebtReport(header="H", summary="S", pages=()).render(0) == ("H\n\nS", None, None)
