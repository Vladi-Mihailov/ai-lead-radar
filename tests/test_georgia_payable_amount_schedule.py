"""Сумма к оплате Грузии — контрольные периоды и лимиты сессий Georgian Card
(reader/fines/payable_amount.py, reader/public_bot/payable_amount_service.py).

Georgian Card — заглушка (mock): ни одного сетевого запроса. SQLite —
настоящий (tmp_path). Номера, квитанции и пользователи синтетические."""

import asyncio
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.georgian_card_client import AmountQuote  # noqa: E402
from reader.fines.models import UNLIMITED_END_DATE  # noqa: E402
from reader.fines.payable_amount import (  # noqa: E402
    CONTROL_PERIOD_COUNT,
    MAX_AUTO_CHECKS_PER_PERIOD,
    OUTCOME_ABANDONED,
    OUTCOME_BLOCKED,
    OUTCOME_ERROR,
    OUTCOME_IN_PROGRESS,
    OUTCOME_INVALID_RESPONSE,
    OUTCOME_OK,
    OUTCOME_REFUSED,
    OUTCOME_TIMEOUT,
    REFUSAL_BUSY,
    REFUSAL_PERIOD_LIMIT,
    TRIGGER_AUTO,
    TRIGGER_MANUAL,
    PayableAmountRepository,
    amount_is_current,
    control_periods,
    latest_started_period,
    open_period,
    term_end_of,
)
from reader.fines.payment_status import (  # noqa: E402
    PAYMENT_NEEDS_REVIEW,
    PAYMENT_PAID,
    PAYMENT_UNPAID,
    PRESENCE_GONE,
    PRESENCE_MISSING,
    PRESENCE_UNVERIFIED,
    FineStatusRepository,
    FineStatusTracker,
)
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.payable_amount_service import (  # noqa: E402
    STATUS_BUSY,
    STATUS_CACHED,
    STATUS_FAILED,
    STATUS_FETCHED,
    STATUS_LIMITED,
    STATUS_NOT_CONFIGURED,
    STATUS_SUSPENDED,
    PayableAmountLimits,
    PayableAmountService,
    ReceiptRef,
)
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402

_TBILISI = ZoneInfo("Asia/Tbilisi")
_CLIENT = 700001
_MANAGER = 900001
_TERM_END = date(2026, 10, 31)  # lastDate: первый период — с 01.11


def _tbilisi(day: date, hh: int = 13, mm: int = 5) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=_TBILISI).astimezone(timezone.utc)


class _FakeGeorgianCard:
    """Mock Georgian Card: отвечает заранее заданной суммой или исходом и
    считает вызовы. Ни карты, ни сети."""

    def __init__(self):
        self.configured = True
        self.amounts: dict[str, float] = {}
        self.outcomes: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.delay = 0.0

    async def fetch_amount(self, *, car_number, protocol_no, base_amount):
        self.calls.append((car_number, protocol_no))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if outcome != OUTCOME_OK:
                return AmountQuote(outcome, detail="mock")
        return AmountQuote(OUTCOME_OK, amount=self.amounts.get(protocol_no, base_amount), currency="GEL")


class _Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self):
        return self.now


async def _no_sleep(_seconds):
    pass


