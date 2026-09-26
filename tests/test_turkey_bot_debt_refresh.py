"""Тесты ConversationController.handle_debt_refresh_pick/confirm/cancel и
"🔄 Проверить авто с задолженностью" в 📊 Статистика (@ProtocolTRbot,
задача "manual Turkey debt refresh") — сквозные, через РЕАЛЬНЫЙ
ConversationController + register()/handlers.py (для проверки Telegram
callback UX, см. задачу TESTS п.17)."""

import sys
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.turkey_bot import texts
from reader.turkey_bot.avrasya.models import AvrasyaSubmitOutcome
from reader.turkey_bot.conversation import ConversationController
from reader.turkey_bot.conversation_state_repository import (
    TurkeyConversationStateRepository,
)
from reader.turkey_bot.debt_refresh_service import TurkeyDebtRefreshService
from reader.turkey_bot.gib.models import (
    CaptchaChallenge,
    GibFineRecord,
    GibSubmitOutcome,
)
from reader.turkey_bot.handlers import register
from reader.turkey_bot.kgm.models import KgmCaptchaChallenge, KgmSubmitOutcome
from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.statistics_service import TurkeyStatisticsService
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository

pytestmark = pytest.mark.asyncio

_TRUSTED_ID = 5712994689
_ORDINARY_ID = 685137235
_TZ = ZoneInfo("UTC")


class _FakeCloseable:
    async def aclose(self) -> None:
        pass


class _FakeGibProvider:
    def __init__(self, *, outcome=None):
        self._outcome = outcome

    async def start(self):
        return CaptchaChallenge(image_id="cid-1", image_png=b"GIB-PNG")

    async def submit(self, *, plate, image_id, captcha_code):
        return self._outcome


class _FakeAvrasyaProvider:
    async def start(self):
        from reader.turkey_bot.avrasya.models import AvrasyaCaptchaChallenge
        return AvrasyaCaptchaChallenge(image_png=b"AVRASYA-PNG")

    async def submit(self, *, plate, captcha_code):
        return AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None)


class _FakeKgmProvider:
    async def start(self):
        return KgmCaptchaChallenge(image_png=b"KGM-PNG")

    async def submit(self, *, plate, captcha_code):
        return KgmSubmitOutcome(kind="no_debt", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0))


class _FakeCaptchaResolver:
    async def resolve(self, *, provider, image_png):
        return "ABCDE"


class _PoisonGibProvider:
    """Если бы open Statistics/pick когда-либо дошёл до реального check —
    это упало бы прямо здесь."""

    async def start(self):
        raise AssertionError("UNEXPECTED provider request from Statistics/pick")


def _make_check_service(*, gib_amount: Decimal = Decimal(0), poison: bool = False) -> UnifiedTurkeyCheckService:
    gib_outcome = (
        GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None)
        if gib_amount <= 0 else
        GibSubmitOutcome(
            kind="has_debt", messages=(), raw_data=None,
            fines=(GibFineRecord(
                protocol_no="P1", plate="X", amount=gib_amount, description="d",
                violation_date=None, authority=None, late_fee=None, discount=None,
            ),),
        )
    )
    gib_provider = _PoisonGibProvider() if poison else _FakeGibProvider(outcome=gib_outcome)
    return UnifiedTurkeyCheckService(
        lambda: (_FakeCloseable(), gib_provider),
        lambda: (_FakeCloseable(), _FakeAvrasyaProvider()),
        lambda: (_FakeCloseable(), _FakeKgmProvider()),
        captcha_resolver=_FakeCaptchaResolver(),
    )


class _Fixture:
    def __init__(self, *, poison_check_service: bool = False, with_debt_refresh: bool = True):
        self.states = TurkeyConversationStateRepository(":memory:")
        self.garage = TurkeyUserCarsRepository(":memory:")
        self.runs = TurkeyCheckRunRepository(":memory:")
        self.subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
        self.known_users = TurkeyBotKnownUsersRepository(":memory:")
        self.statistics = TurkeyStatisticsService(self.known_users, self.runs, self.subscriptions)
        # poison_check_service=True — см. задачу TESTS п.1/п.3/п.16:
        # открытие Статистики/первое нажатие/пустой список НЕ должны
        # доходить до реального provider request — если бы дошли,
        # _PoisonGibProvider.start() бросит AssertionError, а не тихо
        # пройдёт.
        self.check_service = _make_check_service(poison=poison_check_service)
        self.debt_refresh = (
            TurkeyDebtRefreshService(self.garage, self.runs, self.check_service, self.statistics)
            if with_debt_refresh else None
        )
        self.controller = ConversationController(
            self.states, self.garage, self.runs, self.subscriptions, self.statistics, self.check_service,
            trusted_operator_user_ids=frozenset({_TRUSTED_ID}), tz=_TZ,
            debt_refresh_service=self.debt_refresh,
        )

    def add_car(self, *, telegram_user_id: int, car_number: str) -> None:
        self.garage.add_car(telegram_user_id=telegram_user_id, car_number=car_number)

    async def seed(self, *, telegram_user_id: int, plate: str, amount: Decimal) -> None:
        seed_service = _make_check_service(gib_amount=amount)
        result = await seed_service.check(plate)
        self.runs.save(result, telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, initiator="manual")
        self.garage.update_last_result(
            telegram_user_id=telegram_user_id, car_number=plate,
            overall_status=result.overall_status.value, total_amount=result.total_amount,
        )


