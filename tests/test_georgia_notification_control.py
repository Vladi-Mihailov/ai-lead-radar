"""Пауза и граница уведомлений Грузии (reader/fines/notification_control.py):
во время паузы никому ничего не уходит, накопленное явно исключается при её
снятии (без отметки о доставке), после границы "новым штрафом" считается
только квитанция, впервые увиденная позже.
Repository — настоящие (SQLite, tmp_path); Telegram — заглушки; номера и
идентификаторы синтетические."""

import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from telethon import events  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.collector_auth import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import UNLIMITED_END_DATE  # noqa: E402
from reader.fines.notification_control import (  # noqa: E402
    EXCLUSION_KNOWN_BEFORE_BOUNDARY,
    EXCLUSION_NOTIFICATION_BOUNDARY,
    GeorgiaNotificationControl,
)
from reader.fines.notification_coordinator import FineNotificationCoordinator  # noqa: E402
from reader.fines.payment_status import FineStatusRepository  # noqa: E402
from reader.fines.plate_bootstrap_repository import GeorgiaPlateBootstrapRepository  # noqa: E402
from reader.fines.provider import FineProvider  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.notifications.base import NotificationResult, NotificationService  # noqa: E402
from reader.public_bot.collector_api import build_collector_api_app  # noqa: E402
from reader.public_bot.collector_ingest_service import CollectorIngestService  # noqa: E402
from reader.public_bot.conversation import BotReply  # noqa: E402
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository  # noqa: E402
from reader.public_bot.delivery_service import MAX_DELIVERY_ATTEMPTS, ClientDeliveryService  # noqa: E402
from reader.public_bot.handlers import register  # noqa: E402
from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402
from reader.public_bot.manual_bootstrap_service import ManualBootstrapService  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402

_CAR = "AA111BB"
_OWNER = 7001
_TBILISI = ZoneInfo("Asia/Tbilisi")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _set_first_detected(db_path, fine_id, value: datetime) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE detected_fines SET first_detected_at = ? WHERE id = ?",
                 (value.isoformat(sep=" "), fine_id))
    conn.commit()
    conn.close()


class _Sender:
    def __init__(self):
        self.sent: list[int] = []

    async def send_message(self, chat_id, text, *, buttons=None):
        self.sent.append(chat_id)


class _StatusLookup:
    """⏳/⚪ — квитанции, которые сейчас не доставляются (см.
    FineStatusTracker.undeliverable_protocols)."""

    def __init__(self):
        self.undeliverable: set[str] = set()

    def undeliverable_protocols(self, car_number):
        return set(self.undeliverable)


class _OperatorService(NotificationService):
    def __init__(self):
        self.notified: list[int] = []

    async def notify(self, events_):
        ids = [event.detected_fine_id for event in events_]
        self.notified.extend(ids)
        return NotificationResult(delivered_event_ids=ids, failed_event_ids=[])


class _Fixture:
    def __init__(self, tmp_path):
        self.db_path = tmp_path / "users.db"
        self.tasks = FineMonitoringTaskRepository(self.db_path)
        self.fines = DetectedFineRepository(self.db_path)
        self.subscriptions = FineSubscriptionRepository(self.db_path)
        self.deliveries = ClientFineDeliveryRepository(self.db_path)
        self.control = GeorgiaNotificationControl(self.db_path)
        self.status_lookup = _StatusLookup()
        self.sender = _Sender()
        self.client = ClientDeliveryService(
            self.fines, self.subscriptions, self.deliveries, self.sender, tz=_TBILISI,
            status_lookup=self.status_lookup, notification_control=self.control,
        )
        self.operator_service = _OperatorService()
        self.operator = FineNotificationCoordinator(
            self.fines, self.tasks, self.operator_service, notification_control=self.control,
        )

    def task(self, car=_CAR):
        return self.tasks.create(
            car_number=car, label=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
            telegram_chat_id=-1, created_by_user_id=1,
        )

    def fine(self, task, protocol_no, *, first_detected_at: datetime | None = None):
        fine = self.fines.create(
            monitoring_task_id=task.id, car_number=task.car_number, external_fine_id=protocol_no,
            fingerprint=f"fp-{task.id}-{protocol_no}", penalty_date=None, due_date=None,
            delivered_status=None, raw_data="{}", amount=50,
        )
        if first_detected_at is not None:
            _set_first_detected(self.db_path, fine.id, first_detected_at)
        return fine

    def subscribe(self, task, user_id=_OWNER):
        return self.subscriptions.create(
            monitoring_task_id=task.id, car_number=task.car_number, telegram_user_id=user_id,
            telegram_chat_id=user_id, telegram_username=None, start_date=date(2026, 11, 1),
            end_date=UNLIMITED_END_DATE,
        )

    def resume(self, *, at: datetime) -> None:
        client = self.client.exclude_pending(reason=EXCLUSION_NOTIFICATION_BOUNDARY)
        operator = self.operator.exclude_pending(reason=EXCLUSION_NOTIFICATION_BOUNDARY)
        self.control.resume(client_excluded=client, operator_excluded=operator, at=at)

    def close(self):
        for repository in (self.control, self.deliveries, self.subscriptions, self.fines, self.tasks):
            repository.close()


