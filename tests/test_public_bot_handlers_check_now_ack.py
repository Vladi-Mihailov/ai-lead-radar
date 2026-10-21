"""
Тесты reader/public_bot/handlers.py::_on_callback — production incident
O127PX62 (2026-10-21): event.answer() ПОСЛЕ await живой проверки (police.ge
request_timeout=30s + false-zero confirmation retry) легко превышает срок
жизни callback-query token — SetBotCallbackAnswerRequest бросал
telethon.errors.rpcerrorlist.QueryIdInvalidError, непойманную, рушившую
handler ДО _send_reply: итоговый ответ терялся совсем, хотя сама проверка
успешно завершалась (пусть даже с graceful ошибкой, см. production DB:
last_check_status='error' записался, но пользователь ничего не увидел).

ТОТ ЖЕ проверенный приём, что уже чинил идентичный баг на Turkey (см.
tests/test_turkey_bot_handlers_check_callback.py, commit f062cb7 "fix:
preserve Turkey replies after long checks") — эти тесты повторяют его
структуру один в один: _FakeTelethonClient/_FakeCallbackEvent вместо
реального Telethon, реальный ConversationController поверх SQLite/tmp_path.

Покрывает ВСЕ 5 callback-путей, чей handler может делать реальный live-
check (см. задачу "align Georgia user check-now flow" / incident report,
explicit scope decision "fix all structurally-identical call sites now"):
check_now (picker), my_car_check_now_once (ИСТЁК-карточка), period (Add
Car onboarding-проверка), trusted_check_now_once, debt_refresh_confirm.
"""

import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from telethon import events  # noqa: E402
from telethon.errors.rpcerrorlist import QueryIdInvalidError  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import ParsedFineRecord  # noqa: E402
from reader.fines.provider import FineProvider  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.conversation import ConversationController  # noqa: E402
from reader.public_bot.conversation_state_repository import (  # noqa: E402
    BotConversationStateRepository,
)
from reader.public_bot.debt_refresh_service import DebtRefreshService  # noqa: E402
from reader.public_bot.handlers import register  # noqa: E402
from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402
from reader.public_bot.statistics_service import BotStatisticsService  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import SubscriptionService  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

pytestmark = pytest.mark.asyncio

_TBILISI = ZoneInfo("Asia/Tbilisi")
_TRUSTED_ID = 5712994689
_USER_ID = 5319195675  # тот же telegram_user_id, что и в production-инциденте


async def _instant_sleep(_seconds: float) -> None:
    """См. tests/test_debt_refresh_service.py — та же замена реального
    asyncio.sleep для false-zero confirmation/inter-car delay."""


class _TrackingProvider(FineProvider):
    """Как и Turkey's _SlowGibProvider (см. test_turkey_bot_handlers_
    check_callback.py) — НЕ реально медленный (тесты не должны спать),
    просто фиксирует МОМЕНТ, когда реальный provider request начался,
    чтобы доказать порядок: answer() ДО этого момента, не после."""

    def __init__(self, records_by_car=None, *, on_start=None, error: Exception | None = None):
        self._records_by_car = records_by_car or {}
        self._on_start = on_start
        self._error = error

    async def search_by_plate(self, plate: str):
        if self._on_start is not None:
            await self._on_start()
        if self._error is not None:
            raise self._error
        return self._records_by_car.get(plate, [])


def _record(car_number="O127PX62", fingerprint="fp-o127px62") -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number, external_fine_id="AB123456",
        penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
        delivered_status="Не вручено", fingerprint=fingerprint,
        raw_data={"protocolNo": "AB123456"},
    )


class _Fixture:
    def __init__(self, tmp_path, *, provider=None, trusted_operator_user_ids=frozenset()):
        self.db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(self.db_path)
        self.detected_fine_repository = DetectedFineRepository(self.db_path)
        self.subscription_repository = FineSubscriptionRepository(self.db_path)
        self.user_repository = UserRepository(self.db_path)
        self.conversation_state_repository = BotConversationStateRepository(self.db_path)
        self.known_users_repository = BotKnownUsersRepository(self.db_path)
        self.provider = provider if provider is not None else _TrackingProvider()
        self.check_service = FineCheckService(
            self.provider, self.task_repository, self.detected_fine_repository,
            sleep=_instant_sleep,
        )
        self.subscription_service = SubscriptionService(
            self.task_repository, self.subscription_repository,
            self.user_repository, self.check_service,
        )
        self.statistics_service = BotStatisticsService(
            self.known_users_repository, self.subscription_repository, self.detected_fine_repository,
        )
        self.debt_refresh_service = DebtRefreshService(
            self.task_repository, self.check_service, sleep=_instant_sleep,
        )
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

    def make_active_subscription(self, car_number="O127PX62", *, telegram_user_id=_USER_ID) -> int:
        """O127PX62-shaped: status=active, end_date далеко в будущем — та
        же форма, что и production subscription id=254."""
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
        return subscription.id


