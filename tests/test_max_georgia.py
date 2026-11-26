"""MAX-бот Грузии (reader/max_bot/georgia/): сквозные сценарии через
настоящие репозитории SQLite, FineCheckService и PayableAmountService;
police.ge, Georgian Card и MAX API — заглушки. Номера и пользователи
синтетические.

Главное: одна задача мониторинга на номер для Telegram и MAX, независимые
подписки и доставки, никаких исторических уведомлений при подключении MAX,
дедуп и повторы доставки, права по user_id."""

import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.georgian_card_client import AmountQuote  # noqa: E402
from reader.fines.models import UNLIMITED_END_DATE, ParsedFineRecord  # noqa: E402
from reader.fines.notification_control import GeorgiaNotificationControl  # noqa: E402
from reader.fines.payable_amount import OUTCOME_OK, PayableAmountRepository  # noqa: E402
from reader.fines.payment_status import FineStatusRepository, FineStatusTracker  # noqa: E402
from reader.fines.plate_bootstrap_repository import GeorgiaPlateBootstrapRepository  # noqa: E402
from reader.fines.provider import FineProvider  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.fines.validation import normalize_car_number  # noqa: E402
from reader.max_bot.api import MaxApiError  # noqa: E402
from reader.max_bot.georgia.conversation import MaxGeorgiaBot  # noqa: E402
from reader.max_bot.georgia.delivery import MaxGeorgiaDelivery  # noqa: E402
from reader.max_bot.georgia.repository import MaxGeorgiaRepository  # noqa: E402
from reader.max_bot.georgia.service import MAX_TASK_OWNER, MaxGeorgiaService  # noqa: E402
from reader.max_bot.linking import MaxTelegramLinkService  # noqa: E402
from reader.max_bot.runtime import MaxDispatcher, MaxResponder  # noqa: E402
from reader.max_bot.storage import MaxBotStorage  # noqa: E402
from reader.max_bot.telegram_readonly import TelegramReadOnly  # noqa: E402
from reader.max_bot.updates import parse_update  # noqa: E402
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository  # noqa: E402
from reader.public_bot.payable_amount_service import PayableAmountService  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402

_TBILISI = ZoneInfo("Asia/Tbilisi")
_MAX_USER = 880001
_OTHER_MAX_USER = 880002
_MANAGER = 880099
_TELEGRAM_USER = 900000101
_CAR = "AA111BB"
_DUE = date(2099, 12, 31)
_PAY_URL = "https://t.me/payhelp"


async def _instant(_seconds):
    pass


def _record(car, protocol_no, amount):
    return ParsedFineRecord(
        car_number=car, external_fine_id=protocol_no, penalty_date=date(2026, 11, 1), due_date=_DUE,
        delivered_status="Не вручено", fingerprint=f"fp-{car}-{protocol_no}", raw_data={"protocolNo": protocol_no},
        amount=amount,
    )


class _Provider(FineProvider):
    def __init__(self):
        self.records: dict[str, list[ParsedFineRecord]] = {}
        self.calls: list[str] = []

    async def search_by_plate(self, plate):
        self.calls.append(plate)
        return list(self.records.get(plate, []))


class _FakeGeorgianCard:
    def __init__(self):
        self.configured = True
        self.amounts: dict[str, float] = {}
        self.calls: list[str] = []

    async def fetch_amount(self, *, car_number, protocol_no, base_amount):
        self.calls.append(protocol_no)
        return AmountQuote(OUTCOME_OK, amount=self.amounts.get(protocol_no, base_amount), currency="GEL")


