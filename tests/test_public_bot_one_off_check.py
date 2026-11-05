"""
Разовая проверка штрафов Грузии trusted-менеджером без сохранения (см.
задачу "GE-1/GE-2"): FineCheckService.check_plate_readonly,
SubscriptionService.check_plate_once и UI менеджера в ConversationController
и reader/public_bot/handlers.py.

Там, где проверяется, что сервис вообще не трогает БД, — строгие фейки
репозиториев, падающие на ЛЮБОМ обращении; там, где проверяется "ничего не
записано" через весь стек, — настоящие SQLite-репозитории (tmp_path) и
подсчёт строк. Provider и Telegram — фейковые, реальных запросов нет; пауза
перед подтверждающим запросом записывается, а не ждётся.
"""

import asyncio
import sqlite3
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon import events  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import ParsedFineRecord  # noqa: E402
from reader.fines.provider import FineProvider, FineProviderError, PoliceGeUnavailableError  # noqa: E402
from reader.fines.rednet_check_now_provider import AutoFailoverCheckNowProvider  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.fines.translation import FineTranslationError, TranslatedFineText  # noqa: E402
from reader.plate_display import format_plate_for_telegram  # noqa: E402
from reader.public_bot import texts  # noqa: E402
from reader.public_bot.conversation import (  # noqa: E402
    STEP_AWAITING_CAR_NUMBER,
    STEP_AWAITING_CLIENT_DECISION,
    STEP_AWAITING_TRUSTED_CHECK_NOW_CAR_NUMBER,
    ConversationController,
)
from reader.public_bot.conversation_state_repository import BotConversationStateRepository  # noqa: E402
from reader.public_bot.handlers import register  # noqa: E402
from reader.public_bot.keyboards import (  # noqa: E402
    decode_trusted_one_off_add_callback,
    decode_trusted_one_off_another_callback,
    decode_trusted_one_off_check_callback,
    encode_trusted_one_off_add_callback,
    encode_trusted_one_off_another_callback,
    encode_trusted_one_off_check_callback,
)
from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402
from reader.public_bot.statistics_service import BotStatisticsService  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import (  # noqa: E402
    CHECK_ALREADY_IN_PROGRESS,
    SubscriptionService,
)
from reader.users.repository import UserRepository  # noqa: E402

_TBILISI = ZoneInfo("Asia/Tbilisi")
_TRUSTED_ID = 5712994689
_OTHER_TRUSTED_ID = 5712994690
_USER_ID = 5319195675
_PLATE = "E911EE95"
_DENYLISTED_PLATE = "O687KE761"  # см. reader/fines/suppression.py


def _record(car_number=_PLATE, fingerprint="fp-1", *, protocol="AB123456", amount=50.0, place=None) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number, external_fine_id=protocol,
        penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
        delivered_status="Не вручено", fingerprint=fingerprint,
        raw_data={"protocolNo": protocol}, amount=amount, place=place,
    )


class _ScriptedProvider(FineProvider):
    """Каждый вызов берёт следующий ответ из script (список records или
    исключение); последний ответ повторяется, когда script кончился."""

    def __init__(self, *script):
        self._script = list(script) or [[]]
        self.calls: list[str] = []

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        self.calls.append(plate)
        response = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(response, Exception):
            raise response
        return response


class _BlockingProvider(FineProvider):
    """search_by_plate() ждёт gate — позволяет поймать проверку "в полёте"
    и попробовать запустить вторую параллельно."""

    def __init__(self, records=None):
        self._records = [_record()] if records is None else records
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.calls: list[str] = []

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        self.calls.append(plate)
        self.entered.set()
        await self.gate.wait()
        return self._records


class _UntouchableRepository:
    """Любое обращение к атрибуту — провал: разовая проверка не должна ни
    читать, ни писать репозитории."""

    def __init__(self, name: str):
        self._name = name

    def __getattr__(self, attribute):
        raise AssertionError(f"{self._name}.{attribute} touched by the one-off check")


class _SleepRecorder:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _FakeTranslator:
    def __init__(self, error: Exception | None = None):
        self._error = error

    async def translate(self, *, place, violation_description) -> TranslatedFineText:
        if self._error is not None:
            raise self._error
        return TranslatedFineText(place_ru="Тбилиси" if place else None, violation_description_ru=None)


def _readonly_service(provider, translator=None) -> tuple[FineCheckService, _SleepRecorder]:
    sleep = _SleepRecorder()
    service = FineCheckService(
        provider, _UntouchableRepository("task_repository"),
        _UntouchableRepository("detected_fine_repository"), translator, sleep=sleep,
    )
    return service, sleep


