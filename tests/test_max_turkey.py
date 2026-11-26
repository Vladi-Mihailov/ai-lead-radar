"""MAX-бот Турции: диалог, ручная проверка, мониторинг MAX-подписчиков поверх
общей истории проверок, независимость от Telegram-данных. Без сети: фейковые
MAX API и UnifiedTurkeyCheckService, временная SQLite."""

import asyncio
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from reader.max_bot.api import MaxApiError
from reader.max_bot.linking import MaxTelegramLinkService
from reader.max_bot.runtime import MaxDispatcher, MaxResponder
from reader.max_bot.storage import MaxBotStorage
from reader.max_bot.turkey import keyboards as kb
from reader.max_bot.turkey.conversation import MaxTurkeyBot
from reader.max_bot.turkey.monitoring import MaxTurkeyMonitoring, MaxTurkeyMonitoringJob, last_telegram_slot_start
from reader.max_bot.turkey.repository import MaxTurkeyRepository
from reader.max_bot.turkey.service import MaxTurkeyService
from reader.max_bot.updates import parse_update
from reader.turkey_bot import texts as tr_texts
from reader.turkey_bot.monitoring.subscription_repository import TurkeyMonitoringSubscriptionRepository
from reader.turkey_bot.unified.models import (
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    total_amount_for,
)
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository

PLATE = "A123AA123"
USER = 42
OTHER = 77
MANAGER = 900
TELEGRAM_USER = 111
# 22:30 Europe/Istanbul (UTC+3) — время планового прогона MAX.
DAY1 = datetime(2026, 11, 26, 19, 30, tzinfo=timezone.utc)


def _provider(name, status, total="0", *, at=DAY1):
    status = ProviderStatus(status)
    return ProviderCheckResult(
        provider=name, status=status, debt_count=1 if status == ProviderStatus.HAS_DEBT else 0,
        principal_amount=None, penalty_amount=None, total_amount=Decimal(total), items=(),
        error_type="transport_error" if status == ProviderStatus.ERROR else None, checked_at=at,
    )


def _unified(plate, providers, *, at=DAY1):
    providers = tuple(providers)
    return UnifiedCheckResult(
        plate=plate, started_at=at - timedelta(minutes=1), finished_at=at,
        overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers), providers=providers,
    )


class FakeCheckService:
    """state: {provider: (status, total)} на номер; clock — finished_at."""

    def __init__(self):
        self.state = {}
        self.calls = []
        self.clock = DAY1

    def set(self, plate, **providers):
        self.state[plate] = providers

    async def check(self, plate, *, max_attempts=1, mode="manual", providers=None):
        self.calls.append((plate, mode, providers, max_attempts))
        wanted = providers or ("gib", "avrasya", "kgm")
        current = self.state.get(plate, {})
        results = [_provider(name, *current.get(name, ("no_debt", "0")), at=self.clock) for name in wanted]
        return _unified(plate, results, at=self.clock)


class FakeApi:
    def __init__(self):
        self.calls = []
        self._mid = 0

    async def send_message(self, *, text, user_id=None, chat_id=None, attachments=None, fmt="html", notify=True):
        self._mid += 1
        self.calls.append({"op": "send", "user_id": user_id, "text": text, "attachments": attachments})
        return f"mid{self._mid}"

    async def answer_callback(self, callback_id, *, message=None, notification=None, pace_key=None):
        self.calls.append({"op": "answer", "message": message, "notification": notification})

    async def edit_message(self, message_id, *, text, attachments=None, fmt="html", pace_key=None):
        self.calls.append({"op": "edit", "message_id": message_id, "text": text, "attachments": attachments})


class FakeSender:
    def __init__(self, fail_for=()):
        self.sent = []
        self.blocked = []
        self.fail_for = set(fail_for)

    async def send_notification(self, max_user_id, text, keyboard):
        if max_user_id in self.fail_for:
            raise MaxApiError(method="POST", path="/messages", status=500, code="internal", message=None)
        self.sent.append((max_user_id, text))

    def mark_blocked(self, max_user_id):
        self.blocked.append(max_user_id)


