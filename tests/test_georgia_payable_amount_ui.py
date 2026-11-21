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
from reader.public_bot.amount_delivery_service import (  # noqa: E402
    EXCLUSION_DUPLICATE_CHAT,
    EXCLUSION_RECEIPT_NOT_PAYABLE,
    EXCLUSION_SHOWN_IN_REPLY,
    AmountIncreaseDeliveryService,
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
        "💰 К оплате: 550 ₾", "🕒 Суммы уточнены 21.11 в 15:20",
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
    assert "🕒 Сумма уточнена 21.11 в 15:20" in lines
    assert "💰 Сумма: 50 GEL" not in reply.text
    assert reply.cta_buttons[0] == [("🔄 Уточнить сумму", f"amtref:{subscription.id}".encode())]
    assert [label for label, _ in reply.cta_buttons[1]] == ["💳 Оплатить в рублях", "🚗 ОСАГО Грузии"]
    assert fx.gc.calls == []  # показ карточки не открывает сессий


async def test_check_now_without_a_known_amount_does_not_present_the_fine_as_the_amount_to_pay(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    assert "💰 К оплате: требуется уточнение" in reply.text.splitlines()
    assert "💰 К оплате: 50 ₾" not in reply.text
    assert fx.gc.calls == []


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
        "🕒 Сумма уточнена 21.11 в 15:20",
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
        "🕒 Суммы уточнены 21.11 в 15:20",
        "🟢 Оплачено: 0 ₾ (0 шт.)",
    ]
    assert lines.count("🕒 Суммы уточнены 21.11 в 15:20") == 1 and not any("Сумма уточнена" in line for line in lines)
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
    assert not any(line.startswith("🕒") for line in lines)
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