class _Fixture:
    def __init__(self, tmp_path, provider):
        self.db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(self.db_path)
        self.detected_fine_repository = DetectedFineRepository(self.db_path)
        self.subscription_repository = FineSubscriptionRepository(self.db_path)
        self.user_repository = UserRepository(self.db_path)
        self.conversation_state_repository = BotConversationStateRepository(self.db_path)
        self.known_users_repository = BotKnownUsersRepository(self.db_path)
        self.provider = provider
        self.sleep = _SleepRecorder()
        self.check_service = FineCheckService(
            provider, self.task_repository, self.detected_fine_repository, sleep=self.sleep,
        )
        self.service = SubscriptionService(
            self.task_repository, self.subscription_repository, self.user_repository, self.check_service,
        )
        self.controller = ConversationController(
            self.conversation_state_repository, self.service,
            BotStatisticsService(
                self.known_users_repository, self.subscription_repository, self.detected_fine_repository,
            ),
            self.known_users_repository, tz=_TBILISI,
            trusted_operator_user_ids=frozenset({_TRUSTED_ID, _OTHER_TRUSTED_ID}),
        )

    def make_task(self, car_number=_PLATE, *, status="active") -> int:
        task = self.task_repository.create(
            car_number=car_number, label=None, start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
            telegram_chat_id=-100999, created_by_user_id=111,
        )
        if status != "active":
            self.task_repository.set_status(task.id, status)
        return task.id

    def table_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("fine_monitoring_tasks", "detected_fines", "fine_monitoring_subscriptions")
            }

    def close(self):
        self.task_repository.close()
        self.detected_fine_repository.close()
        self.subscription_repository.close()
        self.user_repository.close()
        self.conversation_state_repository.close()
        self.known_users_repository.close()


def _labels(reply) -> list[str]:
    return [label for row in reply.inline_buttons for label, _data in row]


# ==== GE-1: FineCheckService.check_plate_readonly ====


async def test_readonly_check_returns_every_fine_without_touching_repositories():
    provider = _ScriptedProvider([_record(fingerprint="fp-1", protocol="AB1"), _record(fingerprint="fp-2", protocol="AB2")])
    service, sleep = _readonly_service(provider)

    result = await service.check_plate_readonly(_PLATE)

    assert result.status == "ok"
    assert result.shows_as_ok
    assert [fine.external_fine_id for fine in result.fines] == ["AB1", "AB2"]
    assert all(fine.detected_fine_id == 0 and fine.task_id == 0 for fine in result.fines)
    assert provider.calls == [_PLATE]
    assert sleep.calls == []
    assert result.zero_confirmed is False


async def test_empty_result_is_confirmed_by_exactly_one_request_after_5_seconds():
    provider = _ScriptedProvider([], [])
    service, sleep = _readonly_service(provider)

    result = await service.check_plate_readonly(_PLATE)

    assert provider.calls == [_PLATE, _PLATE]
    assert sleep.calls == [5.0]
    assert result.status == "ok"
    assert result.fines == []
    assert result.zero_confirmed is True


async def test_confirmation_request_wins_when_first_answer_was_falsely_empty():
    provider = _ScriptedProvider([], [_record()])
    service, sleep = _readonly_service(provider)

    result = await service.check_plate_readonly(_PLATE)

    assert len(result.fines) == 1
    assert result.zero_confirmed is False
    assert sleep.calls == [5.0]
    assert provider.calls == [_PLATE, _PLATE]


async def test_provider_error_is_reported_not_shown_as_no_fines():
    service, sleep = _readonly_service(_ScriptedProvider(FineProviderError("timeout")))

    result = await service.check_plate_readonly(_PLATE)

    assert result.status == "error"
    assert not result.shows_as_ok
    assert result.error_message == "timeout"
    assert result.provider_unavailable is False
    assert result.fines == []
    assert sleep.calls == []


async def test_unavailable_provider_is_flagged_for_its_own_message():
    service, _sleep = _readonly_service(_ScriptedProvider(PoliceGeUnavailableError("circuit open")))

    result = await service.check_plate_readonly(_PLATE)

    assert result.status == "error"
    assert result.provider_unavailable is True


async def test_error_on_confirmation_request_is_an_error_not_a_confirmed_zero():
    provider = _ScriptedProvider([], FineProviderError("timeout on retry"))
    service, sleep = _readonly_service(provider)

    result = await service.check_plate_readonly(_PLATE)

    assert result.status == "error"
    assert result.zero_confirmed is False
    assert sleep.calls == [5.0]
    assert provider.calls == [_PLATE, _PLATE]


async def test_denylisted_plate_is_never_sent_to_provider():
    provider = _ScriptedProvider([_record(car_number=_DENYLISTED_PLATE)])
    service, sleep = _readonly_service(provider)

    result = await service.check_plate_readonly(_DENYLISTED_PLATE)

    assert provider.calls == []
    assert sleep.calls == []
    assert result.status == "suppressed"
    assert result.shows_as_ok
    assert result.fines == []