class _Fixture:
    def __init__(self, tmp_path, **limits):
        self.db_path = tmp_path / "users.db"
        self.tasks = FineMonitoringTaskRepository(self.db_path)
        self.fines = DetectedFineRepository(self.db_path)
        self.subscriptions = FineSubscriptionRepository(self.db_path)
        self.status_repository = FineStatusRepository(self.db_path)
        self.tracker = FineStatusTracker(self.status_repository)
        self.amounts = PayableAmountRepository(self.db_path)
        self.gc = _FakeGeorgianCard()
        self.clock = _Clock(_tbilisi(_TERM_END - timedelta(days=5)))
        self.service = PayableAmountService(
            self.amounts, self.gc, self.tracker, self.subscriptions, tz=_TBILISI,
            limits=PayableAmountLimits(**{"auto_delay_seconds": 0, **limits}),
            trusted_user_ids=frozenset({_MANAGER}), auto_enabled=True,
            auto_start_time=time(13, 0), auto_cutoff_time=time(14, 0), clock=self.clock, sleep=_no_sleep,
        )
        self._tasks_by_car: dict[str, object] = {}

    def receipt(self, car="AA111BB", protocol="R1", *, base=50.0, term_end=_TERM_END, owner=_CLIENT):
        task = self._tasks_by_car.get(car)
        if task is None:
            task = self.tasks.create(
                car_number=car, label=None, start_date=date(2026, 9, 1), end_date=UNLIMITED_END_DATE,
                telegram_chat_id=-1, created_by_user_id=1,
            )
            self._tasks_by_car[car] = task
            if owner is not None:
                self.subscriptions.create(
                    monitoring_task_id=task.id, car_number=car, telegram_user_id=owner, telegram_chat_id=owner,
                    telegram_username=None, start_date=date(2026, 9, 1), end_date=UNLIMITED_END_DATE,
                )
        self.fines.create(
            monitoring_task_id=task.id, car_number=car, external_fine_id=protocol, fingerprint=f"fp-{car}-{protocol}",
            penalty_date=None, due_date=term_end, delivered_status=None, raw_data="{}", amount=base,
        )
        return self.status_repository.insert_listed(
            car_number=car, protocol_no=protocol, amount=base, at=datetime(2026, 9, 1, 8, 0), source="check",
        )

    def at(self, day: date, hh: int = 13, mm: int = 5) -> None:
        self.clock.now = _tbilisi(day, hh, mm)

    def run(self):
        return asyncio.run(self.service.run_auto_once())

    def run_days(self, first: date, last: date) -> None:
        day = first
        while day <= last:
            self.at(day)
            self.run()
            day += timedelta(days=1)

    def auto_checks(self, protocol="R1", car="AA111BB"):
        return [check for check in self.amounts.checks_for_receipt(car, protocol) if check.trigger == TRIGGER_AUTO]

    def close(self):
        for repository in (self.amounts, self.status_repository, self.subscriptions, self.fines, self.tasks):
            repository.close()


@pytest.fixture()
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


# ---- Контрольные периоды (чистые функции) ----


def test_first_period_starts_the_day_after_last_date_without_adding_thirty_days_again():
    periods = control_periods(_TERM_END)
    assert len(periods) == CONTROL_PERIOD_COUNT == 6
    assert [(p.index, p.start, p.window_end) for p in periods[:2]] == [
        (1, date(2026, 11, 1), date(2026, 11, 10)),
        (2, date(2026, 12, 1), date(2026, 12, 10)),
    ]
    # 30/60/.../180 дней: каждый следующий — через 30 дней после предыдущего
    assert [p.start - periods[0].start for p in periods] == [timedelta(days=30 * i) for i in range(6)]
    assert all(p.window_end - p.start == timedelta(days=9) for p in periods)


def test_open_period_only_inside_ten_day_windows():
    assert open_period(_TERM_END, date(2026, 10, 31)) is None  # срок ещё идёт
    assert open_period(_TERM_END, date(2026, 11, 1)).index == 1
    assert open_period(_TERM_END, date(2026, 11, 10)).index == 1
    assert open_period(_TERM_END, date(2026, 11, 11)) is None  # между окнами
    assert open_period(_TERM_END, date(2026, 11, 30)) is None
    assert open_period(_TERM_END, date(2026, 12, 1)).index == 2
    last = control_periods(_TERM_END)[-1]
    assert open_period(_TERM_END, last.window_end).index == 6
    assert open_period(_TERM_END, last.window_end + timedelta(days=1)) is None
    assert open_period(_TERM_END, last.window_end + timedelta(days=400)) is None
    assert latest_started_period(_TERM_END, date(2026, 10, 31)) is None
    assert latest_started_period(_TERM_END, date(2026, 11, 20)).index == 1


def test_term_end_uses_last_date_then_falls_back_to_delivery_and_publication():
    assert term_end_of(date(2026, 10, 31), {"lastDate": "2026-12-31", "activeDate": "2026-10-01"}) == date(2026, 10, 31)
    assert term_end_of(None, {"lastDate": "2026-10-31"}) == date(2026, 10, 31)
    assert term_end_of(None, {"activeDate": "2026-10-01"}) == date(2026, 10, 31)
    assert term_end_of(None, {"publishDate": "2026-09-01"}) == date(2026, 10, 31)
    assert term_end_of(None, {"publishDate": "01.09.2026"}) == date(2026, 10, 31)
    assert term_end_of(None, {}) is None
    assert term_end_of(None, None) is None