class _FakeMaxApi:
    """Записывает всё, что бот отправил бы в MAX, в порядке отправки."""

    def __init__(self):
        self.log: list[dict] = []

    async def send_message(self, **kwargs):
        self.log.append({"kind": "send", "text": kwargs["text"], "attachments": kwargs.get("attachments")})
        return f"mid-{len(self.log)}"

    async def answer_callback(self, callback_id, *, message=None, notification=None, pace_key=None):
        if message is not None:
            self.log.append({"kind": "answer", "text": message["text"], "attachments": message.get("attachments")})
        if notification is not None:
            self.log.append({"kind": "notify", "text": notification, "attachments": None})

    async def edit_message(self, message_id, *, text, attachments=None, fmt=None, pace_key=None):
        self.log.append({"kind": "edit", "text": text, "attachments": attachments, "message_id": message_id})

    @property
    def answers(self) -> list[dict]:
        return [entry for entry in self.log if entry["kind"] == "answer"]

    def texts(self) -> list[str]:
        return [entry["text"] for entry in self.log]

    def last_text(self) -> str:
        return self.log[-1]["text"]

    def last_buttons(self) -> list[dict]:
        for entry in reversed(self.log):
            if entry["attachments"]:
                return [button for row in entry["attachments"][0]["payload"]["buttons"] for button in row]
        return []

    def clear(self):
        self.log.clear()


class _NotificationSender:
    def __init__(self, *, fail_with: Exception | None = None):
        self.sent: list[tuple[int, str, object]] = []
        self.blocked: list[int] = []
        self.fail_with = fail_with

    async def send_notification(self, max_user_id, text, keyboard):
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((max_user_id, text, keyboard))

    def mark_blocked(self, max_user_id):
        self.blocked.append(max_user_id)


