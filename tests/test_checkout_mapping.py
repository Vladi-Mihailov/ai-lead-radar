"""Тесты reader/checkout/mapping.py — VIN/шасси приоритет, санитайзинг
госномера, список обязательных полей (включая условное требование
Водитель/Владелец при same_as=False)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.checkout.mapping import (  # noqa: E402
    MappingError,
    required_vehicle_fields_missing,
    resolve_payment_bank,
    resolve_period_start,
    resolve_policy_period,
    sanitize_registration_number,
    select_frame_number,
)
from reader.checkout.models import PaymentBank  # noqa: E402
from reader.ocr.models import OcrResult  # noqa: E402


def _full_result(**overrides) -> OcrResult:
    fields = dict(
        policyholder_full_name="Petrov Petr",
        driver_same_as_policyholder=True,
        driver_full_name=None,
        owner_same_as_policyholder=True,
        owner_full_name=None,
        passport_number="AB1234567",
        citizenship="Georgia",
        category="passenger_car",
        manufacturer="Toyota",
        model="Camry",
        registration_number="AA001AA",
        vin="WVWZZZ1KZAW123456",
        chassis_number=None,
        email="tplgee@mail.ru",
        phone="925000000000",
        payment_bank="bog",
        policy_period="15",
        period_start="16.08.2026",
    )
    fields.update(overrides)
    return OcrResult(**fields)


# ---- select_frame_number: VIN приоритет над номером шасси ----


def test_select_frame_number_prefers_vin_when_both_present():
    result = _full_result(vin="VIN123", chassis_number="CHASSIS456")
    assert select_frame_number(result) == "VIN123"


def test_select_frame_number_falls_back_to_chassis_when_vin_missing():
    result = _full_result(vin=None, chassis_number="CHASSIS456")
    assert select_frame_number(result) == "CHASSIS456"


def test_select_frame_number_raises_when_neither_present():
    result = _full_result(vin=None, chassis_number=None)
    with pytest.raises(MappingError):
        select_frame_number(result)


# ---- sanitize_registration_number ----


def test_sanitize_registration_number_strips_dashes_and_spaces():
    assert sanitize_registration_number("AA-001-AA") == "AA001AA"
    assert sanitize_registration_number("AA 001 AA") == "AA001AA"


def test_sanitize_registration_number_uppercases():
    assert sanitize_registration_number("aa001aa") == "AA001AA"


def test_sanitize_registration_number_rejects_non_latin_characters():
    with pytest.raises(MappingError):
        sanitize_registration_number("АА001АА")  # кириллица


def test_sanitize_registration_number_rejects_empty_value():
    with pytest.raises(MappingError):
        sanitize_registration_number("")


# ---- required_vehicle_fields_missing: базовые поля (всегда обязательны) ----


def test_required_vehicle_fields_missing_empty_for_complete_result():
    assert required_vehicle_fields_missing(_full_result()) == []


def test_required_vehicle_fields_missing_flags_absent_policyholder():
    result = _full_result(policyholder_full_name=None)
    assert "Страхователь" in required_vehicle_fields_missing(result)


def test_required_vehicle_fields_missing_flags_absent_vin_and_chassis_together():
    result = _full_result(vin=None, chassis_number=None)
    missing = required_vehicle_fields_missing(result)
    assert "VIN/Номер шасси" in missing


def test_required_vehicle_fields_missing_does_not_flag_vin_when_only_chassis_present():
    result = _full_result(vin=None, chassis_number="CHASSIS1")
    assert "VIN/Номер шасси" not in required_vehicle_fields_missing(result)


# ---- required_vehicle_fields_missing: Водитель/Владелец — условно ----


def test_driver_same_as_policyholder_true_does_not_require_separate_full_name():
    """"+" не требует отдельного ФИО."""
    result = _full_result(driver_same_as_policyholder=True, driver_full_name=None)
    assert "Водитель" not in required_vehicle_fields_missing(result)


def test_driver_same_as_policyholder_false_without_full_name_blocks_checkout():
    """"-" без отдельного ФИО блокирует checkout."""
    result = _full_result(driver_same_as_policyholder=False, driver_full_name=None)
    assert "Водитель" in required_vehicle_fields_missing(result)


def test_driver_same_as_policyholder_false_with_full_name_is_not_missing():
    result = _full_result(driver_same_as_policyholder=False, driver_full_name="Ivanov Ivan")
    assert "Водитель" not in required_vehicle_fields_missing(result)


def test_owner_same_as_policyholder_true_does_not_require_separate_full_name():
    result = _full_result(owner_same_as_policyholder=True, owner_full_name=None)
    assert "Владелец" not in required_vehicle_fields_missing(result)


def test_owner_same_as_policyholder_false_without_full_name_blocks_checkout():
    result = _full_result(owner_same_as_policyholder=False, owner_full_name=None)
    assert "Владелец" in required_vehicle_fields_missing(result)


def test_owner_same_as_policyholder_false_with_full_name_is_not_missing():
    result = _full_result(owner_same_as_policyholder=False, owner_full_name="Sidorov Petr")
    assert "Владелец" not in required_vehicle_fields_missing(result)


# ---- required_vehicle_fields_missing: Банк/Период/Начало периода ----


def test_required_vehicle_fields_missing_flags_absent_payment_bank():
    result = _full_result(payment_bank=None)
    assert "Банк" in required_vehicle_fields_missing(result)


def test_required_vehicle_fields_missing_flags_absent_policy_period():
    result = _full_result(policy_period=None)
    assert "Период" in required_vehicle_fields_missing(result)


def test_required_vehicle_fields_missing_flags_absent_period_start():
    result = _full_result(period_start=None)
    assert "Начало периода" in required_vehicle_fields_missing(result)


# ---- resolve_payment_bank: "bog"/"liberty" -> PaymentBank ----


def test_resolve_payment_bank_bog_maps_to_bank_of_georgia():
    assert resolve_payment_bank("bog") == PaymentBank.BANK_OF_GEORGIA


def test_resolve_payment_bank_liberty_maps_to_liberty_bank():
    """"liberty" — реальный выбор в UI tpl.ge, не придуманное значение (см.
    reader/checkout/models.py::PaymentBank) — но его ecommerce-путь не
    подтверждён research'ом (см. reader/checkout/tpl_client.py, не тронут
    этой задачей)."""
    assert resolve_payment_bank("liberty") == PaymentBank.LIBERTY_BANK


def test_resolve_payment_bank_rejects_unknown_value():
    with pytest.raises(MappingError):
        resolve_payment_bank("tbc")


# ---- resolve_policy_period: "15"/"30"/"90" -> "<N>-D" ----


@pytest.mark.parametrize("value,expected", [("15", "15-D"), ("30", "30-D"), ("90", "90-D")])
def test_resolve_policy_period_maps_to_tpl_ge_period_format(value, expected):
    assert resolve_policy_period(value) == expected


def test_resolve_policy_period_rejects_annual_period():
    """1-Y (годовой полис) в Telegram-flow сознательно не поддерживается."""
    with pytest.raises(MappingError):
        resolve_policy_period("1-Y")


def test_resolve_policy_period_rejects_unsupported_value():
    with pytest.raises(MappingError):
        resolve_policy_period("365")


# ---- resolve_period_start: "ДД.ММ.ГГГГ" -> "YYYY-MM-DD" ----


def test_resolve_period_start_converts_to_iso_format():
    assert resolve_period_start("16.08.2026") == "2026-08-16"


def test_resolve_period_start_rejects_invalid_date():
    with pytest.raises(MappingError):
        resolve_period_start("31.02.2026")  # 31 февраля не существует


def test_resolve_period_start_rejects_malformed_string():
    with pytest.raises(MappingError):
        resolve_period_start("2026-08-16")
