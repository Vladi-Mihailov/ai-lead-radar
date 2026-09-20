"""ChangeDetector — сравнивает текущий UnifiedCheckResult с последним
УСПЕШНЫМ (не-ERROR) результатом каждого провайдера (см. design report
п.10 задачи "Перестроить UX Turkey test bot"). Сравнение — НЕ только
total_amount на верхнем уровне, а по каждому provider отдельно
(status/debt_count/principal/penalty/total — item-level только там, где
есть стабильный reference, см. reader/turkey_bot_test/unified/models.py::
DebtItem докстрок и design report решение п.5).

КЛЮЧЕВОЕ ПРАВИЛО (см. design report п.10/п.7): ERROR провайдера
ПОЛНОСТЬЮ игнорируется для сравнения — ни "долг исчез", ни "долг
появился" никогда не выводится из ERROR-результата. Baseline для
сравнения всегда — последний НЕ-ERROR результат этого провайдера (см.
reader/turkey_bot_test/unified/run_repository.py::
get_last_successful_provider_result), а не предыдущий run целиком."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from reader.turkey_bot_test.unified.models import (
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
)


class ChangeType(StrEnum):
    NEW_DEBT = "new_debt"
    DEBT_RESOLVED = "debt_resolved"
    AMOUNT_CHANGED = "amount_changed"


@dataclass(frozen=True)
class ProviderChange:
    provider: str
    change_type: ChangeType
    previous_total: Decimal
    current_total: Decimal


def detect_changes(
    previous_by_provider: dict[str, ProviderCheckResult | None],
    current: UnifiedCheckResult,
) -> list[ProviderChange]:
    changes: list[ProviderChange] = []

    for result in current.providers:
        if result.status == ProviderStatus.ERROR:
            # ERROR НИКОГДА не сравнивается — ни как "исчезновение", ни
            # как "появление" долга (см. design report п.10/п.7).
            continue

        previous = previous_by_provider.get(result.provider)

        if previous is None or previous.status == ProviderStatus.NO_DEBT:
            if result.status == ProviderStatus.HAS_DEBT:
                changes.append(ProviderChange(
                    provider=result.provider, change_type=ChangeType.NEW_DEBT,
                    previous_total=previous.total_amount if previous else Decimal(0),
                    current_total=result.total_amount,
                ))
            continue

        # previous.status == HAS_DEBT
        if result.status == ProviderStatus.NO_DEBT:
            changes.append(ProviderChange(
                provider=result.provider, change_type=ChangeType.DEBT_RESOLVED,
                previous_total=previous.total_amount, current_total=Decimal(0),
            ))
        elif (
            result.total_amount != previous.total_amount
            or result.principal_amount != previous.principal_amount
            or result.penalty_amount != previous.penalty_amount
        ):
            changes.append(ProviderChange(
                provider=result.provider, change_type=ChangeType.AMOUNT_CHANGED,
                previous_total=previous.total_amount, current_total=result.total_amount,
            ))

    return changes