def _plain(html):
    return re.sub(r"<[^>]+>", "", html or "")


def _texts(api):
    result = []
    for call in api.calls:
        if call["op"] in ("send", "edit"):
            result.append(_plain(call["text"]))
        elif call["op"] == "answer":
            result.append(_plain((call["message"] or {}).get("text")) or (call["notification"] or ""))
    return result


def _payloads(call):
    attachments = call.get("attachments") or (call.get("message") or {}).get("attachments") or []
    return [b.get("payload") for a in attachments for row in a["payload"]["buttons"] for b in row]


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "users.db"
    storage = MaxBotStorage(db, bot="tr")
    repository = MaxTurkeyRepository(db)
    runs = TurkeyCheckRunRepository(db)
    checks = FakeCheckService()
    service = MaxTurkeyService(repository, runs, checks)
    api = FakeApi()
    bot = MaxTurkeyBot(
        service, storage, manager_user_ids=frozenset({MANAGER}), payment_help_url="https://t.me/help",
        telegram_bot_username="ProtocolTRbot", link_service=MaxTelegramLinkService(storage),
        telegram_cars_lookup=lambda telegram_id: [(PLATE, True), ("B777BB77", False)],
        telegram_subscriber_count=lambda plate: 1,
    )
    dispatcher = MaxDispatcher(storage, bot, MaxResponder(api))

    class Env:
        pass

    e = Env()
    e.db, e.storage, e.repository, e.runs, e.checks, e.service, e.api, e.bot, e.dispatcher = (
        db, storage, repository, runs, checks, service, api, bot, dispatcher,
    )
    counter = {"n": 0}

    def _next():
        counter["n"] += 1
        return counter["n"]

    def text(body, user=USER):
        n = _next()
        raw = {"update_type": "message_created", "timestamp": n, "message": {
            "sender": {"user_id": user, "first_name": "Иван"},
            "recipient": {"chat_id": 5000 + user, "chat_type": "dialog"}, "body": {"mid": f"in{n}", "text": body},
        }}
        asyncio.run(dispatcher.process(parse_update(raw)))

    def press(payload, user=USER):
        n = _next()
        raw = {"update_type": "message_callback", "timestamp": n, "callback": {
            "callback_id": f"cb{n}", "payload": payload, "user": {"user_id": user},
        }, "message": {"recipient": {"chat_id": 5000 + user, "chat_type": "dialog"}, "body": {"mid": f"bot{n}"}}}
        asyncio.run(dispatcher.process(parse_update(raw)))

    e.text, e.press = text, press
    yield e
    storage.close()
    repository.close()
    runs.close()


def _add_car(env, user=USER, plate=PLATE):
    env.text(plate, user=user)
    return env.repository.get_car_by_plate(user, plate)


# ---- диалог ----

def test_bot_started_sends_greeting_with_inline_menu(env):
    raw = {"update_type": "bot_started", "timestamp": 1, "chat_id": 5042, "user": {"user_id": USER}}
    asyncio.run(env.dispatcher.process(parse_update(raw)))
    call = env.api.calls[-1]
    assert call["op"] == "send" and call["user_id"] == USER
    assert "Штрафы Турции" in _plain(call["text"])
    assert {kb.ADD, kb.CARS, kb.CHECK, kb.HELP, kb.LINK} <= set(_payloads(call))
    assert kb.STATS not in _payloads(call)


def test_manager_menu_has_manager_buttons(env):
    env.text("/start", user=MANAGER)
    assert {kb.STATS, kb.SEARCH} <= set(_payloads(env.api.calls[-1]))


