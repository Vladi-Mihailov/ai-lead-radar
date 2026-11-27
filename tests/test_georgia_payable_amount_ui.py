"""Сумма к оплате Грузии в боте: карточка «🔎 Проверить сейчас» и «🔄
Уточнить сумму», статистика менеджера и уведомления об увеличении суммы
(reader/public_bot/conversation.py, texts.py, amount_delivery_service.py).

Сквозные — через настоящий ConversationController и SQLite; Georgian Card
и Telegram — заглушки. Номера и пользователи синтетические."""

import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.georgian_card_client import AmountQuote  # noqa: E402
from reader.fines.models import UNLIMITED_END_DATE, ParsedFineRecord  # noqa: E402
from reader.fines.notification_control import EXCLUSION_NOTIFICATION_BOUNDARY, GeorgiaNotificationControl  # noqa: E402
from reader.fines.payable_amount import OUTCOME_OK, PayableAmountRepository  # noqa: E402
from reader.fines.payment_status import FineStatusRepository, FineStatusTracker  # noqa: E402
from reader.fines.provider import FineProvider  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.plate_display import format_plate_for_telegram  # noqa: E402
from reader.public_bot import texts  # noqa: E402
from reader.jobs.notification_flush_job import NotificationFlushJob  # noqa: E402
from reader.public_bot.amount_delivery_service import (  # noqa: E402
    EXCLUSION_BEFORE_OPERATOR_NOTIFICATIONS,
    EXCLUSION_DUPLICATE_CHAT,
    EXCLUSION_RECEIPT_NOT_PAYABLE,
    EXCLUSION_SHOWN_IN_REPLY,
    OPERATOR_ROLE,
    OPERATOR_SUBSCRIPTION_ID,
    AmountIncreaseDeliveryService,
    AmountIncreaseOperatorNotifier,
)
from reader.public_bot.conversation import ConversationController  # noqa: E402
from reader.public_bot.conversation_state_repository import BotConversationStateRepository  # noqa: E402
from reader.public_bot.debt_refresh_service import DebtRefreshService  # noqa: E402
from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402
from reader.public_bot.payable_amount_service import PayableAmountService  # noqa: E402
from reader.public_bot.payable_amount_texts import (  # noqa: E402
    AmountSummary,
    ReceiptAmount,
    format_header_payable_lines,
    format_payable_lines,
    summarize,
)
from reader.public_bot.statistics_service import BotStatisticsService  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import SubscriptionService  # noqa: E402
from reader.users.models import TelegramUserInfo  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_TBILISI = ZoneInfo("Asia/Tbilisi")
_TRUSTED = 900000001
_OWNER = 900000101
_STRANGER = 900000102
_CAR = "AA111BB"
# 21.11.2026 15:20 Тбилиси
_REFRESHED_AT = datetime(2026, 11, 21, 11, 20, tzinfo=timezone.utc)
# Срок оплаты далеко в будущем: суммы не устаревают от реальной даты запуска тестов.
_DUE = date(2099, 12, 31)


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

    async def search_by_plate(self, plate):
        return list(self.records.get(plate, []))


class _FakeGeorgianCard:
    def __init__(self):
        self.configured = True
        self.amounts: dict[str, float] = {}
        self.calls: list[str] = []

    async def fetch_amount(self, *, car_number, protocol_no, base_amount):
        self.calls.append(protocol_no)
        return AmountQuote(OUTCOME_OK, amount=self.amounts.get(protocol_no, base_amount), currency="GEL")


class _Sender:
    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id, text, *, buttons=None):
        self.sent.append((chat_id, text, buttons))


class _Fixture:
    def __init__(self, tmp_path, *, with_amounts=True):
        db_path = tmp_path / "users.db"
        self.db_path = db_path
        self.tasks = FineMonitoringTaskRepository(db_path)
        self.fines = DetectedFineRepository(db_path)
        self.subscriptions = FineSubscriptionRepository(db_path)
        self.users = UserRepository(db_path)
        self.states = BotConversationStateRepository(db_path)
        self.known = BotKnownUsersRepository(db_path)
        self.status_repository = FineStatusRepository(db_path)
        self.tracker = FineStatusTracker(self.status_repository)
        self.amounts = PayableAmountRepository(db_path)
        self.control = GeorgiaNotificationControl(db_path)
        self.provider = _Provider()
        self.gc = _FakeGeorgianCard()
        self.clock_now = _REFRESHED_AT
        self.check_service = FineCheckService(
            self.provider, self.tasks, self.fines, sleep=_instant, status_tracker=self.tracker,
        )
        self.subscription_service = SubscriptionService(self.tasks, self.subscriptions, self.users, self.check_service)
        self.service = PayableAmountService(
            self.amounts, self.gc, self.tracker, self.subscriptions, tz=_TBILISI,
            trusted_user_ids=frozenset({_TRUSTED}), clock=lambda: self.clock_now,
        )
        self.sender = _Sender()
        self.delivery = AmountIncreaseDeliveryService(
            self.amounts, self.subscriptions, self.tracker, self.sender, tz=_TBILISI,
            notification_control=self.control,
        )
        # Уведомления об увеличении включены давно (тесты доставки); граница
        # включения проверяется отдельно.
        self.amounts.set_increase_notifications(enabled=True, at=datetime(2026, 11, 1))
        self.controller = ConversationController(
            self.states, self.subscription_service,
            BotStatisticsService(self.known, self.subscriptions, self.fines), self.known, tz=_TBILISI,
            trusted_operator_user_ids=frozenset({_TRUSTED}),
            debt_refresh_service=DebtRefreshService(self.tasks, self.check_service, sleep=_instant),
            fine_status_tracker=self.tracker,
            payable_amount_service=self.service if with_amounts else None,
            amount_delivery_service=self.delivery if with_amounts else None,
        )

    def car(self, car, fines, *, owner=None, created_by=None):
        task = self.tasks.create(
            car_number=car, label=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
            telegram_chat_id=-1, created_by_user_id=1,
        )
        self.provider.records[car] = [_record(car, protocol, amount) for protocol, amount in fines]
        subscription = None
        if owner is not None:
            subscription = self.subscriptions.create(
                monitoring_task_id=task.id, car_number=car, telegram_user_id=owner, telegram_chat_id=owner,
                telegram_username=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
                created_by_telegram_user_id=created_by, created_by_telegram_chat_id=created_by,
            )
            self.age_subscriptions()
        return task, subscription

    def age_subscriptions(self):
        """Подписки действуют давно — раньше любых событий теста."""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE fine_monitoring_subscriptions SET created_at = '2026-11-01 00:00:00', "
                     "updated_at = '2026-11-01 00:00:00'")
        conn.commit()
        conn.close()

    async def check(self, task):
        await self.check_service.check_task(self.tasks.get(task.id))

    def store(self, car, protocol, amount, *, at=_REFRESHED_AT):
        self.amounts.store_amount(
            car_number=car, protocol_no=protocol, amount=amount, currency="GEL",
            observed_at=at.replace(tzinfo=None) if at.tzinfo is None else at.astimezone(timezone.utc).replace(tzinfo=None),
            source="manual", check_id=None,
        )

    def close(self):
        for repository in (self.control, self.amounts, self.status_repository, self.known, self.states, self.users,
                           self.subscriptions, self.fines, self.tasks):
            repository.close()


@pytest.fixture()
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


# ---- Тексты ----


def _receipt(base, payable=None, at=None):
    return ReceiptAmount(protocol_no="X", base_amount=base, payable=payable,
                         observed_at=at.astimezone(timezone.utc).replace(tzinfo=None) if at else None)


def test_one_date_only_when_every_amount_is_known_and_it_is_the_earliest():
    later = _REFRESHED_AT + timedelta(hours=3)
    summary = summarize([_receipt(50, 150, _REFRESHED_AT), _receipt(100, 400, later)])
    assert format_header_payable_lines(summary, tz=_TBILISI) == [
        "💰 К оплате: 550 ₾", "🕒 Суммы уточнены: 21.11 в 15:20",
    ]


def test_partly_known_amounts_are_preliminary_with_a_count():
    summary = summarize([_receipt(50, 150, _REFRESHED_AT), _receipt(100)])
    lines = format_header_payable_lines(summary, tz=_TBILISI)
    assert lines == ["💰 Сумма к оплате: 250 ₾"]
    assert not any("🕒" in line for line in lines)


def test_unknown_amount_is_never_shown_as_confirmed():
    summary = summarize([_receipt(50)])
    assert format_payable_lines(summary, tz=_TBILISI) == ["💰 К оплате: требуется уточнение"]
    assert format_header_payable_lines(summary, tz=_TBILISI) == ["💰 Сумма к оплате: 50 ₾"]