class _Fixture:
    def __init__(self, tmp_path):
        self.db_path = tmp_path / "users.db"
        db = self.db_path
        self.tasks = FineMonitoringTaskRepository(db)
        self.fines = DetectedFineRepository(db)
        self.tracker = FineStatusTracker(FineStatusRepository(db))
        self.amounts = PayableAmountRepository(db)
        self.control = GeorgiaNotificationControl(db)
        self.bootstrap = GeorgiaPlateBootstrapRepository(db)
        self.telegram_subscriptions = FineSubscriptionRepository(db)
        self.telegram_deliveries = ClientFineDeliveryRepository(db)
        self.provider = _Provider()
        self.gc = _FakeGeorgianCard()
        self.check_service = FineCheckService(self.provider, self.tasks, self.fines, sleep=_instant,
                                              status_tracker=self.tracker)
        self.payable = PayableAmountService(
            self.amounts, self.gc, self.tracker, None, tz=_TBILISI, trusted_user_ids=frozenset(),
        )
        self.repository = MaxGeorgiaRepository(db)
        self.service = MaxGeorgiaService(
            self.repository, self.tasks, self.check_service, tz=_TBILISI, status_tracker=self.tracker,
            payable_amount=self.payable, plate_bootstrap=self.bootstrap,
        )
        self.storage = MaxBotStorage(db, bot="ge")
        self.api = _FakeMaxApi()
        self.links = MaxTelegramLinkService(self.storage)
        self.readonly = TelegramReadOnly(db)
        self.bot = MaxGeorgiaBot(
            self.service, self.storage, tz=_TBILISI, manager_user_ids=frozenset({_MANAGER}),
            payment_help_url=_PAY_URL, telegram_bot_username="ProtocolGEbot", link_service=self.links,
            telegram_cars_lookup=self.readonly.georgia_active_cars,
            telegram_subscriber_count=self.readonly.georgia_subscriber_count,
        )
        self.dispatcher = MaxDispatcher(self.storage, self.bot, MaxResponder(self.api))
        self.sender = _NotificationSender()
        self.amounts.set_increase_notifications(enabled=True, at=datetime(2026, 11, 1))
        self._seq = 0

    def delivery(self, sender=None):
        return MaxGeorgiaDelivery(
            self.repository, self.fines, sender or self.sender, tz=_TBILISI, payment_help_url=_PAY_URL,
            status_tracker=self.tracker, notification_control=self.control, payable_repository=self.amounts,
            plate_bootstrap=self.bootstrap,
        )

    def _next(self):
        self._seq += 1
        return self._seq

    async def text(self, text, *, user=_MAX_USER):
        raw = {
            "update_type": "message_created", "timestamp": self._next(),
            "message": {"sender": {"user_id": user, "first_name": "Анна"},
                        "recipient": {"chat_id": user + 1, "chat_type": "dialog"},
                        "body": {"mid": f"mid-in-{self._seq}", "text": text}},
        }
        await self.dispatcher.process(parse_update(raw))

    async def press(self, payload, *, user=_MAX_USER):
        raw = {
            "update_type": "message_callback", "timestamp": self._next(),
            "callback": {"callback_id": f"cb-{self._seq}", "payload": payload, "user": {"user_id": user}},
            "message": {"recipient": {"chat_id": user + 1, "chat_type": "dialog"}, "body": {"mid": f"kb-{self._seq}"}},
        }
        await self.dispatcher.process(parse_update(raw))

    async def add_car(self, car=_CAR, *, user=_MAX_USER):
        await self.press("ge:add", user=user)
        await self.text(car, user=user)
        await self.press("ge:addok", user=user)
        return self.repository.get_for_user_and_car(user, normalize_car_number(car))

    def sql(self, statement, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.execute(statement, params)
        conn.commit()
        conn.close()

    def age_fines(self, value="2026-01-01 00:00:00"):
        self.sql("UPDATE detected_fines SET first_detected_at = ?", (value,))

    def set_baseline(self, subscription_id, value):
        """Подписка (и первичная проверка номера) — в прошлом: всё, что
        найдено после, — новые штрафы."""
        self.sql("UPDATE max_ge_subscriptions SET baseline_at = ?, history_cutoff_at = ?, updated_at = ? "
                 "WHERE id = ?", (value, value, value, subscription_id))
        self.sql("UPDATE georgia_plate_bootstrap_state SET bootstrap_completed_at = ? "
                 "WHERE bootstrap_status = 'completed'", (value,))

    async def monitoring_check(self, car=_CAR):
        """Плановая проверка FineJob ai-lead-radar — тот же FineCheckService."""
        task = self.tasks.get_active_by_car_number(car)[0]
        await self.check_service.check_task(task)

    def telegram_subscription(self, car=_CAR, user=_TELEGRAM_USER):
        task = self.tasks.get_active_by_car_number(car)
        task_id = task[0].id if task else self.tasks.create(
            car_number=car, label=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
            telegram_chat_id=user, created_by_user_id=user, monitoring_scope="client_bot",
        ).id
        return self.telegram_subscriptions.create(
            monitoring_task_id=task_id, car_number=car, telegram_user_id=user, telegram_chat_id=user,
            telegram_username=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
        )


@pytest.fixture()
def fx(tmp_path):
    return _Fixture(tmp_path)


def _payloads(fx):
    return [button.get("payload") for button in fx.api.last_buttons()]


# ---- начало диалога и добавление автомобиля ----


async def test_start_shows_greeting_and_inline_menu(fx):
    await fx.dispatcher.process(parse_update({
        "update_type": "bot_started", "timestamp": 1, "chat_id": 5, "user": {"user_id": _MAX_USER},
    }))
    assert "Штрафы Грузии" in fx.api.texts()[0]
    assert {"ge:add", "ge:cars", "ge:check", "ge:help", "ge:link"} <= set(_payloads(fx))
    assert "ge:stats" not in _payloads(fx)
    assert fx.storage.get_user(_MAX_USER) is not None


async def test_add_car_creates_shared_client_task_and_shows_check_result(fx):
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50.0)]
    subscription = await fx.add_car("aa 111 bb")

    assert subscription is not None and subscription.is_active and not subscription.baseline_pending
    task = fx.tasks.get(subscription.monitoring_task_id)
    assert task.monitoring_scope == "client_bot"
    # Telegram-код шлёт клиенту по task.telegram_chat_id — у MAX-задачи там 0, не MAX id.
    assert task.telegram_chat_id == MAX_TASK_OWNER and task.created_by_user_id == MAX_TASK_OWNER
    assert fx.provider.calls == [_CAR]
    final = fx.api.last_text()
    assert "AA" in final and "1" in final
    assert fx.bootstrap.get(_CAR).is_completed