async def test_local_failure_falls_back_to_rednet_like_check_now():
    local = _ScriptedProvider(PoliceGeUnavailableError("local breaker open"))
    rednet = _ScriptedProvider([_record(protocol="RED1")])
    service, _sleep = _readonly_service(AutoFailoverCheckNowProvider(local, rednet))

    result = await service.check_plate_readonly(_PLATE)

    assert result.status == "ok"
    assert [fine.external_fine_id for fine in result.fines] == ["RED1"]
    assert local.calls == [_PLATE]
    assert rednet.calls == [_PLATE]


async def test_georgian_place_is_translated_and_translation_failure_is_fail_open():
    record = _record(place="თბილისი")
    translated_service, _ = _readonly_service(_ScriptedProvider([record]), translator=_FakeTranslator())
    failing_service, _ = _readonly_service(
        _ScriptedProvider([record]), translator=_FakeTranslator(error=FineTranslationError("down")),
    )

    translated = await translated_service.check_plate_readonly(_PLATE)
    fallback = await failing_service.check_plate_readonly(_PLATE)

    assert translated.fines[0].place_ru == "Тбилиси"
    assert fallback.status == "ok"
    assert fallback.fines[0].place_ru is None
    assert fallback.fines[0].place == "თბილისი"


# ==== GE-1: SubscriptionService.check_plate_once ====


async def test_one_off_check_writes_nothing_to_the_database(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider([_record(fingerprint="fp-new")]))
    try:
        task_id = fx.make_task(status="completed")
        before_task = fx.task_repository.get(task_id)
        before = fx.table_counts()

        result = await fx.service.check_plate_once(_PLATE, telegram_user_id=_TRUSTED_ID)

        assert len(result.fines) == 1
        assert fx.table_counts() == before
        assert fx.task_repository.get(task_id) == before_task
    finally:
        fx.close()


async def test_one_off_guard_allows_one_check_per_plate_and_per_manager(tmp_path):
    provider = _BlockingProvider()
    fx = _Fixture(tmp_path, provider)
    try:
        started: list[str] = []

        async def on_started() -> None:
            started.append("first")

        first = asyncio.ensure_future(
            fx.service.check_plate_once(_PLATE, telegram_user_id=_TRUSTED_ID, on_check_started=on_started),
        )
        await provider.entered.wait()

        same_plate = await fx.service.check_plate_once(_PLATE, telegram_user_id=_OTHER_TRUSTED_ID)
        same_manager = await fx.service.check_plate_once("AA001AA", telegram_user_id=_TRUSTED_ID)

        assert same_plate is CHECK_ALREADY_IN_PROGRESS
        assert same_manager is CHECK_ALREADY_IN_PROGRESS
        assert provider.calls == [_PLATE]

        provider.gate.set()
        result = await first
        assert result.status == "ok"
        assert started == ["first"]

        again = await fx.service.check_plate_once(_PLATE, telegram_user_id=_OTHER_TRUSTED_ID)
        assert again.status == "ok"
    finally:
        fx.close()


async def test_one_off_checks_of_other_plate_by_other_manager_run_in_parallel(tmp_path):
    provider = _BlockingProvider(records=[_record()])
    fx = _Fixture(tmp_path, provider)
    try:
        first = asyncio.ensure_future(fx.service.check_plate_once(_PLATE, telegram_user_id=_TRUSTED_ID))
        second = asyncio.ensure_future(fx.service.check_plate_once("AA001AA", telegram_user_id=_OTHER_TRUSTED_ID))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert sorted(provider.calls) == ["AA001AA", _PLATE]

        provider.gate.set()
        assert (await first).status == "ok"
        assert (await second).status == "ok"
    finally:
        fx.close()


async def test_one_off_guard_is_released_after_provider_error(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider(FineProviderError("down"), [_record()]))
    try:
        failed = await fx.service.check_plate_once(_PLATE, telegram_user_id=_TRUSTED_ID)
        retried = await fx.service.check_plate_once(_PLATE, telegram_user_id=_TRUSTED_ID)

        assert failed.status == "error"
        assert retried.status == "ok"
    finally:
        fx.close()


async def test_one_off_guard_is_released_after_cancellation(tmp_path):
    provider = _BlockingProvider()
    fx = _Fixture(tmp_path, provider)
    try:
        running = asyncio.ensure_future(fx.service.check_plate_once(_PLATE, telegram_user_id=_TRUSTED_ID))
        await provider.entered.wait()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)

        provider.gate.set()
        result = await fx.service.check_plate_once(_PLATE, telegram_user_id=_TRUSTED_ID)

        assert result is not CHECK_ALREADY_IN_PROGRESS
        assert result.status == "ok"
    finally:
        fx.close()