@pytest.fixture()
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


# ---- состояние ----

def test_initial_state_keeps_old_behaviour(fx):
    state = fx.control.state()
    assert (state.paused, state.paused_at, state.boundary_at) == (False, None, None)


def test_pause_is_idempotent_and_resume_records_the_boundary(fx):
    assert fx.control.pause(note="restore") is True
    first = fx.control.state().paused_at
    assert fx.control.pause() is False
    assert fx.control.state().paused_at == first

    boundary = datetime(2026, 11, 21, 6, 0, 0)
    fx.control.resume(client_excluded=3, operator_excluded=1, at=boundary)

    state = fx.control.state()
    assert (state.paused, state.boundary_at) == (False, boundary)
    assert [(action, c, o) for action, _note, c, o, _at in fx.control.log()] == [("pause", 0, 0), ("resume", 3, 1)]


def test_resume_without_pause_is_refused(fx):
    with pytest.raises(RuntimeError):
        fx.control.resume(client_excluded=0, operator_excluded=0)


def test_receipt_first_seen_is_the_earliest_of_all_tasks_and_statuses(fx):
    early, late = datetime(2026, 10, 1, 8, 0), datetime(2026, 11, 20, 8, 0)
    fx.fine(fx.task(), "GE1", first_detected_at=late)
    fx.fine(fx.task(), "GE1", first_detected_at=late + timedelta(hours=1))
    assert fx.control.receipt_first_seen_at(car_number=_CAR, protocol_no="GE1", row_first_detected_at=None) == late

    FineStatusRepository(fx.db_path).close()
    conn = sqlite3.connect(fx.db_path)
    conn.execute(
        "INSERT INTO georgia_fine_status (car_number, protocol_no, amount, first_listed_at, last_listed_at) "
        "VALUES (?, 'GE1', 50, ?, ?)", (_CAR, early.isoformat(), early.isoformat()),
    )
    conn.commit()
    conn.close()
    assert fx.control.receipt_first_seen_at(car_number=_CAR, protocol_no="GE1", row_first_detected_at=None) == early


# ---- клиенты и менеджеры (client delivery) ----

async def test_pause_sends_nothing_and_records_no_attempts(fx):
    task = fx.task()
    fine = fx.fine(task, "GE1")
    subscription = fx.subscribe(task)
    fx.control.pause()

    result = await fx.client.run_once()

    assert (result.delivered, result.failed, fx.sender.sent) == (0, 0, [])
    assert fx.deliveries.get(fine.id, subscription.id, "owner") is None


async def test_resume_excludes_the_queue_without_marking_it_delivered(fx):
    boundary = _utc_now() - timedelta(hours=1)
    task = fx.task()
    fine = fx.fine(task, "GE1", first_detected_at=boundary - timedelta(days=1))
    subscription = fx.subscribe(task)
    fx.control.pause()
    await fx.client.run_once()

    fx.resume(at=boundary)
    await fx.client.run_once()
    await fx.client.run_once()

    assert fx.sender.sent == []
    delivery = fx.deliveries.get(fine.id, subscription.id, "owner")
    assert delivery.delivered_at is None
    assert delivery.excluded_at is not None
    assert (delivery.exclusion_reason, delivery.attempt_count) == (EXCLUSION_NOTIFICATION_BOUNDARY, 0)


async def test_resume_excludes_pending_retries_but_leaves_exhausted_history(fx):
    boundary = _utc_now() - timedelta(hours=1)
    task = fx.task()
    retrying = fx.fine(task, "GE1", first_detected_at=boundary - timedelta(days=1))
    exhausted = fx.fine(task, "GE2", first_detected_at=boundary - timedelta(days=1))
    subscription = fx.subscribe(task)
    fx.deliveries.record_attempt(retrying.id, subscription.id, "owner")
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        fx.deliveries.record_attempt(exhausted.id, subscription.id, "owner")
    fx.control.pause()

    fx.resume(at=boundary)

    retry = fx.deliveries.get(retrying.id, subscription.id, "owner")
    assert (retry.attempt_count, retry.exclusion_reason) == (1, EXCLUSION_NOTIFICATION_BOUNDARY)
    history = fx.deliveries.get(exhausted.id, subscription.id, "owner")
    assert (history.attempt_count, history.excluded_at, history.delivered_at) == (MAX_DELIVERY_ATTEMPTS, None, None)


