"""
Regression tests for the new unified check result format and overall
total calculation (see task "Улучшить формат unified Turkey check и
расчёт итоговой суммы"). Builds real GibFineRecord/AvrasyaDebtItem/
KgmDebtItem (the actual structured models already returned by the
providers) and runs them through the REAL conversion helpers in
reader/turkey_bot/unified/check_service.py — not synthetic DebtItem
objects — so a future regression in field extraction would be caught
here, not just a layout regression. No real HTTP/CAPTCHA anywhere."""

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot import texts
from reader.turkey_bot.avrasya.models import AvrasyaDebtItem
from reader.turkey_bot.gib.models import GibFineRecord
from reader.turkey_bot.kgm.models import KgmDebtItem
from reader.turkey_bot.unified.check_service import (
    _avrasya_item_description,
    _gib_fine_description,
    _kgm_item_description,
)
from reader.turkey_bot.unified.models import (
    DebtItem,
    OverallStatus,
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    total_amount_for,
)

_NOW = datetime(2026, 9, 20, 15, 43, tzinfo=timezone.utc)


def _provider_result(name, status, *, items=(), total=Decimal(0), principal=None, penalty=None, error_type=None):
    return ProviderCheckResult(
        provider=name, status=status, debt_count=len(items),
        principal_amount=principal if principal is not None else total,
        penalty_amount=penalty if penalty is not None else Decimal(0),
        total_amount=total, items=items, error_type=error_type, checked_at=_NOW,
    )


def _unified(*providers) -> UnifiedCheckResult:
    return UnifiedCheckResult(
        plate="M295YB196", started_at=_NOW, finished_at=_NOW,
        overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers),
        providers=providers,
    )


def _build_example_result() -> UnifiedCheckResult:
    """Ровно сценарий из задачи: GIB=80, Avrasya=2580, KGM=2660."""
    gib_fine = GibFineRecord(
        protocol_no="TR12345", plate="M295YB196", amount=Decimal(80), description="d",
        violation_date=date(2026, 9, 10), authority="Istanbul Trafik", late_fee=None, discount=None,
        location="ISTANBUL", violation_description="Hiz siniri ihlali",
        location_ru="Стамбул", violation_description_ru="Превышение скорости",
    )
    gib = _provider_result(
        "gib", ProviderStatus.HAS_DEBT, total=Decimal(80),
        items=(DebtItem(reference=gib_fine.protocol_no, amount=Decimal(80), description=_gib_fine_description(gib_fine)),),
    )

    avrasya_raw = AvrasyaDebtItem(principal_amount=Decimal(2580), total_amount=Decimal(2580), service_file_type="HGS")
    avrasya = _provider_result(
        "avrasya", ProviderStatus.HAS_DEBT, total=Decimal(2580),
        items=(DebtItem(reference=None, amount=Decimal(2580), description=_avrasya_item_description(avrasya_raw)),),
    )

    kgm_item_1 = KgmDebtItem(
        operator="kgm", date_time=datetime(2025, 10, 9, 13, 41), entry_station="DİLİSKELESİ",  # noqa: DTZ001
        exit_station="LİMAN SGS", vehicle_class="1", base_toll=Decimal(32), payable_amount=Decimal(160),
        penalty_free_deadline=date(2025, 11, 18),
    )
    kgm_item_2 = KgmDebtItem(
        operator="kgm", date_time=datetime(2026, 8, 31, 15, 44), entry_station="GEREDE",  # noqa: DTZ001
        exit_station="TOPAĞAÇ SGS", vehicle_class="1", base_toll=Decimal(237), payable_amount=Decimal(474),
        penalty_free_deadline=date(2026, 9, 15),
    )
    kgm = _provider_result(
        "kgm", ProviderStatus.HAS_DEBT, total=Decimal(2660), principal=Decimal(269), penalty=Decimal(1391),
        items=(
            DebtItem(reference=None, amount=Decimal(160), description=_kgm_item_description(kgm_item_1)),
            DebtItem(reference=None, amount=Decimal(474), description=_kgm_item_description(kgm_item_2)),
        ),
    )

    return _unified(gib, avrasya, kgm)


# ---- 1. GIB=80, Avrasya=2580, KGM=2660 -> overall total=2740 ----

def test_overall_total_excludes_avrasya_matching_task_example():
    result = _build_example_result()

    assert result.provider_result("gib").total_amount == Decimal(80)
    assert result.provider_result("avrasya").total_amount == Decimal(2580)
    assert result.provider_result("kgm").total_amount == Decimal(2660)
    assert result.total_amount == Decimal(2740)
    assert result.overall_status == OverallStatus.HAS_DEBT


# ---- 2. Summary shows translated provider names with the exact labels ----

def test_summary_shows_provider_labels_in_parentheses():
    result = _build_example_result()
    rendered = texts.format_unified_check_result(result)

    assert "GİB (Штрафы)" in rendered
    assert "Avrasya (Тунели)" in rendered
    assert "KGM (Платные дороги, включая тунели)" in rendered
    assert "Итого: 2 740 ₺" in rendered


# ---- 3. "Детализация:" section is present ----

def test_detail_section_header_present():
    result = _build_example_result()
    rendered = texts.format_unified_check_result(result)

    assert "Детализация:" in rendered