@pytest.fixture
def fx():
    return _Fixture(poison_check_service=True)


# ---- 1. Statistics open -> ZERO provider calls ----


async def test_statistics_open_makes_zero_provider_calls(fx):
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(500))

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    assert "AA001AA" in "".join((reply.text, *reply.extra_texts))
    assert reply.debt_refresh_available is True


# ---- 2. Refresh button trusted-only ----


async def test_ordinary_user_cannot_pick_or_confirm_or_cancel(fx):
    pick = fx.controller.handle_debt_refresh_pick(telegram_user_id=_ORDINARY_ID)
    cancel = fx.controller.handle_debt_refresh_cancel(telegram_user_id=_ORDINARY_ID)
    confirm = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_ORDINARY_ID)

    assert pick.text == texts.SEARCH_NOT_AUTHORIZED_TEXT
    assert cancel.text == texts.SEARCH_NOT_AUTHORIZED_TEXT
    assert confirm.text == texts.SEARCH_NOT_AUTHORIZED_TEXT


async def test_statistics_hides_button_for_ordinary_user(fx):
    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID)
    assert "Задолженность" not in reply.text


# ---- 3. First click -> confirmation only, ZERO provider calls ----


async def test_pick_shows_confirmation_without_any_provider_call(fx):
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    fx.add_car(telegram_user_id=2, car_number="BB002BB")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(500))
    await fx.seed(telegram_user_id=2, plate="BB002BB", amount=Decimal(700))

    reply = fx.controller.handle_debt_refresh_pick(telegram_user_id=_TRUSTED_ID)

    assert reply.text == "🔄 Будет проверено автомобилей: 2"
    assert reply.debt_refresh_confirm_car_count == 2


# ---- 16. No debt cars -> ZERO provider calls ----


async def test_pick_with_no_debt_cars_makes_zero_provider_calls(fx):
    reply = fx.controller.handle_debt_refresh_pick(telegram_user_id=_TRUSTED_ID)
    assert reply.text == texts.DEBT_REFRESH_NONE_TEXT
    assert reply.debt_refresh_confirm_car_count is None


async def test_confirm_without_debt_service_falls_back_safely(tmp_path):
    fx = _Fixture(with_debt_refresh=False)
    reply = fx.controller.handle_debt_refresh_pick(telegram_user_id=_TRUSTED_ID)
    assert reply.text == texts.SEARCH_NOT_AUTHORIZED_TEXT


# ---- Confirm actually runs a real (fake) check + honest, stateful result ----


async def test_confirm_runs_refresh_and_returns_simplified_summary():
    """См. задачу "manager Statistics / refresh для обоих ботов" п.7 —
    БЕЗ "Задолженность погашена"/"Задолженность осталась"/Было-Стало:
    только "Проверено"/"Не удалось проверить" + заново построенный
    debt-блок."""
    fx = _Fixture(poison_check_service=False)
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(2740))
    # check_service по умолчанию возвращает no_debt для GİB -> машина погашена.

    reply = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)

    assert "🔄 Проверка завершена" in reply.text
    assert "Проверено: 1" in reply.text
    assert "⚠️ Не удалось проверить: 0" in reply.text
    assert "Задолженность погашена" not in reply.text
    assert "Задолженность осталась" not in reply.text
    assert "Было" not in reply.text
    assert "Стало" not in reply.text
    assert "🚨 Задолженность по последней проверке" in reply.text
    assert "Автомобилей: 0" in reply.text
    assert "AA001AA" not in reply.text


