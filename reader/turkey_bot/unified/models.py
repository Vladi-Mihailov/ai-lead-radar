"""Нормализованная, provider-агностичная модель результата проверки —
единая для GIB/Avrasya/KGM (см. reader/turkey_bot/unified/check_service.py).

ВАЖНО (см. задачу "Перестроить UX Turkey test bot"): captcha_code — входной
технический параметр для providerов (см. captcha_resolver.py), сама эта
модель ничего не знает про CAPTCHA/OCR — только конечный статус проверки.
ERROR (включая "captcha недоступна/отклонена") НИКОГДА не эквивалентен
NO_DEBT — это разные статусы, не путать местами нигде в вызывающем коде."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

_KNOWN_PROVIDERS = ("gib", "avrasya", "kgm")


class ProviderStatus(StrEnum):
    HAS_DEBT = "has_debt"
    NO_DEBT = "no_debt"
    ERROR = "error"


class OverallStatus(StrEnum):
    HAS_DEBT = "has_debt"
    NO_DEBT = "no_debt"
    PARTIAL = "partial"
    ERROR = "error"


@dataclass(frozen=True)
class DebtItem:
    """Нормализованный элемент долга — для истории/change detection.

    reference — стабильный natural key провайдера, ТОЛЬКО если он реально
    существует (см. design report: "не придумывать искусственный id,
    который может быть нестабилен") — сейчас заполняется ТОЛЬКО для GIB
    (GibFineRecord.protocol_no — настоящий номер протокола). У Avrasya/KGM
    (AvrasyaDebtItem/KgmDebtItem) стабильного id нет вообще — reference
    остаётся None, item-level diff для них невозможен (см.
    reader/turkey_bot/monitoring/change_detector.py — сравнение
    падает обратно на provider-level: status/debt_count/principal/
    penalty/total)."""

    reference: str | None
    amount: Decimal
    description: str | None


@dataclass(frozen=True)
class ProviderCheckResult:
    provider: str  # "gib" | "avrasya" | "kgm"
    status: ProviderStatus
    debt_count: int
    principal_amount: Decimal | None
    penalty_amount: Decimal | None
    total_amount: Decimal
    items: tuple[DebtItem, ...]
    error_type: str | None  # None, если status != ERROR
    checked_at: datetime

    def __post_init__(self) -> None:
        if self.provider not in _KNOWN_PROVIDERS:
            raise ValueError(f"Неизвестный provider: {self.provider!r}")
        if self.status == ProviderStatus.ERROR and self.error_type is None:
            raise ValueError("ProviderCheckResult со статусом ERROR должен иметь error_type")


@dataclass(frozen=True)
class UnifiedCheckResult:
    plate: str
    started_at: datetime
    finished_at: datetime
    overall_status: OverallStatus
    total_amount: Decimal
    providers: tuple[ProviderCheckResult, ...]

    def provider_result(self, provider: str) -> ProviderCheckResult | None:
        return next((p for p in self.providers if p.provider == provider), None)


def derive_overall_status(providers: tuple[ProviderCheckResult, ...]) -> OverallStatus:
    """Правило (см. задачу):
      - все ERROR -> ERROR;
      - хотя бы один ERROR, но не все -> PARTIAL;
      - все успешны, хотя бы один HAS_DEBT -> HAS_DEBT;
      - все успешны и все NO_DEBT -> NO_DEBT.
    ERROR никогда не интерпретируется как NO_DEBT (см. задачу п.4/п.7)."""
    statuses = [p.status for p in providers]
    if all(s == ProviderStatus.ERROR for s in statuses):
        return OverallStatus.ERROR
    if any(s == ProviderStatus.ERROR for s in statuses):
        return OverallStatus.PARTIAL
    if any(s == ProviderStatus.HAS_DEBT for s in statuses):
        return OverallStatus.HAS_DEBT
    return OverallStatus.NO_DEBT


def total_amount_for(providers: tuple[ProviderCheckResult, ...]) -> Decimal:
    """Сумма total_amount ТОЛЬКО у HAS_DEBT-провайдеров (NO_DEBT/ERROR
    вносят 0, см. задачу: пример "Итого: 5 477 ₺" суммирует именно так)."""
    return sum(
        (p.total_amount for p in providers if p.status == ProviderStatus.HAS_DEBT),
        Decimal(0),
    )
