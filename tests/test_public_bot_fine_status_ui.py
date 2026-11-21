"""Статусы оплаты в @ProtocolGEbot (reader/public_bot/conversation.py):
"🔎 Проверить сейчас" (строка статуса у штрафа, итог "к оплате", кнопка
оплаты только при неоплаченных), 📊 Статистика (подтверждённая
задолженность / оплачено / неопределённый статус, дата обновления базы,
строка вечерней перепроверки) и экраны менеджера "🧾 Статусы оплаты".
Сквозные, через настоящий ConversationController и SQLite; номера
синтетические."""

import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import UNLIMITED_END_DATE, ParsedFineRecord
from reader.fines.payment_status import (
    MIA_PAYMENT_FAILED_MESSAGE,
    PAYMENT_NEEDS_REVIEW,
    PAYMENT_PAID,
    PAYMENT_UNPAID,
    DebtBreakdown,
    FineStatusRepository,
    FineStatusTracker,
    PaymentPageMarkers,
)
from reader.fines.provider import FineProvider
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.plate_display import format_plate_for_telegram
from reader.public_bot import keyboards, texts
from reader.public_bot.conversation import ConversationController
from reader.public_bot.conversation_state_repository import BotConversationStateRepository
from reader.public_bot.daily_recheck import DailyRecheckRun
from reader.public_bot.debt_refresh_service import DebtRefreshService
from reader.public_bot.known_users_repository import BotKnownUsersRepository
from reader.public_bot.statistics_service import BotStatisticsService
from reader.public_bot.subscription_repository import FineSubscriptionRepository
from reader.public_bot.subscription_service import SubscriptionService
from reader.users.repository import UserRepository

_TBILISI = ZoneInfo("Asia/Tbilisi")
_TRUSTED = 900000001
_OTHER_TRUSTED = 900000002
_OWNER = 900000101
_CHAT = 424242
_CAR = "AA111BB"


async def _instant(_seconds):
    pass


def _record(car, protocol_no, amount):
    return ParsedFineRecord(
        car_number=car, external_fine_id=protocol_no, penalty_date=date(2026, 11, 1), due_date=date(2026, 12, 31),
        delivered_status="Не вручено", fingerprint=f"fp-{car}-{protocol_no}", raw_data={"protocolNo": protocol_no},
        amount=amount,
    )


class _Provider(FineProvider):
    def __init__(self):
        self.records: dict[str, list[ParsedFineRecord]] = {}

    async def search_by_plate(self, plate):
        return list(self.records.get(plate, []))


class _FakeRecheck:
    def __init__(self, run):
        self._run = run

    def latest_run(self):
        return self._run


class _Fixture:
    def __init__(self, tmp_path, *, markers=None, recheck_run=None, with_statuses=True):
        db_path = tmp_path / "users.db"
        self.tasks = FineMonitoringTaskRepository(db_path)
        self.fines = DetectedFineRepository(db_path)
        self.subscriptions = FineSubscriptionRepository(db_path)
        self.users = UserRepository(db_path)
        self.states = BotConversationStateRepository(db_path)
        self.known = BotKnownUsersRepository(db_path)
        self.status_repository = FineStatusRepository(db_path)
        self.tracker = FineStatusTracker(self.status_repository, markers=markers)
        self.provider = _Provider()
        self.check_service = FineCheckService(
            self.provider, self.tasks, self.fines, sleep=_instant,
            status_tracker=self.tracker if with_statuses else None,
        )
        self.subscription_service = SubscriptionService(self.tasks, self.subscriptions, self.users, self.check_service)
        self.controller = ConversationController(
            self.states, self.subscription_service,
            BotStatisticsService(self.known, self.subscriptions, self.fines), self.known, tz=_TBILISI,
            trusted_operator_user_ids=frozenset({_TRUSTED, _OTHER_TRUSTED}),
            debt_refresh_service=DebtRefreshService(self.tasks, self.check_service, sleep=_instant),
            fine_status_tracker=self.tracker if with_statuses else None,
            daily_recheck_service=_FakeRecheck(recheck_run) if recheck_run is not None else None,
        )

    def car(self, car, fines, *, owner=None):
        task = self.tasks.create(
            car_number=car, label=None, start_date=date(2026, 11, 1), end_date=date(2026, 12, 31),
            telegram_chat_id=-1, created_by_user_id=1,
        )
        self.provider.records[car] = [_record(car, protocol, amount) for protocol, amount in fines]
        subscription = None
        if owner is not None:
            subscription = self.subscriptions.create(
                monitoring_task_id=task.id, car_number=car, telegram_user_id=owner, telegram_chat_id=owner,
                telegram_username=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
            )
        return task, subscription

    async def check(self, task):
        await self.check_service.check_task(self.tasks.get(task.id))

    def status(self, car, protocol):
        return self.tracker.statuses_by_protocol(car)[protocol]

    def close(self):
        for repository in (self.status_repository, self.known, self.states, self.users, self.subscriptions,
                           self.fines, self.tasks):
            repository.close()


@pytest.fixture()
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


# ---- 🔎 Проверить сейчас ----


async def test_check_now_shows_statuses_and_excludes_paid_from_amount_to_pay(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50), ("GE2", 100)], owner=_OWNER)
    await fx.check(task)
    fx.tracker.mark_paid(fx.status(_CAR, "GE2").id, actor_user_id=_TRUSTED, note="TBC 1")

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    assert "Статус: 🔴 Не оплачен" in reply.text
    assert "Статус: 🟢 Оплачен" in reply.text
    assert "💰 К оплате: 50 ₾" in reply.text
    assert "🟢 Оплачено, ждёт обновления базы полиции: 100 ₾" in reply.text
    assert reply.cta_buttons  # есть неоплаченный — кнопка оплаты на месте


async def test_check_now_with_only_paid_fines_offers_no_payment(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50), ("GE2", 100)], owner=_OWNER)
    await fx.check(task)
    for protocol in ("GE1", "GE2"):
        fx.tracker.mark_paid(fx.status(_CAR, protocol).id, actor_user_id=_TRUSTED, note="TBC 1")

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    assert reply.cta_buttons is None
    assert "💰 К оплате: 0 ₾" in reply.text


async def test_check_now_uncertain_payment_stays_in_amount_to_pay(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50), ("GE2", 100)], owner=_OWNER)
    await fx.check(task)
    fx.tracker.mark_paid(fx.status(_CAR, "GE1").id, actor_user_id=_TRUSTED, note="TBC 1")
    fx.tracker.mark_needs_review(fx.status(_CAR, "GE2").id, actor_user_id=_TRUSTED, note="отказ")

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    # 🟡 — не оплата: долг остаётся, кнопка оплаты тоже
    assert "💰 К оплате: 100 ₾" in reply.text
    assert "🟡 Из них оплата требует проверки: 100 ₾" in reply.text
    assert reply.cta_buttons


async def test_check_now_shows_unverified_history_separately_not_in_amount_to_pay(fx):
    """⏳ (история, присутствие police.ge ещё не подтвердил) новым штрафом
    подписчику не приходит — в "Проверить сейчас" она отдельной строкой и в
    "К оплате" не входит."""
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    _make_unverified(fx, "OLD1", 30)
    _make_unverified(fx, "OLD2", 20)

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    assert "💰 К оплате: 50 ₾" in reply.text
    assert "⏳ Ждут повторной проверки police.ge: 50 ₾ (2 шт.) — в сумму к оплате не входят" in reply.text
    assert reply.text.rstrip().endswith("в сумму к оплате не входят")


async def test_check_now_without_fines_still_shows_unverified_history(fx):
    task, subscription = fx.car(_CAR, [], owner=_OWNER)
    _make_unverified(fx, "OLD1", 30)

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    assert reply.text.startswith(texts.format_no_fines_found(_CAR))
    assert "⏳ Ждут повторной проверки police.ge: 30 ₾ (1 шт.)" in reply.text
    assert fx.status(_CAR, "OLD1").presence == "unverified"  # одна проверка — ещё не ⚪


async def test_cached_unverified_receipt_is_labelled_and_not_offered_for_payment(fx):
    """Из кэша (police.ge недоступен) в список может попасть ⏳: у неё своя
    метка, в "К оплате" она не входит, и кнопок оплаты только из-за неё нет."""
    task, subscription = fx.car(_CAR, [("OLD1", 30)], owner=_OWNER)
    outcome = await fx.subscription_service.check_now(subscription.id, telegram_user_id=_OWNER)
    fx.tracker._repository._conn.execute(
        "UPDATE georgia_fine_status SET presence = 'unverified' WHERE id = ?", (fx.status(_CAR, "OLD1").id,),
    )
    fx.tracker._repository._conn.commit()

    reply = fx.controller._format_check_now_outcome_reply(outcome)

    assert "Статус: ⏳ Ждёт повторной проверки police.ge" in reply.text
    assert "💰 К оплате: 0 ₾" in reply.text
    assert "⏳ Ждут повторной проверки police.ge: 30 ₾ (1 шт.)" in reply.text
    assert reply.cta_buttons is None


async def test_unverified_line_is_hidden_for_suppressed_plates_and_without_statuses(fx, tmp_path, monkeypatch):
    fx.car(_CAR, [])
    _make_unverified(fx, "OLD1", 30)
    assert fx.controller._unverified_totals(_CAR) == {"unverified_count": 1, "unverified_amount": 30}

    monkeypatch.setattr("reader.public_bot.conversation.is_fine_output_suppressed", lambda car: car == _CAR)
    assert fx.controller._unverified_totals(_CAR) == {}

    fixture = _Fixture(tmp_path / "plain", with_statuses=False)
    assert fixture.controller._unverified_totals(_CAR) == {}
    fixture.close()


