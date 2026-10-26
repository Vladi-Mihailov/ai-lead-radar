"""Tests for the "⏳" immediate-feedback UX and the repeated-click guard on
Check Now (see "Fix the Georgia public bot Check Now flow") — the
_ack_hourglass_then_await_slow_check wiring in reader/public_bot/handlers.py
for exactly the three check-now-family callbacks (check_now, my_car_
check_now_once, trusted_check_now_once). The ack-ordering/QueryIdInvalidError
regression itself is covered by tests/test_public_bot_handlers_check_now_ack
.py (unaffected by this change, still green); this file covers the NEW
behavior layered on top of it, end to end through the real registered
callback handler (not just the SubscriptionService guard, which has its own
dedicated unit tests in tests/test_public_bot_subscription_service.py)."""

import asyncio
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from telethon import events

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.provider import FineProvider
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot.conversation import ConversationController
from reader.public_bot.conversation_state_repository import (
    BotConversationStateRepository,
)
from reader.public_bot.debt_refresh_service import DebtRefreshService
from reader.public_bot.handlers import register
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
_USER_ID = 5319195675


class _BlockingProvider(FineProvider):
    """search_by_plate() blocks on `gate` until released, setting `entered`
    the moment it's called — lets a test start a check, observe it "in
    flight" at the HANDLER level (not just the service level), then attempt
    a concurrent duplicate click."""

    def __init__(self, records_by_car=None):
        self._records_by_car = records_by_car or {}
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.search_calls: list[str] = []

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        self.search_calls.append(plate)
        self.entered.set()
        await self.gate.wait()
        return self._records_by_car.get(plate, [])


def _record(car_number="O127PX62", fingerprint="fp-1") -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number, external_fine_id="AB123456",
        penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
        delivered_status="Не вручено", fingerprint=fingerprint,
        raw_data={"protocolNo": "AB123456"},
    )


class _Fixture:
    def __init__(self, tmp_path, *, provider, trusted_operator_user_ids=frozenset()):
        db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(db_path)
        self.detected_fine_repository = DetectedFineRepository(db_path)
        self.subscription_repository = FineSubscriptionRepository(db_path)
        self.user_repository = UserRepository(db_path)
        self.conversation_state_repository = BotConversationStateRepository(db_path)
        self.known_users_repository = BotKnownUsersRepository(db_path)
        self.provider = provider
        self.check_service = FineCheckService(provider, self.task_repository, self.detected_fine_repository)
        self.subscription_service = SubscriptionService(
            self.task_repository, self.subscription_repository, self.user_repository, self.check_service,
        )
        self.statistics_service = BotStatisticsService(
            self.known_users_repository, self.subscription_repository, self.detected_fine_repository,
        )
        self.debt_refresh_service = DebtRefreshService(self.task_repository, self.check_service)
        self.controller = ConversationController(
            self.conversation_state_repository, self.subscription_service, self.statistics_service,
            self.known_users_repository, tz=_TBILISI,
            trusted_operator_user_ids=frozenset(trusted_operator_user_ids),
            debt_refresh_service=self.debt_refresh_service,
        )

    def close(self):
        self.task_repository.close()
        self.detected_fine_repository.close()
        self.subscription_repository.close()
        self.user_repository.close()
        self.conversation_state_repository.close()
        self.known_users_repository.close()

    def make_active_subscription(self, car_number="O127PX62", *, telegram_user_id=_USER_ID) -> tuple:
        task = self.task_repository.create(
            car_number=car_number, label=None,
            start_date=date(2026, 10, 21), end_date=date(2026, 11, 20),
            telegram_chat_id=telegram_user_id, created_by_user_id=telegram_user_id,
            monitoring_scope="client_bot",
        )
        subscription = self.subscription_repository.create(
            monitoring_task_id=task.id, car_number=car_number,
            telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id,
            telegram_username=None, start_date=task.start_date, end_date=task.end_date,
        )
        return task, subscription