# ==== GE-2: ConversationController ====


async def test_trusted_plate_on_home_screen_offers_one_off_or_monitoring(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider([]))
    try:
        reply = await fx.controller.handle_text(
            "е911ее95", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,  # кириллица
        )

        assert reply.text == texts.format_trusted_plate_choice_prompt(_PLATE)
        [[check_button, monitor_button]] = reply.inline_buttons
        assert check_button[0] == texts.TRUSTED_ONE_OFF_CHECK_LABEL
        assert decode_trusted_one_off_check_callback(check_button[1]) == _PLATE
        assert monitor_button[0] == texts.TRUSTED_ONE_OFF_MONITOR_LABEL
        assert decode_trusted_one_off_add_callback(monitor_button[1]) == _PLATE
        assert fx.conversation_state_repository.get(_TRUSTED_ID) is None
        assert fx.provider.calls == []
    finally:
        fx.close()


async def test_regular_user_plate_on_home_screen_still_starts_add_car(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider([]))
    try:
        reply = await fx.controller.handle_text(_PLATE, chat_id=_USER_ID, telegram_user_id=_USER_ID, username=None)

        assert reply.text == texts.PERIOD_PROMPT
        assert reply.show_period_buttons is True
        assert reply.inline_buttons is None
        assert reply.trusted_one_off_plate is None
    finally:
        fx.close()


async def test_choice_one_off_for_unknown_plate_shows_full_result_and_saves_nothing(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider([
        _record(fingerprint="fp-1", protocol="AB1", amount=50.0),
        _record(fingerprint="fp-2", protocol="AB2", amount=30.5),
    ]))
    try:
        before = fx.table_counts()

        reply = await fx.controller.handle_trusted_plate_choice_check(_PLATE, telegram_user_id=_TRUSTED_ID)

        assert f"🔎 {format_plate_for_telegram(_PLATE)}: найдено штрафов — 2" in reply.text
        assert "💰 Штрафы: 80.5 ₾" in reply.text
        assert reply.text.count("🚨 Обнаружен штраф") == 2
        assert texts.ONE_OFF_CHECK_NOT_SAVED_LINE in reply.text
        assert reply.cta_buttons is None
        [[another], [add]] = reply.inline_buttons
        assert decode_trusted_one_off_another_callback(another[1]) is True
        assert decode_trusted_one_off_add_callback(add[1]) == _PLATE
        assert fx.table_counts() == before
    finally:
        fx.close()


async def test_choice_one_off_for_actively_monitored_plate_keeps_existing_task_check(tmp_path):
    """Q1 — активная задача без изменений: та же task-level проверка, что
    и раньше; результат пишется в задачу как часть мониторинга."""
    fx = _Fixture(tmp_path, _ScriptedProvider([_record(fingerprint="fp-active")]))
    try:
        task_id = fx.make_task(status="active")

        reply = await fx.controller.handle_trusted_plate_choice_check(_PLATE, telegram_user_id=_TRUSTED_ID)

        assert "🚨 Обнаружен штраф" in reply.text
        assert reply.show_main_menu is True
        assert reply.inline_buttons is None
        assert fx.task_repository.get(task_id).last_checked_at is not None
        assert [row.fingerprint for row in fx.detected_fine_repository.list_by_car_number(_PLATE)] == ["fp-active"]
    finally:
        fx.close()


async def test_old_card_for_task_that_is_active_again_uses_existing_task_check(tmp_path):
    """Старая карточка "🔎 Проверить разово", а мониторинг номера с тех пор
    снова включили — та же развилка, что и у остальных входов: активная
    задача остаётся как есть (Q1), результат пишется в задачу."""
    fx = _Fixture(tmp_path, _ScriptedProvider([_record(fingerprint="fp-again")]))
    try:
        task_id = fx.make_task(status="active")

        reply = await fx.controller.handle_trusted_check_now_once(task_id, telegram_user_id=_TRUSTED_ID)

        assert reply.show_main_menu is True
        assert reply.inline_buttons is None
        assert fx.task_repository.get(task_id).last_checked_at is not None
        assert [row.fingerprint for row in fx.detected_fine_repository.list_by_car_number(_PLATE)] == ["fp-again"]
    finally:
        fx.close()


async def test_one_off_check_itself_never_writes_even_for_actively_monitored_plate(tmp_path):
    """handle_trusted_one_off_check — всегда без сохранения; если номер
    успел встать на мониторинг (гонка между вводом и проверкой),
    предлагается только "🔎 Проверить другой номер"."""
    fx = _Fixture(tmp_path, _ScriptedProvider([_record()]))
    try:
        task_id = fx.make_task(status="active")
        before = fx.task_repository.get(task_id)

        reply = await fx.controller.handle_trusted_one_off_check(_PLATE, telegram_user_id=_TRUSTED_ID)

        assert _labels(reply) == [texts.TRUSTED_ONE_OFF_ANOTHER_LABEL]
        assert fx.task_repository.get(task_id) == before
        assert fx.detected_fine_repository.list_by_car_number(_PLATE) == []
    finally:
        fx.close()