async def test_manager_marks_on_unconfirmed_history_as_the_subscriber_sees_them(fx):
    """Подписчику квитанция без подтверждения police.ge — ⏳ и с отметкой 🟡
    (отказ платёжной страницы — не подтверждение долга), как и в доставке;
    🟢 — оплачена, в строку ⏳ не входит. У менеджеров 🟡 остаётся долгом."""
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    review = _make_unverified(fx, "OLD1", 40)
    fx.tracker.mark_needs_review(review.id, actor_user_id=_TRUSTED, note="ჯარიმის გადახდა ვერ მოხერხდა!")
    paid = _make_unverified(fx, "OLD2", 25)
    fx.tracker.mark_paid(paid.id, actor_user_id=_TRUSTED, note="TBC 1")

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)
    stats = fx.controller._format_statistics_reply().text

    assert "💰 К оплате: 50 ₾" in reply.text
    assert "⏳ Ждут повторной проверки police.ge: 40 ₾ (1 шт.)" in reply.text
    assert "🟡 Из них требуют проверки: 1 штраф" in stats.splitlines()


async def test_cached_unconfirmed_receipt_with_a_failed_payment_mark_is_not_offered_for_payment(fx):
    task, subscription = fx.car(_CAR, [("OLD1", 40)], owner=_OWNER)
    outcome = await fx.subscription_service.check_now(subscription.id, telegram_user_id=_OWNER)
    status = fx.status(_CAR, "OLD1")
    fx.tracker._repository._conn.execute(
        "UPDATE georgia_fine_status SET presence = 'unverified' WHERE id = ?", (status.id,),
    )
    fx.tracker._repository._conn.commit()
    fx.tracker.mark_needs_review(status.id, actor_user_id=_TRUSTED, note="ჯარიმის გადახდა ვერ მოხერხდა!")

    reply = fx.controller._format_check_now_outcome_reply(outcome)

    assert "Статус: ⏳ Ждёт повторной проверки police.ge" in reply.text
    assert "💰 К оплате: 0 ₾" in reply.text
    assert "⏳ Ждут повторной проверки police.ge: 40 ₾ (1 шт.)" in reply.text
    assert reply.cta_buttons is None


async def test_cached_receipt_missing_once_is_still_debt_for_the_subscriber(fx):
    """Не найдена один раз ('missing') — ещё не доказательство: подписчику
    это обычный 🔴, в «К оплате» и с кнопками, не ⏳."""
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    outcome = await fx.subscription_service.check_now(subscription.id, telegram_user_id=_OWNER)
    fx.tracker._repository._conn.execute(
        "UPDATE georgia_fine_status SET presence = 'missing', missing_since = '2026-11-19 10:00:00' WHERE id = ?",
        (fx.status(_CAR, "GE1").id,),
    )
    fx.tracker._repository._conn.commit()

    reply = fx.controller._format_check_now_outcome_reply(outcome)

    assert "Статус: 🔴 Не оплачен" in reply.text
    assert "💰 К оплате: 50 ₾" in reply.text
    assert "повторной проверки" not in reply.text  # ни метки ⏳, ни строки ⏳
    assert reply.cta_buttons


async def test_owner_add_car_summary_shows_unverified_history(fx):
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50)]
    _make_unverified(fx, "OLD1", 30)

    reply = await fx.controller._complete_self_add_car(
        car_number=_CAR, username=None, today=date(2026, 11, 19), chat_id=_OWNER, telegram_user_id=_OWNER,
        first_name=None, last_name=None,
    )

    assert reply.text.endswith("⏳ Ждут повторной проверки police.ge: 30 ₾ (1 шт.) — в сумму к оплате не входят")


async def test_trusted_add_car_for_a_client_summary_shows_unverified_history(fx):
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50)]
    _make_unverified(fx, "OLD1", 30)

    reply = await fx.controller._complete_delegated_add_car(
        car_number=_CAR, owner_username="client_user", today=date(2026, 11, 19), chat_id=_CHAT,
        telegram_user_id=_TRUSTED,
    )

    assert "⏳ Ждут повторной проверки police.ge: 30 ₾ (1 шт.) — в сумму к оплате не входят" in reply.text


async def test_trusted_add_car_summary_shows_unverified_history(fx):
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50)]
    _make_unverified(fx, "OLD1", 30)

    reply = await fx.controller._complete_delegated_add_car_without_client(
        car_number=_CAR, today=date(2026, 11, 19), chat_id=_CHAT, telegram_user_id=_TRUSTED,
    )

    assert reply.text.endswith("⏳ Ждут повторной проверки police.ge: 30 ₾ (1 шт.) — в сумму к оплате не входят")


def test_add_car_summaries_show_unverified_history_on_its_own_line():
    common = {"car_number": _CAR, "check_ok": True, "new_fines_count": 1}
    line = "⏳ Ждут повторной проверки police.ge: 30 ₾ (1 шт.) — в сумму к оплате не входят"
    unverified = {"unverified_count": 1, "unverified_amount": 30.0}

    assert texts.format_add_car_summary(**common, **unverified).endswith(line)
    assert texts.format_delegated_add_car_without_client_summary(**common, **unverified).endswith(line)
    delegated = texts.format_delegated_add_car_summary(
        **common, owner_username="client", pending_claim=False, claim_link=None, **unverified,
    )
    assert delegated.endswith(line)
    assert "⏳" not in texts.format_add_car_summary(**common)


async def test_check_now_without_statuses_keeps_old_format(tmp_path):
    fixture = _Fixture(tmp_path, with_statuses=False)
    task, subscription = fixture.car(_CAR, [("GE1", 50)], owner=_OWNER)

    reply = await fixture.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    assert "Статус:" not in reply.text and "К оплате" not in reply.text
    assert reply.cta_buttons
    fixture.close()


# ---- 📊 Статистика ----


async def test_statistics_separates_confirmed_paid_and_uncertain(tmp_path):
    run = DailyRecheckRun(
        run_date=date(2026, 11, 18), status="completed", started_at=datetime(2026, 11, 18, 19, 0),
        heartbeat_at=datetime(2026, 11, 18, 19, 6), finished_at=datetime(2026, 11, 18, 19, 6),
        total=3, checked=3, failed=0, stop_reason=None,
    )
    fixture = _Fixture(tmp_path, recheck_run=run)
    unpaid_task, _ = fixture.car("AA111BB", [("GE1", 50), ("GE2", 100)])
    paid_task, _ = fixture.car("BB222CC", [("GE3", 30)])
    review_task, _ = fixture.car("CC333DD", [("GE4", 70)])
    for task in (unpaid_task, paid_task, review_task):
        await fixture.check(task)
    fixture.tracker.mark_paid(fixture.status("BB222CC", "GE3").id, actor_user_id=_TRUSTED, note="BOG 9")
    fixture.tracker.mark_needs_review(fixture.status("CC333DD", "GE4").id, actor_user_id=_TRUSTED, note=None)
    fixture.tracker.record_source_updated_on(date(2026, 11, 12))

    reply = fixture.controller._format_statistics_reply()

    lines = reply.text.splitlines()
    header = lines.index("🚨 Штрафы по последней проверке")
    assert lines[header:header + 6] == [
        "🚨 Штрафы по последней проверке",
        "Автомобили со штрафами: 3",
        "🔴 Неоплачено: 3 штрафа",  # 🔴 2 + 🟡 1
        "💰 Сумма к оплате: 220 ₾",  # 🔴 150 + 🟡 70, 🟢 не входит
        "🟡 Из них требуют проверки: 1 штраф",
        "🟢 Оплачено: 30 ₾ (1 шт.)",
    ]
    # дата базы police.ge, вечерняя перепроверка и пояснения больше не показываются
    for removed in ("🗓", "🌙", "ℹ️", "⏳", "⚠️"):
        assert not any(line.startswith(removed) for line in lines), removed
    assert "База police.ge" not in reply.text and "перепроверка" not in reply.text
    assert "ещё в базе полиции" not in reply.text and "Предварительно" not in reply.text
    assert reply.fine_status_available
    # подтверждённый долг — первым, оплаченная машина — только строкой 🟢
    position = {car: reply.text.index(format_plate_for_telegram(car)) for car in ("AA111BB", "BB222CC", "CC333DD")}
    assert position["AA111BB"] < position["CC333DD"] < position["BB222CC"]
    paid_block = reply.text[position["BB222CC"]:]
    assert "🟢 Оплачено: 1 штраф — 30 ₾" in paid_block and "🔴" not in paid_block
    review_block = reply.text[position["CC333DD"]:position["BB222CC"]]
    assert "🔴 Неоплачено: 1 штраф — 70 ₾\n💰 Сумма к оплате: 70 ₾" in review_block
    assert "🟡 Из них требуют проверки: 1 штраф — 70 ₾" in review_block
    fixture.close()


def _summary_lines(**totals):
    return texts.format_debt_status_summary_block(
        car_count=2, totals=DebtBreakdown(**totals), source_updated_on=None, recheck_line=None,
    ).splitlines()


def test_summary_without_uncertain_fines_has_no_needs_review_line():
    """🟡 = 0 — строки нет вовсе; пояснений ℹ️ нет."""
    assert _summary_lines(unpaid_amount=150, unpaid_count=2, paid_amount=30, paid_count=1) == [
        "🚨 Штрафы по последней проверке",
        "Автомобили со штрафами: 2",
        "🔴 Неоплачено: 2 штрафа",
        "💰 Сумма к оплате: 150 ₾",
        "🟢 Оплачено: 30 ₾ (1 шт.)",
    ]


def test_summary_ignores_the_base_date_and_the_evening_recheck():
    lines = texts.format_debt_status_summary_block(
        car_count=1, totals=DebtBreakdown(unpaid_amount=50, unpaid_count=1),
        source_updated_on=date(2026, 11, 12), recheck_line="🌙 Вечерняя перепроверка 18.11: завершена",
    ).splitlines()
    assert lines == [
        "🚨 Штрафы по последней проверке", "Автомобили со штрафами: 1", "🔴 Неоплачено: 1 штраф",
        "💰 Сумма к оплате: 50 ₾", "🟢 Оплачено: 0 ₾ (0 шт.)",
    ]