class _FakeTelethonClient:
    def __init__(self):
        self.callback_handler = None

    def on(self, event_filter):
        def decorator(func):
            if isinstance(event_filter, events.CallbackQuery):
                self.callback_handler = func
            return func

        return decorator


class _FakeMessage:
    def __init__(self, calls_log: list[tuple], text: str, buttons):
        self._calls_log = calls_log
        self.text = text
        self.buttons = buttons

    async def edit(self, text, *, buttons=None):
        self._calls_log.append(("message_edit", self.text, text, buttons))
        self.text = text
        self.buttons = buttons


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
        return _FakeMessage(self.calls, text, buttons)


def _register_and_get_event(fx: _Fixture, *, data: bytes, sender_id=_USER_ID):
    client = _FakeTelethonClient()
    register(client, fx.controller, known_users_repository=fx.known_users_repository)
    event = _FakeCallbackEvent(data=data, sender_id=sender_id, chat_id=sender_id)
    return client, event


# ==== "⏳" shown immediately, three callback paths ====


async def test_check_now_shows_hourglass_immediately_then_edits_with_result(tmp_path):
    provider = _BlockingProvider({"O127PX62": [_record()]})
    fx = _Fixture(tmp_path, provider=provider)
    try:
        _task, sub = fx.make_active_subscription()
        client, event = _register_and_get_event(fx, data=b"checknow:" + str(sub.id).encode("ascii"))

        pending = asyncio.ensure_future(client.callback_handler(event))
        await provider.entered.wait()

        # "⏳" is visible immediately — BEFORE the slow provider call returns.
        call_names = [c[0] for c in event.calls]
        assert call_names == ["answer", "respond"]
        assert event.calls[1][1] == "⏳"

        provider.gate.set()
        await pending

        # Final result EDITS the same message — no third message sent.
        call_names = [c[0] for c in event.calls]
        assert call_names == ["answer", "respond", "message_edit"]
        assert "🚨 Обнаружен штраф" in event.calls[2][2]
    finally:
        fx.close()


async def test_my_car_check_now_once_shows_hourglass_then_edits(tmp_path):
    """handle_my_car_check_now_once() works on any actionable subscription
    (no "ИСТЁК" precondition, unlike handle_check_now_choice — see
    conversation.py) — the same basic active subscription fixture suffices."""
    provider = _BlockingProvider({"O127PX62": []})
    fx = _Fixture(tmp_path, provider=provider)
    try:
        _task, sub = fx.make_active_subscription()
        client, event = _register_and_get_event(
            fx, data=b"mycarchecknowonce:" + str(sub.id).encode("ascii"),
        )

        pending = asyncio.ensure_future(client.callback_handler(event))
        await provider.entered.wait()
        assert event.calls[1][1] == "⏳"

        provider.gate.set()
        await pending

        call_names = [c[0] for c in event.calls]
        assert call_names == ["answer", "respond", "message_edit"]
    finally:
        fx.close()


async def test_trusted_check_now_once_shows_hourglass_then_edits(tmp_path):
    provider = _BlockingProvider({"O127PX62": []})
    fx = _Fixture(tmp_path, provider=provider, trusted_operator_user_ids={_TRUSTED_ID})
    try:
        task, _sub = fx.make_active_subscription()
        client, event = _register_and_get_event(
            fx, data=b"tchecknowonce:" + str(task.id).encode("ascii"), sender_id=_TRUSTED_ID,
        )

        pending = asyncio.ensure_future(client.callback_handler(event))
        await provider.entered.wait()
        assert event.calls[1][1] == "⏳"

        provider.gate.set()
        await pending

        call_names = [c[0] for c in event.calls]
        assert call_names == ["answer", "respond", "message_edit"]
    finally:
        fx.close()


# ==== repeated click while a check is already running ====