async def test_reentering_plate_input_during_check_is_not_wiped_by_its_completion(tmp_path):
    """Менеджер за время проверки снова вошёл в ввод номера ("🔎
    Проверить другой номер") — завершившаяся проверка этот новый шаг не
    снимает: следующий номер уйдёт в проверку, а не на экран выбора."""
    provider = _BlockingProvider(records=[])
    fx = _Fixture(tmp_path, provider)
    try:
        await fx.controller.handle_text(
            texts.CHECK_NOW_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        marker = await fx.controller.handle_text(_PLATE, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
        running = asyncio.ensure_future(fx.controller.handle_trusted_one_off_check(
            marker.trusted_one_off_plate, telegram_user_id=_TRUSTED_ID, chat_id=_TRUSTED_ID,
        ))
        await provider.entered.wait()

        fx.controller.handle_trusted_one_off_another(chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)
        provider.gate.set()
        await running

        assert fx.conversation_state_repository.get(_TRUSTED_ID).step == STEP_AWAITING_TRUSTED_CHECK_NOW_CAR_NUMBER
        next_marker = await fx.controller.handle_text(
            "AA001AA", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        assert next_marker.trusted_one_off_plate == "AA001AA"
    finally:
        fx.close()


async def test_unexpected_failure_still_answers_and_releases_step_and_guard(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider(RuntimeError("bug"), [_record()]))
    try:
        await fx.controller.handle_text(
            texts.CHECK_NOW_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        marker = await fx.controller.handle_text(_PLATE, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)

        failed = await fx.controller.handle_trusted_one_off_check(
            marker.trusted_one_off_plate, telegram_user_id=_TRUSTED_ID, chat_id=_TRUSTED_ID,
        )
        retried = await fx.controller.handle_trusted_one_off_check(_PLATE, telegram_user_id=_TRUSTED_ID)

        assert failed.text.startswith("❌ Проверка не выполнена")
        assert fx.conversation_state_repository.get(_TRUSTED_ID) is None
        assert "🚨 Обнаружен штраф" in retried.text
    finally:
        fx.close()


async def test_regular_user_cannot_use_any_one_off_entry_point(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider([_record()]))
    try:
        task_id = fx.make_task(status="completed")

        assert await fx.controller.handle_trusted_plate_choice_check(_PLATE, telegram_user_id=_USER_ID) is None
        assert await fx.controller.handle_trusted_one_off_check(_PLATE, telegram_user_id=_USER_ID) is None
        assert await fx.controller.handle_trusted_check_now_once(task_id, telegram_user_id=_USER_ID) is None
        assert fx.controller.handle_trusted_one_off_add(_PLATE, chat_id=_USER_ID, telegram_user_id=_USER_ID) is None
        assert fx.controller.handle_trusted_one_off_another(chat_id=_USER_ID, telegram_user_id=_USER_ID) is None
        assert fx.provider.calls == []
        assert fx.conversation_state_repository.get(_USER_ID) is None
    finally:
        fx.close()


async def test_choice_check_with_garbage_plate_payload_is_rejected(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider([_record()]))
    try:
        assert await fx.controller.handle_trusted_plate_choice_check("!!", telegram_user_id=_TRUSTED_ID) is None
        assert fx.provider.calls == []
    finally:
        fx.close()


async def test_add_to_monitoring_opens_existing_add_car_flow_with_period(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider([]))
    try:
        reply = fx.controller.handle_trusted_one_off_add(_PLATE, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

        assert reply.text == texts.ADD_CLIENT_DECISION_PROMPT
        assert reply.show_add_client_decision_buttons is True
        state = fx.conversation_state_repository.get(_TRUSTED_ID)
        assert state.step == STEP_AWAITING_CLIENT_DECISION
        assert state.payload == {"car_number": _PLATE}

        period_prompt = fx.controller.handle_add_client_decision(
            False, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID,
        )
        assert period_prompt.text == texts.PERIOD_PROMPT
        await fx.controller.handle_period_choice(
            30, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, first_name=None, last_name=None,
        )
        assert len(fx.task_repository.get_active_by_car_number(_PLATE)) == 1
    finally:
        fx.close()


async def test_check_another_plate_returns_to_plate_input(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider([]))
    try:
        reply = fx.controller.handle_trusted_one_off_another(chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

        assert reply.text == texts.CAR_NUMBER_PROMPT
        assert fx.conversation_state_repository.get(_TRUSTED_ID).step == STEP_AWAITING_TRUSTED_CHECK_NOW_CAR_NUMBER
        marker = await fx.controller.handle_text(
            "AA001AA", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        assert marker.trusted_one_off_plate == "AA001AA"
    finally:
        fx.close()


async def test_plate_sent_again_during_running_check_hits_guard_not_add_car(tmp_path):
    """Шаг ввода номера не сбрасывается, пока идёт проверка: повторно
    присланный номер не уходит в Add Car (ввод номера с главного экрана),
    а упирается в guard менеджера."""
    provider = _BlockingProvider(records=[])
    fx = _Fixture(tmp_path, provider)
    try:
        await fx.controller.handle_text(
            texts.CHECK_NOW_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        marker = await fx.controller.handle_text(_PLATE, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
        running = asyncio.ensure_future(fx.controller.handle_trusted_one_off_check(
            marker.trusted_one_off_plate, telegram_user_id=_TRUSTED_ID, chat_id=_TRUSTED_ID,
        ))
        await provider.entered.wait()

        again = await fx.controller.handle_text(_PLATE, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
        duplicate = await fx.controller.handle_trusted_one_off_check(
            again.trusted_one_off_plate, telegram_user_id=_TRUSTED_ID, chat_id=_TRUSTED_ID,
        )

        assert again.trusted_one_off_plate == _PLATE
        assert duplicate is CHECK_ALREADY_IN_PROGRESS

        provider.gate.set()
        result = await running
        assert "штрафов не найдено" in result.text
        assert provider.calls == [_PLATE, _PLATE]  # запрос + подтверждение, без третьего
        assert fx.conversation_state_repository.get(_TRUSTED_ID) is None
    finally:
        fx.close()


async def test_one_off_completion_keeps_a_flow_started_meanwhile(tmp_path):
    provider = _BlockingProvider(records=[])
    fx = _Fixture(tmp_path, provider)
    try:
        await fx.controller.handle_text(
            texts.CHECK_NOW_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        marker = await fx.controller.handle_text(_PLATE, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None)
        running = asyncio.ensure_future(fx.controller.handle_trusted_one_off_check(
            marker.trusted_one_off_plate, telegram_user_id=_TRUSTED_ID, chat_id=_TRUSTED_ID,
        ))
        await provider.entered.wait()

        await fx.controller.handle_text(
            texts.ADD_CAR_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        provider.gate.set()
        await running

        assert fx.conversation_state_repository.get(_TRUSTED_ID).step == STEP_AWAITING_CAR_NUMBER
    finally:
        fx.close()


async def test_one_off_shows_full_list_of_many_fines(tmp_path):
    records = [_record(fingerprint=f"fp-{i}", protocol=f"AB{i:04d}", amount=10.0) for i in range(30)]
    fx = _Fixture(tmp_path, _ScriptedProvider(records))
    try:
        reply = await fx.controller.handle_trusted_one_off_check(_PLATE, telegram_user_id=_TRUSTED_ID)

        assert "найдено штрафов — 30" in reply.text
        assert "💰 Штрафы: 300 ₾" in reply.text
        for i in range(30):
            assert f"📄 Штраф №: AB{i:04d}" in reply.text
    finally:
        fx.close()


async def test_one_off_total_is_unknown_when_any_amount_is_missing(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider([_record(amount=50.0), _record(fingerprint="fp-2", protocol="AB2", amount=None)]))
    try:
        reply = await fx.controller.handle_trusted_one_off_check(_PLATE, telegram_user_id=_TRUSTED_ID)

        assert "💰 Штрафы: неизвестно" in reply.text
    finally:
        fx.close()


async def test_one_off_error_says_check_not_performed_and_keeps_buttons(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider(FineProviderError("timeout")))
    try:
        reply = await fx.controller.handle_trusted_one_off_check(_PLATE, telegram_user_id=_TRUSTED_ID)

        assert reply.text.startswith("❌ Проверка не выполнена")
        assert "штрафов не найдено" not in reply.text
        assert _labels(reply) == [texts.TRUSTED_ONE_OFF_ANOTHER_LABEL, texts.TRUSTED_ONE_OFF_ADD_LABEL]
    finally:
        fx.close()


async def test_one_off_unavailable_provider_has_its_own_message(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider(PoliceGeUnavailableError("circuit open")))
    try:
        reply = await fx.controller.handle_trusted_one_off_check(_PLATE, telegram_user_id=_TRUSTED_ID)

        assert reply.text.startswith("❌ Проверка не выполнена: сервис police.ge сейчас недоступен.")
    finally:
        fx.close()


async def test_one_off_for_denylisted_plate_looks_like_no_fines(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider([_record(car_number=_DENYLISTED_PLATE)]))
    try:
        reply = await fx.controller.handle_trusted_one_off_check(_DENYLISTED_PLATE, telegram_user_id=_TRUSTED_ID)

        assert "штрафов не найдено" in reply.text
        assert fx.provider.calls == []
    finally:
        fx.close()


# ==== GE-2: reader/public_bot/handlers.py ====


class _FakeTelethonClient:
    def __init__(self):
        self.message_handler = None
        self.callback_handler = None

    def on(self, event_filter):
        def decorator(func):
            if isinstance(event_filter, events.CallbackQuery):
                self.callback_handler = func
            elif isinstance(event_filter, events.NewMessage):
                self.message_handler = func
            return func
        return decorator


class _FakeMessage:
    def __init__(self, calls_log: list[tuple], text: str, buttons):
        self._calls_log = calls_log
        self.text = text
        self.buttons = buttons

    async def edit(self, text, *, buttons=None):
        self._calls_log.append(("message_edit", text, buttons))
        self.text = text
        self.buttons = buttons


class _FakeEvent:
    """Общий фейк NewMessage/CallbackQuery — только то, что читает handlers.py."""

    def __init__(self, *, sender_id: int, raw_text: str | None = None, data: bytes | None = None):
        self.sender_id = sender_id
        self.chat_id = sender_id
        self.raw_text = raw_text
        self.data = data
        self.is_private = True
        self.calls: list[tuple] = []

    async def get_sender(self):
        return None

    async def answer(self, *args, **kwargs):
        self.calls.append(("answer", args, kwargs))

    async def edit(self, text, *, buttons=None):
        self.calls.append(("edit", text, buttons))

    async def respond(self, text, *, buttons=None, **kwargs):
        self.calls.append(("respond", text, buttons))
        return _FakeMessage(self.calls, text, buttons)


def _registered(fx: _Fixture) -> _FakeTelethonClient:
    client = _FakeTelethonClient()
    register(client, fx.controller, known_users_repository=fx.known_users_repository)
    return client


def _button_labels(buttons) -> list[str]:
    return [button.text for row in buttons for button in row]


async def test_trusted_plate_on_home_screen_gets_choice_buttons(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider([]))
    try:
        client = _registered(fx)
        event = _FakeEvent(sender_id=_TRUSTED_ID, raw_text=_PLATE)

        await client.message_handler(event)

        [(name, text, buttons)] = event.calls
        assert name == "respond"
        assert text == texts.format_trusted_plate_choice_prompt(_PLATE)
        assert _button_labels(buttons) == [texts.TRUSTED_ONE_OFF_CHECK_LABEL, texts.TRUSTED_ONE_OFF_MONITOR_LABEL]
        assert fx.provider.calls == []
    finally:
        fx.close()


async def test_plate_typed_after_check_button_shows_hourglass_then_edits_it(tmp_path):
    provider = _BlockingProvider(records=[_record()])
    fx = _Fixture(tmp_path, provider)
    try:
        client = _registered(fx)
        await client.message_handler(_FakeEvent(sender_id=_TRUSTED_ID, raw_text=texts.CHECK_NOW_LABEL))
        event = _FakeEvent(sender_id=_TRUSTED_ID, raw_text=_PLATE)

        pending = asyncio.ensure_future(client.message_handler(event))
        await provider.entered.wait()
        assert event.calls == [("respond", "⏳", None)]

        provider.gate.set()
        await pending

        assert [call[0] for call in event.calls] == ["respond", "message_edit"]
        _name, edited_text, buttons = event.calls[1]
        assert "🚨 Обнаружен штраф" in edited_text
        assert _button_labels(buttons) == [texts.TRUSTED_ONE_OFF_ANOTHER_LABEL, texts.TRUSTED_ONE_OFF_ADD_LABEL]
    finally:
        fx.close()


async def test_plate_typed_again_during_running_check_gets_in_progress_text(tmp_path):
    provider = _BlockingProvider(records=[_record()])
    fx = _Fixture(tmp_path, provider)
    try:
        client = _registered(fx)
        await client.message_handler(_FakeEvent(sender_id=_TRUSTED_ID, raw_text=texts.CHECK_NOW_LABEL))
        first = _FakeEvent(sender_id=_TRUSTED_ID, raw_text=_PLATE)
        pending = asyncio.ensure_future(client.message_handler(first))
        await provider.entered.wait()

        second = _FakeEvent(sender_id=_TRUSTED_ID, raw_text=_PLATE)
        await client.message_handler(second)

        assert second.calls == [("respond", texts.ONE_OFF_CHECK_IN_PROGRESS_TEXT, None)]
        provider.gate.set()
        await pending
        assert provider.calls == [_PLATE]
    finally:
        fx.close()


async def test_one_off_choice_button_acks_shows_hourglass_and_edits_result(tmp_path):
    provider = _BlockingProvider(records=[])
    fx = _Fixture(tmp_path, provider)
    try:
        client = _registered(fx)
        event = _FakeEvent(sender_id=_TRUSTED_ID, data=encode_trusted_one_off_check_callback(_PLATE))

        pending = asyncio.ensure_future(client.callback_handler(event))
        await provider.entered.wait()
        assert [call[0] for call in event.calls] == ["answer", "respond"]

        provider.gate.set()
        await pending

        assert [call[0] for call in event.calls] == ["answer", "respond", "message_edit"]
        assert "штрафов не найдено" in event.calls[2][1]
    finally:
        fx.close()


async def test_double_tap_on_one_off_button_for_monitored_plate_runs_one_guarded_check(tmp_path):
    """Номер с активной задачей — существующая task-level проверка, но с
    guard'ом и "⏳": второе нажатие только получает ack, второй проверки
    задачи (и второго сообщения) нет."""
    provider = _BlockingProvider(records=[_record()])
    fx = _Fixture(tmp_path, provider)
    try:
        fx.make_task(status="active")
        client = _registered(fx)
        first = _FakeEvent(sender_id=_TRUSTED_ID, data=encode_trusted_one_off_check_callback(_PLATE))
        pending = asyncio.ensure_future(client.callback_handler(first))
        await provider.entered.wait()

        second = _FakeEvent(sender_id=_TRUSTED_ID, data=encode_trusted_one_off_check_callback(_PLATE))
        await client.callback_handler(second)

        assert [call[0] for call in second.calls] == ["answer"]
        provider.gate.set()
        await pending
        assert [call[0] for call in first.calls] == ["answer", "respond", "message_edit"]
        assert provider.calls == [_PLATE]
    finally:
        fx.close()


async def test_regular_user_pressing_forged_one_off_buttons_is_refused(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider([_record()]))
    try:
        client = _registered(fx)
        check_event = _FakeEvent(sender_id=_USER_ID, data=encode_trusted_one_off_check_callback(_PLATE))
        add_event = _FakeEvent(sender_id=_USER_ID, data=encode_trusted_one_off_add_callback(_PLATE))

        await client.callback_handler(check_event)
        await client.callback_handler(add_event)

        assert [call[0] for call in check_event.calls] == ["answer", "respond"]
        assert check_event.calls[1][1] == texts.CALLBACK_NOT_AUTHORIZED_TEXT
        assert add_event.calls == [("answer", (texts.CALLBACK_NOT_AUTHORIZED_TEXT,), {"alert": True})]
        assert fx.provider.calls == []
    finally:
        fx.close()


async def test_add_and_another_buttons_answer_with_new_message_keeping_the_result(tmp_path):
    fx = _Fixture(tmp_path, _ScriptedProvider([]))
    try:
        client = _registered(fx)
        add_event = _FakeEvent(sender_id=_TRUSTED_ID, data=encode_trusted_one_off_add_callback(_PLATE))
        another_event = _FakeEvent(sender_id=_TRUSTED_ID, data=encode_trusted_one_off_another_callback())

        await client.callback_handler(add_event)
        await client.callback_handler(another_event)

        assert [call[0] for call in add_event.calls] == ["answer", "respond"]
        assert add_event.calls[1][1] == texts.ADD_CLIENT_DECISION_PROMPT
        assert [call[0] for call in another_event.calls] == ["answer", "respond"]
        assert another_event.calls[1][1] == texts.CAR_NUMBER_PROMPT
    finally:
        fx.close()


async def test_long_full_list_is_split_and_buttons_stay_under_the_last_part(tmp_path):
    records = [
        _record(
            fingerprint=f"fp-{i}", protocol=f"AB{i:04d}", amount=10.0,
            place="Тбилиси, проспект Шота Руставели, рядом со станцией метро Руставели",
        )
        for i in range(40)
    ]
    fx = _Fixture(tmp_path, _ScriptedProvider(records))
    try:
        client = _registered(fx)
        event = _FakeEvent(sender_id=_TRUSTED_ID, data=encode_trusted_one_off_check_callback(_PLATE))

        await client.callback_handler(event)

        names = [call[0] for call in event.calls]
        assert names[:3] == ["answer", "respond", "message_edit"]
        assert names.count("respond") >= 2  # "⏳" + хотя бы одно продолжение
        parts = [call for call in event.calls[2:] if call[0] in ("message_edit", "respond")]
        full_text = "".join(call[1] for call in parts)
        for i in range(40):
            assert f"📄 Штраф №: AB{i:04d}" in full_text
        assert [call for call in parts if call[2]] == [parts[-1]]
    finally:
        fx.close()
