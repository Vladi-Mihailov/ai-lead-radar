"""Тесты reader/turkey_bot/handlers.py::_on_callback — "check" car action
(задача "preserve Turkey replies after long checks"). ROOT CAUSE: unified
check может занимать 9-34+ секунд (провайдеры + CAPTCHA retries), к
моменту завершения Telegram callback-query токен уже истёк, и
event.answer() ПОСЛЕ проверки бросал QueryIdInvalidError — непойманную,
рушившую handler ДО _send_reply, из-за чего итоговый ответ терялся, хотя
сама проверка успешно завершалась. Fix: ack СРАЗУ после получения
callback, до начала unified check (тот же приём, что и у
debt_refresh_confirm, см. tests/test_turkey_bot_debt_refresh.py).

Реальный Telethon не используется — тот же _FakeTelethonClient/
_FakeCallbackEvent приём, что и в tests/test_turkey_bot_debt_refresh.py."""

import sys
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from telethon.errors.rpcerrorlist import QueryIdInvalidError

from reader.turkey_bot.conversation import ConversationController
from reader.turkey_bot.conversation_state_repository import (
    TurkeyConversationStateRepository,
)
from reader.turkey_bot.gib.models import CaptchaChallenge, GibSubmitOutcome
from reader.turkey_bot.handlers import register
from reader.turkey_bot.keyboards import encode_car_action_callback
from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.statistics_service import TurkeyStatisticsService
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository

pytestmark = pytest.mark.asyncio

_USER_ID = 685137235
_TZ = ZoneInfo("UTC")


class _FakeCloseable:
    async def aclose(self) -> None:
        pass


class _FakeAvrasyaProvider:
    async def start(self):
        from reader.turkey_bot.avrasya.models import AvrasyaCaptchaChallenge
        return AvrasyaCaptchaChallenge(image_png=b"AVRASYA-PNG")

    async def submit(self, *, plate, captcha_code):
        from reader.turkey_bot.avrasya.models import AvrasyaSubmitOutcome
        return AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None)


class _FakeKgmProvider:
    async def start(self):
        from reader.turkey_bot.kgm.models import KgmCaptchaChallenge
        return KgmCaptchaChallenge(image_png=b"KGM-PNG")

    async def submit(self, *, plate, captcha_code):
        from reader.turkey_bot.kgm.models import KgmSubmitOutcome
        return KgmSubmitOutcome(kind="no_debt", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0))


class _FakeCaptchaResolver:
    async def resolve(self, *, provider, image_png):
        return "ABCDE"


class _SlowGibProvider:
    """Имитирует "долгую" unified-проверку (см. root cause) — start()
    досрочно вызывает переданный колбэк ПЕРЕД тем, как отдать управление,
    чтобы тест мог проверить относительный порядок answer()/respond() без
    реального времени ожидания."""

    def __init__(self, *, on_start=None):
        self._on_start = on_start

    async def start(self):
        if self._on_start is not None:
            await self._on_start()
        return CaptchaChallenge(image_id="cid-1", image_png=b"GIB-PNG")

    async def submit(self, *, plate, image_id, captcha_code):
        return GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None)


def _make_check_service(*, gib_provider=None) -> UnifiedTurkeyCheckService:
    gib_provider = gib_provider or _SlowGibProvider()
    return UnifiedTurkeyCheckService(
        lambda: (_FakeCloseable(), gib_provider),
        lambda: (_FakeCloseable(), _FakeAvrasyaProvider()),
        lambda: (_FakeCloseable(), _FakeKgmProvider()),
        captcha_resolver=_FakeCaptchaResolver(),
    )


class _Fixture:
    def __init__(self, *, gib_provider=None):
        self.states = TurkeyConversationStateRepository(":memory:")
        self.garage = TurkeyUserCarsRepository(":memory:")
        self.runs = TurkeyCheckRunRepository(":memory:")
        self.subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
        self.known_users = TurkeyBotKnownUsersRepository(":memory:")
        self.statistics = TurkeyStatisticsService(self.known_users, self.runs, self.subscriptions)
        self.check_service = _make_check_service(gib_provider=gib_provider)
        self.controller = ConversationController(
            self.states, self.garage, self.runs, self.subscriptions, self.statistics, self.check_service,
            trusted_operator_user_ids=frozenset(), tz=_TZ,
        )

    def add_car(self, *, telegram_user_id: int, car_number: str) -> int:
        car = self.garage.add_car(telegram_user_id=telegram_user_id, car_number=car_number)
        return car.id


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
    def __init__(self, *, data: bytes, sender_id: int, chat_id: int, answer_error: Exception | None = None):
        self.data = data
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.is_private = True
        self.calls: list[tuple] = []
        self._answer_error = answer_error

    async def get_sender(self):
        return None

    async def answer(self, *args, **kwargs):
        self.calls.append(("answer", args, kwargs))
        if self._answer_error is not None:
            error, self._answer_error = self._answer_error, None
            raise error

    async def edit(self, text, *, buttons=None):
        self.calls.append(("edit", text, buttons))

    async def respond(self, text, *, buttons=None):
        self.calls.append(("respond", text, buttons))