async def test_new_fine_after_the_boundary_is_sent_exactly_once(fx):
    task = fx.task()
    fx.subscribe(task)
    fx.control.pause()
    fx.resume(at=_utc_now() - timedelta(hours=1))
    fx.fine(task, "NEW1")

    await fx.client.run_once()
    await fx.client.run_once()

    assert fx.sender.sent == [_OWNER]


async def test_known_receipt_reaching_a_subscription_later_is_excluded(fx):
    """Восстановленная подписка, включённая заново машина: квитанции номера,
    известные с до границы, не становятся "новыми"."""
    boundary = _utc_now() - timedelta(hours=1)
    task = fx.task()
    fine = fx.fine(task, "OLD1", first_detected_at=boundary - timedelta(days=30))
    fx.control.pause()
    fx.resume(at=boundary)
    subscription = fx.subscribe(task)

    await fx.client.run_once()

    assert fx.sender.sent == []
    assert fx.deliveries.get(fine.id, subscription.id, "owner").exclusion_reason == EXCLUSION_KNOWN_BEFORE_BOUNDARY


async def test_known_receipt_on_a_new_task_row_is_not_a_new_fine(fx):
    boundary = _utc_now() - timedelta(hours=1)
    fx.fine(fx.task(), "OLD1", first_detected_at=boundary - timedelta(days=30))
    second = fx.task()
    fx.subscribe(second)
    fx.control.pause()
    fx.resume(at=boundary)
    fx.fine(second, "OLD1")  # повторное обнаружение той же квитанции другой задачей

    await fx.client.run_once()

    assert fx.sender.sent == []


async def test_unverified_receipt_confirmed_after_the_boundary_is_not_sent(fx):
    boundary = _utc_now() - timedelta(hours=1)
    task = fx.task()
    fx.fine(task, "HIST1", first_detected_at=boundary - timedelta(days=30))
    fx.subscribe(task)
    fx.status_lookup.undeliverable = {"HIST1"}  # ⏳
    fx.control.pause()
    fx.resume(at=boundary)

    fx.status_lookup.undeliverable = set()  # проверка подтвердила квитанцию
    await fx.client.run_once()

    assert fx.sender.sent == []


# ---- операторский чат ----

async def test_operator_flush_is_silent_while_paused(fx):
    fine = fx.fine(fx.task(), "GE1")
    fx.control.pause()

    result = await fx.operator.flush_pending()

    assert (result.delivered_event_ids, fx.operator_service.notified) == ([], [])
    assert [f.id for f in fx.fines.list_pending_notifications()] == [fine.id]


async def test_resume_excludes_the_operator_backlog_without_marking_it_sent(fx):
    fine = fx.fine(fx.task(), "GE1")
    fx.control.pause()

    fx.resume(at=_utc_now() + timedelta(minutes=1))
    await fx.operator.flush_pending()

    assert fx.operator_service.notified == []
    assert fx.fines.list_pending_notifications() == []
    assert fx.fines.get_by_fingerprint(fine.monitoring_task_id, fine.fingerprint).notification_sent_at is None
    assert fx.fines.operator_exclusion_reason(fine.id) == EXCLUSION_NOTIFICATION_BOUNDARY


async def test_operator_gets_only_receipts_first_seen_after_the_boundary(fx):
    boundary = _utc_now() - timedelta(hours=1)
    old = fx.fine(fx.task(), "OLD1", first_detected_at=boundary - timedelta(days=1))
    fx.fines.mark_notification_sent(old.id)
    fx.control.pause()
    fx.resume(at=boundary)
    second = fx.task()
    repeat = fx.fine(second, "OLD1")
    new = fx.fine(second, "NEW1")

    await fx.operator.flush_pending()

    assert fx.operator_service.notified == [new.id]
    assert fx.fines.operator_exclusion_reason(repeat.id) == EXCLUSION_KNOWN_BEFORE_BOUNDARY


# ---- побочные сообщения бота ----

class _NeverProvider(FineProvider):
    async def search_by_plate(self, plate):
        raise AssertionError("no police.ge requests in this test")


class _TelegramStub:
    def __init__(self):
        self.sent: list[int] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(chat_id)