class _FakeTelethonClient:
    def __init__(self):
        self.callback_handler = None
        self.message_handler = None

    def on(self, event_filter):
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


def _register_and_get_event(fx: _Fixture, *, data: bytes, sender_id=_USER_ID, answer_error=None):
    client = _FakeTelethonClient()
    register(client, fx.controller, known_users_repository=fx.known_users_repository)
    event = _FakeCallbackEvent(data=data, sender_id=sender_id, chat_id=sender_id, answer_error=answer_error)
    return client, event


# ==== 1. check_now (picker-выбор) — ТОЧНЫЙ production repro ====


async def test_fast_check_now_answers_then_sends_reply(tmp_path):
    fx = _Fixture(tmp_path, provider=_TrackingProvider({"O127PX62": [_record()]}))
    try:
        subscription_id = fx.make_active_subscription()
        client, event = _register_and_get_event(
            fx, data=b"checknow:" + str(subscription_id).encode("ascii"),
        )

        await client.callback_handler(event)

        call_names = [c[0] for c in event.calls]
        assert "answer" in call_names
        assert "respond" in call_names
        assert call_names.index("answer") < call_names.index("respond")
    finally:
        fx.close()


async def test_expired_callback_token_does_not_prevent_final_reply_with_fine(tmp_path):
    """ТОЧНЫЙ regression-критерий задачи (п.10):
    Given regular user + O127PX62-shaped active subscription + provider
    returns 1 fine. When user presses Check Now. Then: live
    FineCheckService invoked, 1 fine persisted, reply containing the fine
    reaches the Telegram send layer — even though the callback-query
    token is already expired when answer() is finally called."""
    fx = _Fixture(tmp_path, provider=_TrackingProvider({"O127PX62": [_record()]}))
    try:
        subscription_id = fx.make_active_subscription()
        client, event = _register_and_get_event(
            fx, data=b"checknow:" + str(subscription_id).encode("ascii"),
            answer_error=QueryIdInvalidError(request=None),
        )

        await client.callback_handler(event)  # must not raise

        call_names = [c[0] for c in event.calls]
        assert "answer" in call_names
        assert "respond" in call_names
        respond_call = next(c for c in event.calls if c[0] == "respond")
        assert "🚨 Обнаружен штраф" in respond_call[1]

        rows = fx.detected_fine_repository.list_by_car_number("O127PX62")
        assert any(row.fingerprint == "fp-o127px62" for row in rows)
    finally:
        fx.close()


async def test_check_now_ack_happens_before_provider_call_starts(tmp_path):
    order: list[str] = []

    async def _on_start():
        order.append("provider_started")

    fx = _Fixture(tmp_path, provider=_TrackingProvider({"O127PX62": [_record()]}, on_start=_on_start))
    try:
        subscription_id = fx.make_active_subscription()
        client, event = _register_and_get_event(
            fx, data=b"checknow:" + str(subscription_id).encode("ascii"),
        )

        await client.callback_handler(event)

        assert event.calls[0][0] == "answer"
        assert order == ["provider_started"]
    finally:
        fx.close()