def test_debt_row_with_amounts_matches_the_agreed_layout():
    row = texts.format_debt_row(
        car_number=_CAR, owners=[texts.DebtOwner(name="Иван Петров", username="ivan_example")], total_amount=150,
        breakdown=texts.DebtBreakdown(unpaid_amount=150, unpaid_count=3),
        amounts=summarize([_receipt(50, 150, _REFRESHED_AT), _receipt(50, 150, _REFRESHED_AT),
                           _receipt(50, 150, _REFRESHED_AT)]),
    )
    assert row.splitlines() == [
        f"🚗 Госномер: {format_plate_for_telegram(_CAR)}",
        "🔴 Неоплачено: 3 штрафа — 150 ₾",
        "💰 Сумма к оплате: 450 ₾",
        "👤 Пользователь: Иван Петров",
        "🔗 Telegram: @ivan_example",
    ]


def test_debt_row_with_partly_known_amounts_never_adds_original_and_refined():
    row = texts.format_debt_row(
        car_number=_CAR, owners=[], total_amount=150,
        breakdown=texts.DebtBreakdown(unpaid_amount=150, unpaid_count=2),
        amounts=summarize([_receipt(50, 150, _REFRESHED_AT), _receipt(100)]),
    )
    assert row.splitlines()[1:3] == ["🔴 Неоплачено: 2 штрафа — 150 ₾", "💰 Сумма к оплате: 250 ₾"]  # 150 + 100


def test_debt_row_without_amounts_is_unchanged():
    row = texts.format_debt_row(
        car_number=_CAR, owners=[], total_amount=150, breakdown=texts.DebtBreakdown(unpaid_amount=150, unpaid_count=3),
    )
    assert row.splitlines()[:2] == [f"🚗 Госномер: {format_plate_for_telegram(_CAR)}", "🔴 Неоплачено: 3 штрафа — 150 ₾"]


# ---- «🔎 Проверить сейчас» ----


async def test_check_now_card_shows_fine_and_amount_to_pay_with_refresh_button(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.store(_CAR, "GE1", 150)

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    lines = reply.text.splitlines()
    assert "🧾 Штраф: 50 ₾" in lines and "💰 К оплате: 150 ₾" in lines
    assert "🕒 Сумма уточнена: 21.11 в 15:20" in lines
    assert "💰 Сумма: 50 GEL" not in reply.text
    assert reply.cta_buttons[0] == [("🔄 Уточнить сумму", f"amtref:{subscription.id}".encode())]
    assert [label for label, _ in reply.cta_buttons[1]] == ["💳 Оплатить в рублях", "🚗 ОСАГО Грузии"]
    assert fx.gc.calls == []  # показ карточки не открывает сессий


async def test_check_now_fine_within_its_term_without_a_gateway_amount_shows_the_original_amount(fx):
    # Срок оплаты ещё идёт (до 31.12.2099), Georgian Card штраф не проверял.
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    lines = reply.text.splitlines()
    fine = lines.index("🧾 Штраф: 50 ₾")
    assert lines[fine + 1] == "💰 К оплате: 50 ₾"
    assert lines[-2:] == ["🧾 Штраф: 50 ₾", "💰 К оплате: 50 ₾"]
    assert "требуется уточнение" not in reply.text and "Предварительно" not in reply.text
    assert "уточнения" not in reply.text and "🕒" not in reply.text
    assert reply.cta_buttons[0] == [("🔄 Уточнить сумму", f"amtref:{subscription.id}".encode())]
    assert fx.gc.calls == [] and fx.amounts.count_events() == 0


async def test_check_now_without_the_feature_keeps_the_old_layout(tmp_path):
    fixture = _Fixture(tmp_path, with_amounts=False)
    task, subscription = fixture.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fixture.check(task)
    reply = await fixture.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)
    assert "💰 К оплате: 50 ₾" in reply.text and "💰 Сумма: 50 GEL" in reply.text
    assert "Уточнить" not in str(reply.cta_buttons)
    fixture.close()


# ---- «🔄 Уточнить сумму» ----