def test_amount_is_stale_once_a_new_period_starts():
    from reader.fines.payable_amount import PayableAmount

    amount = PayableAmount(
        car_number="AA111BB", protocol_no="R1", amount=150.0, currency="GEL",
        observed_at=_tbilisi(date(2026, 11, 3)).replace(tzinfo=None), source="auto",
        first_amount=150.0, first_observed_at=_tbilisi(date(2026, 11, 3)).replace(tzinfo=None),
    )
    assert amount_is_current(amount, _TERM_END, today=date(2026, 11, 20), tz=_TBILISI)
    assert not amount_is_current(amount, _TERM_END, today=date(2026, 12, 1), tz=_TBILISI)
    assert amount_is_current(amount, None, today=date(2027, 6, 1), tz=_TBILISI)
    assert not amount_is_current(None, _TERM_END, today=date(2026, 11, 20), tz=_TBILISI)


# ---- Автоматический проход ----


def test_no_automatic_checks_before_the_first_control_period(fx):
    fx.receipt()
    fx.run_days(date(2026, 10, 20), date(2026, 10, 31))
    assert fx.gc.calls == []


def test_ten_checks_per_period_not_ten_for_the_whole_life_of_the_fine(fx):
    fx.receipt(base=50.0)
    fx.gc.amounts["R1"] = 50.0  # сумма не растёт
    fx.run_days(date(2026, 11, 1), date(2026, 12, 30))  # период 3 начнётся 31.12

    by_period = {}
    for check in fx.auto_checks():
        by_period.setdefault(check.period_index, []).append(check.check_date)
    # период 1: ровно 10 дней подряд, по одной проверке в сутки; период 2 — снова 10
    assert by_period[1] == [date(2026, 11, 1) + timedelta(days=i) for i in range(10)]
    assert by_period[2] == [date(2026, 12, 1) + timedelta(days=i) for i in range(10)]
    assert len(fx.gc.calls) == 20
    # между окнами — ни одной сессии
    assert not any(date(2026, 11, 11) <= check.check_date <= date(2026, 11, 30) for check in fx.auto_checks())


def test_all_six_periods_then_monitoring_of_the_receipt_stops(fx):
    fx.receipt(base=50.0)
    fx.gc.amounts["R1"] = 50.0
    last = control_periods(_TERM_END)[-1]
    fx.run_days(date(2026, 11, 1), last.window_end + timedelta(days=45))

    periods = sorted({check.period_index for check in fx.auto_checks()})
    assert periods == [1, 2, 3, 4, 5, 6]
    assert len(fx.auto_checks()) == 6 * MAX_AUTO_CHECKS_PER_PERIOD
    assert max(check.check_date for check in fx.auto_checks()) == last.window_end


def test_increase_stops_the_period_and_checks_resume_in_the_next_one(fx):
    fx.receipt(base=50.0)
    fx.gc.amounts["R1"] = 50.0
    fx.run_days(date(2026, 11, 1), date(2026, 11, 10))  # период 1: без увеличения
    fx.gc.amounts["R1"] = 150.0
    fx.run_days(date(2026, 12, 1), date(2026, 12, 10))  # период 2: увеличение в первый же день

    period2 = [check for check in fx.auto_checks() if check.period_index == 2]
    assert [check.check_date for check in period2] == [date(2026, 12, 1)]
    assert period2[0].increased and period2[0].amount == 150.0 and period2[0].previous_amount == 50.0
    events = fx.amounts.events_since(datetime(2000, 1, 1))
    assert [(e.old_amount, e.new_amount) for e in events] == [(50.0, 150.0)]

    fx.run_days(date(2026, 12, 31), date(2027, 1, 9))  # период 3 — новый цикл
    period3 = [check for check in fx.auto_checks() if check.period_index == 3]
    assert len(period3) == 10