async def test_existing_telegram_task_is_reused_not_duplicated(fx):
    telegram = fx.telegram_subscription()
    subscription = await fx.add_car()
    assert subscription.monitoring_task_id == telegram.monitoring_task_id
    assert len(fx.tasks.get_active_by_car_number(_CAR)) == 1


async def test_repeated_add_does_not_create_a_second_subscription(fx):
    first = await fx.add_car()
    second = await fx.add_car()
    assert first.id == second.id
    assert len(fx.repository.list_by_user(_MAX_USER)) == 1
    assert "уже есть" in fx.api.last_text()


async def test_invalid_plate_keeps_asking(fx):
    await fx.press("ge:add")
    await fx.text("!!!")
    assert "❌" in fx.api.last_text()
    assert fx.storage.get_state(_MAX_USER).step == "awaiting_plate"
    assert fx.repository.list_by_user(_MAX_USER) == []


async def test_direct_plate_input_asks_for_confirmation_first(fx):
    await fx.text("AA111BB")
    assert "Добавить автомобиль" in fx.api.last_text()
    assert fx.repository.list_by_user(_MAX_USER) == []


async def test_stale_confirm_without_dialog_creates_nothing(fx):
    await fx.press("ge:addok")
    assert fx.repository.list_by_user(_MAX_USER) == []


# ---- проверка, карточка, права ----


async def test_check_now_shows_fines_with_refresh_and_payment_buttons(fx):
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50.0)]
    subscription = await fx.add_car()
    fx.api.clear()

    await fx.press(f"ge:chk:{subscription.id}")
    assert fx.api.answers[0]["text"].startswith("⏳")
    assert "50" in fx.api.last_text()
    payloads = _payloads(fx)
    assert f"ge:amt:{subscription.id}" in payloads
    assert any(button.get("url") == _PAY_URL for button in fx.api.last_buttons())


async def test_other_user_cannot_use_someone_elses_subscription(fx):
    subscription = await fx.add_car()
    fx.api.clear()
    for action in ("car", "chk", "off", "delok", "amt", "pay", "paid"):
        await fx.press(f"ge:{action}:{subscription.id}", user=_OTHER_MAX_USER)
    assert all(text == "Это действие недоступно — начните заново через меню." for text in fx.api.texts())
    assert fx.repository.get(subscription.id).status == "active"
    assert fx.provider.calls == [_CAR]


async def test_turn_off_on_and_delete_touch_only_the_max_subscription(fx):
    telegram = fx.telegram_subscription()
    subscription = await fx.add_car()

    await fx.press(f"ge:off:{subscription.id}")
    assert fx.repository.get(subscription.id).status == "stopped"
    assert fx.telegram_subscriptions.get(telegram.id).status == "active"
    assert fx.tasks.get(subscription.monitoring_task_id).status == "active"

    await fx.press(f"ge:on:{subscription.id}")
    assert fx.repository.get(subscription.id).status == "active"

    await fx.press(f"ge:del:{subscription.id}")
    await fx.press(f"ge:delok:{subscription.id}")
    assert fx.repository.get(subscription.id).status == "archived"
    assert fx.repository.list_by_user(_MAX_USER) == []
    # Удаление в MAX не останавливает мониторинг для Telegram-клиента.
    assert fx.tasks.get(subscription.monitoring_task_id).status == "active"
    assert fx.telegram_subscriptions.get(telegram.id).status == "active"


async def test_two_max_users_on_one_car_are_independent(fx):
    first = await fx.add_car(user=_MAX_USER)
    second = await fx.add_car(user=_OTHER_MAX_USER)
    assert first.monitoring_task_id == second.monitoring_task_id
    await fx.press(f"ge:delok:{first.id}")
    assert fx.repository.get(second.id).status == "active"
    assert fx.tasks.get(second.monitoring_task_id).status == "active"


# ---- уведомления о новых штрафах ----