async def test_refresh_button_returns_the_agreed_card(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.gc.amounts["GE1"] = 150.0
    started = []

    async def on_started():
        started.append(True)

    reply = await fx.controller.handle_payable_amount_refresh(
        subscription.id, telegram_user_id=_OWNER, chat_id=_OWNER, on_check_started=on_started,
    )

    assert reply.text.splitlines() == [
        f"🚗 Автомобиль: {format_plate_for_telegram(_CAR)}",
        "🧾 Штраф: 50 ₾",
        "💰 К оплате: 150 ₾",
        "🕒 Сумма уточнена: 21.11 в 15:20",
    ]
    assert [label for label, _ in reply.cta_buttons[0]] == ["🔄 Уточнить сумму"]
    assert [label for label, _ in reply.cta_buttons[1]] == ["💳 Оплатить в рублях", "🚗 ОСАГО Грузии"]
    assert fx.gc.calls == ["GE1"] and started == [True]


async def test_refresh_by_someone_else_does_nothing(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    reply = await fx.controller.handle_payable_amount_refresh(
        subscription.id, telegram_user_id=_STRANGER, chat_id=_STRANGER,
    )
    assert reply is None and fx.gc.calls == []


async def test_repeated_refresh_shows_the_saved_amount_without_a_new_session(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    await fx.controller.handle_payable_amount_refresh(subscription.id, telegram_user_id=_OWNER, chat_id=_OWNER)
    fx.clock_now = _REFRESHED_AT + timedelta(hours=1)
    reply = await fx.controller.handle_payable_amount_refresh(subscription.id, telegram_user_id=_OWNER, chat_id=_OWNER)
    assert texts.PAYABLE_AMOUNT_RECENT_TEXT in reply.text
    assert len(fx.gc.calls) == 1


async def test_increase_shown_in_the_refresh_reply_is_not_sent_again(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.control.pause()
    fx.control.resume(client_excluded=0, operator_excluded=0, at=datetime(2026, 11, 1))
    fx.store(_CAR, "GE1", 100, at=_REFRESHED_AT - timedelta(days=1))
    fx.gc.amounts["GE1"] = 150.0

    reply = await fx.controller.handle_payable_amount_refresh(subscription.id, telegram_user_id=_OWNER, chat_id=_OWNER)
    assert "⚠️ Сумма к оплате по вашему штрафу увеличилась: 100 → 150 ₾" in reply.text

    await fx.delivery.run_once(now=_REFRESHED_AT + timedelta(minutes=3))
    assert fx.sender.sent == []
    delivery = fx.amounts.get_delivery(1, subscription.id, "owner")
    assert delivery.exclusion_reason == EXCLUSION_SHOWN_IN_REPLY and delivery.delivered_at is None


# ---- 📊 Статистика ----


async def test_statistics_shows_amounts_to_pay_and_creates_no_gateway_requests(fx):
    first, _ = fx.car("AA111BB", [("GE1", 50), ("GE2", 100)], owner=_OWNER)
    second, _ = fx.car("CD456EF", [("GE3", 100)])
    for task in (first, second):
        await fx.check(task)
    fx.store("AA111BB", "GE1", 150)
    fx.store("AA111BB", "GE2", 300)
    fx.store("CD456EF", "GE3", 100)

    lines = fx.controller._format_statistics_reply().text.splitlines()

    header = lines.index("🚨 Штрафы по последней проверке")
    assert lines[header:header + 6] == [
        "🚨 Штрафы по последней проверке",
        "Автомобили со штрафами: 2",
        "🔴 Неоплачено: 3 штрафа",
        "💰 Сумма к оплате: 550 ₾",
        "🕒 Суммы уточнены: 21.11 в 15:20",
        "🟢 Оплачено: 0 ₾ (0 шт.)",
    ]
    assert lines.count("🕒 Суммы уточнены: 21.11 в 15:20") == 1 and not any("Сумма уточнена" in line for line in lines)
    text = "\n".join(lines)
    assert (f"🚗 Госномер: {format_plate_for_telegram('AA111BB')}\n"
            "🔴 Неоплачено: 2 штрафа — 150 ₾\n💰 Сумма к оплате: 450 ₾\n") in text
    assert (f"🚗 Госномер: {format_plate_for_telegram('CD456EF')}\n"
            f"🔴 Неоплачено: 1 штраф — 100 ₾\n💰 Сумма к оплате: 100 ₾\n{texts.DEBT_OWNER_NOT_LINKED_LINE}") in text
    for removed in ("Активных подписок", "Остановленных", "⏳", "🗓", "База police.ge", "перепроверка", "ℹ️",
                    "⚠️", "Предварительно", "требует уточнения", "ещё в базе полиции", "🧾", "К оплате:"):
        assert not any(removed in line for line in lines), removed
    assert fx.gc.calls == []


async def test_statistics_with_unknown_amounts_is_preliminary(fx):
    first, _ = fx.car("AA111BB", [("GE1", 50), ("GE2", 100)], owner=_OWNER)
    await fx.check(first)
    fx.store("AA111BB", "GE1", 150)

    lines = fx.controller._format_statistics_reply().text.splitlines()

    assert lines.count("💰 Сумма к оплате: 250 ₾") == 2  # шапка и машина: 150 известно + 100 исходная
    assert not any("требует уточнения" in line or "Предварительно" in line for line in lines)
    assert not any(line.startswith("🕒 Сумм") for line in lines)  # «🕒 Сформировано» — время отчёта, не сумм
    assert fx.gc.calls == []


async def test_paid_fines_are_not_in_the_amount_to_pay(fx):
    task, _ = fx.car("AA111BB", [("GE1", 50), ("GE2", 100)], owner=_OWNER)
    await fx.check(task)
    fx.tracker.mark_paid(fx.tracker.statuses_by_protocol("AA111BB")["GE2"].id, actor_user_id=_TRUSTED, note="BOG 1")
    fx.store("AA111BB", "GE1", 150)
    fx.store("AA111BB", "GE2", 300)

    lines = fx.controller._format_statistics_reply().text.splitlines()

    assert lines.count("💰 Сумма к оплате: 150 ₾") == 2  # шапка и машина: оплаченный (300 ₾) не входит
    assert "🔴 Неоплачено: 1 штраф" in lines and "🔴 Неоплачено: 1 штраф — 50 ₾" in lines
    assert "🟢 Оплачено: 100 ₾ (1 шт.)" in lines and "🟢 Оплачено: 1 штраф — 100 ₾" in lines
    assert not any("300" in line or "450" in line for line in lines)


# ---- Уведомления об увеличении ----


def _event(fx, *, old=100.0, new=150.0, protocol="GE1", car=_CAR):
    return fx.amounts.add_increase_event(
        car_number=car, protocol_no=protocol, old_amount=old, new_amount=new,
        observed_at=_REFRESHED_AT.replace(tzinfo=None), check_id=None,
    )


async def test_increase_is_sent_once_to_the_client(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.control.pause()
    fx.control.resume(client_excluded=0, operator_excluded=0, at=datetime(2026, 11, 1))
    _event(fx)

    now = datetime.now(timezone.utc)
    await fx.delivery.run_once(now=now)
    await fx.delivery.run_once(now=now + timedelta(minutes=3))

    assert len(fx.sender.sent) == 1
    chat_id, text, buttons = fx.sender.sent[0]
    assert chat_id == _OWNER
    lines = text.splitlines()
    assert lines[0] == "⚠️ Сумма штрафа увеличилась!" and lines[1] == ""
    assert f"🚗 Автомобиль: {format_plate_for_telegram(_CAR)}" in lines
    assert "🧾 Первоначальный штраф: 50 ₾" in lines and "💰 Было к оплате: 100 ₾" in lines
    assert "💰 Теперь к оплате: 150 ₾" in lines and "📄 Штраф №: GE1" in lines
    assert buttons is not None


async def test_nothing_is_sent_while_paused_and_the_backlog_is_excluded_on_resume(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.control.pause()
    event_id = _event(fx)
    await fx.delivery.run_once()
    assert fx.sender.sent == [] and fx.amounts.get_delivery(event_id, subscription.id, "owner") is None

    fx.control.resume(client_excluded=0, operator_excluded=0, at=datetime.now(timezone.utc) + timedelta(seconds=1))
    await fx.delivery.run_once()
    delivery = fx.amounts.get_delivery(event_id, subscription.id, "owner")
    assert fx.sender.sent == []
    assert delivery.exclusion_reason == EXCLUSION_NOTIFICATION_BOUNDARY and delivery.delivered_at is None


async def test_receipt_paid_before_sending_is_excluded(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.tracker.mark_paid(fx.tracker.statuses_by_protocol(_CAR)["GE1"].id, actor_user_id=_TRUSTED, note="BOG 1")
    event_id = _event(fx)
    await fx.delivery.run_once()
    assert fx.sender.sent == []
    assert fx.amounts.get_delivery(event_id, subscription.id, "owner").exclusion_reason == EXCLUSION_RECEIPT_NOT_PAYABLE


async def test_subscriber_who_joined_after_the_increase_gets_nothing(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    _event(fx)
    import sqlite3
    conn = sqlite3.connect(fx.db_path)
    conn.execute("UPDATE georgia_fine_amount_events SET created_at = '2026-11-01 00:00:00'")
    conn.commit()
    conn.close()
    fx.subscriptions.create(
        monitoring_task_id=task.id, car_number=_CAR, telegram_user_id=_OWNER, telegram_chat_id=_OWNER,
        telegram_username=None, start_date=date(2026, 11, 2), end_date=UNLIMITED_END_DATE,
    )
    await fx.delivery.run_once(now=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc))
    assert fx.sender.sent == []


async def test_one_message_per_chat_even_with_two_subscriptions(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    second_task = fx.tasks.create(
        car_number=_CAR, label=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
        telegram_chat_id=-1, created_by_user_id=1,
    )
    second = fx.subscriptions.create(
        monitoring_task_id=second_task.id, car_number=_CAR, telegram_user_id=_OWNER, telegram_chat_id=_OWNER,
        telegram_username=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
    )
    fx.age_subscriptions()
    event_id = _event(fx)
    await fx.delivery.run_once()
    assert len(fx.sender.sent) == 1
    reasons = {fx.amounts.get_delivery(event_id, sid, "owner").exclusion_reason
               for sid in (second.id, second.id - 1)}
    assert EXCLUSION_DUPLICATE_CHAT in reasons


async def test_manager_of_a_delegated_subscription_gets_the_manager_text_without_buttons(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)], owner=_OWNER, created_by=_TRUSTED)
    await fx.check(task)
    _event(fx)
    await fx.delivery.run_once()
    by_chat = {chat_id: (text, buttons) for chat_id, text, buttons in fx.sender.sent}
    assert set(by_chat) == {_OWNER, _TRUSTED}
    assert by_chat[_TRUSTED][0].startswith("⚠️ Сумма штрафа увеличилась! (автомобиль, поставленный вами на мониторинг)")
    assert by_chat[_TRUSTED][1] is None


async def test_old_events_are_not_sent(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    _event(fx)
    await fx.delivery.run_once(now=datetime.now(timezone.utc) + timedelta(days=4))
    assert fx.sender.sent == []


def test_same_new_amount_is_one_event(fx):
    assert _event(fx, old=100, new=150) is not None
    assert _event(fx, old=120, new=150) is None
    assert _event(fx, old=150, new=200) is not None


def test_amount_summary_adds_up():
    total = sum([summarize([_receipt(50, 150, _REFRESHED_AT)]), summarize([_receipt(100)])], AmountSummary())
    assert (total.count, total.known_total, total.unknown_count, total.preliminary_total) == (2, 150, 1, 250)


# ---- После независимого review ----


async def test_poller_running_during_a_multi_fine_refresh_does_not_duplicate_the_shown_increase(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50), ("GE2", 60)], owner=_OWNER)
    await fx.check(task)
    fx.control.pause()
    fx.control.resume(client_excluded=0, operator_excluded=0, at=datetime(2026, 11, 1))
    fx.store(_CAR, "GE1", 100, at=_REFRESHED_AT - timedelta(days=1))
    fx.gc.amounts["GE1"] = 150.0
    original = fx.gc.fetch_amount

    async def fetch_with_poller(*, car_number, protocol_no, base_amount):
        if protocol_no == "GE2":
            # поллер доставки срабатывает, пока идёт сессия второго штрафа
            await fx.delivery.run_once(now=datetime.now(timezone.utc))
        return await original(car_number=car_number, protocol_no=protocol_no, base_amount=base_amount)

    fx.gc.fetch_amount = fetch_with_poller
    reply = await fx.controller.handle_payable_amount_refresh(subscription.id, telegram_user_id=_OWNER, chat_id=_OWNER)

    assert "100 → 150 ₾" in reply.text
    assert fx.sender.sent == []


async def test_refresh_button_of_a_deleted_car_opens_no_session(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.subscriptions.archive(subscription.id)
    reply = await fx.controller.handle_payable_amount_refresh(subscription.id, telegram_user_id=_OWNER, chat_id=_OWNER)
    assert reply is None and fx.gc.calls == []


async def test_too_old_event_is_excluded_explicitly(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    event_id = _event(fx)
    await fx.delivery.run_once(now=datetime.now(timezone.utc) + timedelta(days=4))
    delivery = fx.amounts.get_delivery(event_id, subscription.id, "owner")
    assert fx.sender.sent == [] and delivery.exclusion_reason == "event_too_old" and delivery.delivered_at is None


async def test_paid_fine_in_the_card_shows_the_fine_amount_without_amount_to_pay(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50), ("GE2", 100)], owner=_OWNER)
    await fx.check(task)
    fx.tracker.mark_paid(fx.tracker.statuses_by_protocol(_CAR)["GE2"].id, actor_user_id=_TRUSTED, note="BOG 1")
    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)
    assert "💰 Сумма:" not in reply.text
    assert "🧾 Штраф: 100 ₾" in reply.text.splitlines()
    assert "🟢 Оплачено, ждёт обновления базы полиции: 100 ₾" in reply.text


# ---- Запоздалые уведомления и граница включения ----

import sqlite3 as _sqlite3  # noqa: E402

from reader.public_bot.amount_delivery_service import (  # noqa: E402
    EXCLUSION_BEFORE_NOTIFICATIONS,
    EXCLUSION_SUBSCRIPTION_CHANGED,
)


def _hours_ago(hours: float) -> str:
    """Время в прошлом относительно реальных часов (run_once берёт "сейчас"
    из них) — тесты не зависят от даты запуска."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _set_event_created(fx, event_id, value: str):
    conn = _sqlite3.connect(fx.db_path)
    conn.execute("UPDATE georgia_fine_amount_events SET created_at = ? WHERE id = ?", (value, event_id))
    conn.commit()
    conn.close()


async def test_increase_notifications_off_send_and_mark_nothing(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.amounts.set_increase_notifications(enabled=False, at=datetime.now(timezone.utc))
    event_id = _event(fx)
    await fx.delivery.run_once()
    assert fx.sender.sent == [] and fx.amounts.get_delivery(event_id, subscription.id, "owner") is None


async def test_first_enable_never_sends_increases_found_before_it(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.amounts.set_increase_notifications(enabled=False, at=datetime.now(timezone.utc))
    old = _event(fx, old=100, new=150)
    _set_event_created(fx, old, _hours_ago(2))
    fx.amounts.set_increase_notifications(
        enabled=True, at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    new = _event(fx, old=150, new=200)

    await fx.delivery.run_once()

    assert len(fx.sender.sent) == 1 and "💰 Теперь к оплате: 200 ₾" in fx.sender.sent[0][1]
    assert fx.amounts.get_delivery(old, subscription.id, "owner").exclusion_reason == EXCLUSION_BEFORE_NOTIFICATIONS
    assert fx.amounts.get_delivery(new, subscription.id, "owner").delivered_at is not None


def test_restart_keeps_the_enable_boundary_and_off_on_resets_it(fx):
    first = fx.amounts.set_increase_notifications(enabled=True, at=datetime(2026, 11, 21, 12, 0))
    assert first == datetime(2026, 11, 1)  # включены раньше (фикстура) — перезапуск ничего не сдвигает
    fx.amounts.set_increase_notifications(enabled=False, at=datetime(2026, 11, 21, 12, 5))
    assert fx.amounts.increase_notifications_since() is None
    again = fx.amounts.set_increase_notifications(enabled=True, at=datetime(2026, 11, 21, 12, 10))
    assert again == datetime(2026, 11, 21, 12, 10)


async def test_increase_found_while_the_manager_had_stopped_the_subscription_is_not_sent_after_resume(fx):
    """⛔ менеджера -> рост суммы найден -> ▶️ менеджера: позднего «увеличилась»
    нет; следующий рост после включения приходит, один раз."""
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.subscription_service.stop_task_for_trusted_admin(task.id, stopped_by=_TRUSTED)
    during = _event(fx, old=100, new=150)
    _set_event_created(fx, during, _hours_ago(2))
    conn = _sqlite3.connect(fx.db_path)
    conn.execute("UPDATE georgia_fine_amount_events SET observed_at = created_at")
    conn.commit()
    conn.close()

    fx.subscriptions.restore_after_manager_resume(
        task.id, now=datetime.now(timezone.utc).replace(tzinfo=None), before_restore=lambda sub: 0,
    )
    assert fx.subscriptions.get(subscription.id).status == "active"
    await fx.delivery.run_once()
    await fx.delivery.run_once()
    assert fx.sender.sent == []
    assert fx.amounts.get_delivery(during, subscription.id, "owner").exclusion_reason == EXCLUSION_SUBSCRIPTION_CHANGED

    import time
    time.sleep(1.1)  # следующее событие — строго позже включения подписки
    after = _event(fx, old=150, new=200)
    await fx.delivery.run_once()
    await fx.delivery.run_once()
    assert [chat for chat, _, _ in fx.sender.sent] == [_OWNER]
    assert fx.amounts.get_delivery(after, subscription.id, "owner").delivered_at is not None


async def test_increase_found_while_the_client_had_notifications_off_is_not_sent_after_his_own_on(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.subscription_service.turn_off_car(subscription.id, telegram_user_id=_OWNER)
    during = _event(fx)
    _set_event_created(fx, during, _hours_ago(2))
    fx.subscription_service.turn_on_car(subscription.id, telegram_user_id=_OWNER, today=datetime.now(timezone.utc).date())
    await fx.delivery.run_once()
    assert fx.sender.sent == []
    assert fx.amounts.get_delivery(during, subscription.id, "owner").exclusion_reason == EXCLUSION_SUBSCRIPTION_CHANGED


async def test_repeated_ticks_and_restarts_never_send_the_excluded_history(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.subscription_service.turn_off_car(subscription.id, telegram_user_id=_OWNER)
    during = _event(fx)
    _set_event_created(fx, during, _hours_ago(2))
    fx.subscription_service.turn_on_car(subscription.id, telegram_user_id=_OWNER, today=datetime.now(timezone.utc).date())
    for _ in range(3):
        await fx.delivery.run_once()
        # "перезапуск": новый сервис над той же БД
        fx.delivery = AmountIncreaseDeliveryService(
            fx.amounts, fx.subscriptions, fx.tracker, fx.sender, tz=_TBILISI, notification_control=fx.control,
        )
    assert fx.sender.sent == []


async def test_failed_first_attempt_then_off_and_on_does_not_resend_the_old_increase(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    event_id = _event(fx)
    _set_event_created(fx, event_id, _hours_ago(1))

    class _Failing:
        async def send_message(self, chat_id, text, *, buttons=None):
            raise RuntimeError("telegram down")

    failing = AmountIncreaseDeliveryService(
        fx.amounts, fx.subscriptions, fx.tracker, _Failing(), tz=_TBILISI, notification_control=fx.control,
    )
    await failing.run_once()
    assert fx.amounts.get_delivery(event_id, subscription.id, "owner").attempt_count == 1

    fx.subscription_service.turn_off_car(subscription.id, telegram_user_id=_OWNER)
    fx.subscription_service.turn_on_car(subscription.id, telegram_user_id=_OWNER, today=datetime.now(timezone.utc).date())
    await fx.delivery.run_once(now=datetime.now(timezone.utc) + timedelta(minutes=2))

    assert fx.sender.sent == []
    delivery = fx.amounts.get_delivery(event_id, subscription.id, "owner")
    assert delivery.exclusion_reason == EXCLUSION_SUBSCRIPTION_CHANGED and delivery.delivered_at is None

# ---- Сумма Georgian Card ниже исходной (решение владельца 2026-11-21) ----


@pytest.mark.parametrize("gc_amount, shown", [(40, "50 ₾"), (50, "50 ₾"), (150, "150 ₾")])
async def test_amount_to_pay_is_never_shown_below_the_original_fine(fx, gc_amount, shown):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.store(_CAR, "GE1", gc_amount)

    card = (await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)).text.splitlines()
    stats = fx.controller._format_statistics_reply().text.splitlines()

    assert "🧾 Штраф: 50 ₾" in card and f"💰 К оплате: {shown}" in card
    assert stats.count(f"💰 Сумма к оплате: {shown}") == 2  # шапка и машина
    assert fx.amounts.get_amount(_CAR, "GE1").amount == gc_amount  # ответ шлюза хранится как есть
    assert fx.gc.calls == []


async def test_total_to_pay_uses_the_original_where_the_gateway_answered_less(fx):
    task, _ = fx.car(_CAR, [("GE1", 50), ("GE2", 100)], owner=_OWNER)
    await fx.check(task)
    fx.store(_CAR, "GE1", 40)
    fx.store(_CAR, "GE2", 300)

    lines = fx.controller._format_statistics_reply().text.splitlines()

    assert lines.count("💰 Сумма к оплате: 350 ₾") == 2  # 50 (не 40) + 300
    assert "🔴 Неоплачено: 2 штрафа — 150 ₾" in lines


@pytest.mark.parametrize("new_amount", [40.0, 45.0, 50.0])
async def test_gateway_rising_up_to_the_original_is_not_an_increase(fx, new_amount):
    """Клиенту показывали 50 ₾ (ответ шлюза 40 ниже исходной): ответ 45 или
    50 — та же показанная сумма, события и уведомления нет."""
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.store(_CAR, "GE1", 40, at=_REFRESHED_AT - timedelta(days=1))
    fx.gc.amounts["GE1"] = new_amount

    reply = await fx.controller.handle_payable_amount_refresh(subscription.id, telegram_user_id=_OWNER, chat_id=_OWNER)

    assert fx.gc.calls == ["GE1"]
    assert "увеличилась" not in reply.text and "💰 К оплате: 50 ₾" in reply.text.splitlines()
    assert fx.amounts.events_since(datetime(2000, 1, 1)) == []
    assert fx.amounts.get_amount(_CAR, "GE1").amount == new_amount  # ответ шлюза хранится как есть


async def test_increase_after_a_below_original_answer_counts_from_the_shown_amount(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.store(_CAR, "GE1", 40, at=_REFRESHED_AT - timedelta(days=1))
    fx.gc.amounts["GE1"] = 150.0

    reply = await fx.controller.handle_payable_amount_refresh(subscription.id, telegram_user_id=_OWNER, chat_id=_OWNER)

    assert "⚠️ Сумма к оплате по вашему штрафу увеличилась: 50 → 150 ₾" in reply.text
    [event] = fx.amounts.events_since(datetime(2000, 1, 1))
    assert (event.old_amount, event.new_amount) == (50, 150)


# ---- Увеличение суммы — оператору (main-процесс) ----


class _OperatorChannel:
    """Заглушка TelegramNotificationService.notify_text: fail — Telegram недоступен."""

    def __init__(self, *, fail=False):
        self.fail = fail
        self.texts: list[str] = []

    async def notify_text(self, text):
        if self.fail:
            raise RuntimeError("telegram is down")
        self.texts.append(text)
        return True


class _FailingSender:
    async def send_message(self, chat_id, text, *, buttons=None):
        raise RuntimeError("telegram is down")


def _operator(fx, channel, *, since=datetime(2026, 11, 1)):
    fx.amounts.start_operator_increase_notifications(at=since)
    return AmountIncreaseOperatorNotifier(
        fx.amounts, fx.tracker, channel, user_repository=fx.users, notification_control=fx.control,
    )


def _link_client(fx, *, user_id=_OWNER, username="ivan_example", first_name="Иван", last_name="Петров", car=_CAR):
    fx.users.upsert(TelegramUserInfo(
        user_id=user_id, username=username, first_name=first_name, last_name=last_name, is_bot=False,
    ))
    fx.users.add_car_numbers(user_id, [car])


async def _car_with_open_notifications(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.control.pause()
    fx.control.resume(client_excluded=0, operator_excluded=0, at=datetime(2026, 11, 1))
    return subscription


async def test_new_increase_reaches_both_the_client_and_the_operator(fx):
    subscription = await _car_with_open_notifications(fx)
    _link_client(fx)
    channel = _OperatorChannel()
    operator = _operator(fx, channel)
    event_id = _event(fx)

    await fx.delivery.run_once()
    await operator.run_once()

    assert [chat_id for chat_id, _, _ in fx.sender.sent] == [_OWNER]
    assert fx.sender.sent[0][1].splitlines()[0] == "⚠️ Сумма штрафа увеличилась!"  # клиенту — прежний формат
    assert channel.texts == ["\n".join([
        "⚠️ Увеличилась сумма штрафа!",
        "",
        f"🚗 Госномер: {_CAR}",
        "🧾 Первоначальный штраф: 50 ₾",
        "💰 Было к оплате: 100 ₾",
        "💰 Стало к оплате: 150 ₾",
        "📈 Увеличение: +50 ₾",
        "👤 Клиент: Иван Петров",
        "🔗 Telegram: @ivan_example",
    ])]
    assert fx.amounts.get_delivery(event_id, subscription.id, "owner").delivered_at is not None
    assert fx.amounts.get_delivery(event_id, OPERATOR_SUBSCRIPTION_ID, OPERATOR_ROLE).delivered_at is not None


async def test_operator_message_without_a_linked_client(fx):
    await _car_with_open_notifications(fx)
    fx.users.upsert(TelegramUserInfo(user_id=_STRANGER, username=None, first_name=None, last_name=None, is_bot=False))
    channel = _OperatorChannel()
    _event(fx)

    await _operator(fx, channel).run_once()

    [text] = channel.texts
    assert text.splitlines()[-1] == "👤 Клиент: не привязан"
    assert "🔗" not in text


async def test_operator_message_shows_id_when_the_client_has_no_username(fx):
    await _car_with_open_notifications(fx)
    _link_client(fx, username=None, first_name=None, last_name=None)
    channel = _OperatorChannel()
    _event(fx)

    await _operator(fx, channel).run_once()

    assert channel.texts[0].splitlines()[-2:] == ["👤 Клиент: Имя не указано", f"🔗 Telegram: ID {_OWNER}"]


async def test_operator_failure_does_not_block_the_client_and_is_retried_once(fx):
    subscription = await _car_with_open_notifications(fx)
    channel = _OperatorChannel(fail=True)
    operator = _operator(fx, channel)
    event_id = _event(fx)
    now = datetime.now(timezone.utc)

    await operator.run_once(now=now)
    await fx.delivery.run_once(now=now)

    assert [chat_id for chat_id, _, _ in fx.sender.sent] == [_OWNER]
    failed = fx.amounts.get_delivery(event_id, OPERATOR_SUBSCRIPTION_ID, OPERATOR_ROLE)
    assert failed.delivered_at is None and failed.excluded_at is None and failed.attempt_count == 1

    channel.fail = False
    await operator.run_once(now=now + timedelta(minutes=2))
    await operator.run_once(now=now + timedelta(minutes=10))

    assert len(channel.texts) == 1
    assert fx.amounts.get_delivery(event_id, OPERATOR_SUBSCRIPTION_ID, OPERATOR_ROLE).delivered_at is not None
    assert fx.amounts.get_delivery(event_id, subscription.id, "owner").attempt_count == 1


async def test_client_failure_does_not_block_the_operator(fx):
    subscription = await _car_with_open_notifications(fx)
    channel = _OperatorChannel()
    failing = AmountIncreaseDeliveryService(
        fx.amounts, fx.subscriptions, fx.tracker, _FailingSender(), tz=_TBILISI, notification_control=fx.control,
    )
    event_id = _event(fx)

    await failing.run_once()
    await _operator(fx, channel).run_once()

    assert len(channel.texts) == 1
    assert fx.amounts.get_delivery(event_id, subscription.id, "owner").delivered_at is None


async def test_flush_job_sends_increases_even_when_new_fines_fail(fx):
    await _car_with_open_notifications(fx)
    channel = _OperatorChannel()

    class _BrokenCoordinator:
        async def flush_pending(self):
            raise RuntimeError("new fines failed")

    _event(fx)
    job = NotificationFlushJob(_BrokenCoordinator(), _operator(fx, channel))

    with pytest.raises(RuntimeError, match="new fines failed"):
        await job.run()  # Scheduler залогирует ошибку новых штрафов
    assert len(channel.texts) == 1


async def test_operator_gets_each_increase_once_and_a_repeated_amount_creates_nothing(fx):
    await _car_with_open_notifications(fx)
    channel = _OperatorChannel()
    operator = _operator(fx, channel)
    _event(fx)
    now = datetime.now(timezone.utc)

    await operator.run_once(now=now)
    await operator.run_once(now=now + timedelta(minutes=5))
    assert _event(fx) is None  # та же сумма — события нет
    await operator.run_once(now=now + timedelta(hours=4))

    assert len(channel.texts) == 1


async def test_operator_never_gets_increases_from_before_its_start_or_before_the_flag(fx):
    await _car_with_open_notifications(fx)
    channel = _OperatorChannel()
    before_flag = _event(fx, old=100, new=150)
    _set_event_created(fx, before_flag, _hours_ago(3))
    fx.amounts.set_increase_notifications(enabled=False, at=datetime.now(timezone.utc))
    fx.amounts.set_increase_notifications(enabled=True, at=datetime.now(timezone.utc) - timedelta(hours=2))
    before_operator = _event(fx, old=150, new=200)
    _set_event_created(fx, before_operator, _hours_ago(1.5))
    operator = _operator(fx, channel, since=datetime.now(timezone.utc) - timedelta(hours=1))
    new = _event(fx, old=200, new=250)

    await operator.run_once()

    assert len(channel.texts) == 1 and "💰 Стало к оплате: 250 ₾" in channel.texts[0]
    def reason(event_id):
        return fx.amounts.get_delivery(event_id, OPERATOR_SUBSCRIPTION_ID, OPERATOR_ROLE).exclusion_reason

    assert reason(before_flag) == EXCLUSION_BEFORE_NOTIFICATIONS
    assert reason(before_operator) == EXCLUSION_BEFORE_OPERATOR_NOTIFICATIONS
    assert fx.amounts.get_delivery(new, OPERATOR_SUBSCRIPTION_ID, OPERATOR_ROLE).delivered_at is not None


def test_operator_start_boundary_survives_a_restart(fx):
    first = fx.amounts.start_operator_increase_notifications(at=datetime(2026, 11, 22, 10, 0))
    again = fx.amounts.start_operator_increase_notifications(at=datetime(2026, 11, 22, 12, 0))
    assert first == again == datetime(2026, 11, 22, 10, 0)


async def test_operator_gets_nothing_while_paused_or_with_increase_notifications_off(fx):
    await _car_with_open_notifications(fx)
    channel = _OperatorChannel()
    operator = _operator(fx, channel)
    event_id = _event(fx)

    fx.control.pause()
    await operator.run_once()
    fx.control.resume(client_excluded=0, operator_excluded=0, at=datetime(2026, 11, 1))
    fx.amounts.set_increase_notifications(enabled=False, at=datetime.now(timezone.utc))
    await operator.run_once()

    assert channel.texts == []
    assert fx.amounts.get_delivery(event_id, OPERATOR_SUBSCRIPTION_ID, OPERATOR_ROLE) is None


async def test_operator_skips_a_receipt_paid_before_sending(fx):
    await _car_with_open_notifications(fx)
    channel = _OperatorChannel()
    fx.tracker.mark_paid(fx.tracker.statuses_by_protocol(_CAR)["GE1"].id, actor_user_id=_TRUSTED, note="BOG 1")
    event_id = _event(fx)

    await _operator(fx, channel).run_once()

    assert channel.texts == []
    assert fx.amounts.get_delivery(event_id, OPERATOR_SUBSCRIPTION_ID, OPERATOR_ROLE).exclusion_reason == (
        EXCLUSION_RECEIPT_NOT_PAYABLE
    )


# ---- Просрочка по окончательному сроку оплаты ----
#
# Даты считаются от реальных часов: контроллер берёт «сегодня» из них, а
# сумма Georgian Card актуальна только в текущем контрольном периоде.

from reader.public_bot.conversation import _SearchHit  # noqa: E402
from reader.public_bot.payable_amount_texts import format_overdue_line, format_refreshed_at  # noqa: E402

_OVERDUE_TITLE = "⚠️ Срок оплаты истёк."
_INCREASED_ONE = "⚠️ Срок оплаты истёк. Сумма штрафа увеличилась."


def _today_tbilisi() -> date:
    return datetime.now(timezone.utc).astimezone(_TBILISI).date()


def _dated_car(fx, car, fines, *, last_date: date, active: bool = True, owner=_OWNER):
    """Машина, у квитанций которой police.ge отдаёт lastDate и (active) activeDate:
    срок окончательный только с activeDate."""
    task, subscription = fx.car(car, fines, owner=owner)
    raw_extra = {"lastDate": last_date.isoformat()}
    if active:
        raw_extra["activeDate"] = (last_date - timedelta(days=30)).isoformat()
    fx.provider.records[car] = [
        ParsedFineRecord(
            car_number=car, external_fine_id=protocol, penalty_date=date(2026, 8, 21), due_date=last_date,
            delivered_status="Вручено", fingerprint=f"fp-{car}-{protocol}",
            raw_data={"protocolNo": protocol, **raw_extra}, amount=amount,
        )
        for protocol, amount in fines
    ]
    return task, subscription


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _overdue_receipt(base, payable=None, *, overdue=True):
    return ReceiptAmount(protocol_no="X", base_amount=base, payable=payable, observed_at=None, overdue=overdue)


@pytest.mark.parametrize("receipts, expected", [
    ([_overdue_receipt(50, 150)], _INCREASED_ONE),
    ([_overdue_receipt(50, 50)], _OVERDUE_TITLE),
    ([_overdue_receipt(50)], _OVERDUE_TITLE),
    ([_overdue_receipt(50, 150, overdue=False)], None),
    ([_overdue_receipt(50, 150), _overdue_receipt(100, 300)], "⚠️ Срок оплаты истёк. Сумма штрафов увеличилась."),
    ([_overdue_receipt(50, 150), _overdue_receipt(100)], "⚠️ Срок оплаты истёк. Сумма увеличилась по 1 из 2 штрафов."),
    ([_overdue_receipt(50, 150), _overdue_receipt(100, overdue=False)],
     "⚠️ Срок оплаты истёк по 1 из 2 штрафов. Сумма увеличилась по 1 из 2 штрафов."),
    ([_overdue_receipt(50), _overdue_receipt(100, overdue=False)], "⚠️ Срок оплаты истёк по 1 из 2 штрафов."),
    # Увеличение по штрафу, срок которого не истёк, в строку просрочки не входит.
    ([_overdue_receipt(50), _overdue_receipt(100, 300, overdue=False)], "⚠️ Срок оплаты истёк по 1 из 2 штрафов."),
])
def test_overdue_line_claims_an_increase_only_where_the_gateway_confirmed_it(receipts, expected):
    assert format_overdue_line(summarize(receipts)) == expected


async def test_overdue_refresh_card_with_a_confirmed_increase_matches_the_agreed_layout(fx):
    fx.clock_now = _now_utc()
    task, subscription = _dated_car(fx, _CAR, [("GE1", 50)], last_date=_today_tbilisi() - timedelta(days=5))
    await fx.check(task)
    fx.gc.amounts["GE1"] = 150.0

    reply = await fx.controller.handle_payable_amount_refresh(subscription.id, telegram_user_id=_OWNER, chat_id=_OWNER)

    assert reply.text.splitlines() == [
        f"🚗 Автомобиль: {format_plate_for_telegram(_CAR)}",
        "🧾 Первоначальный штраф: 50 ₾",
        "💰 К оплате: 150 ₾",
        _INCREASED_ONE,
        format_refreshed_at(fx.clock_now, tz=_TBILISI),
    ]
    assert format_refreshed_at(fx.clock_now, tz=_TBILISI).startswith("🕒 Сумма уточнена: ")
    # Первое значение Georgian Card сохраняется молча: событий и уведомлений нет.
    assert fx.amounts.count_events() == 0
    await fx.delivery.run_once(now=fx.clock_now + timedelta(minutes=3))
    assert fx.sender.sent == []


@pytest.mark.parametrize("gc_amount", [40.0, 50.0])
async def test_overdue_without_a_confirmed_increase_never_says_the_amount_grew(fx, gc_amount):
    fx.clock_now = _now_utc()
    task, subscription = _dated_car(fx, _CAR, [("GE1", 50)], last_date=_today_tbilisi() - timedelta(days=5))
    await fx.check(task)
    fx.gc.amounts["GE1"] = gc_amount

    reply = await fx.controller.handle_payable_amount_refresh(subscription.id, telegram_user_id=_OWNER, chat_id=_OWNER)

    lines = reply.text.splitlines()
    assert lines[1:4] == ["🧾 Первоначальный штраф: 50 ₾", "💰 К оплате: 50 ₾", _OVERDUE_TITLE]  # максимум сохранён
    assert "увеличилась" not in reply.text


async def test_overdue_with_an_unknown_amount_warns_and_offers_a_refresh(fx):
    task, subscription = _dated_car(fx, _CAR, [("GE1", 50), ("GE2", 100)], last_date=_today_tbilisi() - timedelta(days=5))
    await fx.check(task)

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    lines = reply.text.splitlines()
    first = lines.index("🧾 Первоначальный штраф: 50 ₾")
    assert lines[first + 1:first + 3] == ["💰 К оплате: требуется уточнение", _OVERDUE_TITLE]
    summary = lines.index("🧾 Первоначальные штрафы: 2 шт. — 150 ₾")
    assert lines[summary + 1:summary + 3] == ["💰 К оплате: требуется уточнение", _OVERDUE_TITLE]
    assert "увеличилась" not in reply.text
    assert reply.cta_buttons[0] == [("🔄 Уточнить сумму", f"amtref:{subscription.id}".encode())]
    assert fx.gc.calls == [] and fx.amounts.count_events() == 0


async def test_check_now_shows_the_increase_under_each_overdue_fine(fx):
    task, subscription = _dated_car(fx, _CAR, [("GE1", 50), ("GE2", 100)], last_date=_today_tbilisi() - timedelta(days=5))
    await fx.check(task)
    observed = _now_utc() - timedelta(hours=1)
    fx.store(_CAR, "GE1", 150, at=observed)
    fx.store(_CAR, "GE2", 100, at=observed)

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    lines = reply.text.splitlines()
    first = lines.index("🧾 Первоначальный штраф: 50 ₾")
    assert lines[first + 1:first + 3] == ["💰 К оплате: 150 ₾", _INCREASED_ONE]
    second = lines.index("🧾 Первоначальный штраф: 100 ₾")
    assert lines[second + 1:second + 3] == ["💰 К оплате: 100 ₾", _OVERDUE_TITLE]
    summary = lines.index("🧾 Первоначальные штрафы: 2 шт. — 150 ₾")
    assert lines[summary + 1:summary + 4] == [
        "💰 К оплате: 250 ₾",
        "⚠️ Срок оплаты истёк. Сумма увеличилась по 1 из 2 штрафов.",
        format_refreshed_at(observed, tz=_TBILISI, plural=True),
    ]
    assert fx.gc.calls == []


async def test_amount_from_an_earlier_control_period_is_not_a_confirmed_increase(fx):
    # Срок истёк 40 дней назад: идёт второй период, сумма получена в первом.
    task, subscription = _dated_car(fx, _CAR, [("GE1", 50)], last_date=_today_tbilisi() - timedelta(days=40))
    await fx.check(task)
    fx.store(_CAR, "GE1", 150, at=_now_utc() - timedelta(days=20))

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    lines = reply.text.splitlines()
    first = lines.index("🧾 Первоначальный штраф: 50 ₾")
    assert lines[first + 1:first + 3] == ["💰 К оплате: требуется уточнение", _OVERDUE_TITLE]
    assert "увеличилась" not in reply.text


@pytest.mark.parametrize("last_date_offset, active", [
    (-5, False),  # предварительный срок (без activeDate) просрочкой не считается
    (10, True),   # окончательный срок ещё идёт
    (0, True),    # последний день срока — ещё не просрочка
])
async def test_no_overdue_warning_without_a_final_expired_term(fx, last_date_offset, active):
    today = _today_tbilisi()
    fx.controller._today = lambda: today  # без гонки с полуночью по Тбилиси
    task, subscription = _dated_car(
        fx, _CAR, [("GE1", 50)], last_date=today + timedelta(days=last_date_offset), active=active,
    )
    await fx.check(task)
    fx.store(_CAR, "GE1", 150, at=_now_utc() - timedelta(hours=1))

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    assert "Срок оплаты истёк" not in reply.text and "Первоначальн" not in reply.text
    assert "🧾 Штраф: 50 ₾" in reply.text.splitlines()


async def test_statistics_rows_show_overdue_without_a_line_about_unrefined_amounts(fx):
    overdue_day = _today_tbilisi() - timedelta(days=5)
    first, _ = _dated_car(fx, "AA111BB", [("GE1", 50), ("GE2", 100)], last_date=overdue_day)
    second, _ = _dated_car(fx, "CD456EF", [("GE3", 50), ("GE4", 50)], last_date=overdue_day, owner=None)
    third, _ = fx.car("EE777KK", [("GE5", 100)])  # срок не окончательный — без строки
    for task in (first, second, third):
        await fx.check(task)
    observed = _now_utc() - timedelta(hours=1)
    fx.store("AA111BB", "GE1", 150, at=observed)
    fx.store("AA111BB", "GE2", 300, at=observed)
    fx.store("CD456EF", "GE3", 150, at=observed)  # GE4 — сумма неизвестна

    lines = fx.controller._format_statistics_reply().text.splitlines()
    text = "\n".join(lines)

    assert (f"🚗 Госномер: {format_plate_for_telegram('AA111BB')}\n🔴 Неоплачено: 2 штрафа — 150 ₾\n"
            "💰 Сумма к оплате: 450 ₾\n⚠️ Срок оплаты истёк. Сумма штрафов увеличилась.\n") in text
    assert (f"🚗 Госномер: {format_plate_for_telegram('CD456EF')}\n🔴 Неоплачено: 2 штрафа — 100 ₾\n"
            "💰 Сумма к оплате: 200 ₾\n⚠️ Срок оплаты истёк. Сумма увеличилась по 1 из 2 штрафов.\n") in text
    assert (f"🚗 Госномер: {format_plate_for_telegram('EE777KK')}\n🔴 Неоплачено: 1 штраф — 100 ₾\n"
            "💰 Сумма к оплате: 100 ₾\n") in text
    header = lines[lines.index("🚨 Штрафы по последней проверке"):]
    assert not any("⚠️" in line for line in header[:5])  # шапка не меняется
    assert not any("требует уточнения" in line or "Предварительно" in line for line in lines)
    assert fx.gc.calls == [] and fx.amounts.count_events() == 0


async def test_manager_search_block_shows_overdue_after_the_fines_line(fx):
    task, _ = _dated_car(fx, _CAR, [("GE1", 50)], last_date=_today_tbilisi() - timedelta(days=5))
    await fx.check(task)
    fx.store(_CAR, "GE1", 150, at=_now_utc() - timedelta(hours=1))
    fines = (await fx.check_service.check_task(fx.tasks.get(task.id))).current_fines

    block = fx.controller._format_search_block(
        _SearchHit(task_id=task.id, owner_telegram_user_id=_OWNER, car_number=_CAR),
        monitoring_active=True, check_ok=True, fines=fines, checked_at=_now_utc(),
    )

    lines = block.splitlines()
    money = lines.index("💰 Штрафы: 50 ₾")
    assert lines[money + 1] == _INCREASED_ONE
    assert fx.gc.calls == []


async def test_refresh_card_with_only_some_fines_overdue_counts_them(fx):
    fx.clock_now = _now_utc()
    today = _today_tbilisi()
    fx.controller._today = lambda: today
    task, subscription = _dated_car(fx, _CAR, [("GE1", 50)], last_date=today - timedelta(days=5))
    fx.provider.records[_CAR].append(ParsedFineRecord(
        car_number=_CAR, external_fine_id="GE2", penalty_date=date(2026, 8, 21), due_date=today + timedelta(days=10),
        delivered_status="Вручено", fingerprint=f"fp-{_CAR}-GE2",
        raw_data={"protocolNo": "GE2", "lastDate": (today + timedelta(days=10)).isoformat(),
                  "activeDate": (today - timedelta(days=20)).isoformat()},
        amount=100,
    ))
    await fx.check(task)
    fx.gc.amounts.update({"GE1": 150.0, "GE2": 100.0})

    reply = await fx.controller.handle_payable_amount_refresh(subscription.id, telegram_user_id=_OWNER, chat_id=_OWNER)

    assert reply.text.splitlines()[1:4] == [
        "🧾 Первоначальные штрафы: 2 шт. — 150 ₾",
        "💰 К оплате: 250 ₾",
        "⚠️ Срок оплаты истёк по 1 из 2 штрафов. Сумма увеличилась по 1 из 2 штрафов.",
    ]


async def test_paid_fine_with_an_expired_term_keeps_the_plain_fine_label(fx):
    task, subscription = _dated_car(fx, _CAR, [("GE1", 50), ("GE2", 100)], last_date=_today_tbilisi() - timedelta(days=5))
    await fx.check(task)
    fx.tracker.mark_paid(fx.tracker.statuses_by_protocol(_CAR)["GE2"].id, actor_user_id=_TRUSTED, note="BOG 1")

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    lines = reply.text.splitlines()
    assert "🧾 Штраф: 100 ₾" in lines and "🧾 Первоначальный штраф: 50 ₾" in lines
    summary = lines.index("🧾 Первоначальный штраф: 50 ₾", lines.index("🧾 Штраф: 100 ₾"))
    assert lines[summary + 1:summary + 3] == ["💰 К оплате: требуется уточнение", _OVERDUE_TITLE]


# ---- «🔎 Проверить сейчас» менеджера: та же логика суммы, что у клиента ----


async def _manager_check_now_by_plate(fx, car):
    await fx.controller.handle_text(texts.CHECK_NOW_LABEL, chat_id=_TRUSTED, telegram_user_id=_TRUSTED, username=None)
    return await fx.controller.handle_text(car, chat_id=_TRUSTED, telegram_user_id=_TRUSTED, username=None)


async def _overdue_car_with_increase(fx):
    """Две квитанции с истёкшим окончательным сроком: по одной Georgian Card
    подтвердил увеличение, по другой — нет; третья (новая) — в сроке."""
    task, subscription = _dated_car(fx, _CAR, [("GE1", 100), ("GE2", 50)], last_date=_today_tbilisi() - timedelta(days=5))
    fx.provider.records[_CAR].append(ParsedFineRecord(
        car_number=_CAR, external_fine_id="GE3", penalty_date=date(2026, 11, 27), due_date=date(2027, 1, 26),
        delivered_status="Не вручено", fingerprint=f"fp-{_CAR}-GE3",
        raw_data={"protocolNo": "GE3", "lastDate": "2027-01-26"}, amount=50,
    ))
    await fx.check(task)
    observed = _now_utc() - timedelta(hours=1)
    fx.store(_CAR, "GE1", 300, at=observed)
    fx.store(_CAR, "GE2", 50, at=observed)
    return task, subscription, observed


async def test_manager_check_now_by_plate_shows_the_gateway_amount_like_the_client(fx):
    _, subscription, observed = await _overdue_car_with_increase(fx)
    client = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    reply = await _manager_check_now_by_plate(fx, _CAR)

    lines = reply.text.splitlines()
    first = lines.index("🧾 Первоначальный штраф: 100 ₾")
    assert lines[first + 1:first + 3] == ["💰 К оплате: 300 ₾", _INCREASED_ONE]
    second = lines.index("🧾 Первоначальный штраф: 50 ₾")
    assert lines[second + 1:second + 3] == ["💰 К оплате: 50 ₾", _OVERDUE_TITLE]
    assert "🧾 Штраф: 50 ₾" in lines  # новая квитанция в сроке — без «Первоначальный»
    assert "💰 Сумма: 100 GEL" not in reply.text
    assert "Статус: 🔴 Не оплачен" in lines
    new_fine = lines.index("🧾 Штраф: 50 ₾")
    assert lines[new_fine + 1] == "💰 К оплате: 50 ₾"  # в сроке, без суммы Georgian Card — исходная
    summary = lines.index("🧾 Первоначальные штрафы: 3 шт. — 200 ₾")
    assert lines[summary + 1:] == [
        "💰 К оплате: 400 ₾",
        "⚠️ Срок оплаты истёк по 2 из 3 штрафов. Сумма увеличилась по 1 из 3 штрафов.",
    ]
    # Тот же текст, что у клиента; без кнопок оплаты и «🔄 Уточнить сумму»,
    # под ответом — меню менеджера.
    assert reply.text == client.text
    assert reply.show_main_menu is True
    assert not reply.cta_buttons
    # Только чтение: ни сессий Georgian Card, ни событий, ни сообщений.
    assert fx.gc.calls == [] and fx.amounts.count_events() == 0
    await fx.delivery.run_once(now=_now_utc() + timedelta(minutes=3))
    assert fx.sender.sent == []


async def test_manager_check_now_from_a_button_uses_the_same_reply(fx):
    task, _, _ = await _overdue_car_with_increase(fx)
    by_plate = await _manager_check_now_by_plate(fx, _CAR)

    reply = await fx.controller.handle_trusted_check_now_once(task.id, telegram_user_id=_TRUSTED)

    assert reply.text == by_plate.text
    assert "💰 К оплате: 300 ₾" in reply.text
    assert reply.show_main_menu is True and not reply.cta_buttons


async def test_manager_check_now_without_the_amount_feature_shows_statuses_not_gateway_amounts(tmp_path):
    fixture = _Fixture(tmp_path, with_amounts=False)
    try:
        task, _ = _dated_car(fixture, _CAR, [("GE1", 100)], last_date=_today_tbilisi() - timedelta(days=5))
        await fixture.check(task)

        reply = await _manager_check_now_by_plate(fixture, _CAR)

        assert "💰 Сумма: 100 GEL" in reply.text
        assert "Статус: 🔴 Не оплачен" in reply.text
        assert "💰 К оплате: 100 ₾" in reply.text
        assert "Первоначальный" not in reply.text and fixture.gc.calls == []
        assert reply.show_main_menu is True
    finally:
        fixture.close()


# ---- Штраф в сроке оплаты без суммы Georgian Card — исходная сумма ----


def _plain_car(fx, car, fines, *, due_date, owner=_OWNER):
    """Квитанции без lastDate/activeDate в ответе police.ge: срок — только due_date (может быть None)."""
    task, subscription = fx.car(car, fines, owner=owner)
    fx.provider.records[car] = [
        ParsedFineRecord(
            car_number=car, external_fine_id=protocol, penalty_date=date(2026, 11, 1), due_date=due_date,
            delivered_status="Не вручено", fingerprint=f"fp-{car}-{protocol}", raw_data={"protocolNo": protocol},
            amount=amount,
        )
        for protocol, amount in fines
    ]
    return task, subscription


async def test_final_term_not_expired_without_a_gateway_amount_shows_the_original_amount(fx):
    task, subscription = _dated_car(fx, _CAR, [("GE1", 50)], last_date=_today_tbilisi() + timedelta(days=10))
    await fx.check(task)

    lines = (await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)).text.splitlines()

    fine = lines.index("🧾 Штраф: 50 ₾")
    assert lines[fine + 1] == "💰 К оплате: 50 ₾"
    assert lines[-2:] == ["🧾 Штраф: 50 ₾", "💰 К оплате: 50 ₾"]
    # Только отображение: в БД сумма Georgian Card не появилась.
    assert fx.amounts.amounts_for_cars([_CAR]) == {} and fx.gc.calls == []


@pytest.mark.parametrize("make_car", [
    # Предварительный срок прошёл, а квитанция так и не вручена: срок неизвестен.
    lambda fx: _dated_car(fx, _CAR, [("GE1", 50)], last_date=_today_tbilisi() - timedelta(days=5), active=False),
    # police.ge не дал срока вовсе.
    lambda fx: _plain_car(fx, _CAR, [("GE1", 50)], due_date=None),
])
async def test_unknown_term_is_not_treated_as_within_the_term(fx, make_car):
    task, subscription = make_car(fx)
    await fx.check(task)

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    lines = reply.text.splitlines()
    assert lines[lines.index("🧾 Штраф: 50 ₾") + 1] == "💰 К оплате: требуется уточнение"
    assert lines[-1] == "💰 К оплате: требуется уточнение"
    assert _OVERDUE_TITLE not in reply.text  # и не просрочен


async def test_overdue_without_an_amount_next_to_a_fine_within_its_term(fx):
    task, subscription = _dated_car(fx, _CAR, [("GE1", 100)], last_date=_today_tbilisi() - timedelta(days=5))
    fx.provider.records[_CAR].append(ParsedFineRecord(
        car_number=_CAR, external_fine_id="GE3", penalty_date=date(2026, 11, 27), due_date=date(2027, 1, 26),
        delivered_status="Не вручено", fingerprint=f"fp-{_CAR}-GE3",
        raw_data={"protocolNo": "GE3", "lastDate": "2027-01-26"}, amount=50,
    ))
    await fx.check(task)

    lines = (await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)).text.splitlines()

    overdue = lines.index("🧾 Первоначальный штраф: 100 ₾")
    assert lines[overdue + 1:overdue + 3] == ["💰 К оплате: требуется уточнение", _OVERDUE_TITLE]
    in_term = lines.index("🧾 Штраф: 50 ₾")
    assert lines[in_term + 1] == "💰 К оплате: 50 ₾"
    summary = lines.index("🧾 Первоначальные штрафы: 2 шт. — 150 ₾")
    assert lines[summary + 1:] == [
        "💰 Предварительно к оплате: 150 ₾",
        "⚠️ Срок оплаты истёк по 1 из 2 штрафов.",
        "⚠️ По 1 штрафу сумма требует уточнения",  # только просроченный
    ]


async def test_refreshed_line_only_when_every_amount_came_from_the_gateway(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50), ("GE2", 100)], owner=_OWNER)
    await fx.check(task)
    fx.store(_CAR, "GE1", 50)

    mixed = (await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)).text.splitlines()
    assert mixed[-2:] == ["🧾 Штрафы: 2 шт. — 150 ₾", "💰 К оплате: 150 ₾"]  # без «🕒»: GE2 не уточнялся

    fx.store(_CAR, "GE2", 100)
    full = (await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)).text.splitlines()
    assert full[-1] == format_refreshed_at(_REFRESHED_AT, tz=_TBILISI, plural=True)


async def test_statistics_use_the_same_rule_as_the_check(fx):
    await _overdue_car_with_increase(fx)

    text = fx.controller._format_statistics_reply().text
    lines = text.splitlines()

    row = lines.index(f"🚗 Госномер: {format_plate_for_telegram(_CAR)}")
    assert lines[row + 1:row + 4] == [
        "🔴 Неоплачено: 3 штрафа — 200 ₾",
        "💰 Сумма к оплате: 400 ₾",
        "⚠️ Срок оплаты истёк по 2 из 3 штрафов. Сумма увеличилась по 1 из 3 штрафов.",
    ]
    assert lines.count("💰 Сумма к оплате: 400 ₾") == 2  # шапка и машина
    assert "Предварительно" not in text and "уточнения" not in text
    assert not any(line.startswith("🕒 Сумм") for line in lines)