def test_increase_found_on_a_later_day_stops_the_rest_of_the_window(fx):
    fx.receipt(base=50.0)
    fx.gc.amounts["R1"] = 50.0
    fx.run_days(date(2026, 11, 1), date(2026, 11, 3))
    fx.gc.amounts["R1"] = 150.0
    fx.run_days(date(2026, 11, 4), date(2026, 11, 10))
    assert [check.check_date.day for check in fx.auto_checks()] == [1, 2, 3, 4]


def test_first_amount_already_increased_is_saved_silently_and_closes_the_period(fx):
    fx.receipt(base=50.0)
    fx.gc.amounts["R1"] = 150.0
    fx.run_days(date(2026, 12, 1), date(2026, 12, 10))  # первое получение — сразу в периоде 2

    checks = fx.auto_checks()
    assert len(checks) == 1 and checks[0].increased and checks[0].previous_amount is None
    assert fx.amounts.events_since(datetime(2000, 1, 1)) == []  # без исторической рассылки
    stored = fx.amounts.get_amount("AA111BB", "R1")
    assert stored.amount == stored.first_amount == 150.0


def test_next_increase_relative_to_the_saved_amount_is_a_new_event(fx):
    fx.receipt(base=50.0)
    fx.gc.amounts["R1"] = 150.0
    fx.run_days(date(2026, 11, 1), date(2026, 11, 1))  # первое: 150, молча
    fx.gc.amounts["R1"] = 200.0
    fx.run_days(date(2026, 12, 1), date(2026, 12, 1))
    events = fx.amounts.events_since(datetime(2000, 1, 1))
    assert [(e.old_amount, e.new_amount) for e in events] == [(150.0, 200.0)]


def test_same_amount_or_a_decrease_creates_no_event_but_a_decrease_is_saved(fx):
    fx.receipt(base=150.0)  # первая сумма = исходной: период не закрывается
    fx.gc.amounts["R1"] = 150.0
    fx.run_days(date(2026, 11, 1), date(2026, 11, 1))
    fx.run_days(date(2026, 11, 2), date(2026, 11, 2))  # та же сумма
    fx.gc.amounts["R1"] = 120.0
    fx.run_days(date(2026, 11, 3), date(2026, 11, 3))  # снижение
    assert fx.amounts.events_since(datetime(2000, 1, 1)) == []
    assert fx.amounts.get_amount("AA111BB", "R1").amount == 120.0


@pytest.mark.parametrize("mark", ["paid", "unverified", "gone"])
def test_paid_history_and_gone_receipts_are_never_checked(fx, mark):
    status = fx.receipt()
    at = datetime(2026, 9, 2)
    if mark == "paid":
        fx.status_repository.update_payment(
            status.id, payment_status=PAYMENT_PAID, source="receipt", note="BOG 1", actor_user_id=_MANAGER,
            at=at, event="paid",
        )
    else:
        presence = PRESENCE_UNVERIFIED if mark == "unverified" else PRESENCE_GONE
        fx.status_repository.update_presence(status.id, presence=presence, at=at, event=mark, source="check")
    fx.run_days(date(2026, 11, 1), date(2026, 11, 10))
    assert fx.gc.calls == []


def test_receipt_missing_once_and_uncertain_payment_are_still_checked(fx):
    first = fx.receipt(protocol="R1")
    second = fx.receipt(protocol="R2")
    fx.status_repository.update_presence(
        first.id, presence=PRESENCE_MISSING, at=datetime(2026, 9, 2), event="missing", source="check",
    )
    fx.status_repository.update_payment(
        second.id, payment_status=PAYMENT_NEEDS_REVIEW, source="manager", note=None, actor_user_id=_MANAGER,
        at=datetime(2026, 9, 2), event="needs_review",
    )
    fx.run_days(date(2026, 11, 1), date(2026, 11, 1))
    assert sorted(protocol for _, protocol in fx.gc.calls) == ["R1", "R2"]