async def test_history_is_not_sent_when_car_is_connected_in_max(fx):
    # Номер уже давно в мониторинге Telegram, штраф найден до подключения MAX.
    fx.telegram_subscription()
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50.0)]
    await fx.monitoring_check()
    fx.age_fines()
    fx.bootstrap.ensure_pending(_CAR)
    fx.bootstrap.mark_completed(_CAR, completed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    await fx.add_car()
    result = await fx.delivery().run_once()
    assert result.delivered == 0 and fx.sender.sent == []
    counts = fx.repository.fine_delivery_counts()
    assert counts["excluded"] == 1 and counts["pending"] == 0


async def test_fines_found_by_the_add_check_itself_are_not_sent_again(fx):
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50.0), _record(_CAR, "GE2", 70.0)]
    await fx.add_car()
    assert (await fx.delivery().run_once()).delivered == 0


async def test_new_fine_is_sent_once_with_payment_buttons(fx):
    subscription = await fx.add_car()
    fx.set_baseline(subscription.id, "2026-01-01 00:00:00")
    fx.provider.records[_CAR] = [_record(_CAR, "GE9", 40.0)]
    await fx.monitoring_check()

    delivery = fx.delivery()
    first = await delivery.run_once()
    second = await delivery.run_once()
    assert first.delivered == 1 and second.delivered == 0
    user_id, text, keyboard = fx.sender.sent[0]
    assert user_id == _MAX_USER and "новый штраф" in text
    assert keyboard[0][0].url == _PAY_URL
    # Telegram-доставки не затронуты.
    count = sqlite3.connect(fx.db_path).execute("SELECT COUNT(*) FROM client_fine_deliveries").fetchone()[0]
    assert count == 0


async def test_failed_max_delivery_is_retried_and_blocked_user_is_marked(fx):
    subscription = await fx.add_car()
    fx.set_baseline(subscription.id, "2026-01-01 00:00:00")
    fx.provider.records[_CAR] = [_record(_CAR, "GE9", 40.0)]
    await fx.monitoring_check()

    blocked = MaxApiError(method="POST", path="/messages", status=403, code="chat.denied", message="blocked")
    failing = _NotificationSender(fail_with=blocked)
    result = await fx.delivery(failing).run_once()
    assert result.failed == 1 and failing.blocked == [_MAX_USER]

    # Первый повтор — не раньше, чем через минуту (тот же backoff, что у Telegram).
    assert (await fx.delivery().run_once()).delivered == 0
    fx.sql("UPDATE max_ge_fine_deliveries SET last_attempt_at = '2026-01-01 00:00:00'")
    assert (await fx.delivery().run_once()).delivered == 1


async def test_turned_off_subscription_gets_nothing_until_turned_on(fx):
    subscription = await fx.add_car()
    fx.set_baseline(subscription.id, "2026-01-01 00:00:00")
    await fx.press(f"ge:off:{subscription.id}")
    fx.provider.records[_CAR] = [_record(_CAR, "GE9", 40.0)]
    await fx.monitoring_check()
    assert (await fx.delivery().run_once()).delivered == 0
    await fx.press(f"ge:on:{subscription.id}")
    assert (await fx.delivery().run_once()).delivered == 1


async def test_pause_blocks_and_boundary_excludes(fx):
    subscription = await fx.add_car()
    fx.set_baseline(subscription.id, "2026-01-01 00:00:00")
    fx.provider.records[_CAR] = [_record(_CAR, "GE9", 40.0)]
    await fx.monitoring_check()

    fx.control.pause()
    assert (await fx.delivery().run_once()).delivered == 0
    fx.control.resume(client_excluded=0, operator_excluded=0, at=datetime.now(timezone.utc))
    assert (await fx.delivery().run_once()).delivered == 0
    assert fx.repository.get_fine_delivery(fx.fines.list_by_car_number(_CAR)[0].id, subscription.id).exclusion_reason


