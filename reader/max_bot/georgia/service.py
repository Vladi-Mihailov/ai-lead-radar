"""Бизнес-операции MAX-бота Грузии поверх ОБЩИХ компонентов мониторинга:

- задача мониторинга — FineMonitoringTaskRepository (одна активная задача на
  номер для всех мессенджеров; новая создаётся как client_bot, её проверяет
  тот же плановый FineJob ai-lead-radar, что и Telegram-машины);
- проверка — тот же FineCheckService.check_task_or_read_cached (police.ge
  локально/через REDNET, circuit breaker, дедуп detected_fines);
- статусы оплаты — FineStatusTracker, суммы — PayableAmountService
  (Georgian Card: те же атомарные лимиты в БД, что и у Telegram-бота);
- тексты — общие reader/public_bot/texts.py и payable_amount_texts.py.

Отдельно — только MAX-подписка (reader/max_bot/georgia/repository.py).

Задача, созданная из MAX, получает telegram_chat_id = created_by_user_id =
MAX_TASK_OWNER (0): эти поля Telegram-код использует как адрес клиента
(сообщение «штрафов нет» после ручного bootstrap), и настоящий MAX id там
мог бы совпасть с чужим Telegram id. Отправка на 0 просто не удаётся.

Ручные уточнения суммы из MAX учитываются в лимите на пользователя под
requested_by = -MAX user_id (requester_id): отрицательные значения не
пересекаются с Telegram id.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from reader.fines.check_service import FineCheckService
from reader.fines.models import UNLIMITED_END_DATE, FineMonitoringTask
from reader.fines.payable_amount import amount_is_current, display_payable
from reader.fines.payment_status import (
    DISPLAY_GONE,
    DISPLAY_UNVERIFIED,
    PAYMENT_PAID,
    PAYMENT_UNPAID,
    FineStatus,
    FineStatusTracker,
)
from reader.fines.plate_bootstrap_repository import GeorgiaPlateBootstrapRepository
from reader.fines.suppression import is_fine_output_suppressed
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.max_bot.georgia.repository import MaxGeorgiaRepository, MaxGeSubscription
from reader.public_bot.conversation import _subscriber_display_status
from reader.public_bot.payable_amount_service import ManualRefreshOutcome, PayableAmountService
from reader.public_bot.payable_amount_texts import ReceiptAmount
from reader.public_bot.subscription_service import CHECK_ALREADY_IN_PROGRESS, CheckNowOutcome

MAX_TASK_OWNER = 0

EXCLUSION_SHOWN_IN_REFRESH = "shown_in_refresh_reply"
# К оплате не предлагаются 🟢, ⚪ и ⏳ (как у Telegram-бота).
_NOT_PAYABLE = (PAYMENT_PAID, DISPLAY_GONE, DISPLAY_UNVERIFIED)

__all__ = ["CHECK_ALREADY_IN_PROGRESS", "MaxAddCarOutcome", "MaxGeorgiaService", "requester_id"]


def requester_id(max_user_id: int) -> int:
    return -max_user_id


@dataclass(frozen=True)
class MaxAddCarOutcome:
    subscription: MaxGeSubscription
    check_ok: bool
    new_fines_count: int
    provider_unavailable: bool
    already_added: bool


@dataclass(frozen=True)
class _CarRef:
    """Всё, что PayableAmountService.refresh_for_subscription берёт из подписки."""

    car_number: str


class MaxGeorgiaService:
    def __init__(
        self,
        repository: MaxGeorgiaRepository,
        task_repository: FineMonitoringTaskRepository,
        check_service: FineCheckService,
        *,
        tz: ZoneInfo,
        status_tracker: FineStatusTracker | None = None,
        payable_amount: PayableAmountService | None = None,
        plate_bootstrap: GeorgiaPlateBootstrapRepository | None = None,
    ):
        self.repository = repository
        self._tasks = task_repository
        self._check_service = check_service
        self._tz = tz
        self._statuses = status_tracker
        self._payable = payable_amount
        self._bootstrap = plate_bootstrap
        self._checks_in_progress: set[int] = set()
        self._refresh_in_progress: set[int] = set()

    @property
    def payable_enabled(self) -> bool:
        return self._payable is not None and self._statuses is not None

    def today(self) -> date:
        return datetime.now(timezone.utc).astimezone(self._tz).date()

    def recover_pending_baselines(self) -> int:
        """При запуске процесса: ни одно добавление сейчас не идёт, поэтому
        любая отложенная точка отсчёта осталась от прерванного добавления."""
        now = datetime.now(timezone.utc)
        return self.repository.resolve_stale_pending(older_than=now + timedelta(seconds=1), at=now)

    # ---- автомобили ----

    async def add_car(self, *, max_user_id: int, car_number: str) -> MaxAddCarOutcome:
        """Подключает машину к общей задаче мониторинга (создаёт её, если
        номер ещё никто не проверяет) и сразу проверяет. Повторное добавление
        той же машины возвращает существующую подписку (выключенную —
        включает), второй подписки не создаётся."""
        existing = self.repository.get_for_user_and_car(max_user_id, car_number)
        if existing is not None:
            if not existing.is_active:
                existing = self.turn_on(existing.id, max_user_id=max_user_id) or existing
            task = self._tasks.get(existing.monitoring_task_id)
            result = await self._check_service.check_task_or_read_cached(task) if task else None
            return MaxAddCarOutcome(
                subscription=existing, already_added=True,
                check_ok=bool(result and result.shows_as_ok),
                new_fines_count=len(result.new_fines) if result and result.shows_as_ok else 0,
                provider_unavailable=bool(result and result.provider_unavailable),
            )

        if self._bootstrap is not None:
            self._bootstrap.ensure_pending(car_number)
        task = self._active_task_for(car_number)
        subscription = self.repository.create_pending(
            max_user_id=max_user_id, car_number=car_number, monitoring_task_id=task.id,
        )
        try:
            result = await self._check_service.check_task_or_read_cached(task)
            if (
                self._bootstrap is not None and result.status == "ok" and not result.from_cache
            ):
                self._bootstrap.mark_completed(car_number, completed_at=datetime.now(timezone.utc))
        finally:
            # Всё, что система знает о номере к концу первичной проверки, —
            # история: клиент видит это в ответе, новыми эти штрафы не придут.
            self._set_baseline(subscription)
        return MaxAddCarOutcome(
            subscription=self.repository.get(subscription.id), already_added=False,
            check_ok=result.shows_as_ok,
            new_fines_count=len(result.new_fines) if result.shows_as_ok else 0,
            provider_unavailable=result.provider_unavailable,
        )

    def import_car(self, *, max_user_id: int, car_number: str) -> MaxGeSubscription:
        """Перенос машины из привязанного Telegram-аккаунта: без живой
        проверки (номер уже проверяется), точка отсчёта — сейчас."""
        existing = self.repository.get_for_user_and_car(max_user_id, car_number)
        if existing is not None:
            return existing
        task = self._active_task_for(car_number)
        subscription = self.repository.create_pending(
            max_user_id=max_user_id, car_number=car_number, monitoring_task_id=task.id,
        )
        self._set_baseline(subscription)
        return self.repository.get(subscription.id)

    def _set_baseline(self, subscription: MaxGeSubscription) -> None:
        state = self._bootstrap.get(subscription.car_number) if self._bootstrap is not None else None
        self.repository.set_baseline(
            subscription.id, at=datetime.now(timezone.utc), history_final=state is None or state.is_completed,
        )

    def _active_task_for(self, car_number: str) -> FineMonitoringTask:
        existing = self._tasks.get_active_by_car_number(car_number)
        if existing:
            return self._tasks.extend_period_if_shorter(existing[0].id, UNLIMITED_END_DATE)
        today = self.today()
        return self._tasks.create(
            car_number=car_number, label=None, start_date=today, end_date=UNLIMITED_END_DATE,
            telegram_chat_id=MAX_TASK_OWNER, created_by_user_id=MAX_TASK_OWNER, monitoring_scope="client_bot",
        )

    def task_statuses(self, car_number: str) -> list[str]:
        return [task.status for task in self._tasks.list_by_car_number(car_number)]

    def list_cars(self, max_user_id: int) -> list[MaxGeSubscription]:
        return self.repository.list_by_user(max_user_id)

    def get_owned(self, subscription_id: int, *, max_user_id: int) -> MaxGeSubscription | None:
        return self.repository.get_owned(subscription_id, max_user_id=max_user_id)

    def turn_off(self, subscription_id: int, *, max_user_id: int) -> MaxGeSubscription | None:
        """Только уведомления этой MAX-подписки: задача мониторинга и
        подписки других клиентов (Telegram и MAX) не меняются."""
        subscription = self.get_owned(subscription_id, max_user_id=max_user_id)
        if subscription is None:
            return None
        if subscription.is_active:
            self.repository.set_status(subscription.id, "stopped")
        return self.repository.get(subscription.id)

    def turn_on(self, subscription_id: int, *, max_user_id: int) -> MaxGeSubscription | None:
        """Включает уведомления и, если задача номера была остановлена,
        возвращает её в мониторинг — те же правила, что у Telegram-бота
        (SubscriptionService.turn_on_car): если номер уже проверяет другая
        активная задача, подписка переезжает на неё."""
        subscription = self.get_owned(subscription_id, max_user_id=max_user_id)
        if subscription is None:
            return None
        task = self._tasks.get(subscription.monitoring_task_id)
        if task is not None and task.status == "active":
            task_id = task.id
        else:
            active = self._tasks.get_active_by_car_number(subscription.car_number)
            task_id = active[0].id if active else subscription.monitoring_task_id
        if task_id != subscription.monitoring_task_id:
            self.repository.move_to_task(subscription.id, task_id)
        target = self._tasks.get(task_id)
        if target is not None:
            if target.status != "active":
                self._tasks.return_to_active_monitoring(task_id, start_date=self.today(), end_date=UNLIMITED_END_DATE)
            else:
                self._tasks.extend_period_if_shorter(task_id, UNLIMITED_END_DATE)
        if not subscription.is_active:
            self.repository.set_status(subscription.id, "active")
        return self.repository.get(subscription.id)

    def delete(self, subscription_id: int, *, max_user_id: int) -> bool:
        """Мягкое удаление MAX-подписки. Задачу мониторинга не трогает:
        другие клиенты номера продолжают получать уведомления."""
        subscription = self.get_owned(subscription_id, max_user_id=max_user_id)
        if subscription is None:
            return False
        self.repository.set_status(subscription.id, "archived")
        return True

    # ---- проверка ----

    async def check_now(
        self, subscription_id: int, *, max_user_id: int,
        on_check_started: Callable[[], Awaitable[None]] | None = None,
    ):
        """CheckNowOutcome | CHECK_ALREADY_IN_PROGRESS | None (не своя/удалена)."""
        subscription = self.get_owned(subscription_id, max_user_id=max_user_id)
        if subscription is None:
            return None
        task = self._tasks.get(subscription.monitoring_task_id)
        if task is None:
            return None
        if task.id in self._checks_in_progress:
            return CHECK_ALREADY_IN_PROGRESS
        self._checks_in_progress.add(task.id)
        try:
            if on_check_started is not None:
                await on_check_started()
            result = await self._check_service.check_task_or_read_cached(task)
        finally:
            self._checks_in_progress.discard(task.id)
        return CheckNowOutcome(
            car_number=subscription.car_number, check_ok=result.shows_as_ok,
            fines=result.current_fines if result.shows_as_ok else [], is_owner=True,
            provider_unavailable=result.provider_unavailable, subscription_id=subscription.id,
        )

    # ---- статусы и суммы (только чтение БД, как у Telegram-бота) ----

    def subscriber_statuses(self, car_number: str) -> dict[str, str] | None:
        if self._statuses is None:
            return None
        return {
            protocol_no: _subscriber_display_status(status)
            for protocol_no, status in self._statuses.statuses_by_protocol(car_number).items()
        }

    def unverified_totals(self, car_number: str) -> dict:
        if self._statuses is None or is_fine_output_suppressed(car_number):
            return {}
        unconfirmed = [
            status for status in self._statuses.statuses_for_car(car_number, include_gone=False)
            if _subscriber_display_status(status) == DISPLAY_UNVERIFIED
        ]
        if not unconfirmed:
            return {}
        return {
            "unverified_count": len(unconfirmed),
            "unverified_amount": sum(status.amount or 0.0 for status in unconfirmed),
        }

    def has_payable(self, fines, statuses: dict[str, str] | None) -> bool:
        return statuses is None or any(
            statuses.get(fine.external_fine_id or "", PAYMENT_UNPAID) not in _NOT_PAYABLE
            for fine in fines
        )

    def receipt_amounts(self, car_number: str, statuses: list[FineStatus]) -> dict[str, ReceiptAmount]:
        """{protocolNo: ReceiptAmount}: сохранённая сумма Georgian Card, если
        она актуальна для текущего контрольного периода, иначе «уточнить»."""
        if self._payable is None:
            return {}
        repository = self._payable.repository
        stored = repository.amounts_for_cars([car_number])
        term_ends = repository.term_ends_for_cars([car_number])
        today = self.today()
        result = {}
        for status in statuses:
            key = (car_number, status.protocol_no)
            amount = stored.get(key)
            current = amount_is_current(amount, term_ends.get(key), today=today, tz=self._tz)
            result[status.protocol_no] = ReceiptAmount(
                protocol_no=status.protocol_no, base_amount=status.amount,
                payable=display_payable(amount.amount, status.amount) if current else None,
                observed_at=amount.observed_at if current else None,
            )
        return result

    def check_now_amounts(self, car_number: str, fines, statuses: dict[str, str]) -> dict[str, ReceiptAmount]:
        payable_protocols = {
            fine.external_fine_id for fine in fines
            if fine.external_fine_id
            and statuses.get(fine.external_fine_id, PAYMENT_UNPAID) not in _NOT_PAYABLE
        }
        known = [
            status for status in self._statuses.statuses_for_car(car_number, include_gone=False)
            if status.protocol_no in payable_protocols
        ]
        amounts = self.receipt_amounts(car_number, known)
        result = {}
        for fine in fines:
            protocol_no = fine.external_fine_id
            if not protocol_no or protocol_no not in payable_protocols:
                continue
            receipt = amounts.get(protocol_no)
            result[protocol_no] = ReceiptAmount(
                protocol_no=protocol_no, base_amount=fine.amount,
                payable=display_payable(receipt.payable, fine.amount) if receipt else None,
                observed_at=receipt.observed_at if receipt else None,
            )
        return result

    def debt_receipts(self, car_number: str) -> list[ReceiptAmount]:
        """🔴/🟡 квитанции номера с суммами — для карточки «💰 К оплате»."""
        if self._statuses is None or is_fine_output_suppressed(car_number):
            return []
        debt = [
            status for status in self._statuses.statuses_for_car(car_number, include_gone=False)
            if status.counts_as_listed and status.payment_status != PAYMENT_PAID
        ]
        amounts = self.receipt_amounts(car_number, debt)
        return [amounts[status.protocol_no] for status in debt if status.protocol_no in amounts]

    def paid_statuses(self, car_number: str) -> list[FineStatus]:
        if self._statuses is None or is_fine_output_suppressed(car_number):
            return []
        return [
            status for status in self._statuses.statuses_for_car(car_number, include_gone=True)
            if status.payment_status == PAYMENT_PAID
        ]

    async def refresh_amount(
        self, subscription_id: int, *, max_user_id: int,
        on_check_started: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[MaxGeSubscription, ManualRefreshOutcome] | object | None:
        """«🔄 Уточнить сумму»: сессии Georgian Card только для 🔴/🟡 квитанций
        номера, без карты и оплаты. Увеличение, показанное в ответе, этой же
        подписке поллером повторно не отправляется."""
        if not self.payable_enabled:
            return None
        subscription = self.get_owned(subscription_id, max_user_id=max_user_id)
        if subscription is None:
            return None
        if subscription.id in self._refresh_in_progress:
            return CHECK_ALREADY_IN_PROGRESS

        def exclude_shown(event_id: int) -> None:
            self.repository.exclude_amount(event_id, subscription.id, reason=EXCLUSION_SHOWN_IN_REFRESH)

        self._refresh_in_progress.add(subscription.id)
        try:
            if on_check_started is not None:
                await on_check_started()
            outcome = await self._payable.refresh_for_subscription(
                _CarRef(subscription.car_number), telegram_user_id=requester_id(max_user_id), on_event=exclude_shown,
            )
        finally:
            self._refresh_in_progress.discard(subscription.id)
        return subscription, outcome