@pytest.mark.parametrize("link", ["none", "manager_only", "stopped"])
def test_cars_without_a_live_client_link_are_not_checked_automatically(fx, link):
    owner = {"none": None, "manager_only": _MANAGER, "stopped": _CLIENT}[link]
    fx.receipt(owner=owner)
    if link == "stopped":
        subscription = fx.subscriptions.list_by_car_number("AA111BB")[0]
        fx.subscriptions.stop_by_owner_or_creator(subscription.id, telegram_user_id=_CLIENT)
    fx.run_days(date(2026, 11, 1), date(2026, 11, 3))
    assert fx.gc.calls == []


def test_refusal_closes_the_period_and_never_marks_the_fine_paid(fx):
    status = fx.receipt()
    fx.gc.outcomes = [OUTCOME_REFUSED]
    fx.run_days(date(2026, 11, 1), date(2026, 11, 10))
    assert len(fx.auto_checks()) == 1 and fx.auto_checks()[0].outcome == OUTCOME_REFUSED
    assert fx.tracker.get(status.id).payment_status == PAYMENT_UNPAID
    assert fx.amounts.get_amount("AA111BB", "R1") is None


def test_errors_are_retried_on_later_days_and_never_mean_payment(fx):
    status = fx.receipt()
    fx.gc.outcomes = [OUTCOME_TIMEOUT, OUTCOME_INVALID_RESPONSE]
    fx.gc.amounts["R1"] = 50.0
    fx.run_days(date(2026, 11, 1), date(2026, 11, 3))
    outcomes = [check.outcome for check in fx.auto_checks()]
    assert outcomes == [OUTCOME_TIMEOUT, OUTCOME_INVALID_RESPONSE, OUTCOME_OK]
    assert fx.tracker.get(status.id).payment_status == PAYMENT_UNPAID
    # попытка с ошибкой тоже считается в лимите периода (сессия могла быть создана)
    assert fx.amounts.auto_checks_in_period("AA111BB", "R1", 1) == 3


def test_consecutive_errors_stop_the_day_run(fx):
    for index in range(5):
        fx.receipt(car=f"AA{index}11BB", protocol=f"R{index}")
    fx.gc.outcomes = [OUTCOME_ERROR, OUTCOME_ERROR, OUTCOME_ERROR, OUTCOME_ERROR, OUTCOME_ERROR]
    fx.at(date(2026, 11, 1))
    assert fx.run() == "stopped"
    assert len(fx.gc.calls) == 2  # max_consecutive_errors = 2
    run = fx.amounts.get_run(date(2026, 11, 1))
    assert run.stop_reason == "errors" and run.sessions == 2 and run.errors == 2
    fx.at(date(2026, 11, 1), 13, 30)
    assert fx.run() is None  # в этот день проход больше не начинается


def test_blocked_response_suspends_all_sessions(fx):
    fx.receipt(protocol="R1")
    fx.receipt(protocol="R2")
    fx.gc.outcomes = [OUTCOME_BLOCKED]
    fx.at(date(2026, 11, 1))
    assert fx.run() == "stopped"
    assert len(fx.gc.calls) == 1
    assert fx.amounts.get_run(date(2026, 11, 1)).stop_reason == "blocked"
    # ручное уточнение тоже не открывает сессию, пока действует приостановка
    subscription = fx.subscriptions.list_by_car_number("AA111BB")[0]
    outcome = asyncio.run(fx.service.refresh_for_subscription(subscription, telegram_user_id=_CLIENT))
    # R1 пытались только что — показана сохранённая (без сессии), R2 — приостановка
    assert [result.status for result in outcome.results] == [STATUS_CACHED, STATUS_SUSPENDED]
    assert len(fx.gc.calls) == 1
    # на следующий день (24 ч) — снова можно
    fx.at(date(2026, 11, 2), 13, 10)
    fx.run()
    assert len(fx.gc.calls) == 3


def test_daily_session_limit_is_a_hard_cap(tmp_path):
    fixture = _Fixture(tmp_path, max_sessions_per_day=3)
    for index in range(5):
        fixture.receipt(car=f"AA{index}11BB", protocol=f"R{index}")
    fixture.at(date(2026, 11, 1))
    assert fixture.run() == "stopped"
    assert len(fixture.gc.calls) == 3
    assert fixture.amounts.get_run(date(2026, 11, 1)).stop_reason == "daily_limit"
    fixture.close()


