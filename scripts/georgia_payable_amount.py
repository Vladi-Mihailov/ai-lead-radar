#!/usr/bin/env python
"""Сумма к оплате штрафов Грузии через Georgian Card (см. docs/georgia-payable-amount.md).

    python -m scripts.georgia_payable_amount status
    python -m scripts.georgia_payable_amount plan [--date 2026-12-01]
    python -m scripts.georgia_payable_amount fetch --car-number AB123CD --protocol-no 0123456789
    python -m scripts.georgia_payable_amount fetch --car-number AB123CD --protocol-no 0123456789 --execute
    python -m scripts.georgia_payable_amount fetch ... --merchant-id M --page-id P   # без правки config.yaml
    python -m scripts.georgia_payable_amount clear-suspension

status — сколько сумм сохранено, проверок сегодня (по триггерам и исходам),
событий увеличения и их доставки, приостановка после 403/429, последний
автоматический проход.

plan — сколько квитанций автоматический проход проверил бы в этот день (по
контрольным периодам) и сколько это сессий. Без сети.

fetch — пилот на ОДНОЙ квитанции. Без --execute ничего не отправляет:
показывает, разрешена ли квитанция (🔴/🟡 в базе полиции) и что будет
сделано. С --execute — ровно одна сессия Georgian Card (без карты, без
оплаты): сумма сохраняется молча (триггер pilot, без уведомлений), даже
если функция в боте выключена. Сессия учитывается в дневном лимите; при
приостановке после 403/429 не выполняется. Печатает обезличенный ход
сессии (шаги, коды ответов, имена полей, state/currency/amount) — чтобы
единственная сессия пилота была понятна и при неожиданном ответе.
--merchant-id/--page-id — merch_id/page_id МВД из JavaScript police.ge для
этого запуска (config.yaml при этом не меняется).

clear-suspension — явно снять приостановку сессий после 403/429.

Номер и квитанция печатаются маскированными. Запускать от того же
пользователя, что и бот (sudo -u leadradar): БД в WAL-режиме."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time as time_module
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.georgian_card_client import GeorgianCardAmountClient, GeorgianCardConfig  # noqa: E402
from reader.fines.payable_amount import (  # noqa: E402
    TRIGGER_PILOT,
    PayableAmountRepository,
    control_periods,
)
from reader.fines.payment_status import FineStatusRepository, FineStatusTracker  # noqa: E402
from reader.fines.validation import FineValidationError, normalize_car_number  # noqa: E402
from reader.public_bot.payable_amount_service import (  # noqa: E402
    PayableAmountLimits,
    PayableAmountService,
)
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.settings import load_settings  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def _mask(value: str) -> str:
    return f"…{value[-3:]}" if len(value) > 3 else "…"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("status", "plan", "fetch", "clear-suspension"))
    parser.add_argument("--date", default=None, help="plan: день (YYYY-MM-DD), по умолчанию сегодня.")
    parser.add_argument("--car-number", default=None, help="fetch: госномер.")
    parser.add_argument("--protocol-no", default=None, help="fetch: номер квитанции.")
    parser.add_argument("--execute", action="store_true", help="fetch: реально открыть одну сессию Georgian Card.")
    parser.add_argument("--merchant-id", default=None, help="fetch: merch_id МВД для этого запуска (вместо config).")
    parser.add_argument("--page-id", default=None, help="fetch: page_id МВД для этого запуска (вместо config).")
    parser.add_argument("--config", default=None,
                        help="config.yaml (по умолчанию — из этого checkout); его каталог задаёт БД и .env.")
    return parser.parse_args(argv)


class _TracingClient:
    """Обёртка клиента: запоминает обезличенный ход последней сессии."""

    def __init__(self, client):
        self._client = client
        self.last_trace: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return self._client.configured

    async def fetch_amount(self, **kwargs):
        quote = await self._client.fetch_amount(**kwargs)
        self.last_trace = quote.trace
        return quote


class _Context:
    def __init__(self, *, merchant_id: str | None = None, page_id: str | None = None, config_path: Path = CONFIG_PATH):
        settings = load_settings(config_path)
        self.fine_monitor = settings.fine_monitor
        self.tz = ZoneInfo(settings.fine_monitor.timezone)
        db_path = settings.app.users_db_file
        self.amounts = PayableAmountRepository(db_path)
        self._statuses_repository = FineStatusRepository(db_path)
        self.statuses = FineStatusTracker(self._statuses_repository)
        self.subscriptions = FineSubscriptionRepository(db_path)
        fm = settings.fine_monitor
        self.client = _TracingClient(GeorgianCardAmountClient(GeorgianCardConfig(
            base_url=fm.payable_amount_gc_base_url,
            merchant_id=(merchant_id or fm.payable_amount_gc_merchant_id).strip(),
            page_id=(page_id or fm.payable_amount_gc_page_id).strip(),
            amount_unit=fm.payable_amount_gc_amount_unit,
            request_timeout_seconds=fm.payable_amount_request_timeout_seconds,
        )))
        self.service = PayableAmountService(
            self.amounts, self.client, self.statuses, self.subscriptions,
            tz=self.tz,
            limits=PayableAmountLimits(
                max_sessions_per_day=fm.payable_amount_max_sessions_per_day,
                blocked_suspension=timedelta(hours=fm.payable_amount_blocked_suspension_hours),
            ),
            trusted_user_ids=frozenset(settings.public_bot.trusted_operator_user_ids),
        )

    def close(self) -> None:
        self.amounts.close()
        self._statuses_repository.close()
        self.subscriptions.close()


def _status(ctx: _Context) -> None:
    fm = ctx.fine_monitor
    today = datetime.now(timezone.utc).astimezone(ctx.tz).date()
    print(f"Функция в боте: {'ВКЛ' if fm.payable_amount_enabled else 'выкл'}; "
          f"автоматические проверки: {'ВКЛ' if fm.payable_amount_auto_checks_enabled else 'выкл'}; "
          f"Georgian Card: {'настроен' if ctx.client.configured else 'НЕ настроен (merch_id/page_id)'}")
    print(f"Лимит сессий в сутки: {fm.payable_amount_max_sessions_per_day}; окно автоматических проверок "
          f"{fm.payable_amount_auto_time:%H:%M}–{fm.payable_amount_auto_cutoff_time:%H:%M} {fm.timezone}")
    suspended = ctx.amounts.suspended_until()
    if suspended is not None and suspended[0] > datetime.now(timezone.utc).replace(tzinfo=None):
        print(f"⛔ Сессии приостановлены до {suspended[0]:%Y-%m-%d %H:%M} UTC ({suspended[1] or '—'})")
    print(f"Сохранено сумм: {ctx.amounts.count_amounts()}; событий увеличения: {ctx.amounts.count_events()}; "
          f"доставка событий: {ctx.amounts.delivery_counts()}")
    counts = ctx.amounts.outcome_counts_on(today)
    print(f"Сессий сегодня ({today}): {sum(counts.values())}")
    for (trigger, outcome), count in sorted(counts.items()):
        print(f"  {trigger}/{outcome}: {count}")
    run = ctx.amounts.get_run(today)
    if run is not None:
        print(f"Автоматический проход сегодня: {run.status}"
              + (f" ({run.stop_reason})" if run.stop_reason else "")
              + f", сессий {run.sessions}, ошибок {run.errors}")


def _plan(ctx: _Context, day: date) -> None:
    candidates = ctx.service.auto_candidates(day)
    by_period: dict[int, int] = {}
    for _, period in candidates:
        by_period[period.index] = by_period.get(period.index, 0) + 1
    print(f"Автоматический проход {day}: квитанций к проверке {len(candidates)} "
          f"(= сессий Georgian Card, лимит {ctx.fine_monitor.payable_amount_max_sessions_per_day}).")
    for index in sorted(by_period):
        print(f"  период {index} ({index * 30} дней): {by_period[index]}")
    print("Сеть не использовалась.")


def _fetch(ctx: _Context, car_number: str, protocol_no: str, *, execute: bool) -> int:
    refs = {ref.protocol_no: ref for ref in ctx.service.debt_receipts(car_number)}
    ref = refs.get(protocol_no)
    print(f"Квитанция {_mask(protocol_no)} номера {_mask(car_number)}: "
          + ("🔴/🟡 в базе полиции — разрешена." if ref else "не найдена среди 🔴/🟡 в базе полиции — пилот запрещён."))
    if ref is None:
        return 1
    term_ends = ctx.amounts.term_ends_for_cars([car_number])
    term_end = term_ends.get((car_number, protocol_no))
    if term_end is not None:
        periods = control_periods(term_end)
        print(f"Срок оплаты до {term_end}; контрольные периоды: "
              + ", ".join(f"{p.index}: {p.start:%d.%m}–{p.window_end:%d.%m}" for p in periods))
    stored = ctx.amounts.get_amount(car_number, protocol_no)
    print(f"Исходная сумма: {ref.base_amount}; сохранённая сумма Georgian Card: "
          + (f"{stored.amount} ({stored.observed_at:%Y-%m-%d %H:%M} UTC)" if stored else "нет"))
    if not ctx.client.configured:
        print("Georgian Card не настроен (payable_amount_gc_merchant_id/page_id) — сессия невозможна.")
        return 1
    if not execute:
        print("\nDry-run — ни одного запроса не отправлено. С --execute будет открыта ОДНА сессия Georgian Card "
              "(без карты и оплаты), сумма сохранится молча.")
        return 0
    started = time_module.monotonic()
    result = asyncio.run(ctx.service.refresh(ref, trigger=TRIGGER_PILOT, notify=False))
    elapsed = time_module.monotonic() - started
    checks = ctx.amounts.checks_for_receipt(car_number, protocol_no)
    last = checks[-1] if checks else None
    if ctx.client.last_trace:
        print("Ход сессии (обезличенно):")
        for step in ctx.client.last_trace:
            print(f"  - {step}")
    print(f"\nРезультат: {result.status}; исход Georgian Card: {result.outcome or '—'}"
          + (f" ({last.detail})" if last is not None and last.detail else "")
          + f"; {elapsed:.1f} с")
    if last is not None and last.amount is not None:
        print(f"Сумма к оплате: {last.amount} GEL; увеличение относительно "
              f"{'исходной' if last.previous_amount is None else 'прежней'} суммы: {'да' if last.increased else 'нет'}")
    return 0 if result.status == "fetched" else 2


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    ctx = _Context(
        merchant_id=args.merchant_id, page_id=args.page_id,
        config_path=Path(args.config) if args.config else CONFIG_PATH,
    )
    try:
        if args.action == "status":
            _status(ctx)
            return 0
        if args.action == "plan":
            day = date.fromisoformat(args.date) if args.date else datetime.now(timezone.utc).astimezone(ctx.tz).date()
            _plan(ctx, day)
            return 0
        if args.action == "clear-suspension":
            ctx.amounts.clear_suspension()
            print("Приостановка снята.")
            return 0
        if not args.car_number or not args.protocol_no:
            print("fetch: нужны --car-number и --protocol-no", file=sys.stderr)
            return 1
        try:
            car_number = normalize_car_number(args.car_number)
        except FineValidationError:
            print("fetch: номер не распознан", file=sys.stderr)
            return 1
        return _fetch(ctx, car_number, args.protocol_no.strip(), execute=args.execute)
    finally:
        ctx.close()


if __name__ == "__main__":
    raise SystemExit(main())