async def test_confirm_row_format_is_car_colon_owner_colon_amount():
    fx = _Fixture(poison_check_service=False)
    fx.add_car(telegram_user_id=1, car_number="BB002BB")
    await fx.seed(telegram_user_id=1, plate="BB002BB", amount=Decimal(1000))
    fx.check_service = _make_check_service(gib_amount=Decimal(1800))
    fx.debt_refresh._check_service = fx.check_service

    reply = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)

    full_text = "\n".join((reply.text, *reply.extra_texts))
    assert "🚗 BB002BB: —: 1 800 ₺" in full_text


async def test_next_refresh_candidates_exclude_car_that_became_zero():
    """См. задачу п.5/п.9 — после refresh (2740 -> 0) следующий pick
    должен предложить проверить на 1 меньше машину, а следующий confirm —
    её не трогать вовсе."""
    fx = _Fixture(poison_check_service=False)
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    fx.add_car(telegram_user_id=2, car_number="BB002BB")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(2740))
    await fx.seed(telegram_user_id=2, plate="BB002BB", amount=Decimal(500))
    # check_service по умолчанию возвращает no_debt для GİB -> обе "погашены"
    # на первом refresh (это ОК для этого теста — важен именно факт, что
    # AA001AA больше не кандидат на втором refresh).

    await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)

    pick_reply = fx.controller.handle_debt_refresh_pick(telegram_user_id=_TRUSTED_ID)
    assert pick_reply.text == texts.DEBT_REFRESH_NONE_TEXT


# ---- 18. Statistics after refresh reflects updated persisted data ----


async def test_statistics_after_refresh_reflects_updated_data():
    fx = _Fixture(poison_check_service=False)
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(2740))

    before = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)
    assert "AA001AA" in "".join((before.text, *before.extra_texts))

    await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)

    after = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)
    assert "AA001AA" not in "".join((after.text, *after.extra_texts))
    assert "Автомобилей: 0" in after.text


# ---- 17. Callback query answered correctly (не бесконечный spinner) ----


class _FakeTelethonClient:
    def __init__(self):
        self.callback_handler = None
        self.message_handler = None

    def on(self, event_filter):
        from telethon import events

        def decorator(func):
            if isinstance(event_filter, events.CallbackQuery):
                self.callback_handler = func
            elif isinstance(event_filter, events.NewMessage):
                self.message_handler = func
            return func

        return decorator


class _FakeCallbackEvent:
    def __init__(self, *, data: bytes, sender_id: int, chat_id: int):
        self.data = data
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.is_private = True
        self.calls: list[tuple] = []

    async def get_sender(self):
        return None

    async def answer(self, *args, **kwargs):
        self.calls.append(("answer", args, kwargs))

    async def edit(self, text, *, buttons=None):
        self.calls.append(("edit", text, buttons))

    async def respond(self, text, *, buttons=None):
        self.calls.append(("respond", text, buttons))


async def test_debt_refresh_pick_callback_is_answered():
    fx = _Fixture(poison_check_service=True)
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(500))

    client = _FakeTelethonClient()
    register(client, fx.controller, known_users_repository=None)
    assert client.callback_handler is not None

    event = _FakeCallbackEvent(data=b"turkeydebtrefreshpick", sender_id=_TRUSTED_ID, chat_id=_TRUSTED_ID)
    await client.callback_handler(event)

    call_names = [c[0] for c in event.calls]
    assert "answer" in call_names


async def test_debt_refresh_confirm_callback_answered_before_slow_refresh_response():
    """См. задачу "TELEGRAM CALLBACK UX" — event.answer() должен прийти
    ДО отправки финального результата (который может занять долгое время
    при большом числе машин/captcha retries), чтобы Telegram-клиент не
    показывал спиннер кнопки всё это время."""
    fx = _Fixture(poison_check_service=False)
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(500))

    client = _FakeTelethonClient()
    register(client, fx.controller, known_users_repository=None)

    event = _FakeCallbackEvent(data=b"turkeydebtrefreshyes", sender_id=_TRUSTED_ID, chat_id=_TRUSTED_ID)
    await client.callback_handler(event)

    call_names = [c[0] for c in event.calls]
    assert "answer" in call_names
    assert "respond" in call_names
    assert call_names.index("answer") < call_names.index("respond")


async def test_debt_refresh_cancel_callback_is_answered():
    fx = _Fixture(poison_check_service=True)
    client = _FakeTelethonClient()
    register(client, fx.controller, known_users_repository=None)

    event = _FakeCallbackEvent(data=b"turkeydebtrefreshno", sender_id=_TRUSTED_ID, chat_id=_TRUSTED_ID)
    await client.callback_handler(event)

    call_names = [c[0] for c in event.calls]
    assert "answer" in call_names