def test_run_stops_at_the_window_cutoff_and_does_not_run_outside_it(fx):
    fx.receipt()
    fx.at(date(2026, 11, 1), 12, 59)
    assert fx.run() is None
    fx.at(date(2026, 11, 1), 14, 0)
    assert fx.run() is None
    assert fx.gc.calls == []


def test_auto_checks_are_off_unless_enabled(tmp_path):
    fixture = _Fixture(tmp_path)
    fixture.service._auto_enabled = False
    fixture.receipt()
    fixture.run_days(date(2026, 11, 1), date(2026, 11, 3))
    assert fixture.gc.calls == []
    fixture.close()


def test_not_configured_client_opens_no_session(fx):
    fx.receipt()
    fx.gc.configured = False
    fx.at(date(2026, 11, 1))
    assert fx.run() == "stopped"
    assert fx.gc.calls == []
    assert fx.amounts.checks_for_receipt("AA111BB", "R1") == []


# ---- Лимиты в одном INSERT ----


def test_begin_check_refuses_the_eleventh_automatic_check_of_a_period(fx):
    for day in range(10):
        check_id = fx.amounts.begin_check(
            car_number="AA111BB", protocol_no="R1", trigger=TRIGGER_AUTO, period_index=1, requested_by=None,
            check_date=date(2026, 11, 1) + timedelta(days=day), started_at=_tbilisi(date(2026, 11, 1) + timedelta(days=day)),
            max_sessions_per_day=100,
        )
        assert isinstance(check_id, int)
        fx.amounts.finish_check(check_id, outcome=OUTCOME_OK, finished_at=_tbilisi(date(2026, 11, 1)), amount=50.0)
    refused = fx.amounts.begin_check(
        car_number="AA111BB", protocol_no="R1", trigger=TRIGGER_AUTO, period_index=1, requested_by=None,
        check_date=date(2026, 11, 20), started_at=_tbilisi(date(2026, 11, 20)), max_sessions_per_day=100,
    )
    assert refused == REFUSAL_PERIOD_LIMIT
    # другой период — свой лимит
    assert isinstance(fx.amounts.begin_check(
        car_number="AA111BB", protocol_no="R1", trigger=TRIGGER_AUTO, period_index=2, requested_by=None,
        check_date=date(2026, 12, 1), started_at=_tbilisi(date(2026, 12, 1)), max_sessions_per_day=100,
    ), int)


def test_one_session_per_receipt_at_a_time_across_processes(fx):
    first = fx.amounts.begin_check(
        car_number="AA111BB", protocol_no="R1", trigger=TRIGGER_MANUAL, period_index=None, requested_by=_CLIENT,
        check_date=date(2026, 11, 1), started_at=_tbilisi(date(2026, 11, 1)), max_sessions_per_day=100,
    )
    second_db = PayableAmountRepository(fx.db_path)  # второй процесс
    try:
        second = second_db.begin_check(
            car_number="AA111BB", protocol_no="R1", trigger=TRIGGER_AUTO, period_index=1, requested_by=None,
            check_date=date(2026, 11, 1), started_at=_tbilisi(date(2026, 11, 1), 13, 6), max_sessions_per_day=100,
        )
    finally:
        second_db.close()
    assert isinstance(first, int) and second == REFUSAL_BUSY


def test_abandoned_session_still_counts_towards_limits(fx):
    check_id = fx.amounts.begin_check(
        car_number="AA111BB", protocol_no="R1", trigger=TRIGGER_AUTO, period_index=1, requested_by=None,
        check_date=date(2026, 11, 1), started_at=_tbilisi(date(2026, 11, 1)), max_sessions_per_day=100,
    )
    assert fx.amounts.get_check(check_id).outcome == OUTCOME_IN_PROGRESS
    fx.amounts.abandon_stale_checks(now=_tbilisi(date(2026, 11, 1), 13, 30))
    assert fx.amounts.get_check(check_id).outcome == OUTCOME_ABANDONED
    assert fx.amounts.auto_checks_in_period("AA111BB", "R1", 1) == 1
    assert fx.amounts.sessions_on(date(2026, 11, 1)) == 1


