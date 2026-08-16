"""Тесты reader/checkout/mapping.py — VIN/шасси приоритет, санитайзинг
госномера, список обязательных полей."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.checkout.mapping import (  # noqa: E402
    MappingError,
    required_vehicle_fields_missing,
    sanitize_registration_number,
    select_frame_number,
)
from reader.ocr.models import OcrResult  # noqa: E402


def _full_result(**overrides) -> OcrResult:
    fields = dict(
        owner_full_name="Ivanov Ivan",
        driver_full_name="Petrov Petr",
        policyholder_full_name="Petrov Petr",
        passport_number="AB1234567",
        citizenship="Georgia",
        category="passenger_car",
        registration_number="AA001AA",
        vin="WVWZZZ1KZAW123456",
        chassis_number=None,
        manufacturer="Toyota",
        model="Camry",
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


# ---- required_vehicle_fields_missing ----


def test_required_vehicle_fields_missing_empty_for_complete_result():
    assert required_vehicle_fields_missing(_full_result()) == []


def test_required_vehicle_fields_missing_lists_absent_names():
    result = _full_result(owner_full_name=None, driver_full_name=None)
    missing = required_vehicle_fields_missing(result)
    assert "Собственник" in missing
    assert "Водитель" in missing
    assert "Страхователь" not in missing


def test_required_vehicle_fields_missing_flags_absent_vin_and_chassis_together():
    result = _full_result(vin=None, chassis_number=None)
    missing = required_vehicle_fields_missing(result)
    assert "VIN/Номер шасси" in missing


def test_required_vehicle_fields_missing_does_not_flag_vin_when_only_chassis_present():
    result = _full_result(vin=None, chassis_number="CHASSIS1")
    assert "VIN/Номер шасси" not in required_vehicle_fields_missing(result)