@pytest.mark.parametrize("count, expected", [
    (1, "1 штраф"), (2, "2 штрафа"), (4, "4 штрафа"), (5, "5 штрафов"), (11, "11 штрафов"),
    (12, "12 штрафов"), (14, "14 штрафов"), (20, "20 штрафов"), (21, "21 штраф"), (22, "22 штрафа"),
    (25, "25 штрафов"), (101, "101 штраф"), (111, "111 штрафов"), (112, "112 штрафов"), (122, "122 штрафа"),
])
def test_summary_shows_uncertain_fines_as_a_compact_count(count, expected):
    """🟡 > 0 — подмножество 🔴: строка «Из них» сразу под 🔴, только число с
    нужной формой «штраф», без суммы (она уже внутри 🔴)."""
    lines = _summary_lines(needs_review_amount=70.0, needs_review_count=count, paid_amount=30, paid_count=1)

    assert lines[2:] == [
        f"🔴 Неоплачено: {expected}",
        "💰 Сумма к оплате: 70 ₾",
        f"🟡 Из них требуют проверки: {expected}",
        "🟢 Оплачено: 30 ₾ (1 шт.)",
    ]


async def test_statistics_without_uncertain_fines_has_no_needs_review_line(fx):
    task, _ = fx.car(_CAR, [("GE1", 50), ("GE2", 100)])
    await fx.check(task)
    fx.tracker.mark_paid(fx.status(_CAR, "GE2").id, actor_user_id=_TRUSTED, note="TBC 1")

    lines = fx.controller._format_statistics_reply().text.splitlines()

    assert not any(line.startswith("🟡") for line in lines)
    debt = lines.index("🔴 Неоплачено: 1 штраф")  # заголовок
    assert lines[debt + 1:debt + 3] == ["💰 Сумма к оплате: 50 ₾", "🟢 Оплачено: 100 ₾ (1 шт.)"]


async def test_statistics_counts_uncertain_fines_of_all_cars_and_keeps_their_amounts_elsewhere(fx):
    """🟡 всех машин — одним числом; суммы 🟡 по-прежнему в блоках машин и
    на экране "🧾 Статусы оплаты"."""
    first, _ = fx.car("AA111BB", [("GE1", 50), ("GE2", 100)])
    second, _ = fx.car("BB222CC", [("GE3", 30), ("GE4", 40)])
    for task in (first, second):
        await fx.check(task)
    for car, protocol in (("AA111BB", "GE1"), ("AA111BB", "GE2"), ("BB222CC", "GE3")):
        fx.tracker.mark_needs_review(fx.status(car, protocol).id, actor_user_id=_TRUSTED, note=None)

    stats = fx.controller._format_statistics_reply().text
    screen = await _open_car(fx, car="AA111BB")

    assert "🟡 Из них требуют проверки: 3 штрафа" in stats.splitlines()
    assert "🔴 Неоплачено: 4 штрафа" in stats.splitlines()
    assert "💰 Сумма к оплате: 220 ₾" in stats.splitlines()  # 🔴 40 + 🟡 180
    assert "🟡 Из них требуют проверки: 2 штрафа — 150 ₾" in stats
    assert "🟡 Из них требуют проверки: 1 штраф — 30 ₾" in stats
    assert "№GE1 — 50 ₾ — 🟡 Оплата требует проверки" in screen.text
    assert "№GE2 — 100 ₾ — 🟡 Оплата требует проверки" in screen.text


async def test_car_with_only_gone_receipts_leaves_statistics_despite_old_snapshot(fx):
    task, _ = fx.car(_CAR, [("GE1", 70)])
    await fx.check(task)  # положительный снимок задачи остаётся прежним
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fx.tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=now - timedelta(hours=13))
    fx.tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=now + timedelta(seconds=5))

    reply = fx.controller._format_statistics_reply()

    assert "Автомобили со штрафами: 0" in reply.text
    assert "🔴 Неоплачено: 0 штрафов\n💰 Сумма к оплате: 0 ₾" in reply.text


async def test_unverified_history_is_not_in_the_main_statistics(fx):
    """⏳ (квитанции из истории) — ни в заголовке, ни в блоке машины
    основной статистики; видны только на экране «🧾 Статусы оплаты»."""
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    old = fx.tracker._repository.insert_listed(
        car_number=_CAR, protocol_no="OLD1", amount=30, at=datetime(2026, 10, 1, 10, 0), source="check",
    )
    fx.tracker._repository._conn.execute(
        "UPDATE georgia_fine_status SET presence = 'unverified' WHERE id = ?", (old.id,),
    )
    fx.tracker._repository._conn.commit()

    stats = fx.controller._format_statistics_reply().text
    screen = await _open_car(fx)

    assert "🔴 Неоплачено: 1 штраф" in stats.splitlines() and "🔴 Неоплачено: 1 штраф — 50 ₾" in stats.splitlines()
    assert stats.splitlines().count("💰 Сумма к оплате: 50 ₾") == 2  # заголовок и машина, без ⏳
    assert not any(line.startswith("⏳") for line in stats.splitlines())
    assert "30 ₾" not in stats
    assert "№OLD1 — 30 ₾ — ⏳ Ждёт повторной проверки police.ge" in screen.text


async def test_snapshot_newer_than_statuses_is_still_shown_as_debt(fx):
    """Квитанция давно ⚪, а потом проверка БЕЗ статусов (операторский
    процесс, сбой записи статусов) нашла настоящий долг — он не прячется."""
    task, _ = fx.car(_CAR, [("GE1", 70)])
    long_ago = datetime(2026, 1, 1, 10, 0)
    fx.tracker.record_full_check(_CAR, [_record(_CAR, "GE1", 70)], complete=True, at=long_ago)
    fx.tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=long_ago + timedelta(hours=1))
    fx.tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=long_ago + timedelta(hours=14))
    assert fx.status(_CAR, "GE1").presence == "gone"
    untracked = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant)  # без статусов
    fx.provider.records[_CAR] = [_record(_CAR, "GE5", 90)]
    await untracked.check_task(fx.tasks.get(task.id))

    reply = fx.controller._format_statistics_reply()

    assert "🔴 Неоплачено: не менее 1 штрафа" in reply.text.splitlines()
    assert reply.text.splitlines().count("💰 Сумма к оплате: 90 ₾") == 2
    assert "🔴 Неоплачено: 90 ₾ (количество уточнится при следующей проверке)" in reply.text


async def test_stale_statuses_with_listed_receipts_do_not_hide_a_newer_debt(fx):
    """Статусы есть (🔴 50), но более новая проверка без статусов нашла ещё
    один штраф — в статистике сумма снимка, а не устаревшие статусы."""
    task, _ = fx.car(_CAR, [("GE1", 50)])
    two_hours_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    fx.tracker.record_full_check(_CAR, [_record(_CAR, "GE1", 50)], complete=True, at=two_hours_ago)
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50), _record(_CAR, "GE5", 30)]
    untracked = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant)  # без статусов
    await untracked.check_task(fx.tasks.get(task.id))

    stats = fx.controller._format_statistics_reply().text

    assert "🔴 Неоплачено: 80 ₾ (количество уточнится при следующей проверке)" in stats
    assert "🔴 Неоплачено: не менее 1 штрафа" in stats.splitlines()
    assert stats.splitlines().count("💰 Сумма к оплате: 80 ₾") == 2


async def test_paid_mark_set_after_a_newer_snapshot_explains_it_after_the_next_full_check(fx):
    """Проверка без статусов, затем отметка 🟢: до следующей полной проверки
    машина показывает снимок, как раньше; после неё — 🟢 и ноль долга."""
    task, _ = fx.car(_CAR, [("GE1", 100)])
    two_hours_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    fx.tracker.record_full_check(_CAR, [_record(_CAR, "GE1", 100)], complete=True, at=two_hours_ago)
    untracked = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant)
    await untracked.check_task(fx.tasks.get(task.id))
    fx.tracker.mark_paid(fx.status(_CAR, "GE1").id, actor_user_id=_TRUSTED, note="TBC 1")

    assert "🔴 Неоплачено: 100 ₾ (количество уточнится при следующей проверке)" in (
        fx.controller._format_statistics_reply().text
    )

    await fx.check(task)  # полная проверка со статусами
    stats = fx.controller._format_statistics_reply().text

    assert "🔴 Неоплачено: 0 штрафов" in stats.splitlines() and "💰 Сумма к оплате: 0 ₾" in stats.splitlines()
    assert "🟢 Оплачено: 100 ₾ (1 шт.)" in stats.splitlines()
    assert _car_block(stats, _CAR).splitlines()[1:] == ["🟢 Оплачено: 1 штраф — 100 ₾", texts.DEBT_OWNER_NOT_LINKED_LINE]


async def test_paid_mark_on_history_does_not_hide_a_newer_snapshot(fx):
    """Проверка без статусов нашла ещё один штраф, а менеджер затем отметил
    🟢 старую квитанцию ⏳ на ту же сумму — новый долг не прячется."""
    task, _ = fx.car(_CAR, [("GE1", 50)])
    two_hours_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    fx.tracker.record_full_check(_CAR, [_record(_CAR, "GE1", 50)], complete=True, at=two_hours_ago)
    history = _make_unverified(fx, "OLD1", 30)
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50), _record(_CAR, "GE5", 30)]
    untracked = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant)
    await untracked.check_task(fx.tasks.get(task.id))
    fx.tracker.mark_paid(history.id, actor_user_id=_TRUSTED, note="TBC 2")

    stats = fx.controller._format_statistics_reply().text

    assert "🔴 Неоплачено: 80 ₾ (количество уточнится при следующей проверке)" in stats