def test_add_car_via_button_then_invalid_then_valid_plate(env):
    env.press(kb.ADD)
    assert _texts(env.api)[-1] == tr_texts.ASK_PLATE_FOR_NEW_CAR_TEXT
    env.text("!!")
    assert _texts(env.api)[-1] == tr_texts.INVALID_PLATE_TEXT
    env.text("а123аа123")  # кириллица российского номера нормализуется
    car = env.repository.get_car_by_plate(USER, PLATE)
    assert car is not None
    assert "добавлен" in _texts(env.api)[-1]
    assert kb.action("chk", car.id) in _payloads(env.api.calls[-1])
    env.text(PLATE)
    assert "уже есть" in _texts(env.api)[-1]
    assert len(env.repository.list_cars(USER)) == 1


def test_manual_check_saves_shared_run_as_max_and_shows_result(env):
    car = _add_car(env)
    env.checks.set(PLATE, gib=("has_debt", "500"))
    env.press(kb.action("chk", car.id))
    assert env.checks.calls[-1][1] == "manual" and env.checks.calls[-1][3] == 35
    progress, result = env.api.calls[-2], env.api.calls[-1]
    assert progress["op"] == "answer" and "Проверяю" in _plain(progress["message"]["text"])
    assert result["op"] == "edit" and "Итого" in _plain(result["text"])
    row = sqlite3.connect(env.db).execute(
        "SELECT telegram_user_id, telegram_chat_id, initiator, plate FROM turkey_check_runs").fetchone()
    assert row == (0, 0, "max_manual", PLATE)
    assert env.repository.get_car_by_plate(USER, PLATE).last_overall_status == "has_debt"


def test_enable_disable_monitoring_and_history(env):
    car = _add_car(env)
    env.press(kb.action("on", car.id))
    assert env.service.monitoring_active(USER, PLATE)
    assert kb.action("off", car.id) in _payloads(env.api.calls[-1])
    env.press(kb.action("off", car.id))
    assert not env.service.monitoring_active(USER, PLATE)
    env.press(kb.action("hist", car.id))
    assert "История" in _texts(env.api)[-1]


def test_ownership_other_user_cannot_open_or_delete(env):
    car = _add_car(env)
    env.press(kb.action("car", car.id), user=OTHER)
    assert env.api.calls[-1]["notification"] == tr_texts.CAR_NOT_FOUND_TEXT
    env.press(kb.action("delok", car.id), user=OTHER)
    assert env.repository.get_car_by_plate(USER, PLATE) is not None
    env.press(kb.action("delok", car.id))
    assert env.repository.get_car_by_plate(USER, PLATE) is None


def test_manager_screens_only_for_managers(env):
    env.press(kb.STATS)
    assert env.api.calls[-1]["notification"] == "Раздел доступен только менеджерам."
    _add_car(env)
    env.press(kb.STATS, user=MANAGER)
    assert "Автомобилей: 1" in _texts(env.api)[-1]
    env.press(kb.SEARCH, user=MANAGER)
    env.text(PLATE, user=MANAGER)
    text = _texts(env.api)[-1]
    assert "MAX: в списках 1" in text and "Telegram: мониторинг 1" in text
    assert env.checks.calls == []  # поиск — только чтение, без живых проверок
    assert env.repository.get_car_by_plate(MANAGER, PLATE) is None


def test_georgia_payload_is_stale_for_turkey_bot(env):
    env.press("ge:cars")
    assert env.api.calls[-1]["notification"] == "Кнопка устарела — откройте меню заново."


def test_link_code_and_import_from_telegram(env):
    env.press(kb.LINK_NEW_CODE)
    code = re.search(r"/max_link (\w{4}-\w{4})", _texts(env.api)[-1]).group(1)
    assert MaxTelegramLinkService(env.storage).confirm(code, telegram_user_id=TELEGRAM_USER).status == "linked"
    env.press(kb.LINK_IMPORT)
    assert {car.plate for car in env.repository.list_cars(USER)} == {PLATE, "B777BB77"}
    assert env.service.monitoring_active(USER, PLATE)
    assert not env.service.monitoring_active(USER, "B777BB77")