def test_late_response_does_not_overwrite_a_fresher_amount(fx):
    newer = _tbilisi(date(2026, 11, 2)).replace(tzinfo=None)
    older = _tbilisi(date(2026, 11, 1)).replace(tzinfo=None)
    fx.amounts.store_amount(car_number="AA111BB", protocol_no="R1", amount=150.0, currency="GEL", observed_at=newer,
                            source="manual", check_id=None)
    result = fx.amounts.store_amount(car_number="AA111BB", protocol_no="R1", amount=100.0, currency="GEL",
                                     observed_at=older, source="auto", check_id=None)
    assert not result.stored
    assert fx.amounts.get_amount("AA111BB", "R1").amount == 150.0


def test_concurrent_refreshes_of_one_receipt_open_one_session(fx):
    fx.receipt()
    fx.gc.delay = 0.05
    fx.at(date(2026, 11, 5))
    ref = ReceiptRef("AA111BB", "R1", 50.0)

    async def both():
        return await asyncio.gather(
            fx.service.refresh(ref, trigger=TRIGGER_MANUAL, requested_by=_CLIENT),
            fx.service.refresh(ref, trigger=TRIGGER_MANUAL, requested_by=_CLIENT),
        )

    results = asyncio.run(both())
    assert sorted(result.status for result in results) == [STATUS_BUSY, STATUS_FETCHED]
    assert len(fx.gc.calls) == 1


# ---- «🔄 Уточнить сумму» ----


def _subscription(fx, car="AA111BB"):
    return fx.subscriptions.list_by_car_number(car)[0]


def test_manual_refresh_saves_the_amount_and_respects_its_own_interval(fx):
    fx.receipt(base=50.0)
    fx.gc.amounts["R1"] = 50.0
    fx.at(date(2026, 10, 20), 10, 0)  # в первоначальный срок — вручную можно
    outcome = asyncio.run(fx.service.refresh_for_subscription(_subscription(fx), telegram_user_id=_CLIENT))
    assert [result.status for result in outcome.results] == [STATUS_FETCHED]
    fx.at(date(2026, 10, 20), 18, 0)  # через 8 часов — показывается сохранённая, без сессии
    outcome = asyncio.run(fx.service.refresh_for_subscription(_subscription(fx), telegram_user_id=_CLIENT))
    assert [result.status for result in outcome.results] == [STATUS_CACHED]
    assert outcome.results[0].amount.amount == 50.0
    assert len(fx.gc.calls) == 1


def test_manual_refresh_has_a_per_user_daily_limit(tmp_path):
    fixture = _Fixture(tmp_path, manual_max_sessions_per_user_per_day=2)
    for protocol in ("R1", "R2", "R3"):
        fixture.receipt(protocol=protocol)
    fixture.at(date(2026, 10, 20), 10, 0)
    outcome = asyncio.run(fixture.service.refresh_for_subscription(_subscription(fixture), telegram_user_id=_CLIENT))
    assert [result.status for result in outcome.results] == [STATUS_FETCHED, STATUS_FETCHED, STATUS_LIMITED]
    assert len(fixture.gc.calls) == 2
    fixture.close()


def test_manual_increase_inside_a_window_closes_the_automatic_period(fx):
    fx.receipt(base=50.0)
    fx.gc.amounts["R1"] = 50.0
    fx.run_days(date(2026, 11, 1), date(2026, 11, 2))
    fx.gc.amounts["R1"] = 150.0
    fx.at(date(2026, 11, 3), 10, 0)  # до автоматического окна дня
    outcome = asyncio.run(fx.service.refresh_for_subscription(_subscription(fx), telegram_user_id=_CLIENT))
    assert outcome.results[0].increased_from == 50.0 and outcome.results[0].event_id is not None
    fx.run_days(date(2026, 11, 3), date(2026, 11, 10))
    assert len(fx.auto_checks()) == 2


def test_manual_refresh_does_not_touch_paid_or_history_receipts(fx):
    fx.receipt(protocol="R1")
    paid = fx.receipt(protocol="R2")
    fx.status_repository.update_payment(
        paid.id, payment_status=PAYMENT_PAID, source="receipt", note="BOG 2", actor_user_id=_MANAGER,
        at=datetime(2026, 9, 2), event="paid",
    )
    fx.at(date(2026, 10, 20), 10, 0)
    outcome = asyncio.run(fx.service.refresh_for_subscription(_subscription(fx), telegram_user_id=_CLIENT))
    assert [result.ref.protocol_no for result in outcome.results] == ["R1"]
    assert fx.gc.calls == [("AA111BB", "R1")]