async def test_paid_mark_older_than_a_newer_snapshot_does_not_hide_new_debt(fx):
    """🟢 поставлен до проверки без статусов, которая нашла новый штраф
    (оплаченный к тому времени исчез из ответа): та же сумма снимка не
    выдаётся за «оплачено» — показывается снимок."""
    task, _ = fx.car(_CAR, [("GE1", 50), ("GE2", 50)])
    two_hours_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    fx.tracker.record_full_check(
        _CAR, [_record(_CAR, "GE1", 50), _record(_CAR, "GE2", 50)], complete=True, at=two_hours_ago,
    )
    fx.tracker.mark_paid(fx.status(_CAR, "GE2").id, actor_user_id=_TRUSTED, note="TBC 1",
                         at=two_hours_ago + timedelta(minutes=5))
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50), _record(_CAR, "GE3", 50)]
    untracked = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant)
    await untracked.check_task(fx.tasks.get(task.id))

    stats = fx.controller._format_statistics_reply().text

    assert "🔴 Неоплачено: 100 ₾ (количество уточнится при следующей проверке)" in stats
    assert "🔴 Неоплачено: не менее 1 штрафа" in stats.splitlines()
    assert stats.splitlines().count("💰 Сумма к оплате: 100 ₾") == 2


async def test_feed_match_does_not_make_outdated_statuses_current(fx):
    """Feed снова увидел уже известную квитанцию — это не полная проверка:
    более новый снимок с ещё одним штрафом по-прежнему не прячется."""
    task, _ = fx.car(_CAR, [("GE1", 50)])
    two_hours_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    fx.tracker.record_full_check(_CAR, [_record(_CAR, "GE1", 50)], complete=True, at=two_hours_ago)
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50), _record(_CAR, "GE5", 70)]
    untracked = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant)
    await untracked.check_task(fx.tasks.get(task.id))
    fx.tracker.record_listed(_CAR, _record(_CAR, "GE1", 50))  # совпадение feed'а — сейчас

    stats = fx.controller._format_statistics_reply().text

    assert "🔴 Неоплачено: 120 ₾ (количество уточнится при следующей проверке)" in stats


def _make_unverified(fx, protocol_no, amount):
    status = fx.tracker._repository.insert_listed(
        car_number=_CAR, protocol_no=protocol_no, amount=amount, at=datetime(2026, 10, 1, 10, 0), source="check",
    )
    fx.tracker._repository._conn.execute(
        "UPDATE georgia_fine_status SET presence = 'unverified' WHERE id = ?", (status.id,),
    )
    fx.tracker._repository._conn.commit()
    return status


async def test_fallback_row_does_not_count_history_twice(fx):
    """Квитанция ⏳, которую потом нашла проверка без статусов: сумма снимка —
    в долге машины, а в строке ⏳ эта квитанция второй раз не считается."""
    task, _ = fx.car(_CAR, [("OLD1", 30)])
    _make_unverified(fx, "OLD1", 30)
    untracked = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant)
    await untracked.check_task(fx.tasks.get(task.id))

    stats = fx.controller._format_statistics_reply().text

    assert "🔴 Неоплачено: не менее 1 штрафа" in stats.splitlines()
    assert stats.splitlines().count("💰 Сумма к оплате: 30 ₾") == 2
    assert "⏳ Ждут повторной проверки" not in stats


async def test_unverified_receipt_marked_failed_payment_stays_in_debt(fx):
    """Квитанция из истории (⏳), по которой сайт МВД отказал в оплате, —
    🟡 и в долге, как любая 🟡; машина видна в списке даже без снимка долга."""
    fx.car(_CAR, [])  # ни одной успешной проверки — снимка долга нет
    status = _make_unverified(fx, "OLD1", 40)
    fx.tracker.mark_needs_review(status.id, actor_user_id=_TRUSTED, note="ჯარიმის გადახდა ვერ მოხერხდა!")

    stats = fx.controller._format_statistics_reply().text

    assert "🟡 Из них требуют проверки: 1 штраф" in stats.splitlines()
    assert "🔴 Неоплачено: 1 штраф — 40 ₾" in stats and "🟡 Из них требуют проверки: 1 штраф — 40 ₾" in stats
    assert "⏳ Ждут повторной проверки" not in stats


async def test_statistics_without_statuses_is_unchanged(tmp_path):
    fixture = _Fixture(tmp_path, with_statuses=False)
    task, _ = fixture.car(_CAR, [("GE1", 50)])
    await fixture.check(task)

    reply = fixture.controller._format_statistics_reply()

    assert "Общая сумма: 50 ₾" in reply.text
    assert "Подтверждённая задолженность" not in reply.text
    assert "🔴 Неоплачено" not in reply.text and "Автомобилей: 1" in reply.text
    assert not reply.fine_status_available
    fixture.close()


# ---- 🧾 Статусы оплаты ----


def _press_confirm(fx, reply, *, user=_TRUSTED):
    status_id, token = keyboards.decode_fine_status_confirm_callback(reply.inline_buttons[0][0][1])
    return fx.controller.handle_fine_status_confirm(status_id, token, chat_id=_CHAT, telegram_user_id=user)


async def _open_car(fx, car=_CAR, user=_TRUSTED):
    assert fx.controller.handle_fine_status_prompt(chat_id=_CHAT, telegram_user_id=user) is not None
    return await fx.controller.handle_text(car, chat_id=_CHAT, telegram_user_id=user, username=None)