def test_detail_section_absent_when_nothing_to_detail():
    """Явное дополнение — если ни у одного provider'а нет items (все
    NO_DEBT/ERROR), раздел "Детализация:" вообще не должен появляться."""
    result = _unified(
        _provider_result("gib", ProviderStatus.NO_DEBT),
        _provider_result("avrasya", ProviderStatus.NO_DEBT),
        _provider_result("kgm", ProviderStatus.ERROR, error_type="transport_error"),
    )
    rendered = texts.format_unified_check_result(result)

    assert "Детализация:" not in rendered


# ---- 4. KGM structured item shows date/time, route, Стоимость, К оплате,
# Без штрафа до, exactly as specified in the task ----

def test_kgm_item_detail_shows_all_structured_fields_when_present():
    kgm_item = KgmDebtItem(
        operator="kgm", date_time=datetime(2025, 10, 9, 13, 41), entry_station="DİLİSKELESİ",  # noqa: DTZ001
        exit_station="LİMAN SGS", vehicle_class="1", base_toll=Decimal(32), payable_amount=Decimal(160),
        penalty_free_deadline=date(2025, 11, 18),
    )

    description = _kgm_item_description(kgm_item)

    assert description == (
        "09.10.2025 13:41\n"
        "DİLİSKELESİ → LİMAN SGS\n"
        "Стоимость: 32 ₺\n"
        "К оплате: 160 ₺\n"
        "Без штрафа до: 18.11.2025"
    )


def test_kgm_item_detail_omits_deadline_when_absent_never_invents_it():
    kgm_item = KgmDebtItem(
        operator="kgm", date_time=datetime(2025, 10, 9, 13, 41), entry_station="A",  # noqa: DTZ001
        exit_station="B", vehicle_class=None, base_toll=Decimal(32), payable_amount=Decimal(160),
        penalty_free_deadline=None,
    )

    description = _kgm_item_description(kgm_item)

    assert "Без штрафа до" not in description


def test_multiple_kgm_items_all_shown_in_detail_section():
    result = _build_example_result()
    rendered = texts.format_unified_check_result(result)

    assert "09.10.2025 13:41" in rendered
    assert "DİLİSKELESİ → LİMAN SGS" in rendered
    assert "Без штрафа до: 18.11.2025" in rendered
    assert "31.08.2026 15:44" in rendered
    assert "GEREDE → TOPAĞAÇ SGS" in rendered
    assert "Без штрафа до: 15.09.2026" in rendered


# ---- 5. Avrasya is not lost from the detail section despite being
# excluded from the overall total ----

def test_avrasya_still_appears_in_detail_section_despite_exclusion_from_total():
    result = _build_example_result()
    rendered = texts.format_unified_check_result(result)

    assert "Avrasya (Тунели)" in rendered
    assert "HGS" in rendered
    assert "2 580 ₺" in rendered  # собственная сумма Avrasya видна как есть


def test_avrasya_provider_result_keeps_its_own_total_amount_unchanged():
    """Явное требование задачи: "provider result Avrasya должен
    по-прежнему хранить собственные 2580 ₺ — меняется только overall
    total"."""
    result = _build_example_result()

    assert result.provider_result("avrasya").total_amount == Decimal(2580)


# ---- 6. PARTIAL/ERROR formatting is not broken ----

def test_partial_formatting_still_safe_with_new_layout():
    result = _unified(
        _provider_result("gib", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider_result("avrasya", ProviderStatus.NO_DEBT),
        _provider_result(
            "kgm", ProviderStatus.HAS_DEBT, total=Decimal(160),
            items=(DebtItem(reference=None, amount=Decimal(160), description="09.10.2025 13:41\nA → B\nСтоимость: 32 ₺\nК оплате: 160 ₺"),),
        ),
    )
    rendered = texts.format_unified_check_result(result)

    assert "Итого: 160" not in rendered
    assert "Итоговая сумма может быть неполной" in rendered
    assert "GİB (Штрафы) — ⚠️ временно не удалось проверить" in rendered
    assert "Детализация:" in rendered  # KGM всё ещё детализирован, несмотря на PARTIAL


def test_error_formatting_still_safe_with_new_layout():
    result = _unified(
        _provider_result("gib", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider_result("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider_result("kgm", ProviderStatus.ERROR, error_type="captcha_unavailable"),
    )
    rendered = texts.format_unified_check_result(result)

    assert "Итого:" not in rendered
    assert "Итоговая сумма не определена" in rendered
    assert "Детализация:" not in rendered


# ---- Cross-check: production formula must never re-introduce GIB +
# Avrasya + KGM without excluding Avrasya ----

def test_total_amount_for_never_sums_gib_avrasya_kgm_naively():
    gib = _provider_result("gib", ProviderStatus.HAS_DEBT, total=Decimal(80))
    avrasya = _provider_result("avrasya", ProviderStatus.HAS_DEBT, total=Decimal(2580))
    kgm = _provider_result("kgm", ProviderStatus.HAS_DEBT, total=Decimal(2660))

    total = total_amount_for((gib, avrasya, kgm))

    naive_sum = Decimal(80) + Decimal(2580) + Decimal(2660)
    assert total == Decimal(2740)
    assert total != naive_sum