async def test_repeated_click_does_not_start_second_provider_call_or_message(tmp_path):
    provider = _BlockingProvider({"O127PX62": [_record()]})
    fx = _Fixture(tmp_path, provider=provider)
    try:
        _task, sub = fx.make_active_subscription()
        client, first_event = _register_and_get_event(fx, data=b"checknow:" + str(sub.id).encode("ascii"))

        first_pending = asyncio.ensure_future(client.callback_handler(first_event))
        await provider.entered.wait()

        # Second click, same subscription, same user, while the first is
        # still blocked inside the provider.
        second_event = _FakeCallbackEvent(
            data=b"checknow:" + str(sub.id).encode("ascii"), sender_id=_USER_ID, chat_id=_USER_ID,
        )
        await client.callback_handler(second_event)

        # The duplicate click is acked and nothing else — no second "⏳",
        # no error message, no second provider call.
        assert [c[0] for c in second_event.calls] == ["answer"]
        assert provider.search_calls == ["O127PX62"]

        provider.gate.set()
        await first_pending

        # The original click's flow completed normally and exactly once.
        assert [c[0] for c in first_event.calls] == ["answer", "respond", "message_edit"]
        assert provider.search_calls == ["O127PX62"]
    finally:
        fx.close()


async def test_different_subscriptions_do_not_block_each_other(tmp_path):
    provider = _BlockingProvider({"AA001AA": [], "BB002BB": []})
    fx = _Fixture(tmp_path, provider=provider)
    try:
        _task_a, sub_a = fx.make_active_subscription(car_number="AA001AA", telegram_user_id=1)
        _task_b, sub_b = fx.make_active_subscription(car_number="BB002BB", telegram_user_id=2)
        client, event_a = _register_and_get_event(fx, data=b"checknow:" + str(sub_a.id).encode("ascii"), sender_id=1)

        pending_a = asyncio.ensure_future(client.callback_handler(event_a))
        await provider.entered.wait()

        event_b = _FakeCallbackEvent(data=b"checknow:" + str(sub_b.id).encode("ascii"), sender_id=2, chat_id=2)
        pending_b = asyncio.ensure_future(client.callback_handler(event_b))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert sorted(provider.search_calls) == ["AA001AA", "BB002BB"]

        provider.gate.set()
        await pending_a
        await pending_b

        assert [c[0] for c in event_a.calls] == ["answer", "respond", "message_edit"]
        assert [c[0] for c in event_b.calls] == ["answer", "respond", "message_edit"]
    finally:
        fx.close()


async def test_check_now_does_not_show_hourglass_for_expired_card(tmp_path):
    """handle_check_now_choice's "ИСТЁК" branch (status still 'active' in
    the DB, but end_date already passed — see texts.car_monitoring_state())
    returns an informative card WITHOUT ever calling check_now() —
    on_check_started is never invoked, so no "⏳" is shown and the one
    message sent is the card itself, exactly as before this change."""
    provider = _BlockingProvider()
    fx = _Fixture(tmp_path, provider=provider)
    try:
        car_number = "O127PX62"
        past_start, past_end = date(2020, 1, 1), date(2020, 2, 1)
        task = fx.task_repository.create(
            car_number=car_number, label=None, start_date=past_start, end_date=past_end,
            telegram_chat_id=_USER_ID, created_by_user_id=_USER_ID, monitoring_scope="client_bot",
        )
        sub = fx.subscription_repository.create(
            monitoring_task_id=task.id, car_number=car_number,
            telegram_user_id=_USER_ID, telegram_chat_id=_USER_ID,
            telegram_username=None, start_date=past_start, end_date=past_end,
        )
        client, event = _register_and_get_event(fx, data=b"checknow:" + str(sub.id).encode("ascii"))

        await client.callback_handler(event)

        assert provider.search_calls == []
        call_names = [c[0] for c in event.calls]
        assert call_names == ["answer", "respond"]
        assert "message_edit" not in call_names
    finally:
        fx.close()
