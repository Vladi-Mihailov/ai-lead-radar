"""
Тесты reader/turkey_bot/texts.py::format_unified_check_result — итоговая
строка (см. задачу "Root cause: M295YB196 0 ₺" / "Минимальный production
fix"). Presentation-only regression: overall_status/total_amount модель
расчёта НЕ менялась (см. reader/turkey_bot/unified/models.py::
derive_overall_status/total_amount_for) — эти тесты фиксируют ТОЛЬКО
текст, который видит пользователь."""

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot import texts
from reader.turkey_bot.unified.models import (
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    total_amount_for,
)

_NOW = datetime(2026, 9, 20, 12, 19, 2, tzinfo=timezone.utc)


def _provider(provider: str, status: ProviderStatus, *, total=Decimal(0), error_type=None) -> ProviderCheckResult:
    return ProviderCheckResult(
        provider=provider, status=status, debt_count=1 if status == ProviderStatus.HAS_DEBT else 0,
        principal_amount=total, penalty_amount=Decimal(0), total_amount=total,
        items=(), error_type=error_type, checked_at=_NOW,
    )


def _result(*providers: ProviderCheckResult) -> UnifiedCheckResult:
    return UnifiedCheckResult(
        plate="M295YB196", started_at=_NOW, finished_at=_NOW,
        overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers),
        providers=providers,
    )


def test_all_providers_error_does_not_show_zero_total():
    """Явное требование задачи — regression на реальный production
    инцидент: GİB/Avrasya/KGM все ERROR (captcha_unavailable, см. root
    cause отчёт) НЕ должны показывать "Итого: 0 ₺"."""
    result = _result(
        _provider("gib", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("kgm", ProviderStatus.ERROR, error_type="captcha_unavailable"),
    )
    assert result.overall_status.value == "error"

    rendered = texts.format_unified_check_result(result)

    assert "Итого: 0" not in rendered
    assert "Итоговая сумма не определена" in rendered


def test_partial_does_not_show_confirmed_zero_total():
    result = _result(
        _provider("gib", ProviderStatus.ERROR, error_type="transport_error"),
        _provider("avrasya", ProviderStatus.NO_DEBT),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    assert result.overall_status.value == "partial"

    rendered = texts.format_unified_check_result(result)

    assert "Итого: 0" not in rendered
    assert "Итоговая сумма может быть неполной" in rendered


def test_partial_with_debt_does_not_show_that_debt_as_confirmed_total():
    """PARTIAL с реальной найденной суммой у успешного провайдера — сумма
    ВСЁ РАВНО не должна выглядеть как подтверждённый "Итого" (один
    провайдер не ответил, итог не может считаться финальным). Провайдер с
    долгом здесь — KGM, не Avrasya: Avrasya сознательно ИСКЛЮЧЕНА из
    агрегированного total_amount всегда (см. reader/turkey_bot/unified/
    models.py::total_amount_for — её долг уже учтён внутри KGM), поэтому
    для проверки именно "PARTIAL прячет реальный total" нужен provider,
    который в принципе входит в агрегат."""
    result = _result(
        _provider("gib", ProviderStatus.ERROR, error_type="transport_error"),
        _provider("avrasya", ProviderStatus.NO_DEBT),
        _provider("kgm", ProviderStatus.HAS_DEBT, total=Decimal(500)),
    )
    assert result.overall_status.value == "partial"
    assert result.total_amount == Decimal(500)

    rendered = texts.format_unified_check_result(result)

    assert "Итого: 500" not in rendered
    assert "Итоговая сумма может быть неполной" in rendered


def test_has_debt_still_shows_normal_total():
    result = _result(
        _provider("gib", ProviderStatus.HAS_DEBT, total=Decimal(5477)),
        _provider("avrasya", ProviderStatus.NO_DEBT),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    assert result.overall_status.value == "has_debt"

    rendered = texts.format_unified_check_result(result)

    assert "Итого: 5 477 ₺" in rendered
    assert "Итоговая сумма не определена" not in rendered
    assert "Итоговая сумма может быть неполной" not in rendered


def test_no_debt_ux_unchanged():
    result = _result(
        _provider("gib", ProviderStatus.NO_DEBT),
        _provider("avrasya", ProviderStatus.NO_DEBT),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    assert result.overall_status.value == "no_debt"

    rendered = texts.format_unified_check_result(result)

    assert "Итого: 0 ₺" in rendered
    assert "Итоговая сумма не определена" not in rendered
    assert "Итоговая сумма может быть неполной" not in rendered