@pytest.mark.parametrize("paused, expected", [(True, []), (False, [-100111])])
async def test_bootstrap_no_fines_message_is_held_while_paused(tmp_path, paused, expected):
    db_path = tmp_path / "users.db"
    tasks, fines = FineMonitoringTaskRepository(db_path), DetectedFineRepository(db_path)
    bootstrap = GeorgiaPlateBootstrapRepository(db_path)
    check_service = FineCheckService(_NeverProvider(), tasks, fines)
    ingest = CollectorIngestService(
        tasks, fines, check_service, bootstrap, ManualBootstrapService(bootstrap, tasks, fines, check_service),
    )
    telegram = _TelegramStub()
    app = build_collector_api_app(
        ingest, hmac_secret="s", telegram_client=telegram, notification_paused=lambda: paused,
    )
    tasks.create(car_number=_CAR, label=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
                 telegram_chat_id=-100111, created_by_user_id=1)
    bootstrap.ensure_pending(_CAR)
    body = json.dumps({"car_number": _CAR, "fines": []}).encode("utf-8")
    ts = str(time.time())
    headers = {
        TIMESTAMP_HEADER: ts, "Content-Type": "application/json",
        SIGNATURE_HEADER: sign("s", method="POST", path="/internal/georgia/bootstrap-result", timestamp=ts, body=body),
    }
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/internal/georgia/bootstrap-result", data=body, headers=headers)
            assert response.status == 200
        assert telegram.sent == expected
        assert bootstrap.is_completed(_CAR)  # bootstrap завершается и на паузе
    finally:
        for repository in (bootstrap, fines, tasks):
            repository.close()


class _Client:
    def __init__(self):
        self.message_handler = None

    def on(self, event_filter):
        def decorator(func):
            if isinstance(event_filter, events.NewMessage):
                self.message_handler = func
            return func
        return decorator


class _Controller:
    def is_trusted(self, user_id):
        return False

    def reset_conversation(self, chat_id):
        pass

    async def handle_text(self, text, **kwargs):
        return BotReply(text="ok", show_main_menu=True)


class _MessageEvent:
    def __init__(self, client):
        self.client = client
        self.sender_id = self.chat_id = 4242
        self.raw_text = "hello"
        self.is_private = True
        self.responses: list[str] = []

    async def get_sender(self):
        return None

    async def respond(self, text, **kwargs):
        self.responses.append(text)


@pytest.mark.parametrize("paused, expected", [(True, []), (False, [-500])])
async def test_manager_alerts_are_held_while_paused_but_the_user_gets_a_reply(tmp_path, paused, expected):
    known = BotKnownUsersRepository(tmp_path / "users.db")
    client = _Client()
    register(client, _Controller(), known, manager_chat_id=-500, notification_paused=lambda: paused)
    telegram = _TelegramStub()
    event = _MessageEvent(telegram)
    try:
        await client.message_handler(event)
    finally:
        known.close()
    assert telegram.sent == expected  # "новый пользователь" менеджерам
    assert event.responses == ["ok"]


async def test_exhausted_attempts_stay_as_history_after_the_boundary(fx):
    boundary = _utc_now() - timedelta(hours=1)
    task = fx.task()
    fine = fx.fine(task, "GE1", first_detected_at=boundary - timedelta(days=1))
    subscription = fx.subscribe(task)
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        fx.deliveries.record_attempt(fine.id, subscription.id, "owner")
    fx.control.pause()
    fx.resume(at=boundary)

    await fx.client.run_once()

    history = fx.deliveries.get(fine.id, subscription.id, "owner")
    assert (history.excluded_at, history.attempt_count, fx.sender.sent) == (None, MAX_DELIVERY_ATTEMPTS, [])


def _calls(module_path, name):
    """Вызовы name(...) в модуле — по ast, а не по тексту."""
    import ast
    tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == name
    ]


def test_bot_main_wires_the_pause_into_every_sender():
    """Без этой проводки пауза молча не действовала бы на бота."""
    main = PROJECT_ROOT / "reader" / "public_bot" / "main.py"
    expectations = {
        "ClientDeliveryService": "notification_control",
        "register": "notification_paused",
        "run_collector_api_forever": "notification_paused",
    }
    for name, keyword in expectations.items():
        calls = _calls(main, name)
        assert calls, name
        assert all(keyword in {kw.arg for kw in call.keywords} for call in calls), name


async def test_an_explicit_exclusion_holds_even_without_a_boundary(fx):
    """Строка без номера квитанции (старые записи), исключённая явно, не
    уходит и тогда, когда граница ещё не зафиксирована."""
    task = fx.task()
    fine = fx.fine(task, None)
    subscription = fx.subscribe(task)
    fx.deliveries.exclude(fine.id, subscription.id, "owner", reason="test")

    await fx.client.run_once()

    assert fx.sender.sent == []
    assert fx.deliveries.get(fine.id, subscription.id, "owner").delivered_at is None
