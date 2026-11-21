"""PayableAmountService — сумма к оплате штрафов Грузии через платёжную
страницу Georgian Card (см. reader/fines/payable_amount.py — правила
периодов и лимиты, reader/fines/georgian_card_client.py — сама сессия,
docs/georgia-payable-amount.md).

Два входа:

- «🔄 Уточнить сумму» клиента (refresh_for_subscription): только получает и
  сохраняет сумму, без карты и оплаты. Своя частота: квитанцию, которую
  уточняли (или пытались) позже manual_min_interval, заново не открывают —
  показывается сохранённая сумма; не больше manual_max_sessions_per_user_per_day
  сессий на пользователя в сутки.
- Дневной автоматический проход (run_auto_once) — только квитанции в окне
  контрольного периода, строго по одной, с паузой, с остановкой на дневном
  лимите, на HTTP 403/429 (сессии приостанавливаются на blocked_suspension)
  и после max_consecutive_errors ошибок подряд. Включается отдельным флагом.

Квитанции: только 🔴/🟡 в базе полиции (listed/missing) — без 🟢, ⏳ и ⚪ —
номера без скрытия вывода. Автоматически — ещё и только номера с
действующей клиентской подпиской (не менеджерской).

Ни уведомлений, ни Telegram здесь нет: подтверждённое увеличение
записывается событием (georgia_fine_amount_events), рассылает его
AmountIncreaseDeliveryService под общей паузой/границей уведомлений.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from reader.fines.georgian_card_client import GeorgianCardClientLike
from reader.fines.payable_amount import (
    OUTCOME_BLOCKED,
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_REFUSED,
    REFUSAL_BUSY,
    REFUSAL_AUTO_DAILY_LIMIT,
    REFUSAL_DAILY_LIMIT,
    TRIGGER_AUTO,
    TRIGGER_INITIAL,
    TRIGGER_MANUAL,
    ControlPeriod,
    PayableAmount,
    PayableAmountRepository,
    display_payable,
    is_increase,
    open_period,
)
from reader.fines.payment_status import PAYMENT_PAID, FineStatusTracker
from reader.fines.suppression import is_fine_output_suppressed
from reader.public_bot.models import FineMonitoringSubscription

logger = logging.getLogger(__name__)

# RefreshResult.status
STATUS_FETCHED = "fetched"  # сессия прошла, сумма получена и сохранена
STATUS_STALE = "stale"  # сумма получена, но более свежая уже была сохранена
STATUS_FAILED = "failed"  # сессия прошла, суммы нет (отказ МВД, ошибка, тайм-аут)
STATUS_CACHED = "cached"  # сессия не открывалась: уточняли недавно
STATUS_BUSY = "busy"  # эту квитанцию уже уточняют
STATUS_LIMITED = "limited"  # исчерпан лимит (дневной, пользователя, периода)
STATUS_SUSPENDED = "suspended"  # сессии приостановлены после 403/429
STATUS_NOT_CONFIGURED = "not_configured"  # merch_id/page_id не заданы

# Исходы, которые считаются ошибкой для остановки автоматического прохода
# (отказ МВД — обычный ответ, не ошибка).
_NON_ERROR_OUTCOMES = frozenset({OUTCOME_OK, OUTCOME_REFUSED})


@dataclass(frozen=True)
class PayableAmountLimits:
    max_sessions_per_day: int = 40
    # Необязательный отдельный предел автоматических проверок в сутки (внутри
    # общего); None — отдельного предела нет (решение владельца 2026-11-21:
    # «10» — это 10 дней окна у квитанции, а не 10 проверок в сутки).
    max_auto_sessions_per_day: int | None = None
    max_consecutive_errors: int = 2
    manual_min_interval: timedelta = timedelta(hours=12)
    manual_max_sessions_per_user_per_day: int = 5
    blocked_suspension: timedelta = timedelta(hours=24)
    auto_delay_seconds: float = 30.0


@dataclass(frozen=True)
class ReceiptRef:
    car_number: str
    protocol_no: str
    base_amount: float | None


@dataclass(frozen=True)
class RefreshResult:
    ref: ReceiptRef
    status: str
    outcome: str | None = None
    # Сохранённая сумма после вызова (в том числе прежняя, если новой нет).
    amount: PayableAmount | None = None
    # Подтверждённое увеличение относительно прежней суммы Georgian Card.
    increased_from: float | None = None
    event_id: int | None = None
    refusal: str | None = None


@dataclass(frozen=True)
class InitialActualization:
    """Итог разовой первичной актуализации (initial_actualization)."""

    eligible: int = 0
    sessions: int = 0
    fetched: int = 0
    failed: int = 0
    skipped: int = 0
    stop_reason: str | None = None
    outcomes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ManualRefreshOutcome:
    car_number: str
    results: list[RefreshResult] = field(default_factory=list)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class PayableAmountService:
    def __init__(
        self,
        repository: PayableAmountRepository,
        client: GeorgianCardClientLike,
        status_tracker: FineStatusTracker,
        subscription_repository,
        *,
        tz: ZoneInfo,
        limits: PayableAmountLimits = PayableAmountLimits(),
        trusted_user_ids: frozenset[int] = frozenset(),
        auto_enabled: bool = False,
        auto_start_time: time = time(13, 0),
        auto_cutoff_time: time = time(14, 0),
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._repository = repository
        self._client = client
        self._statuses = status_tracker
        self._subscriptions = subscription_repository
        self._tz = tz
        self._limits = limits
        self._trusted_user_ids = frozenset(trusted_user_ids)
        self._auto_enabled = auto_enabled
        self._auto_start_time = auto_start_time
        self._auto_cutoff_time = auto_cutoff_time
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        # Одна сессия квитанции за раз и внутри процесса (между процессами —
        # строка in_progress, см. PayableAmountRepository.begin_check).
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    @property
    def repository(self) -> PayableAmountRepository:
        return self._repository

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    @property
    def auto_enabled(self) -> bool:
        return self._auto_enabled

    def _now(self) -> datetime:
        now = self._clock()
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)

    def _today(self, now: datetime) -> date:
        return now.astimezone(self._tz).date()

    # ---- Квитанции ----

    def debt_receipts(self, car_number: str) -> list[ReceiptRef]:
        """🔴 и 🟡 в базе полиции (listed/missing): без 🟢, ⏳ ('unverified',
        с любой отметкой) и ⚪. Номер со скрытым выводом — пусто."""
        if is_fine_output_suppressed(car_number):
            return []
        return [
            ReceiptRef(car_number=status.car_number, protocol_no=status.protocol_no, base_amount=status.amount)
            for status in self._statuses.statuses_for_car(car_number, include_gone=False)
            if status.counts_as_listed and status.payment_status != PAYMENT_PAID
        ]

    def client_linked_car_numbers(self, car_numbers: list[str], *, today: date) -> set[str]:
        """Номера с действующей клиентской подпиской: active, срок не истёк,
        клиент известен и это не менеджер (его собственные подписки клиентской
        связью не считаются)."""
        if not car_numbers:
            return set()
        linked = set()
        for subscription in self._subscriptions.list_by_car_numbers(car_numbers):
            if (
                subscription.is_effectively_active(today=today)
                and subscription.telegram_user_id is not None
                and subscription.telegram_user_id not in self._trusted_user_ids
            ):
                linked.add(subscription.car_number)
        return linked

    # ---- Одна сессия ----

    def suspended(self, *, now: datetime | None = None) -> bool:
        now = now or self._now()
        state = self._repository.suspended_until()
        return state is not None and state[0] > _naive_utc(now)

    async def refresh(
        self, ref: ReceiptRef, *, trigger: str, period_index: int | None = None,
        requested_by: int | None = None, notify: bool = True, on_event: Callable[[int], None] | None = None,
    ) -> RefreshResult:
        """Одна сессия Georgian Card для одной квитанции — если лимиты
        позволяют. notify=False — увеличение не создаёт события (пилот).
        on_event(event_id) вызывается сразу после записи события, без
        переключения задач — до того, как поллер доставки может его увидеть."""
        key = (ref.car_number, ref.protocol_no)
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return RefreshResult(ref, STATUS_BUSY, amount=self._repository.get_amount(*key), refusal=REFUSAL_BUSY)
        async with lock:
            return await self._refresh_locked(
                ref, trigger=trigger, period_index=period_index, requested_by=requested_by, notify=notify,
                on_event=on_event,
            )

    async def _refresh_locked(
        self, ref: ReceiptRef, *, trigger: str, period_index: int | None, requested_by: int | None, notify: bool,
        on_event: Callable[[int], None] | None,
    ) -> RefreshResult:
        key = (ref.car_number, ref.protocol_no)
        if not self._client.configured:
            return RefreshResult(ref, STATUS_NOT_CONFIGURED, amount=self._repository.get_amount(*key))
        started_at = self._now()
        if self.suspended(now=started_at):
            return RefreshResult(ref, STATUS_SUSPENDED, amount=self._repository.get_amount(*key))
        begun = self._repository.begin_check(
            car_number=ref.car_number, protocol_no=ref.protocol_no, trigger=trigger, period_index=period_index,
            requested_by=requested_by, check_date=self._today(started_at), started_at=started_at,
            max_sessions_per_day=self._limits.max_sessions_per_day,
            max_manual_per_user=(
                self._limits.manual_max_sessions_per_user_per_day if trigger == TRIGGER_MANUAL else None
            ),
            max_auto_per_day=self._limits.max_auto_sessions_per_day if trigger == TRIGGER_AUTO else None,
        )
        if isinstance(begun, str):
            status = STATUS_BUSY if begun == REFUSAL_BUSY else STATUS_LIMITED
            return RefreshResult(ref, status, amount=self._repository.get_amount(*key), refusal=begun)
        check_id = begun

        try:
            quote = await self._client.fetch_amount(
                car_number=ref.car_number, protocol_no=ref.protocol_no, base_amount=ref.base_amount,
            )
        except Exception:
            logger.exception("Georgia payable amount: check id=%d failed unexpectedly", check_id)
            self._repository.finish_check(check_id, outcome=OUTCOME_ERROR, finished_at=self._now(), detail="exception")
            return RefreshResult(ref, STATUS_FAILED, outcome=OUTCOME_ERROR, amount=self._repository.get_amount(*key))
        finished_at = self._now()

        if quote.outcome != OUTCOME_OK or quote.amount is None:
            if quote.outcome == OUTCOME_BLOCKED:
                self._repository.suspend(until=finished_at + self._limits.blocked_suspension, reason=quote.detail or "")
                logger.warning(
                    "Georgia payable amount: Georgian Card refused requests (%s) — sessions suspended for %s",
                    quote.detail, self._limits.blocked_suspension,
                )
            self._repository.finish_check(check_id, outcome=quote.outcome, finished_at=finished_at, detail=quote.detail)
            logger.info("Georgia payable amount: check id=%d trigger=%s outcome=%s", check_id, trigger, quote.outcome)
            return RefreshResult(ref, STATUS_FAILED, outcome=quote.outcome, amount=self._repository.get_amount(*key))

        stored = self._repository.store_amount(
            car_number=ref.car_number, protocol_no=ref.protocol_no, amount=quote.amount,
            currency=quote.currency or "GEL", observed_at=started_at, source=trigger, check_id=check_id,
        )
        if not stored.stored:
            # Запоздавший ответ: пока шла эта сессия, сохранили более свежую сумму.
            self._repository.finish_check(
                check_id, outcome=OUTCOME_OK, finished_at=finished_at, amount=quote.amount,
                previous_amount=stored.previous_amount, detail="stale_response",
            )
            return RefreshResult(ref, STATUS_STALE, outcome=OUTCOME_OK, amount=self._repository.get_amount(*key))

        increased_from = None
        event_id = None
        if stored.first:
            # Первое значение — молча; увеличение относительно исходной суммы
            # только закрывает текущий контрольный период.
            increased = ref.base_amount is not None and is_increase(quote.amount, ref.base_amount)
        else:
            # Сравниваются показанные суммы (не меньше исходной): ответ ниже
            # исходной клиент видел как исходную, и её «рост» до исходной —
            # не увеличение.
            shown_before = display_payable(stored.previous_amount, ref.base_amount)
            shown_now = display_payable(quote.amount, ref.base_amount)
            increased = is_increase(shown_now, shown_before)
            if increased:
                increased_from = shown_before
                if notify:
                    event_id = self._repository.add_increase_event(
                        car_number=ref.car_number, protocol_no=ref.protocol_no, old_amount=shown_before,
                        new_amount=shown_now, observed_at=started_at, check_id=check_id,
                    )
                    if event_id is not None and on_event is not None:
                        try:
                            on_event(event_id)
                        except Exception:
                            logger.exception("Georgia payable amount: on_event failed for event=%s", event_id)
        self._repository.finish_check(
            check_id, outcome=OUTCOME_OK, finished_at=finished_at, amount=quote.amount,
            previous_amount=stored.previous_amount, increased=increased,
        )
        logger.info(
            "Georgia payable amount: check id=%d trigger=%s outcome=ok first=%s increased=%s event=%s",
            check_id, trigger, stored.first, increased, event_id,
        )
        return RefreshResult(
            ref, STATUS_FETCHED, outcome=OUTCOME_OK, amount=self._repository.get_amount(*key),
            increased_from=increased_from, event_id=event_id,
        )

    # ---- «🔄 Уточнить сумму» ----

    async def refresh_for_subscription(
        self, subscription: FineMonitoringSubscription, *, telegram_user_id: int,
        on_event: Callable[[int], None] | None = None,
    ) -> ManualRefreshOutcome:
        """Все 🔴/🟡 квитанции номера подписки (авторизацию проверяет
        вызывающий код). Квитанция, которую уточняли не раньше
        manual_min_interval назад, заново не открывается; после 403/429
        оставшиеся не открываются."""
        now = self._now()
        today = self._today(now)
        results: list[RefreshResult] = []
        stop_status: str | None = None
        for ref in self.debt_receipts(subscription.car_number):
            key = (ref.car_number, ref.protocol_no)
            if stop_status is not None:
                results.append(RefreshResult(ref, stop_status, amount=self._repository.get_amount(*key)))
                continue
            last_started = self._repository.last_check_started_at(*key)
            if last_started is not None and _naive_utc(now) - last_started < self._limits.manual_min_interval:
                results.append(RefreshResult(ref, STATUS_CACHED, amount=self._repository.get_amount(*key)))
                continue
            if (
                self._repository.manual_sessions_by_user_on(telegram_user_id, today)
                >= self._limits.manual_max_sessions_per_user_per_day
            ):
                results.append(RefreshResult(ref, STATUS_LIMITED, amount=self._repository.get_amount(*key)))
                continue
            result = await self.refresh(
                ref, trigger=TRIGGER_MANUAL, requested_by=telegram_user_id, on_event=on_event,
            )
            results.append(result)
            if result.status in (STATUS_SUSPENDED, STATUS_NOT_CONFIGURED) or result.outcome == OUTCOME_BLOCKED:
                stop_status = STATUS_NOT_CONFIGURED if result.status == STATUS_NOT_CONFIGURED else STATUS_SUSPENDED
            elif result.refusal == REFUSAL_DAILY_LIMIT:
                stop_status = STATUS_LIMITED
        return ManualRefreshOutcome(car_number=subscription.car_number, results=results)

    # ---- Разовая первичная актуализация ----

    def initial_actualization_receipts(self) -> list[ReceiptRef]:
        """Все 🔴/🟡 квитанции в базе полиции (без 🟢, ⏳, ⚪ и скрытых номеров),
        по которым ещё нет сохранённой суммы Georgian Card и не было попытки
        первичной актуализации (повторных сессий нет)."""
        refs = []
        for car_number in self._statuses.listed_car_numbers():
            for ref in self.debt_receipts(car_number):
                if self._repository.get_amount(ref.car_number, ref.protocol_no) is not None:
                    continue
                if self._repository.had_check(ref.car_number, ref.protocol_no, trigger=TRIGGER_INITIAL):
                    continue
                refs.append(ref)
        return refs

    async def initial_actualization(self, *, limit: int, delay_seconds: float) -> InitialActualization:
        """Одна сессия на квитанцию, строго по очереди, с паузой. Полученные
        суммы — первичные снимки: событий и уведомлений нет. Останавливается
        на 403/429/WAF (сессии приостанавливаются на сутки), после
        max_consecutive_errors ошибок подряд и на limit сессий. Отказ МВД —
        обычный ответ, не ошибка; оплатой не считается."""
        refs = self.initial_actualization_receipts()
        sessions = fetched = failed = skipped = 0
        consecutive_errors = 0
        outcomes: dict[str, int] = {}
        stop_reason = None
        for index, ref in enumerate(refs):
            if sessions >= limit:
                stop_reason = "limit"
                break
            if index and sessions:
                await self._sleep(delay_seconds)
            # Пока шла пауза, сумму могли получить (клиент нажал «Уточнить»)
            # или квитанцию — взять другой запуск: тогда сессии нет.
            if (
                self._repository.get_amount(ref.car_number, ref.protocol_no) is not None
                or self._repository.had_check(ref.car_number, ref.protocol_no, trigger=TRIGGER_INITIAL)
            ):
                skipped += 1
                continue
            result = await self.refresh(ref, trigger=TRIGGER_INITIAL, notify=False)
            if result.status in (STATUS_SUSPENDED, STATUS_NOT_CONFIGURED):
                stop_reason = result.status
                break
            if result.status in (STATUS_BUSY, STATUS_LIMITED):
                skipped += 1
                continue
            sessions += 1
            outcomes[result.outcome or "-"] = outcomes.get(result.outcome or "-", 0) + 1
            if result.status in (STATUS_FETCHED, STATUS_STALE):
                fetched += 1
            else:
                failed += 1
            if result.outcome == OUTCOME_BLOCKED:
                stop_reason = "blocked"
                break
            is_error = result.outcome not in _NON_ERROR_OUTCOMES
            consecutive_errors = consecutive_errors + 1 if is_error else 0
            if consecutive_errors >= self._limits.max_consecutive_errors:
                stop_reason = "errors"
                break
        logger.info(
            "Georgia payable amount: initial actualization — eligible %d, sessions %d, fetched %d, failed %d, stop %s",
            len(refs), sessions, fetched, failed, stop_reason,
        )
        return InitialActualization(
            eligible=len(refs), sessions=sessions, fetched=fetched, failed=failed, skipped=skipped,
            stop_reason=stop_reason, outcomes=outcomes,
        )

    # ---- Дневной автоматический проход ----

    def auto_candidates(self, today: date) -> list[tuple[ReceiptRef, ControlPeriod]]:
        """Квитанции, которым сегодня положена автоматическая проверка: окно
        контрольного периода открыто, период не закрыт (увеличение, отказ
        МВД, лимит проверок), сегодня по квитанции ещё ничего не открывали,
        у номера есть действующая клиентская подписка. Порядок — по очереди:
        сначала ни разу не проверявшиеся, затем дольше всех не проверявшиеся
        (при дневном лимите меньше числа квитанций одни и те же не
        выигрывают каждый день), при равенстве — более ранние периоды."""
        car_numbers = [car for car in self._statuses.listed_car_numbers() if not is_fine_output_suppressed(car)]
        linked = self.client_linked_car_numbers(car_numbers, today=today)
        cars = [car for car in car_numbers if car in linked]
        term_ends = self._repository.term_ends_for_cars(cars)
        candidates: list[tuple[ReceiptRef, ControlPeriod]] = []
        for car_number in cars:
            for ref in self.debt_receipts(car_number):
                term_end = term_ends.get((ref.car_number, ref.protocol_no))
                if term_end is None:
                    continue
                period = open_period(term_end, today)
                if period is None:
                    continue
                if self._repository.checked_on(ref.car_number, ref.protocol_no, today):
                    continue
                if self._repository.period_closed_reason(ref.car_number, ref.protocol_no, period, tz=self._tz):
                    continue
                candidates.append((ref, period))
        last_checked = {
            (ref.car_number, ref.protocol_no): self._repository.last_check_started_at(ref.car_number, ref.protocol_no)
            for ref, _ in candidates
        }
        candidates.sort(key=lambda item: (
            last_checked[(item[0].car_number, item[0].protocol_no)] or datetime.min,
            item[1].start, item[0].car_number, item[0].protocol_no,
        ))
        return candidates

    def _in_window(self, now: datetime) -> bool:
        local = now.astimezone(self._tz).time()
        return self._auto_start_time <= local < self._auto_cutoff_time

    async def run_auto_once(self) -> str | None:
        """Один тик фонового цикла. None — сегодня делать нечего (выключено,
        вне окна, проход уже завершён); иначе — итог прохода."""
        if not self._auto_enabled:
            return None
        now = self._now()
        if not self._in_window(now):
            return None
        today = self._today(now)
        run = self._repository.get_run(today)
        if run is not None and run.status != "running":
            return None
        if run is None:
            self._repository.start_run(today, at=now)
        if self.suspended(now=now):
            return self._finish(today, "stopped", "suspended")
        if not self._client.configured:
            return self._finish(today, "stopped", "not_configured")

        consecutive_errors = 0
        for ref, period in self.auto_candidates(today):
            now = self._now()
            if not self._in_window(now) or self._today(now) != today:
                return self._finish(today, "postponed", "cutoff")
            result = await self.refresh(ref, trigger=TRIGGER_AUTO, period_index=period.index)
            if result.status == STATUS_SUSPENDED or result.outcome == OUTCOME_BLOCKED:
                if result.outcome is not None:
                    self._repository.add_run_progress(today, sessions=1, errors=1)
                return self._finish(today, "stopped", "blocked")
            if result.status == STATUS_NOT_CONFIGURED:
                return self._finish(today, "stopped", "not_configured")
            if result.status == STATUS_LIMITED and result.refusal == REFUSAL_DAILY_LIMIT:
                return self._finish(today, "stopped", "daily_limit")
            if result.status == STATUS_LIMITED and result.refusal == REFUSAL_AUTO_DAILY_LIMIT:
                # Остальные квитанции — в следующие дни окна; очереди повторов нет.
                return self._finish(today, "stopped", "auto_daily_limit")
            if result.status in (STATUS_BUSY, STATUS_LIMITED):
                continue
            is_error = result.outcome not in _NON_ERROR_OUTCOMES
            self._repository.add_run_progress(today, sessions=1, errors=1 if is_error else 0)
            consecutive_errors = consecutive_errors + 1 if is_error else 0
            if consecutive_errors >= self._limits.max_consecutive_errors:
                return self._finish(today, "stopped", "errors")
            await self._sleep(self._limits.auto_delay_seconds)
        return self._finish(today, "completed", None)

    def _finish(self, today: date, status: str, reason: str | None) -> str:
        self._repository.finish_run(today, status=status, stop_reason=reason, at=self._now())
        run = self._repository.get_run(today)
        logger.info(
            "Georgia payable amount: auto run %s — %s%s, sessions %d, errors %d",
            today.isoformat(), status, f" ({reason})" if reason else "", run.sessions if run else 0,
            run.errors if run else 0,
        )
        return status


async def run_payable_amount_loop(service: PayableAmountService, *, poll_interval_seconds: float = 60.0) -> None:
    """Фоновый цикл процесса бота — ошибка одного тика не останавливает цикл."""
    while True:
        try:
            await service.run_auto_once()
        except Exception:
            logger.exception("Georgia payable amount: auto tick failed")
        await asyncio.sleep(poll_interval_seconds)