def test_manual_refresh_failure_keeps_the_previous_amount(fx):
    fx.receipt(base=50.0)
    fx.gc.amounts["R1"] = 150.0
    fx.at(date(2026, 10, 1), 10, 0)
    asyncio.run(fx.service.refresh_for_subscription(_subscription(fx), telegram_user_id=_CLIENT))
    fx.gc.outcomes = [OUTCOME_ERROR]
    fx.at(date(2026, 10, 2), 10, 0)
    outcome = asyncio.run(fx.service.refresh_for_subscription(_subscription(fx), telegram_user_id=_CLIENT))
    assert outcome.results[0].status == STATUS_FAILED
    assert outcome.results[0].amount.amount == 150.0


def test_not_configured_manual_refresh_reports_it_without_a_session(fx):
    fx.receipt()
    fx.gc.configured = False
    fx.at(date(2026, 10, 20), 10, 0)
    outcome = asyncio.run(fx.service.refresh_for_subscription(_subscription(fx), telegram_user_id=_CLIENT))
    assert [result.status for result in outcome.results] == [STATUS_NOT_CONFIGURED]
    assert fx.gc.calls == []


# ---- После независимого review ----


def test_per_user_manual_limit_holds_in_the_insert(fx):
    kwargs = dict(trigger=TRIGGER_MANUAL, period_index=None, requested_by=_CLIENT, check_date=date(2026, 11, 1),
                  max_sessions_per_day=100, max_manual_per_user=1)
    assert isinstance(fx.amounts.begin_check(
        car_number="AA111BB", protocol_no="R1", started_at=_tbilisi(date(2026, 11, 1)), **kwargs), int)
    from reader.fines.payable_amount import REFUSAL_USER_LIMIT

    assert fx.amounts.begin_check(
        car_number="CC222DD", protocol_no="R9", started_at=_tbilisi(date(2026, 11, 1), 13, 6), **kwargs,
    ) == REFUSAL_USER_LIMIT


def test_tbilisi_date_decides_the_window_around_midnight(fx):
    """02:00 по Тбилиси 01.11 — это ещё 31.10 по UTC: проверка относится к
    01.11 (первый день периода 1), и найденное увеличение закрывает период."""
    fx.receipt(base=50.0)
    fx.amounts.store_amount(car_number="AA111BB", protocol_no="R1", amount=50.0, currency="GEL",
                            observed_at=datetime(2026, 10, 20, 8, 0), source="manual", check_id=None)
    fx.gc.amounts["R1"] = 150.0
    fx.at(date(2026, 11, 1), 2, 0)
    assert fx.clock.now.date() == date(2026, 10, 31)
    outcome = asyncio.run(fx.service.refresh_for_subscription(_subscription(fx), telegram_user_id=_CLIENT))
    assert outcome.results[0].increased_from == 50.0
    assert fx.amounts.checks_for_receipt("AA111BB", "R1")[-1].check_date == date(2026, 11, 1)
    fx.run_days(date(2026, 11, 1), date(2026, 11, 10))
    assert fx.auto_checks() == []


def test_real_outcome_replaces_an_abandoned_mark(fx):
    check_id = fx.amounts.begin_check(
        car_number="AA111BB", protocol_no="R1", trigger=TRIGGER_AUTO, period_index=1, requested_by=None,
        check_date=date(2026, 11, 1), started_at=_tbilisi(date(2026, 11, 1)), max_sessions_per_day=100,
    )
    fx.amounts.abandon_stale_checks(now=_tbilisi(date(2026, 11, 1), 13, 30))
    fx.amounts.finish_check(check_id, outcome=OUTCOME_OK, finished_at=_tbilisi(date(2026, 11, 1), 13, 31),
                            amount=150.0, increased=True)
    check = fx.amounts.get_check(check_id)
    assert check.outcome == OUTCOME_OK and check.increased