# ---- независимость от Telegram ----

def test_max_actions_do_not_touch_telegram_subscription_or_baseline(env):
    telegram = TurkeyMonitoringSubscriptionRepository(env.db)
    telegram.enable(telegram_user_id=TELEGRAM_USER, telegram_chat_id=TELEGRAM_USER, plate=PLATE)
    before = telegram.get(telegram_user_id=TELEGRAM_USER, plate=PLATE)
    car = _add_car(env)
    env.press(kb.action("on", car.id))
    env.checks.set(PLATE, gib=("has_debt", "500"))
    env.press(kb.action("chk", car.id))
    env.press(kb.action("off", car.id))
    env.press(kb.action("delok", car.id))
    after = telegram.get(telegram_user_id=TELEGRAM_USER, plate=PLATE)
    assert after == before and after.active
    assert env.runs.get_last_successful_provider_result_for_subscriber(
        PLATE, "gib", telegram_user_id=TELEGRAM_USER) is None
    telegram.close()


# ---- мониторинг ----

def _monitoring(env, sender):
    return MaxTurkeyMonitoring(env.repository, env.runs, env.checks, sender)


def _enable(env, user=USER):
    car, _ = env.service.add_car(max_user_id=user, plate=PLATE)
    env.service.enable_monitoring(car)
    return car


def _run(env, monitoring, at):
    env.checks.clock = at
    return asyncio.run(monitoring.run_batch(now=at))


def test_first_run_stores_snapshot_without_notification(env):
    _enable(env)
    env.checks.set(PLATE, gib=("has_debt", "500"), kgm=("has_debt", "90"))
    sender = FakeSender()
    result = _run(env, _monitoring(env, sender), DAY1)
    assert sender.sent == [] and result.checks == 1
    assert env.checks.calls[-1][1:3] == ("scheduled", ("gib", "avrasya", "kgm"))
    assert set(env.repository.get_seen(env.repository.get_monitoring(USER, PLATE).id)) == {"gib", "avrasya", "kgm"}


def test_new_debt_notifies_once_and_not_again(env):
    _enable(env)
    sender = FakeSender()
    monitoring = _monitoring(env, sender)
    _run(env, monitoring, DAY1)
    env.checks.set(PLATE, gib=("has_debt", "500"))
    _run(env, monitoring, DAY1 + timedelta(days=1))
    assert len(sender.sent) == 1
    user, text = sender.sent[0]
    assert user == USER and "Новая задолженность" in text and PLATE in text
    _run(env, monitoring, DAY1 + timedelta(days=2))
    assert len(sender.sent) == 1


def test_failed_send_is_retried_next_run_and_does_not_block_others(env):
    _enable(env, USER)
    _enable(env, OTHER)
    sender = FakeSender(fail_for={USER})
    monitoring = _monitoring(env, sender)
    _run(env, monitoring, DAY1)
    env.checks.set(PLATE, kgm=("has_debt", "90"))
    calls_before = len(env.checks.calls)
    result = _run(env, monitoring, DAY1 + timedelta(days=1))
    assert [u for u, _ in sender.sent] == [OTHER] and result.failed == 1
    assert len(env.checks.calls) == calls_before + 1  # одна проверка на номер, не на подписчика
    sender.fail_for.clear()
    _run(env, monitoring, DAY1 + timedelta(days=2))
    assert [u for u, _ in sender.sent] == [OTHER, USER]


def test_recipient_unavailable_marks_blocked(env):
    _enable(env)
    monitoring_id = env.repository.get_monitoring(USER, PLATE).id

    class Blocking(FakeSender):
        async def send_notification(self, max_user_id, text, keyboard):
            raise MaxApiError(method="POST", path="/messages", status=403, code="chat.denied", message=None)

    sender = Blocking()
    monitoring = _monitoring(env, sender)
    _run(env, monitoring, DAY1)
    env.checks.set(PLATE, gib=("has_debt", "500"))
    _run(env, monitoring, DAY1 + timedelta(days=1))
    assert sender.blocked == [USER]
    assert env.repository.get_seen(monitoring_id)["gib"].status == ProviderStatus.NO_DEBT