async def test_only_trusted_managers_can_use_payment_statuses(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    status_id = fx.status(_CAR, "GE1").id

    assert fx.controller.handle_fine_status_prompt(chat_id=_CHAT, telegram_user_id=_OWNER) is None
    assert fx.controller.handle_fine_status_open(status_id, chat_id=_CHAT, telegram_user_id=_OWNER) is None
    assert fx.controller.handle_fine_status_action(status_id, "paid", chat_id=_CHAT, telegram_user_id=_OWNER) is None
    assert fx.controller.handle_fine_status_confirm(status_id, "abcd1234", chat_id=_CHAT, telegram_user_id=_OWNER) is None
    assert fx.status(_CAR, "GE1").payment_status == PAYMENT_UNPAID


async def test_car_screen_lists_receipts_with_buttons(fx):
    task, _ = fx.car(_CAR, [("GE1", 50), ("GE2", 100)])
    await fx.check(task)

    reply = await _open_car(fx, car="aa 111-bb")

    assert "№GE1 — 50 ₾ — 🔴 Не оплачен" in reply.text
    assert len(reply.inline_buttons) == 2
    assert reply.inline_buttons[0][0][1] == keyboards.encode_fine_status_open_callback(fx.status(_CAR, "GE1").id)


async def test_mark_paid_requires_reason_and_confirmation(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    status_id = fx.status(_CAR, "GE1").id

    prompt = fx.controller.handle_fine_status_action(status_id, "paid", chat_id=_CHAT, telegram_user_id=_TRUSTED)
    assert prompt.text == texts.FINE_STATUS_PAYMENT_NOTE_PROMPT
    empty = fx.controller._handle_fine_payment_note_input(
        " \t ", chat_id=_CHAT, telegram_user_id=_TRUSTED, payload={"status_id": status_id},
    )
    assert empty.text == texts.FINE_STATUS_PAYMENT_NOTE_EMPTY_TEXT
    confirm = await fx.controller.handle_text(
        "TBC Pay 123456, 18.11.2026", chat_id=_CHAT, telegram_user_id=_TRUSTED, username=None,
    )
    assert "Станет: 🟢 Оплачен" in confirm.text and "TBC Pay 123456" in confirm.text
    assert fx.status(_CAR, "GE1").payment_status == PAYMENT_UNPAID  # до подтверждения — ничего

    done = _press_confirm(fx, confirm)

    status = fx.status(_CAR, "GE1")
    assert status.payment_status == PAYMENT_PAID and status.payment_updated_by == _TRUSTED
    assert texts.FINE_STATUS_SAVED_TEXT in done.text and "Основание (квитанция)" in done.text


async def test_mia_failure_button_is_needs_review_whatever_the_markers(tmp_path):
    fixture = _Fixture(tmp_path, markers=PaymentPageMarkers(already_paid=("ქვითარი უკვე გადახდილია",)))
    task, _ = fixture.car(_CAR, [("GE1", 50)])
    await fixture.check(task)
    status_id = fixture.status(_CAR, "GE1").id

    confirm = fixture.controller.handle_fine_status_action(
        status_id, "failed", chat_id=_CHAT, telegram_user_id=_TRUSTED,
    )
    assert "Станет: 🟡 Оплата требует проверки" in confirm.text
    _press_confirm(fixture, confirm)

    assert fixture.status(_CAR, "GE1").payment_status == PAYMENT_NEEDS_REVIEW
    fixture.close()


def test_generic_failure_can_never_be_configured_as_paid(tmp_path):
    with pytest.raises(ValueError):
        _Fixture(tmp_path, markers=PaymentPageMarkers(already_paid=(MIA_PAYMENT_FAILED_MESSAGE,)))


@pytest.mark.parametrize("page_text, expected_status, expected_label", [
    ("ჯარიმის გადახდა ვერ მოხერხდა!", PAYMENT_NEEDS_REVIEW, "Станет: 🟡 Оплата требует проверки"),
    ("Unknown error 0x1", PAYMENT_NEEDS_REVIEW, "Станет: 🟡 Оплата требует проверки"),
    ("ქვითარი უკვე გადახდილია", PAYMENT_PAID, "Станет: 🟢 Оплачен"),
    ("Technical error, try later", PAYMENT_UNPAID, "Станет: без изменений"),
])
async def test_payment_page_text_classification(tmp_path, page_text, expected_status, expected_label):
    fixture = _Fixture(tmp_path, markers=PaymentPageMarkers(
        already_paid=("ქვითარი უკვე გადახდილია",), technical_error=("technical error",),
    ))
    task, _ = fixture.car(_CAR, [("GE1", 50)])
    await fixture.check(task)
    status_id = fixture.status(_CAR, "GE1").id

    fixture.controller.handle_fine_status_action(status_id, "text", chat_id=_CHAT, telegram_user_id=_TRUSTED)
    confirm = await fixture.controller.handle_text(page_text, chat_id=_CHAT, telegram_user_id=_TRUSTED, username=None)
    assert expected_label in confirm.text
    _press_confirm(fixture, confirm)

    assert fixture.status(_CAR, "GE1").payment_status == expected_status
    assert fixture.tracker.events(status_id)[-1]["event"] in (
        "payment_paid", "payment_needs_review", "payment_page_technical_error",
    )
    fixture.close()


async def test_stale_or_foreign_confirmation_changes_nothing(fx):
    task, _ = fx.car(_CAR, [("GE1", 50), ("GE2", 60)])
    await fx.check(task)
    first, second = fx.status(_CAR, "GE1").id, fx.status(_CAR, "GE2").id
    confirm = fx.controller.handle_fine_status_action(first, "failed", chat_id=_CHAT, telegram_user_id=_TRUSTED)
    _, token = keyboards.decode_fine_status_confirm_callback(confirm.inline_buttons[0][0][1])

    other_manager = _press_confirm(fx, confirm, user=_OTHER_TRUSTED)
    wrong_receipt = fx.controller.handle_fine_status_confirm(second, token, chat_id=_CHAT, telegram_user_id=_TRUSTED)

    assert other_manager.text == texts.STALE_DIALOG_TEXT and wrong_receipt.text == texts.STALE_DIALOG_TEXT
    assert fx.status(_CAR, "GE1").payment_status == PAYMENT_UNPAID
    assert fx.status(_CAR, "GE2").payment_status == PAYMENT_UNPAID
    # нажатия чужой/неверной кнопки не сбросили настоящий диалог
    _press_confirm(fx, confirm)
    assert fx.status(_CAR, "GE1").payment_status == PAYMENT_NEEDS_REVIEW


async def test_button_of_an_older_screen_does_not_apply_a_newer_action(tmp_path):
    fixture = _Fixture(tmp_path, markers=PaymentPageMarkers(already_paid=("ქვითარი უკვე გადახდილია",)))
    task, _ = fixture.car(_CAR, [("GE1", 50)])
    await fixture.check(task)
    status_id = fixture.status(_CAR, "GE1").id
    old_screen = fixture.controller.handle_fine_status_action(
        status_id, "failed", chat_id=_CHAT, telegram_user_id=_TRUSTED,
    )  # «Станет: 🟡»
    fixture.controller.handle_fine_status_action(status_id, "text", chat_id=_CHAT, telegram_user_id=_TRUSTED)
    new_screen = await fixture.controller.handle_text(
        "ქვითარი უკვე გადახდილია", chat_id=_CHAT, telegram_user_id=_TRUSTED, username=None,
    )  # «Станет: 🟢»
    assert "Станет: 🟢 Оплачен" in new_screen.text

    stale = _press_confirm(fixture, old_screen)

    assert stale.text == texts.STALE_DIALOG_TEXT
    assert fixture.status(_CAR, "GE1").payment_status == PAYMENT_UNPAID
    fixture.close()


async def test_confirmation_rejected_when_status_changed_meanwhile(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    status_id = fx.status(_CAR, "GE1").id
    confirm = fx.controller.handle_fine_status_action(status_id, "failed", chat_id=_CHAT, telegram_user_id=_TRUSTED)
    # тем временем другой менеджер подтвердил оплату по квитанции
    fx.tracker.mark_paid(status_id, actor_user_id=_OTHER_TRUSTED, note="BOG 77")

    reply = _press_confirm(fx, confirm)

    assert texts.FINE_STATUS_CHANGED_TEXT in reply.text
    assert fx.status(_CAR, "GE1").payment_status == PAYMENT_PAID


async def test_manager_note_is_escaped_on_confirmation_screen(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    status_id = fx.status(_CAR, "GE1").id
    fx.controller.handle_fine_status_action(status_id, "paid", chat_id=_CHAT, telegram_user_id=_TRUSTED)

    confirm = await fx.controller.handle_text(
        "**TBC** [x](http://evil) 123", chat_id=_CHAT, telegram_user_id=_TRUSTED, username=None,
    )

    assert "**TBC**" not in confirm.text and "](" not in confirm.text


async def test_cancel_and_clear(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    status_id = fx.status(_CAR, "GE1").id
    fx.tracker.mark_needs_review(status_id, actor_user_id=_TRUSTED, note=None)

    detail = fx.controller.handle_fine_status_open(status_id, chat_id=_CHAT, telegram_user_id=_TRUSTED)
    actions = [data for row in detail.inline_buttons for _, data in row]
    assert keyboards.encode_fine_status_action_callback(status_id, "unpaid") in actions

    fx.controller.handle_fine_status_action(status_id, "unpaid", chat_id=_CHAT, telegram_user_id=_TRUSTED)
    cancelled = fx.controller.handle_fine_status_cancel(status_id, chat_id=_CHAT, telegram_user_id=_TRUSTED)
    assert texts.FINE_STATUS_CANCELLED_TEXT in cancelled.text
    assert fx.status(_CAR, "GE1").payment_status == PAYMENT_NEEDS_REVIEW

    confirm = fx.controller.handle_fine_status_action(status_id, "unpaid", chat_id=_CHAT, telegram_user_id=_TRUSTED)
    _press_confirm(fx, confirm)
    assert fx.status(_CAR, "GE1").payment_status == PAYMENT_UNPAID


async def test_gone_receipt_is_shown_but_not_actionable(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    status_id = fx.status(_CAR, "GE1").id
    t0 = datetime(2026, 11, 18, 19, 0)
    fx.tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=t0)
    fx.tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=t0 + timedelta(hours=13))

    screen = await _open_car(fx)
    action = fx.controller.handle_fine_status_action(status_id, "paid", chat_id=_CHAT, telegram_user_id=_TRUSTED)

    assert "⚪ Больше не отображается в базе полиции" in screen.text
    assert screen.inline_buttons is None
    assert action.text == texts.FINE_STATUS_NOT_FOUND_TEXT


def test_callback_round_trips_and_rejects_garbage():
    assert keyboards.decode_fine_status_open_callback(keyboards.encode_fine_status_open_callback(42)) == 42
    assert keyboards.decode_fine_status_action_callback(
        keyboards.encode_fine_status_action_callback(42, "failed"),
    ) == (42, "failed")
    assert keyboards.decode_fine_status_action_callback(b"fsta:42:delete") is None
    assert keyboards.decode_fine_status_action_callback(b"fsta:x:paid") is None
    assert keyboards.decode_fine_status_confirm_callback(
        keyboards.encode_fine_status_confirm_callback(7, "a1b2c3d4"),
    ) == (7, "a1b2c3d4")
    assert keyboards.decode_fine_status_confirm_callback(b"fsty:7") is None
    assert keyboards.decode_fine_status_confirm_callback(b"fsty:7:..") is None
    assert keyboards.decode_fine_status_cancel_callback(keyboards.encode_fine_status_cancel_callback(7)) == 7
    assert keyboards.decode_fine_status_back_callback(keyboards.encode_fine_status_back_callback(7)) == 7
    assert keyboards.is_fine_status_ask_callback(b"fstask")
    assert all(len(keyboards.encode_fine_status_action_callback(10**9, a)) <= 64 for a in keyboards.FINE_STATUS_ACTIONS)


def test_daily_recheck_line_labels_every_stop_reason():
    for reason in ("blocked", "unavailable", "errors", "cutoff", "busy", "interrupted"):
        run = DailyRecheckRun(
            run_date=date(2026, 11, 18), status="postponed", started_at=datetime(2026, 11, 18, 19, 0),
            heartbeat_at=datetime(2026, 11, 18, 19, 1), finished_at=datetime(2026, 11, 18, 19, 1),
            total=5, checked=2, failed=0, stop_reason=reason,
        )
        line = texts.format_daily_recheck_line(run)
        assert reason not in line, line  # у каждой причины — русская подпись


def test_statistics_keyboard_has_payment_status_button_only_when_enabled():
    with_button = keyboards.debt_statistics_keyboard(page=None, total_pages=None, fine_status_button=True)
    without = keyboards.debt_statistics_keyboard(page=None, total_pages=None)
    assert any(button.text == texts.FINE_STATUS_BUTTON_LABEL for row in with_button for button in row)
    assert not any(button.text == texts.FINE_STATUS_BUTTON_LABEL for row in without for button in row)


# ---- 🟢: не новый штраф, не в сумме, без кнопок оплаты ----


def _changed_card(car, protocol_no, amount):
    """Та же квитанция с изменившейся карточкой на police.ge (новый fingerprint)."""
    return ParsedFineRecord(
        car_number=car, external_fine_id=protocol_no, penalty_date=date(2026, 11, 1), due_date=date(2026, 12, 31),
        delivered_status="Вручено", fingerprint=f"fp-{car}-{protocol_no}-changed",
        raw_data={"protocolNo": protocol_no}, amount=amount,
    )


async def test_add_car_does_not_count_a_paid_receipt_found_again_as_new(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    fx.tracker.mark_paid(fx.status(_CAR, "GE1").id, actor_user_id=_TRUSTED, note="TBC 1")
    fx.provider.records[_CAR] = [_changed_card(_CAR, "GE1", 50)]

    reply = await fx.controller._complete_self_add_car(
        car_number=_CAR, username=None, today=date(2026, 11, 19), chat_id=_OWNER, telegram_user_id=_OWNER,
        first_name=None, last_name=None,
    )

    assert "🔎 Штрафы проверены: новых штрафов нет" in reply.text


async def test_check_now_shows_a_paid_receipt_without_payment_and_out_of_the_amount(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50)], owner=_OWNER)
    await fx.check(task)
    fx.tracker.mark_paid(fx.status(_CAR, "GE1").id, actor_user_id=_TRUSTED, note="TBC 1")
    fx.provider.records[_CAR] = [_changed_card(_CAR, "GE1", 50)]  # police.ge показал её снова

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    assert "Статус: 🟢 Оплачен" in reply.text
    assert "💰 К оплате: 0 ₾" in reply.text
    assert reply.cta_buttons is None
    assert fx.status(_CAR, "GE1").payment_status == "paid"


def test_search_money_line_leaves_paid_fines_out_of_the_total():
    from types import SimpleNamespace

    fines = [SimpleNamespace(external_fine_id="GE1", amount=50.0), SimpleNamespace(external_fine_id="GE2", amount=100.0)]

    assert texts.format_search_money_line(check_ok=True, fines=fines) == "💰 Штрафы: 150 ₾"
    assert texts.format_search_money_line(
        check_ok=True, fines=fines, statuses={"GE2": "paid"},
    ) == "💰 Штрафы: 50 ₾ (🟢 оплачено: 100 ₾)"
    assert texts.format_search_money_line(
        check_ok=True, fines=fines, statuses={"GE1": "paid", "GE2": "paid"},
    ) == "💰 Штрафы: 0 ₾ (🟢 оплачено: 150 ₾)"
    # живая проверка: police.ge эти штрафы только что вернул — устаревший ⚪/⏳
    # в статусах (не записался) сумму не занижает, 🟡 — долг
    assert texts.format_search_money_line(
        check_ok=True, fines=fines, statuses={"GE1": "needs_review", "GE2": "gone"},
    ) == "💰 Штрафы: 150 ₾"


async def test_manager_search_block_leaves_paid_fines_out_of_the_total(fx):
    from reader.public_bot.conversation import _SearchHit

    task, subscription = fx.car(_CAR, [("GE1", 50), ("GE2", 100)], owner=_OWNER)
    await fx.check(task)
    fx.tracker.mark_paid(fx.status(_CAR, "GE2").id, actor_user_id=_TRUSTED, note="TBC 1")
    outcome = await fx.subscription_service.check_now(subscription.id, telegram_user_id=_OWNER)

    block = fx.controller._format_search_block(
        _SearchHit(task_id=task.id, owner_telegram_user_id=_OWNER, car_number=_CAR),
        monitoring_active=True, check_ok=True, fines=outcome.fines, checked_at=datetime(2026, 11, 19, 10, 0),
    )

    assert "💰 Штрафы: 50 ₾ (🟢 оплачено: 100 ₾)" in block


async def test_trusted_one_off_check_leaves_paid_fines_out_of_the_total(fx):
    """Разовая проверка менеджера (номер без активной задачи) — та же сумма,
    что в поиске: 🟢 не входит, показывается рядом."""
    task, _ = fx.car(_CAR, [("GE1", 50), ("GE2", 100)])
    await fx.check(task)
    fx.tracker.mark_paid(fx.status(_CAR, "GE2").id, actor_user_id=_TRUSTED, note="TBC 1")
    fx.tasks._conn.execute("UPDATE fine_monitoring_tasks SET status = 'stopped' WHERE id = ?", (task.id,))
    fx.tasks._conn.commit()

    result = await fx.check_service.check_plate_readonly(_CAR)
    reply = fx.controller._format_one_off_reply(result)

    assert "💰 Штрафы: 50 ₾ (🟢 оплачено: 100 ₾)" in reply.text


async def test_trusted_one_off_check_leaves_out_a_paid_receipt_back_after_gone(fx):
    """Разовая проверка статусы не пишет: 🟢, ушедший в ⚪ и снова показанный
    police.ge, остаётся 'gone' в статусах, но в сумму всё равно не входит."""
    task, _ = fx.car(_CAR, [("GE1", 50), ("GE2", 100)])
    await fx.check(task)
    paid = fx.status(_CAR, "GE2")
    fx.tracker.mark_paid(paid.id, actor_user_id=_TRUSTED, note="TBC 1")
    fx.tracker._repository._conn.execute("UPDATE georgia_fine_status SET presence = 'gone' WHERE id = ?", (paid.id,))
    fx.tracker._repository._conn.commit()
    fx.tasks._conn.execute("UPDATE fine_monitoring_tasks SET status = 'stopped' WHERE id = ?", (task.id,))
    fx.tasks._conn.commit()

    result = await fx.check_service.check_plate_readonly(_CAR)
    reply = fx.controller._format_one_off_reply(result)

    assert "💰 Штрафы: 50 ₾ (🟢 оплачено: 100 ₾)" in reply.text


# ---- 📊 Статистика: число и сумма штрафов по каждой машине (задача
# "показывать по каждой машине количество и сумму отдельно") ----

_PLATE_LINE = f"🚗 Госномер: {format_plate_for_telegram('AA111BB')}"


def _debt_row_lines(breakdown, *, owners=(), total_amount=0.0):
    return texts.format_debt_row(
        car_number="AA111BB", owners=list(owners), total_amount=total_amount, breakdown=breakdown,
    ).splitlines()


def _car_block(text, car):
    """Блок одной машины из ответа 📊 Статистика — до пустой строки."""
    start = text.index(f"🚗 Госномер: {format_plate_for_telegram(car)}")
    end = text.find("\n\n", start)
    return text[start:] if end == -1 else text[start:end]


def _unverified_receipt(fx, car, protocol_no, amount):
    status = fx.tracker._repository.insert_listed(
        car_number=car, protocol_no=protocol_no, amount=amount, at=datetime(2026, 10, 1, 10, 0), source="check",
    )
    fx.tracker._repository._conn.execute(
        "UPDATE georgia_fine_status SET presence = 'unverified' WHERE id = ?", (status.id,),
    )
    fx.tracker._repository._conn.commit()
    return status


def test_debt_row_unpaid_only_shows_count_and_amount():
    assert _debt_row_lines(DebtBreakdown(unpaid_amount=150, unpaid_count=2)) == [
        _PLATE_LINE, "🔴 Неоплачено: 2 штрафа — 150 ₾", "💰 Сумма к оплате: 150 ₾", texts.DEBT_OWNER_NOT_LINKED_LINE,
    ]


def test_debt_row_paid_only_has_no_unpaid_line():
    assert _debt_row_lines(DebtBreakdown(paid_amount=30, paid_count=1)) == [
        _PLATE_LINE, "🟢 Оплачено: 1 штраф — 30 ₾", texts.DEBT_OWNER_NOT_LINKED_LINE,
    ]


def test_debt_row_needs_review_stays_in_unpaid_and_is_shown_apart():
    assert _debt_row_lines(DebtBreakdown(
        unpaid_amount=50, unpaid_count=1, needs_review_amount=100, needs_review_count=2,
    )) == [
        _PLATE_LINE,
        "🔴 Неоплачено: 3 штрафа — 150 ₾",
        "💰 Сумма к оплате: 150 ₾",
        "🟡 Из них требуют проверки: 2 штрафа — 100 ₾",
        texts.DEBT_OWNER_NOT_LINKED_LINE,
    ]


def test_debt_row_never_mixes_paid_and_unpaid_sums():
    lines = _debt_row_lines(DebtBreakdown(unpaid_amount=50, unpaid_count=1, paid_amount=30, paid_count=2))

    assert lines[1:4] == ["🔴 Неоплачено: 1 штраф — 50 ₾", "💰 Сумма к оплате: 50 ₾", "🟢 Оплачено: 2 штрафа — 30 ₾"]
    assert not any("80 ₾" in line for line in lines)


def test_debt_row_unverified_history_is_not_unpaid():
    lines = _debt_row_lines(DebtBreakdown(unpaid_amount=50, unpaid_count=1, unverified_amount=30, unverified_count=1))

    assert lines[1:] == ["🔴 Неоплачено: 1 штраф — 50 ₾", "💰 Сумма к оплате: 50 ₾", texts.DEBT_OWNER_NOT_LINKED_LINE]


def test_debt_row_lines_keep_their_order_and_owners_stay_last():
    owners = [
        texts.DebtOwner(name="Иван", username="ivan_p"),
        texts.DebtOwner(name=None, username=None, notifications_off=True),
    ]
    lines = _debt_row_lines(DebtBreakdown(
        unpaid_amount=50, unpaid_count=1, needs_review_amount=40, needs_review_count=1,
        paid_amount=30, paid_count=1, unverified_amount=20, unverified_count=1,
    ), owners=owners)

    assert lines == [
        _PLATE_LINE,
        "🔴 Неоплачено: 2 штрафа — 90 ₾",
        "💰 Сумма к оплате: 90 ₾",
        "🟡 Из них требуют проверки: 1 штраф — 40 ₾",
        "🟢 Оплачено: 1 штраф — 30 ₾",
        "👤 Пользователь: Иван",
        "🔗 Telegram: @ivan_p",
        "👤 Пользователь: Имя не указано (уведомления выключены)",
        "🔗 Telegram: Логин не указан",
    ]


def test_debt_row_with_unknown_count_shows_the_amount_without_a_count():
    """Снимок суммы, который статусы не объясняют: число штрафов неизвестно,
    нижняя граница 1 из итогов в строке машины не печатается."""
    assert _debt_row_lines(texts.SnapshotDebt(unpaid_amount=90, unpaid_count=1)) == [
        _PLATE_LINE,
        "🔴 Неоплачено: 90 ₾ (количество уточнится при следующей проверке)",
        "💰 Сумма к оплате: 90 ₾",
        texts.DEBT_OWNER_NOT_LINKED_LINE,
    ]


def test_debt_row_single_fine_explained_by_statuses_is_counted():
    """Неизвестное число — явный тип, а не догадка по unpaid_count == 1."""
    assert _debt_row_lines(DebtBreakdown(unpaid_amount=90, unpaid_count=1))[1] == "🔴 Неоплачено: 1 штраф — 90 ₾"


def test_debt_row_without_statuses_keeps_the_amount_only_line():
    assert _debt_row_lines(None, total_amount=250) == [
        _PLATE_LINE, "💰 Задолженность: 250 ₾", texts.DEBT_OWNER_NOT_LINKED_LINE,
    ]


@pytest.mark.parametrize("count, word", [
    (1, "штраф"), (2, "штрафа"), (4, "штрафа"), (5, "штрафов"), (11, "штрафов"), (12, "штрафов"),
    (14, "штрафов"), (20, "штрафов"), (21, "штраф"), (22, "штрафа"), (25, "штрафов"), (101, "штраф"),
    (111, "штрафов"), (112, "штрафов"), (122, "штрафа"),
])
def test_debt_row_uses_the_russian_plural_on_every_line(count, word):
    lines = _debt_row_lines(DebtBreakdown(
        unpaid_amount=10, unpaid_count=count, paid_amount=20, paid_count=count,
        unverified_amount=30, unverified_count=count,
    ))
    review_lines = _debt_row_lines(DebtBreakdown(needs_review_amount=40, needs_review_count=count))

    assert lines[1:4] == [
        f"🔴 Неоплачено: {count} {word} — 10 ₾", "💰 Сумма к оплате: 10 ₾", f"🟢 Оплачено: {count} {word} — 20 ₾",
    ]
    assert not any(line.startswith("⏳") for line in lines)
    assert review_lines[1:4] == [
        f"🔴 Неоплачено: {count} {word} — 40 ₾", "💰 Сумма к оплате: 40 ₾",
        f"🟡 Из них требуют проверки: {count} {word} — 40 ₾",
    ]


async def test_paid_only_car_is_listed_but_never_counted_as_debt(fx):
    """Машина только с 🟢 — в списке и в «Автомобили со штрафами», но ни её
    число, ни её сумма не входят ни в «🔴 Неоплачено» заголовка, ни в чью-либо
    строку «🔴 Неоплачено»."""
    debt_task, _ = fx.car("AA111BB", [("GE1", 50)])
    paid_task, _ = fx.car("BB222CC", [("GE2", 30), ("GE3", 70)])
    for task in (debt_task, paid_task):
        await fx.check(task)
    for protocol in ("GE2", "GE3"):
        fx.tracker.mark_paid(fx.status("BB222CC", protocol).id, actor_user_id=_TRUSTED, note="TBC 1")

    text = fx.controller._format_statistics_reply().text

    assert "Автомобили со штрафами: 2" in text.splitlines()
    assert "Автомобилей:" not in text
    assert "🔴 Неоплачено: 1 штраф" in text.splitlines()
    assert text.splitlines().count("💰 Сумма к оплате: 50 ₾") == 2  # заголовок и AA111BB, без 🟢
    assert "🟢 Оплачено: 100 ₾ (2 шт.)" in text.splitlines()
    assert _car_block(text, "AA111BB").splitlines()[1:] == [
        "🔴 Неоплачено: 1 штраф — 50 ₾", "💰 Сумма к оплате: 50 ₾", texts.DEBT_OWNER_NOT_LINKED_LINE,
    ]
    assert _car_block(text, "BB222CC").splitlines()[1:] == [
        "🟢 Оплачено: 2 штрафа — 100 ₾", texts.DEBT_OWNER_NOT_LINKED_LINE,
    ]
    assert "150 ₾" not in text


async def test_unverified_only_and_gone_only_cars_stay_out_of_the_list(fx):
    debt_task, _ = fx.car("AA111BB", [("GE1", 50)])
    gone_task, _ = fx.car("CC333DD", [("GE5", 70)])
    fx.car("BB222CC", [])  # ни одной успешной проверки — только квитанция из истории
    for task in (debt_task, gone_task):
        await fx.check(task)
    _unverified_receipt(fx, "BB222CC", "OLD1", 40)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fx.tracker.record_full_check("CC333DD", [], complete=True, empty_confirmed=True, at=now - timedelta(hours=13))
    fx.tracker.record_full_check("CC333DD", [], complete=True, empty_confirmed=True, at=now + timedelta(seconds=5))

    text = fx.controller._format_statistics_reply().text

    assert format_plate_for_telegram("BB222CC") not in text
    assert format_plate_for_telegram("CC333DD") not in text
    assert "Автомобили со штрафами: 1" in text.splitlines()
    assert text.splitlines().count("💰 Сумма к оплате: 50 ₾") == 2  # заголовок и AA111BB
    assert not any(line.startswith("⏳") for line in text.splitlines())  # история — не в основной статистике


async def test_unknown_count_car_keeps_exact_amounts_and_sorting(fx):
    """Снимок новее статусов: в строке машины — сумма без числа; в заголовке
    сумма точная, а число — «не менее» (такая машина посчитана одним
    штрафом)."""
    known_task, _ = fx.car("AA111BB", [("GE1", 50), ("GE2", 100)])
    await fx.check(known_task)
    task, _ = fx.car("BB222CC", [("GE5", 50)])
    two_hours_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    fx.tracker.record_full_check("BB222CC", [_record("BB222CC", "GE5", 50)], complete=True, at=two_hours_ago)
    fx.provider.records["BB222CC"] = [_record("BB222CC", "GE5", 50), _record("BB222CC", "GE6", 30)]
    untracked = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant)  # без статусов
    await untracked.check_task(fx.tasks.get(task.id))

    text = fx.controller._format_statistics_reply().text

    assert "Автомобили со штрафами: 2" in text.splitlines()
    assert "🔴 Неоплачено: не менее 3 штрафов" in text.splitlines()
    assert "💰 Сумма к оплате: 230 ₾" in text.splitlines()
    assert _car_block(text, "AA111BB").splitlines()[1] == "🔴 Неоплачено: 2 штрафа — 150 ₾"
    unknown_block = _car_block(text, "BB222CC")
    assert unknown_block.splitlines()[1] == "🔴 Неоплачено: 80 ₾ (количество уточнится при следующей проверке)"
    assert "штраф" not in unknown_block
    assert text.index(format_plate_for_telegram("AA111BB")) < text.index(format_plate_for_telegram("BB222CC"))


async def test_debt_list_sorting_owners_and_pagination_are_unchanged(fx):
    """Сортировка прежняя: долг (🔴 + 🟡) по убыванию, затем оплаченное по
    убыванию, затем номер; владельцы — после строк статусов; страницы — по 10
    машин. У машин с большим оплаченным номер нарочно идёт ПОЗЖЕ пары —
    иначе порядок по оплаченному не отличить от порядка по номеру."""
    tie_paid, _ = fx.car("BB222CC", [("GE1", 50), ("GE2", 30)], owner=_OWNER)
    tie_plain, _ = fx.car("AA111BB", [("GE3", 50)])
    paid_more, _ = fx.car("DD444EE", [("GE4", 80)])
    paid_less, _ = fx.car("CC333DD", [("GE5", 70)])
    small = [fx.car(f"EE{100 + index}FF", [(f"GE{10 + index}", 5)])[0] for index in range(8)]
    for task in (tie_paid, tie_plain, paid_more, paid_less, *small):
        await fx.check(task)
    for car, protocol in (("BB222CC", "GE2"), ("DD444EE", "GE4"), ("CC333DD", "GE5")):
        fx.tracker.mark_paid(fx.status(car, protocol).id, actor_user_id=_TRUSTED, note="TBC 1")
    fx.known.record_seen(
        telegram_user_id=_OWNER, telegram_chat_id=_OWNER, telegram_username="owner_test", first_name="Анна",
    )

    first = fx.controller._format_statistics_reply()
    second = fx.controller._format_statistics_reply(debt_page=1)

    assert (first.debt_list_page, first.debt_list_total_pages) == (0, 2)
    order = ["BB222CC", "AA111BB", *(f"EE{100 + index}FF" for index in range(8))]
    positions = [first.text.index(format_plate_for_telegram(car)) for car in order]
    assert positions == sorted(positions)
    assert second.text.index(format_plate_for_telegram("DD444EE")) < second.text.index(
        format_plate_for_telegram("CC333DD"),
    )
    assert format_plate_for_telegram("CC333DD") not in first.text
    assert _car_block(first.text, "BB222CC").splitlines()[1:] == [
        "🔴 Неоплачено: 1 штраф — 50 ₾",
        "💰 Сумма к оплате: 50 ₾",
        "🟢 Оплачено: 1 штраф — 30 ₾",
        "👤 Пользователь: Анна",
        "🔗 Telegram: @owner_test",
    ]
    assert "Автомобили со штрафами: 12" in second.text.splitlines()
    assert "🔴 Неоплачено: 10 штрафов" in second.text.splitlines()
    assert "💰 Сумма к оплате: 140 ₾" in second.text.splitlines()
    assert "🟢 Оплачено: 180 ₾ (3 шт.)" in second.text.splitlines()


_DEBT_LINE = re.compile(r"🔴 Неоплачено: (?:не менее )?(\d+) штраф\w* — (\d+(?:\.\d+)?) ₾")


async def test_header_and_cards_agree_on_mixed_data(fx):
    """🔴 заголовка — 🔴 + 🟡 всех машин: суммы и числа строк «🔴 Неоплачено»
    машин складываются ровно в него; 🟡 — подмножество (только число), 🟢 —
    отдельно и в 🔴 не входит; ⏳ не показывается вовсе."""
    mixed, _ = fx.car("AA111BB", [("GE1", 50), ("GE2", 100), ("GE3", 70)])
    with_paid, _ = fx.car("BB222CC", [("GE4", 40), ("GE5", 25)])
    review_only, _ = fx.car("CC333DD", [("GE6", 20), ("GE7", 15)])
    paid_only, _ = fx.car("DD444EE", [("GE8", 60)])
    for task in (mixed, with_paid, review_only, paid_only):
        await fx.check(task)
    for car, protocol in (("AA111BB", "GE3"), ("CC333DD", "GE6"), ("CC333DD", "GE7")):
        fx.tracker.mark_needs_review(fx.status(car, protocol).id, actor_user_id=_TRUSTED, note=None)
    for car, protocol in (("BB222CC", "GE5"), ("DD444EE", "GE8")):
        fx.tracker.mark_paid(fx.status(car, protocol).id, actor_user_id=_TRUSTED, note="TBC 1")
    _unverified_receipt(fx, "AA111BB", "OLD1", 30)

    lines = fx.controller._format_statistics_reply().text.splitlines()

    header_index = lines.index("Автомобили со штрафами: 4")
    assert lines[header_index + 1:header_index + 5] == [
        "🔴 Неоплачено: 6 штрафов",  # 🔴 50 + 100 + 40, 🟡 70 + 20 + 15
        "💰 Сумма к оплате: 295 ₾",
        "🟡 Из них требуют проверки: 3 штрафа",
        "🟢 Оплачено: 85 ₾ (2 шт.)",
    ]
    assert not any(line.startswith("⏳") for line in lines)
    cards = [_DEBT_LINE.fullmatch(line) for line in lines if line.startswith("🔴 Неоплачено:") and "—" in line]
    assert len(cards) == 3  # у машины только с 🟢 строки 🔴 нет
    assert sum(int(card[1]) for card in cards) == 6
    assert sum(float(card[2]) for card in cards) == 295
    assert [line for line in lines if line.startswith("💰 Сумма к оплате:")][1:] == [
        "💰 Сумма к оплате: 220 ₾", "💰 Сумма к оплате: 40 ₾", "💰 Сумма к оплате: 35 ₾",
    ]
    assert "🔴 Неоплачено: 3 штрафа — 220 ₾" in lines and "🟡 Из них требуют проверки: 1 штраф — 70 ₾" in lines
    assert "🔴 Неоплачено: 2 штрафа — 35 ₾" in lines and "🟡 Из них требуют проверки: 2 штрафа — 35 ₾" in lines
    assert "🔴 Неоплачено: 1 штраф — 40 ₾" in lines and "🟢 Оплачено: 1 штраф — 25 ₾" in lines
    assert "🟢 Оплачено: 1 штраф — 60 ₾" in lines


@pytest.mark.parametrize("count, expected", [
    (1, "не менее 1 штрафа"), (2, "не менее 2 штрафов"), (4, "не менее 4 штрафов"), (5, "не менее 5 штрафов"),
    (11, "не менее 11 штрафов"), (21, "не менее 21 штрафа"), (22, "не менее 22 штрафов"),
    (101, "не менее 101 штрафа"), (111, "не менее 111 штрафов"),
])
def test_summary_marks_a_lower_bound_count_with_ne_menee(count, expected):
    """Когда у части машин число штрафов неизвестно, число заголовка —
    нижняя граница: «не менее N штрафов» (родительный падеж), сумма точная."""
    lines = texts.format_debt_status_summary_block(
        car_count=2, totals=DebtBreakdown(unpaid_amount=230, unpaid_count=count),
        source_updated_on=None, recheck_line=None, debt_count_is_lower_bound=True,
    ).splitlines()

    assert lines[2:4] == [f"🔴 Неоплачено: {expected}", "💰 Сумма к оплате: 230 ₾"]


async def test_long_status_pages_stay_single_messages(tmp_path):
    """Самые длинные блоки со статусами (все четыре строки, машины с
    неизвестным числом штрафов, по 3 пользователя с максимальными именами)
    под самым длинным заголовком: каждая страница — ОДНО сообщение Telegram,
    каждая машина — ровно на одной странице."""
    from reader.public_bot.handlers import _TELEGRAM_SAFE_TEXT_LIMIT, _split_telegram_text

    run = DailyRecheckRun(
        run_date=date(2026, 11, 18), status="stopped", started_at=datetime(2026, 11, 18, 19, 0),
        heartbeat_at=datetime(2026, 11, 18, 19, 40), finished_at=datetime(2026, 11, 18, 19, 40),
        total=4000, checked=1234, failed=567, stop_reason="blocked",
    )
    fixture = _Fixture(tmp_path, recheck_run=run)
    fixture.tracker.record_source_updated_on(date(2026, 11, 12))
    plates = [f"LP{100 + index}AA" for index in range(12)]
    two_hours_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    untracked = FineCheckService(fixture.provider, fixture.tasks, fixture.fines, sleep=_instant)
    for index, car in enumerate(plates):
        fines = [(f"R{index}", 1000 + index), (f"Y{index}", 2000), (f"G{index}", 3000)]
        task, _ = fixture.car(car, fines)
        if index % 4 == 3:  # снимок новее статусов — число штрафов неизвестно
            fixture.tracker.record_full_check(
                car, [_record(car, protocol, amount) for protocol, amount in fines], complete=True, at=two_hours_ago,
            )
            fixture.provider.records[car].append(_record(car, f"N{index}", 99999))
            await untracked.check_task(fixture.tasks.get(task.id))
        else:
            await fixture.check(task)
            fixture.tracker.mark_needs_review(fixture.status(car, f"Y{index}").id, actor_user_id=_TRUSTED, note=None)
            fixture.tracker.mark_paid(fixture.status(car, f"G{index}").id, actor_user_id=_TRUSTED, note="TBC 1")
            _unverified_receipt(fixture, car, f"H{index}", 4000)
        for owner_index in range(3):
            user_id = 900001000 + index * 10 + owner_index
            fixture.subscriptions.create(
                monitoring_task_id=task.id, car_number=car, telegram_user_id=user_id, telegram_chat_id=user_id,
                telegram_username=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
            )
            fixture.known.record_seen(
                telegram_user_id=user_id, telegram_chat_id=user_id, telegram_username=f"user_{user_id}",
                first_name="Я" * 64, last_name="Ж" * 64,
            )

    first_page = fixture.controller._format_statistics_reply()
    total_pages = first_page.debt_list_total_pages
    assert total_pages > 2

    seen: list[str] = []
    for page in range(total_pages):
        reply = fixture.controller.handle_debt_list_page(page, telegram_user_id=_TRUSTED)
        assert len(reply.text) <= _TELEGRAM_SAFE_TEXT_LIMIT
        assert _split_telegram_text(reply.text) == [reply.text]
        seen.extend(car for car in plates if format_plate_for_telegram(car) in reply.text)
    assert sorted(seen) == sorted(plates)
    assert "(количество уточнится при следующей проверке)" in "".join(
        fixture.controller.handle_debt_list_page(page, telegram_user_id=_TRUSTED).text for page in range(total_pages)
    )
    fixture.close()


def test_debt_row_lines_are_decided_by_count_not_amount():
    """Квитанция без суммы (police.ge не дал amount — в статусах 0) всё равно
    штраф: каждая строка показывается по числу, а не по сумме."""
    assert _debt_row_lines(DebtBreakdown(
        unpaid_count=1, needs_review_count=1, paid_count=1, unverified_count=1,
    )) == [
        _PLATE_LINE,
        "🔴 Неоплачено: 2 штрафа — 0 ₾",
        "💰 Сумма к оплате: 0 ₾",
        "🟡 Из них требуют проверки: 1 штраф — 0 ₾",
        "🟢 Оплачено: 1 штраф — 0 ₾",
        texts.DEBT_OWNER_NOT_LINKED_LINE,
    ]


async def test_receipt_without_amount_is_listed_and_counted(fx):
    task, _ = fx.car("AA111BB", [("GE1", None)])
    await fx.check(task)

    text = fx.controller._format_statistics_reply().text

    assert "Автомобили со штрафами: 1" in text.splitlines()
    assert "🔴 Неоплачено: 1 штраф" in text.splitlines() and "🔴 Неоплачено: 1 штраф — 0 ₾" in text.splitlines()
    assert text.splitlines().count("💰 Сумма к оплате: 0 ₾") == 2  # заголовок и машина


async def test_unknown_count_header_counts_one_fine_per_car(fx):
    """Ревью: за снимком 4 штрафа, статусы знают 3 — но они могли уже уйти из
    ответа, поэтому надёжная нижняя граница — один штраф на машину."""
    task, _ = fx.car("AA111BB", [("GE1", 50), ("GE2", 60), ("GE3", 70)])
    two_hours_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    fx.tracker.record_full_check(
        "AA111BB", [_record("AA111BB", protocol, amount) for protocol, amount in (("GE1", 50), ("GE2", 60), ("GE3", 70))],
        complete=True, at=two_hours_ago,
    )
    fx.provider.records["AA111BB"].append(_record("AA111BB", "GE4", 80))
    untracked = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant)  # без статусов
    await untracked.check_task(fx.tasks.get(task.id))

    text = fx.controller._format_statistics_reply().text

    assert "🔴 Неоплачено: не менее 1 штрафа" in text.splitlines()
    assert text.splitlines().count("💰 Сумма к оплате: 260 ₾") == 2
    assert _car_block(text, "AA111BB").splitlines()[1] == (
        "🔴 Неоплачено: 260 ₾ (количество уточнится при следующей проверке)"
    )


async def test_known_limitation_issue_62_saved_total_shows_a_paid_car_as_unpaid(fx):
    """Известное ограничение (issue #62), поведение не меняется: после
    проверки без статусов (ручная операторская `fine check`) снимок новее
    статусов, и машина только с 🟢 до следующей полной проверки показана
    неоплаченной с неизвестным числом. Её 🟢 при этом не показывается и не
    считается второй раз: строки 🟢 у машины нет, в 🟢 заголовка её нет."""
    task, _ = fx.car("BB222CC", [("GE9", 100)])
    two_hours_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    fx.tracker.record_full_check("BB222CC", [_record("BB222CC", "GE9", 100)], complete=True, at=two_hours_ago)
    untracked = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant)  # без статусов
    await untracked.check_task(fx.tasks.get(task.id))
    fx.tracker.mark_paid(fx.status("BB222CC", "GE9").id, actor_user_id=_TRUSTED, note="TBC 1")

    text = fx.controller._format_statistics_reply().text

    assert _car_block(text, "BB222CC").splitlines()[1:] == [
        "🔴 Неоплачено: 100 ₾ (количество уточнится при следующей проверке)", "💰 Сумма к оплате: 100 ₾",
        texts.DEBT_OWNER_NOT_LINKED_LINE,
    ]
    assert "🔴 Неоплачено: не менее 1 штрафа" in text.splitlines()
    assert "🟢 Оплачено: 0 ₾ (0 шт.)" in text.splitlines()