async def test_check_now_unrelated_telegram_exception_is_not_swallowed(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        subscription_id = fx.make_active_subscription()
        client, event = _register_and_get_event(
            fx, data=b"checknow:" + str(subscription_id).encode("ascii"),
            answer_error=RuntimeError("some other Telegram error"),
        )

        with pytest.raises(RuntimeError, match="some other Telegram error"):
            await client.callback_handler(event)
    finally:
        fx.close()


async def test_check_now_unknown_subscription_after_early_ack_gets_plain_message_not_crash(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        client, event = _register_and_get_event(fx, data=b"checknow:999999")

        await client.callback_handler(event)  # must not raise

        call_names = [c[0] for c in event.calls]
        assert "answer" in call_names
        assert "respond" in call_names
    finally:
        fx.close()


async def test_check_now_graceful_provider_error_still_reaches_user(tmp_path):
    """Production repro ИМЕННО того, что произошло с O127PX62 после
    предыдущего деплоя — police.ge недоступен (FineProviderError, УЖЕ
    graceful, см. PoliceGeSession fix), но ответ ВСЁ РАВНО терялся из-за
    QueryIdInvalidError. Здесь — тот же сценарий целиком, включая
    expired callback token."""
    from reader.fines.provider import FineProviderError

    fx = _Fixture(tmp_path, provider=_TrackingProvider(error=FineProviderError("police.ge недоступен")))
    try:
        subscription_id = fx.make_active_subscription()
        client, event = _register_and_get_event(
            fx, data=b"checknow:" + str(subscription_id).encode("ascii"),
            answer_error=QueryIdInvalidError(request=None),
        )

        await client.callback_handler(event)  # must not raise

        call_names = [c[0] for c in event.calls]
        assert "respond" in call_names
        respond_call = next(c for c in event.calls if c[0] == "respond")
        assert "не удалось" in respond_call[1]

        task = fx.task_repository.get(fx.subscription_repository.get(subscription_id).monitoring_task_id)
        assert task.last_check_status == "error"
    finally:
        fx.close()


# ==== 2. my_car_check_now_once (ИСТЁК-карточка, "🔎 Проверить разово") ====


async def test_my_car_check_now_once_survives_expired_callback_token(tmp_path):
    fx = _Fixture(tmp_path, provider=_TrackingProvider({"O127PX62": [_record()]}))
    try:
        subscription_id = fx.make_active_subscription()
        client, event = _register_and_get_event(
            fx, data=b"mycarchecknowonce:" + str(subscription_id).encode("ascii"),
            answer_error=QueryIdInvalidError(request=None),
        )

        await client.callback_handler(event)  # must not raise

        call_names = [c[0] for c in event.calls]
        assert "answer" in call_names
        assert "respond" in call_names
        assert call_names.index("answer") < call_names.index("respond")
    finally:
        fx.close()


# ==== 3. period (Add Car onboarding-проверка) ====


async def test_period_choice_survives_expired_callback_token(tmp_path):
    fx = _Fixture(tmp_path, provider=_TrackingProvider({"O127PX62": [_record()]}))
    try:
        fx.conversation_state_repository.set(
            _USER_ID, telegram_user_id=_USER_ID, step="awaiting_period",
            payload={"car_number": "O127PX62", "username": "alice"},
        )
        client, event = _register_and_get_event(
            fx, data=b"period:30", answer_error=QueryIdInvalidError(request=None),
        )

        await client.callback_handler(event)  # must not raise

        call_names = [c[0] for c in event.calls]
        assert "answer" in call_names
        assert "respond" in call_names
        [task] = fx.task_repository.list_active()
        assert task.car_number == "O127PX62"
    finally:
        fx.close()


# ==== 4. trusted_check_now_once ====


async def test_trusted_check_now_once_survives_expired_callback_token(tmp_path):
    fx = _Fixture(
        tmp_path, provider=_TrackingProvider({"O127PX62": [_record()]}),
        trusted_operator_user_ids={_TRUSTED_ID},
    )
    try:
        task = fx.task_repository.create(
            car_number="O127PX62", label=None,
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
            telegram_chat_id=_TRUSTED_ID, created_by_user_id=_TRUSTED_ID,
        )
        fx.task_repository.set_status(task.id, "completed")
        client, event = _register_and_get_event(
            fx, data=b"tchecknowonce:" + str(task.id).encode("ascii"),
            sender_id=_TRUSTED_ID, answer_error=QueryIdInvalidError(request=None),
        )

        await client.callback_handler(event)  # must not raise

        call_names = [c[0] for c in event.calls]
        assert "answer" in call_names
        assert "respond" in call_names
    finally:
        fx.close()


# ==== 5. debt_refresh_confirm (батч-проверка) ====


async def test_debt_refresh_confirm_survives_expired_callback_token(tmp_path):
    fx = _Fixture(
        tmp_path, provider=_TrackingProvider({"O127PX62": [_record()]}),
        trusted_operator_user_ids={_TRUSTED_ID},
    )
    try:
        fx.make_active_subscription(telegram_user_id=_TRUSTED_ID)
        client, event = _register_and_get_event(
            fx, data=b"debtrefreshyes", sender_id=_TRUSTED_ID,
            answer_error=QueryIdInvalidError(request=None),
        )

        await client.callback_handler(event)  # must not raise

        call_names = [c[0] for c in event.calls]
        assert "answer" in call_names
        assert "respond" in call_names
    finally:
        fx.close()
