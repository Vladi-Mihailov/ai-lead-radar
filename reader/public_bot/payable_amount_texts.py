"""Тексты суммы к оплате (см. reader/public_bot/payable_amount_service.py):
«🧾 Штраф» — исходная сумма police.ge, «💰 К оплате» — сумма Georgian Card
на момент уточнения. Исходная сумма за подтверждённую не выдаётся: пока
актуальной суммы Georgian Card нет, — «требуется уточнение» или
«Предварительно к оплате». Исходная и уточнённая суммы одной квитанции не
складываются: уточнённая уже включает исходную.

Просрочка («⚠️ Срок оплаты истёк») — только по окончательному сроку оплаты
(см. reader/fines/payable_amount.py::term_end_of); «Сумма штрафа
увеличилась» — только когда актуальная сумма Georgian Card (с правилом
максимума display_payable) больше исходной. Коэффициенты не считаются.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from reader.fines.payable_amount import is_increase

REFRESH_BUTTON_LABEL = "🔄 Уточнить сумму"
NEEDS_REFRESH_TEXT = "требуется уточнение"
AMOUNTS_FOOTNOTE = "ℹ️ 💰 К оплате — сумма с начислениями по данным платёжной страницы на момент уточнения."


def format_money(value: float) -> str:
    return f"{_number(value)} ₾"


def _plural(count: int, one: str, few: str, many: str) -> str:
    last_two = abs(count) % 100
    last = last_two % 10
    if last == 1 and last_two != 11:
        return one
    if 2 <= last <= 4 and not 12 <= last_two <= 14:
        return few
    return many


@dataclass(frozen=True)
class ReceiptAmount:
    """Одна квитанция «к оплате»: payable — актуальная сумма Georgian Card
    (None — нет или устарела), observed_at — когда она получена (naive UTC),
    overdue — окончательный срок оплаты истёк (предварительный срок
    просрочкой не считается)."""

    protocol_no: str
    base_amount: float | None
    payable: float | None
    observed_at: datetime | None
    overdue: bool = False

    @property
    def increase_confirmed(self) -> bool:
        """Georgian Card подтвердил сумму больше исходной (payable — уже с
        правилом максимума, см. display_payable)."""
        return self.payable is not None and self.base_amount is not None and is_increase(self.payable, self.base_amount)


@dataclass(frozen=True)
class AmountSummary:
    count: int = 0
    base_total: float = 0.0
    known_count: int = 0
    known_total: float = 0.0
    # Квитанции без актуальной суммы и их исходные суммы (нижняя граница).
    unknown_count: int = 0
    unknown_base_total: float = 0.0
    oldest_observed_at: datetime | None = None
    # Окончательный срок оплаты истёк; из них — с подтверждённым увеличением.
    overdue_count: int = 0
    overdue_increased_count: int = 0

    @property
    def all_known(self) -> bool:
        return self.count > 0 and self.unknown_count == 0

    @property
    def preliminary_total(self) -> float:
        return self.known_total + self.unknown_base_total

    def __add__(self, other: AmountSummary) -> AmountSummary:
        return self.add(other)

    def add(self, other: AmountSummary) -> AmountSummary:
        oldest = [value for value in (self.oldest_observed_at, other.oldest_observed_at) if value is not None]
        return AmountSummary(
            count=self.count + other.count,
            base_total=self.base_total + other.base_total,
            known_count=self.known_count + other.known_count,
            known_total=self.known_total + other.known_total,
            unknown_count=self.unknown_count + other.unknown_count,
            unknown_base_total=self.unknown_base_total + other.unknown_base_total,
            oldest_observed_at=min(oldest) if oldest else None,
            overdue_count=self.overdue_count + other.overdue_count,
            overdue_increased_count=self.overdue_increased_count + other.overdue_increased_count,
        )


def summarize(receipts: Iterable[ReceiptAmount]) -> AmountSummary:
    summary = AmountSummary()
    for receipt in receipts:
        base = receipt.base_amount or 0.0
        overdue = {
            "overdue_count": int(receipt.overdue),
            "overdue_increased_count": int(receipt.overdue and receipt.increase_confirmed),
        }
        if receipt.payable is not None:
            part = AmountSummary(
                count=1, base_total=base, known_count=1, known_total=receipt.payable,
                oldest_observed_at=receipt.observed_at, **overdue,
            )
        else:
            part = AmountSummary(count=1, base_total=base, unknown_count=1, unknown_base_total=base, **overdue)
        summary = summary.add(part)
    return summary


def unknown_amount_summary(base_total: float) -> AmountSummary:
    """Долг без известных квитанций (сумма снимка проверки): одна квитанция
    без актуальной суммы."""
    return AmountSummary(count=1, base_total=base_total, unknown_count=1, unknown_base_total=base_total)


def format_refreshed_at(observed_at: datetime, *, tz: ZoneInfo, plural: bool = False) -> str:
    local = observed_at.replace(tzinfo=timezone.utc) if observed_at.tzinfo is None else observed_at
    local = local.astimezone(tz)
    prefix = "🕒 Суммы уточнены" if plural else "🕒 Сумма уточнена"
    return f"{prefix}: {local:%d.%m} в {local:%H:%M}"


def format_fines_line(count: int, base_total: float, *, original: bool = False) -> str:
    """original — среди штрафов есть просроченные: исходная сумма называется
    «первоначальной», чтобы её не приняли за сумму к оплате."""
    if count == 1:
        label = "Первоначальный штраф" if original else "Штраф"
        return f"🧾 {label}: {format_money(base_total)}"
    label = "Первоначальные штрафы" if original else "Штрафы"
    return f"🧾 {label}: {count} шт. — {format_money(base_total)}"


def format_overdue_line(summary: AmountSummary) -> str | None:
    """Просрочка по окончательному сроку оплаты (None — просроченных нет):
        ⚠️ Срок оплаты истёк. Сумма штрафа увеличилась.  (увеличение подтверждено)
        ⚠️ Срок оплаты истёк.                            (увеличения нет или сумма неизвестна)
    Для нескольких штрафов — сколько из них просрочено и по скольким
    Georgian Card подтвердил увеличение. Об увеличении — только по
    актуальной сумме Georgian Card, никаких расчётов."""
    if summary.overdue_count == 0:
        return None
    count = summary.count
    of_count = f"из {count} {_plural(count, 'штрафа', 'штрафов', 'штрафов')}"
    if summary.overdue_count == count:
        parts = ["⚠️ Срок оплаты истёк."]
    else:
        parts = [f"⚠️ Срок оплаты истёк по {summary.overdue_count} {of_count}."]
    increased = summary.overdue_increased_count
    if increased == count:
        parts.append("Сумма штрафа увеличилась." if count == 1 else "Сумма штрафов увеличилась.")
    elif increased:
        parts.append(f"Сумма увеличилась по {increased} {of_count}.")
    return " ".join(parts)


def format_receipt_overdue_line(receipt: ReceiptAmount) -> str | None:
    """Строка просрочки под одним штрафом (карточка «🔎 Проверить сейчас»)."""
    return format_overdue_line(summarize([receipt]))


def format_fine_payable_line(receipt: ReceiptAmount) -> str:
    """Под одним штрафом — без даты (дата одна, в итоге)."""
    if receipt.payable is None:
        return f"💰 К оплате: {NEEDS_REFRESH_TEXT}"
    return f"💰 К оплате: {format_money(receipt.payable)}"


def format_payable_lines(
    summary: AmountSummary, *, tz: ZoneInfo, with_date: bool = True, with_overdue: bool = True,
) -> list[str]:
    """«💰 К оплате» по набору квитанций:
    - все суммы актуальны — сумма и (with_date) одна дата: самая ранняя из
      уточнений, то есть все суммы уточнены не раньше неё;
    - ни одной — «требуется уточнение»;
    - часть — «Предварительно к оплате» (известные суммы плюс исходные суммы
      остальных, нижняя граница) и сколько квитанций требуют уточнения.
    with_overdue — сразу под суммой строка просрочки (format_overdue_line),
    если окончательный срок оплаты истёк."""
    if summary.count == 0:
        return []
    if summary.all_known:
        lines = [f"💰 К оплате: {format_money(summary.known_total)}"]
        if with_date and summary.oldest_observed_at is not None:
            lines.append(format_refreshed_at(summary.oldest_observed_at, tz=tz, plural=summary.count > 1))
    elif summary.known_count == 0:
        lines = [f"💰 К оплате: {NEEDS_REFRESH_TEXT}"]
    else:
        count = summary.unknown_count
        lines = [
            f"💰 Предварительно к оплате: {format_money(summary.preliminary_total)}",
            f"⚠️ По {count} {_plural(count, 'штрафу', 'штрафам', 'штрафам')} сумма требует уточнения",
        ]
    overdue = format_overdue_line(summary) if with_overdue else None
    if overdue is not None:
        lines.insert(1, overdue)
    return lines


def format_header_payable_lines(summary: AmountSummary, *, tz: ZoneInfo) -> list[str]:
    """Шапка статистики: «💰 К оплате» с одной датой — только когда все суммы
    выборки уточнены; иначе «💰 Сумма к оплате» без даты (известные суммы
    Georgian Card плюс исходные суммы остальных — решение владельца
    2026-11-21: без отдельной строки о неуточнённых)."""
    if summary.count == 0:
        return ["💰 К оплате: 0 ₾"]
    if summary.all_known:
        return format_payable_lines(summary, tz=tz, with_overdue=False)
    return [f"💰 Сумма к оплате: {format_money(summary.preliminary_total)}"]


def format_statistics_row_payable_line(summary: AmountSummary) -> str:
    """Строка машины в статистике: «💰 К оплате», если уточнены все её
    штрафы, иначе «💰 Сумма к оплате» (как в шапке, без даты)."""
    if summary.all_known:
        return f"💰 К оплате: {format_money(summary.known_total)}"
    return f"💰 Сумма к оплате: {format_money(summary.preliminary_total)}"


def _number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_amount_increase_line(old_amount: float, new_amount: float) -> str:
    """«⚠️ Сумма к оплате по вашему штрафу увеличилась: 100 → 150 ₾»."""
    return f"⚠️ Сумма к оплате по вашему штрафу увеличилась: {_number(old_amount)} → {format_money(new_amount)}"


def format_trusted_amount_increase_line(old_amount: float, new_amount: float) -> str:
    return (
        "⚠️ Сумма к оплате по штрафу автомобиля, поставленного вами на мониторинг, увеличилась: "
        f"{_number(old_amount)} → {format_money(new_amount)}"
    )