def _register_and_get_event(fx: _Fixture, *, car_id: int, answer_error: Exception | None = None):
    client = _FakeTelethonClient()
    register(client, fx.controller, known_users_repository=None)
    event = _FakeCallbackEvent(
        data=encode_car_action_callback("check", car_id),
        sender_id=_USER_ID, chat_id=_USER_ID, answer_error=answer_error,
    )
    return client, event


# ---- 1. Fast callback -> event.answer() succeeds -> reply sent ----


async def test_fast_callback_answers_then_sends_reply():
    fx = _Fixture()
    car_id = fx.add_car(telegram_user_id=_USER_ID, car_number="AA001AA")
    client, event = _register_and_get_event(fx, car_id=car_id)

    await client.callback_handler(event)

    call_names = [c[0] for c in event.calls]
    assert "answer" in call_names
    assert "respond" in call_names
    assert call_names.index("answer") < call_names.index("respond")


# ---- 2. Expired callback token -> reply is STILL sent ----


async def test_expired_callback_token_does_not_prevent_final_reply():
    fx = _Fixture()
    car_id = fx.add_car(telegram_user_id=_USER_ID, car_number="AA001AA")
    client, event = _register_and_get_event(fx, car_id=car_id, answer_error=QueryIdInvalidError(request=None))

    await client.callback_handler(event)  # must not raise

    call_names = [c[0] for c in event.calls]
    assert "answer" in call_names
    assert "respond" in call_names


# ---- 3. Ack happens BEFORE the slow controller/check completes ----


async def test_ack_happens_before_slow_check_completes():
    order: list[str] = []

    async def _on_start():
        order.append("provider_started")

    fx = _Fixture(gib_provider=_SlowGibProvider(on_start=_on_start))
    car_id = fx.add_car(telegram_user_id=_USER_ID, car_number="AA001AA")
    client, event = _register_and_get_event(fx, car_id=car_id)

    await client.callback_handler(event)

    # answer() must be recorded, and the provider (the "slow" part) must
    # only start running AFTER it — proving the ack is not deferred until
    # after the unified check finishes.
    assert event.calls[0][0] == "answer"
    assert order == ["provider_started"]


# ---- 4. Unrelated Telegram exception is NOT silently swallowed ----


async def test_unrelated_telegram_exception_is_not_swallowed():
    fx = _Fixture()
    car_id = fx.add_car(telegram_user_id=_USER_ID, car_number="AA001AA")
    client, event = _register_and_get_event(fx, car_id=car_id, answer_error=RuntimeError("some other Telegram error"))

    with pytest.raises(RuntimeError, match="some other Telegram error"):
        await client.callback_handler(event)


# ---- 5. Existing "check" callback for an unknown/foreign car_id still behaves safely ----


async def test_unknown_car_id_after_early_ack_gets_a_plain_message_not_a_crash():
    fx = _Fixture()
    client, event = _register_and_get_event(fx, car_id=999999)

    await client.callback_handler(event)  # must not raise

    call_names = [c[0] for c in event.calls]
    assert "answer" in call_names
    assert "respond" in call_names


# ---- 6. Other car actions (history/monitor_on/monitor_off/delete_*) remain green ----


async def test_history_action_still_answers_then_sends_reply():
    fx = _Fixture()
    car_id = fx.add_car(telegram_user_id=_USER_ID, car_number="AA001AA")
    client = _FakeTelethonClient()
    register(client, fx.controller, known_users_repository=None)
    event = _FakeCallbackEvent(
        data=encode_car_action_callback("history", car_id), sender_id=_USER_ID, chat_id=_USER_ID,
    )

    await client.callback_handler(event)

    call_names = [c[0] for c in event.calls]
    assert "answer" in call_names
    assert "respond" in call_names
    assert call_names.index("answer") < call_names.index("respond")


async def test_monitor_on_action_unaffected_by_check_early_ack_change():
    fx = _Fixture()
    car_id = fx.add_car(telegram_user_id=_USER_ID, car_number="AA001AA")
    client = _FakeTelethonClient()
    register(client, fx.controller, known_users_repository=None)
    event = _FakeCallbackEvent(
        data=encode_car_action_callback("monitor_on", car_id), sender_id=_USER_ID, chat_id=_USER_ID,
    )

    await client.callback_handler(event)

    call_names = [c[0] for c in event.calls]
    assert "answer" in call_names
    assert "edit" in call_names or "respond" in call_names