async def test_pending_bootstrap_holds_then_history_is_excluded(fx):
    fx.provider.records[_CAR] = []
    subscription = await fx.add_car()
    fx.set_baseline(subscription.id, "2026-01-01 00:00:00")
    # Первичная проверка номера не завершена к моменту подписки — граница
    # истории ещё не зафиксирована.
    fx.sql("UPDATE max_ge_subscriptions SET history_cutoff_at = NULL")
    fx.sql("UPDATE georgia_plate_bootstrap_state SET bootstrap_status = 'pending', bootstrap_completed_at = NULL")
    fx.provider.records[_CAR] = [_record(_CAR, "OLD1", 30.0)]
    await fx.monitoring_check()

    assert (await fx.delivery().run_once()).delivered == 0
    assert fx.repository.fine_delivery_counts() == {"delivered": 0, "excluded": 0, "pending": 0}

    fx.bootstrap.mark_completed(_CAR, completed_at=datetime.now(timezone.utc))
    assert (await fx.delivery().run_once()).delivered == 0
    assert fx.repository.fine_delivery_counts()["excluded"] == 1


# ---- сумма к оплате и увеличения ----


async def _car_with_amount(fx):
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50.0)]
    subscription = await fx.add_car()
    fx.set_baseline(subscription.id, "2026-01-01 00:00:00")
    # Штраф найден до подписки — история; в этих тестах важны только увеличения.
    fx.age_fines("2025-12-31 00:00:00")
    return subscription


def _increase(fx, *, old=50.0, new=150.0):
    return fx.amounts.add_increase_event(
        car_number=_CAR, protocol_no="GE1", old_amount=old, new_amount=new,
        observed_at=datetime.now(timezone.utc).replace(tzinfo=None), check_id=None,
    )


async def test_refresh_amount_uses_negative_requester_id_and_shows_card(fx):
    subscription = await _car_with_amount(fx)
    fx.gc.amounts["GE1"] = 150.0
    fx.api.clear()
    await fx.press(f"ge:amt:{subscription.id}")

    assert fx.gc.calls == ["GE1"]
    requested_by = sqlite3.connect(fx.db_path).execute(
        "SELECT requested_by FROM georgia_fine_amount_checks").fetchone()[0]
    assert requested_by == -_MAX_USER
    assert "150" in fx.api.last_text()


async def test_increase_shown_in_refresh_reply_is_not_sent_again_to_that_subscription(fx):
    subscription = await _car_with_amount(fx)
    other = await fx.add_car(user=_OTHER_MAX_USER)
    fx.set_baseline(other.id, "2026-01-01 00:00:00")
    fx.payable.repository.store_amount(
        car_number=_CAR, protocol_no="GE1", amount=50.0, currency="GEL",
        observed_at=datetime(2026, 11, 20), source="manual", check_id=None,
    )
    fx.gc.amounts["GE1"] = 150.0
    await fx.press(f"ge:amt:{subscription.id}")
    assert fx.amounts.count_events() == 1

    await fx.delivery().run_once()
    assert [user for user, _, _ in fx.sender.sent] == [_OTHER_MAX_USER]


async def test_increase_after_subscription_is_sent_once_with_refresh_button(fx):
    subscription = await _car_with_amount(fx)
    _increase(fx)
    delivery = fx.delivery()
    assert (await delivery.run_once()).delivered == 1
    assert (await delivery.run_once()).delivered == 0
    _, text, keyboard = fx.sender.sent[0]
    assert "увеличилась" in text.lower()
    assert keyboard[0][0].payload == f"ge:amt:{subscription.id}"


async def test_increase_before_max_subscription_is_not_sent(fx):
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50.0)]
    subscription = await fx.add_car()
    fx.sql("UPDATE max_ge_subscriptions SET baseline_at = '2099-01-01 00:00:00'")
    _increase(fx)
    assert (await fx.delivery().run_once()).delivered == 0
    events = fx.amounts.events_since(datetime(2026, 1, 1))
    assert fx.repository.get_amount_delivery(events[0].id, subscription.id).exclusion_reason == "before_subscription"


async def test_same_amount_again_creates_no_second_notification(fx):
    await _car_with_amount(fx)
    assert _increase(fx) is not None
    assert _increase(fx) is None
    await fx.delivery().run_once()
    assert len(fx.sender.sent) == 1


# ---- разное ----


