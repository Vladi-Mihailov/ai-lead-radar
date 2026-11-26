"""Уведомления MAX-клиентам Грузии: новые штрафы и подтверждённые увеличения
суммы к оплате. Источник событий — ОБЩИЙ (detected_fines, события увеличения
georgia_fine_amount_events), доставка и её статусы — свои (max_ge_*), поэтому
сбой MAX не влияет на Telegram и наоборот, у каждого получателя свой статус.

Правила — те же, что у Telegram-доставки, плюс точка отсчёта подписки:
- пауза уведомлений Грузии — ничего не отправляется; граница рассылки —
  квитанции, увиденные раньше неё, исключаются;
- штраф доставляется, только если система впервые увидела его ПОСЛЕ точки
  отсчёта подписки (baseline_at) и после завершения первичной проверки номера
  (bootstrap): исторические штрафы при подключении MAX не рассылаются; пока
  первичная проверка номера не завершена, решение откладывается;
- одна доставка на квитанцию (каноническая — самая ранняя строка), без ⏳ и ⚪;
- увеличение суммы — те же условия события, что у Telegram (_event_exclusion:
  флаг уведомлений, граница, срок давности 3 дня, квитанция к оплате), плюс
  событие должно быть позже точки отсчёта и последнего изменения подписки;
- повтор неудачной отправки — тот же backoff (0/1м/5м/15м/1ч/3ч, 6 попыток);
  заблокировавший бота пользователь помечается blocked_at.

Операторы получают уведомления по-прежнему из ai-lead-radar — этот модуль их
не касается.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import DetectedFine
from reader.fines.notification_control import EXCLUSION_KNOWN_BEFORE_BOUNDARY
from reader.fines.suppression import is_fine_output_suppressed
from reader.max_bot.api import MaxApiError
from reader.max_bot.georgia import keyboards as ge_keyboards
from reader.max_bot.georgia.repository import MaxGeorgiaRepository, MaxGeSubscription
from reader.public_bot.amount_delivery_service import DEFAULT_MAX_EVENT_AGE, _event_exclusion
from reader.public_bot.delivery_service import DeliveryTickResult, _is_due_for_attempt
from reader.public_bot.delivery_texts import format_owner_amount_increase_message, format_owner_fine_message

logger = logging.getLogger(__name__)

EXCLUSION_HISTORY = "max_history_before_subscription"
EXCLUSION_BEFORE_SUBSCRIPTION = "before_subscription"
EXCLUSION_SUBSCRIPTION_CHANGED = "subscription_changed_after_event"
_EVENT_LOOKBACK = timedelta(days=30)
_STALE_PENDING_AFTER = timedelta(minutes=30)


class MaxSenderLike(Protocol):
    async def send_notification(self, max_user_id: int, text: str, keyboard) -> None: ...

    def mark_blocked(self, max_user_id: int) -> None: ...


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)


class MaxGeorgiaDelivery:
    def __init__(
        self,
        repository: MaxGeorgiaRepository,
        detected_fine_repository: DetectedFineRepository,
        sender: MaxSenderLike,
        *,
        tz: ZoneInfo,
        payment_help_url: str | None,
        status_tracker=None,
        notification_control=None,
        payable_repository=None,
        plate_bootstrap=None,
        max_event_age: timedelta = DEFAULT_MAX_EVENT_AGE,
    ):
        self._repository = repository
        self._fines = detected_fine_repository
        self._sender = sender
        self._tz = tz
        self._payment_help_url = payment_help_url
        self._statuses = status_tracker
        self._control = notification_control
        self._amounts = payable_repository
        self._bootstrap = plate_bootstrap
        self._max_event_age = max_event_age

    async def run_once(self, *, now: datetime | None = None) -> DeliveryTickResult:
        now = now or datetime.now(timezone.utc)
        control = self._control.state() if self._control is not None else None
        if control is not None and control.paused:
            return DeliveryTickResult(delivered=0, failed=0, flood_wait_hit=False)
        boundary_at = control.boundary_at if control is not None else None
        # Добавление, прерванное без падения процесса (зависшая проверка), не
        # должно оставить подписку без уведомлений навсегда.
        self._repository.resolve_stale_pending(older_than=_naive_utc(now) - _STALE_PENDING_AFTER, at=now)
        subscriptions = [
            subscription for subscription in self._repository.list_active()
            if not subscription.baseline_pending and not is_fine_output_suppressed(subscription.car_number)
        ]
        delivered, failed = await self._deliver_fines(subscriptions, boundary_at=boundary_at, now=now)
        if self._amounts is not None:
            more_delivered, more_failed = await self._deliver_increases(subscriptions, boundary_at=boundary_at, now=now)
            delivered += more_delivered
            failed += more_failed
        return DeliveryTickResult(delivered=delivered, failed=failed, flood_wait_hit=False)

    # ---- новые штрафы ----

    def _history_cutoff(self, subscription: MaxGeSubscription) -> datetime | None:
        """None — первичная проверка номера ещё не завершена: что из уже
        найденного история, станет ясно после неё. Граница фиксируется в
        подписке при первом определении (см. repository.py про
        history_cutoff_at) и дальше не пересчитывается."""
        if subscription.history_cutoff_at is not None:
            return subscription.history_cutoff_at
        cutoff = subscription.baseline_at
        if self._bootstrap is not None:
            state = self._bootstrap.get(subscription.car_number)
            if state is not None:
                if not state.is_completed:
                    return None
                completed_at = _naive_utc(state.bootstrap_completed_at)
                if completed_at is not None and completed_at > cutoff:
                    cutoff = completed_at
        self._repository.set_history_cutoff(subscription.id, at=cutoff)
        return cutoff

    def _deliverable_fines(self, car_number: str) -> list[DetectedFine]:
        """Каноническая строка на квитанцию, без ⏳ и ⚪ (как у Telegram)."""
        fines = sorted(self._fines.list_by_car_number(car_number), key=lambda fine: fine.id)
        undeliverable = (
            self._statuses.undeliverable_protocols(car_number) if self._statuses is not None and fines else set()
        )
        result, seen = [], set()
        for fine in fines:
            protocol_no = fine.external_fine_id
            if protocol_no:
                if protocol_no in seen or protocol_no in undeliverable:
                    continue
                seen.add(protocol_no)
            result.append(fine)
        return result

    async def _deliver_fines(
        self, subscriptions: list[MaxGeSubscription], *, boundary_at: datetime | None, now: datetime,
    ) -> tuple[int, int]:
        delivered = failed = 0
        for subscription in subscriptions:
            cutoff = self._history_cutoff(subscription)
            if cutoff is None:
                continue
            fines = self._deliverable_fines(subscription.car_number)
            handled = self._repository.handled_protocols(subscription.id) if fines else set()
            for fine in fines:
                protocol_no = fine.external_fine_id
                if protocol_no and protocol_no in handled:
                    continue
                existing = self._repository.get_fine_delivery(fine.id, subscription.id)
                if existing is not None and (existing.delivered_at is not None or existing.excluded_at is not None):
                    continue
                first_seen = _naive_utc(fine.first_detected_at)
                reason = None
                if first_seen is None or first_seen <= cutoff:
                    reason = EXCLUSION_HISTORY
                elif boundary_at is not None and self._control.known_before(
                    boundary_at, car_number=subscription.car_number, protocol_no=protocol_no,
                    row_first_detected_at=first_seen,
                ):
                    reason = EXCLUSION_KNOWN_BEFORE_BOUNDARY
                if reason is not None:
                    self._repository.exclude_fine(fine.id, subscription.id, protocol_no, reason=reason)
                    if protocol_no:
                        handled.add(protocol_no)
                    continue
                if not _is_due_for_attempt(existing, now=now):
                    continue
                self._repository.record_fine_attempt(fine.id, subscription.id, protocol_no)
                text = format_owner_fine_message(car_number=subscription.car_number, fine=fine)
                if await self._send(subscription, text, ge_keyboards.payment_cta(self._payment_help_url)):
                    self._repository.mark_fine_delivered(fine.id, subscription.id)
                    if protocol_no:
                        handled.add(protocol_no)
                    delivered += 1
                else:
                    failed += 1
        return delivered, failed

    # ---- увеличения суммы к оплате ----

    async def _deliver_increases(
        self, subscriptions: list[MaxGeSubscription], *, boundary_at: datetime | None, now: datetime,
    ) -> tuple[int, int]:
        enabled_since = self._amounts.increase_notifications_since()
        if enabled_since is None or not subscriptions:
            return 0, 0
        naive_now = _naive_utc(now)
        events = self._amounts.events_since(naive_now - _EVENT_LOOKBACK)
        by_car: dict[str, list[MaxGeSubscription]] = {}
        for subscription in subscriptions:
            by_car.setdefault(subscription.car_number, []).append(subscription)
        delivered = failed = 0
        for event in events:
            targets = by_car.get(event.car_number)
            if not targets:
                continue
            status = (
                self._statuses.statuses_by_protocol(event.car_number).get(event.protocol_no)
                if self._statuses is not None else None
            )
            exclusion = _event_exclusion(
                event, status, boundary_at=boundary_at, enabled_since=enabled_since,
                too_old_before=naive_now - self._max_event_age,
            )
            for subscription in targets:
                existing = self._repository.get_amount_delivery(event.id, subscription.id)
                if not _is_due_for_attempt(existing, now=now):
                    continue
                reason = exclusion
                if reason is None and event.created_at <= subscription.baseline_at:
                    reason = EXCLUSION_BEFORE_SUBSCRIPTION
                if reason is None and subscription.updated_at >= event.created_at:
                    reason = EXCLUSION_SUBSCRIPTION_CHANGED
                if reason is not None:
                    self._repository.exclude_amount(event.id, subscription.id, reason=reason)
                    continue
                self._repository.record_amount_attempt(event.id, subscription.id)
                text = format_owner_amount_increase_message(
                    car_number=event.car_number, protocol_no=event.protocol_no, base_amount=status.amount,
                    old_amount=event.old_amount, new_amount=event.new_amount, observed_at=event.observed_at,
                    tz=self._tz,
                )
                keyboard = ge_keyboards.amount_card(subscription.id, self._payment_help_url)
                if await self._send(subscription, text, keyboard):
                    self._repository.mark_amount_delivered(event.id, subscription.id)
                    delivered += 1
                else:
                    failed += 1
        return delivered, failed

    async def _send(self, subscription: MaxGeSubscription, text: str, keyboard) -> bool:
        try:
            await self._sender.send_notification(subscription.max_user_id, text, keyboard)
        except MaxApiError as exc:
            if exc.recipient_unavailable:
                self._sender.mark_blocked(subscription.max_user_id)
            logger.warning("MAX GE: уведомление подписке id=%s не доставлено: %s", subscription.id, exc.code)
            return False
        except Exception:
            logger.exception("MAX GE: уведомление подписке id=%s не доставлено", subscription.id)
            return False
        return True