def test_fresh_telegram_run_is_reused_without_new_check(env):
    _enable(env)
    sender = FakeSender()
    monitoring = _monitoring(env, sender)
    _run(env, monitoring, DAY1)
    calls_before = len(env.checks.calls)
    # Telegram-мониторинг в 21:00 следующего дня уже проверил номер.
    telegram_at = DAY1 + timedelta(days=1) - timedelta(minutes=45)
    env.runs.save(
        _unified(PLATE, [
            _provider("gib", "has_debt", "700", at=telegram_at), _provider("avrasya", "no_debt", at=telegram_at),
            _provider("kgm", "no_debt", at=telegram_at),
        ], at=telegram_at),
        telegram_user_id=TELEGRAM_USER, telegram_chat_id=TELEGRAM_USER, initiator="monitoring_21:00",
    )
    result = _run(env, monitoring, DAY1 + timedelta(days=1))
    assert len(env.checks.calls) == calls_before and result.checks == 0
    assert len(sender.sent) == 1 and "700" in sender.sent[0][1]


def test_only_stale_providers_are_checked(env):
    _enable(env)
    telegram_at = DAY1 - timedelta(minutes=45)
    env.runs.save(
        _unified(PLATE, [_provider("gib", "no_debt", at=telegram_at)], at=telegram_at),
        telegram_user_id=TELEGRAM_USER, telegram_chat_id=TELEGRAM_USER, initiator="monitoring_21:00",
    )
    _run(env, _monitoring(env, FakeSender()), DAY1)
    assert env.checks.calls[-1][2] == ("avrasya", "kgm")


def test_enable_uses_latest_known_result_as_baseline(env):
    known_at = DAY1 - timedelta(days=3)
    env.runs.save(
        _unified(PLATE, [_provider("gib", "has_debt", "500", at=known_at)], at=known_at),
        telegram_user_id=TELEGRAM_USER, telegram_chat_id=TELEGRAM_USER, initiator="manual",
    )
    _enable(env)
    env.checks.set(PLATE, gib=("has_debt", "500"))
    sender = FakeSender()
    _run(env, _monitoring(env, sender), DAY1)
    assert sender.sent == []  # известная задолженность новой не считается


def test_error_provider_is_never_compared(env):
    _enable(env)
    sender = FakeSender()
    monitoring = _monitoring(env, sender)
    env.checks.set(PLATE, gib=("has_debt", "500"))
    _run(env, monitoring, DAY1)
    env.checks.set(PLATE, gib=("error", "0"))
    _run(env, monitoring, DAY1 + timedelta(days=1))
    assert sender.sent == []


def test_job_runs_once_at_2230_istanbul():
    class Monitoring:
        runs = 0

        async def run_batch(self):
            Monitoring.runs += 1

    job = MaxTurkeyMonitoringJob(Monitoring())
    assert asyncio.run(job.should_run(DAY1))
    assert not asyncio.run(job.should_run(DAY1 - timedelta(minutes=1)))
    assert last_telegram_slot_start(DAY1) == datetime(2026, 11, 26, 18, 0, tzinfo=timezone.utc)
    assert last_telegram_slot_start(DAY1 - timedelta(hours=2)) == datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)