async def test_paid_fines_and_payable_card(fx):
    subscription = await _car_with_amount(fx)
    status = fx.tracker.statuses_by_protocol(_CAR)["GE1"]
    fx.tracker.mark_paid(status.id, actor_user_id=1, note="квитанция")

    await fx.press(f"ge:paid:{subscription.id}")
    assert "GE1" in fx.api.last_text()
    await fx.press(f"ge:pay:{subscription.id}")
    assert "неоплаченных штрафов для уточнения суммы нет" in fx.api.last_text()


async def test_manager_screens_need_the_max_allowlist(fx):
    await fx.press("ge:stats")
    assert fx.api.texts()[-1] == "Раздел доступен только менеджерам."

    fx.telegram_subscription()
    await fx.add_car(user=_OTHER_MAX_USER)
    await fx.press("ge:stats", user=_MANAGER)
    assert "MAX — штрафы Грузии" in fx.api.last_text()
    await fx.press("ge:search", user=_MANAGER)
    await fx.text(_CAR, user=_MANAGER)
    assert "Telegram 1, MAX 1" in fx.api.last_text()


async def test_turkey_payload_is_stale_for_the_georgia_bot(fx):
    await fx.press("tr:cars")
    assert fx.api.texts()[-1] == "Кнопка устарела — откройте меню заново."


async def test_link_and_import_cars_from_telegram_without_history(fx):
    fx.telegram_subscription()
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50.0)]
    await fx.monitoring_check()

    await fx.press("ge:import")
    assert "Сначала привяжите" in fx.api.last_text()

    await fx.press("ge:linkcode")
    code = fx.links.issue_code(_MAX_USER)
    assert fx.links.confirm(code, telegram_user_id=_TELEGRAM_USER).status == "linked"
    await fx.press("ge:import")

    imported = fx.repository.get_for_user_and_car(_MAX_USER, _CAR)
    assert imported is not None and imported.is_active
    assert fx.provider.calls == [_CAR]
    assert (await fx.delivery().run_once()).delivered == 0
    # Telegram-подписка не изменилась.
    assert fx.telegram_subscriptions.list_by_user(_TELEGRAM_USER)[0].status == "active"


def test_stale_pending_baseline_is_recovered(fx):
    task = fx.tasks.create(car_number=_CAR, label=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
                           telegram_chat_id=0, created_by_user_id=0, monitoring_scope="client_bot")
    pending = fx.repository.create_pending(max_user_id=_MAX_USER, car_number=_CAR, monitoring_task_id=task.id)
    # При запуске процесса — без ограничения по возрасту: ни одно добавление не идёт.
    assert fx.service.recover_pending_baselines() == 1
    assert not fx.repository.get(pending.id).baseline_pending


async def test_another_client_adding_the_same_car_does_not_hide_new_fines(fx):
    """Ревью B1/B2: повторная отметка завершения первичной проверки (новый
    клиент добавил номер) не сдвигает границу истории старой подписки."""
    subscription = await fx.add_car()
    fx.set_baseline(subscription.id, "2026-01-01 00:00:00")
    assert (await fx.delivery().run_once()).delivered == 0

    fx.provider.records[_CAR] = [_record(_CAR, "NEW1", 40.0)]
    await fx.add_car(user=_OTHER_MAX_USER)
    assert fx.bootstrap.get(_CAR).bootstrap_completed_at.replace(tzinfo=None) > datetime(2026, 1, 2)

    await fx.delivery().run_once()
    assert [user for user, _, _ in fx.sender.sent] == [_MAX_USER]


async def test_add_interrupted_without_restart_is_recovered_by_the_poller(fx):
    task = fx.tasks.create(car_number=_CAR, label=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
                           telegram_chat_id=0, created_by_user_id=0, monitoring_scope="client_bot")
    pending = fx.repository.create_pending(max_user_id=_MAX_USER, car_number=_CAR, monitoring_task_id=task.id)
    await fx.delivery().run_once()
    assert fx.repository.get(pending.id).baseline_pending
    fx.sql("UPDATE max_ge_subscriptions SET created_at = '2026-01-01 00:00:00'")
    await fx.delivery().run_once()
    assert not fx.repository.get(pending.id).baseline_pending