def test_job_waits_while_the_telegram_batch_is_still_writing_results():
    class Monitoring:
        async def run_batch(self):
            pass

    last_telegram = {"at": DAY1 - timedelta(minutes=3)}
    job = MaxTurkeyMonitoringJob(Monitoring(), telegram_last_finished_at=lambda: last_telegram["at"])
    assert not asyncio.run(job.should_run(DAY1))
    last_telegram["at"] = DAY1 - timedelta(minutes=11)
    assert asyncio.run(job.should_run(DAY1 + timedelta(minutes=1)))
    # С 23:30 — без ожидания, даже если Telegram ещё пишет.
    last_telegram["at"] = DAY1 + timedelta(minutes=59)
    assert asyncio.run(job.should_run(DAY1 + timedelta(hours=1)))
    asyncio.run(job.run())
    assert not asyncio.run(job.should_run(DAY1 + timedelta(hours=1, minutes=5)))


def test_last_telegram_monitoring_time_ignores_max_and_manual_runs(env):
    assert env.repository.last_telegram_monitoring_finished_at() is None
    telegram_at = DAY1 - timedelta(minutes=30)
    env.runs.save(
        _unified(PLATE, [_provider("gib", "no_debt", at=telegram_at)], at=telegram_at),
        telegram_user_id=TELEGRAM_USER, telegram_chat_id=TELEGRAM_USER, initiator="monitoring_21:00",
    )
    later = DAY1 - timedelta(minutes=5)
    for initiator in ("manual", "max_monitoring", "max_manual"):
        env.runs.save(
            _unified(PLATE, [_provider("gib", "no_debt", at=later)], at=later),
            telegram_user_id=0, telegram_chat_id=0, initiator=initiator,
        )
    assert env.repository.last_telegram_monitoring_finished_at() == telegram_at


def test_stale_result_of_another_user_is_not_a_baseline(env):
    """Ревью B1: результат 90-дневной давности — не база: долг, возникший
    до подписки, не приходит «новым»; первое наблюдение — молча."""
    old_at = DAY1 - timedelta(days=90)
    env.runs.save(
        _unified(PLATE, [_provider("gib", "no_debt", at=old_at)], at=old_at),
        telegram_user_id=TELEGRAM_USER, telegram_chat_id=TELEGRAM_USER, initiator="manual",
    )
    car, _ = env.service.add_car(max_user_id=USER, plate=PLATE)
    env.service.enable_monitoring(car, now=DAY1)
    assert env.repository.get_seen(env.repository.get_monitoring(USER, PLATE).id) == {}

    env.checks.set(PLATE, gib=("has_debt", "500"))
    sender = FakeSender()
    _run(env, _monitoring(env, sender), DAY1)
    assert sender.sent == []


def test_fresh_result_is_a_baseline_and_later_debt_is_new(env):
    fresh_at = DAY1 - timedelta(minutes=40)
    env.runs.save(
        _unified(PLATE, [_provider("gib", "no_debt", at=fresh_at)], at=fresh_at),
        telegram_user_id=TELEGRAM_USER, telegram_chat_id=TELEGRAM_USER, initiator="monitoring_21:00",
    )
    car, _ = env.service.add_car(max_user_id=USER, plate=PLATE)
    env.service.enable_monitoring(car, now=DAY1)
    assert set(env.repository.get_seen(env.repository.get_monitoring(USER, PLATE).id)) == {"gib"}

    env.checks.set(PLATE, gib=("has_debt", "500"))
    sender = FakeSender()
    _run(env, _monitoring(env, sender), DAY1 + timedelta(days=1))
    assert len(sender.sent) == 1 and "500" in sender.sent[0][1]


def test_provider_that_telegram_already_tried_tonight_is_not_rechecked(env):
    _enable(env)
    telegram_at = DAY1 - timedelta(minutes=45)
    env.runs.save(
        _unified(PLATE, [_provider("gib", "no_debt", at=telegram_at), _provider("kgm", "error", at=telegram_at)],
                 at=telegram_at),
        telegram_user_id=TELEGRAM_USER, telegram_chat_id=TELEGRAM_USER, initiator="monitoring_21:00",
    )
    _run(env, _monitoring(env, FakeSender()), DAY1)
    assert env.checks.calls[-1][2] == ("avrasya",)
