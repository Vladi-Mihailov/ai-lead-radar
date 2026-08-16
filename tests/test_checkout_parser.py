"""Тесты reader/checkout/parser.py — обратный разбор "Распознано: ..." и
разбор исправленных полей оператора. Никакого реального OCR/Telegram здесь
нет — чистый текстовый парсинг."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.checkout.parser import (  # noqa: E402
    ReplyParseError,
    apply_corrections,
    is_pay_trigger,
    parse_correction_reply,
    parse_ocr_message,
)
from reader.ocr.models import OcrResult  # noqa: E402


def _full_ocr_message() -> str:
    return (
        "Распознано:\n\n"
        "Собственник: Ivanov Ivan\n"
        "Водитель: Petrov Petr\n"
        "Страхователь: Petrov Petr\n"
        "Номер паспорта: AB1234567\n"
        "Гражданство: Georgia\n"
        "Категория: passenger_car\n"
        "Марка: Toyota\n"
        "Модель: Camry\n"
        "VIN: JTMBR12345678901\n"
        "Номер шасси: не распознано\n"
        "Госномер: AA001AA\n\n"
        "Проверь данные."
    )


# ---- is_pay_trigger ----


def test_is_pay_trigger_recognizes_pay_case_insensitively():
    assert is_pay_trigger("pay")
    assert is_pay_trigger("PAY")
    assert is_pay_trigger("  Pay  ")


def test_is_pay_trigger_rejects_anything_else():
    assert not is_pay_trigger("pay now")
    assert not is_pay_trigger("Категория: passenger_car")
    assert not is_pay_trigger("")


# ---- parse_ocr_message ----


def test_parse_ocr_message_reconstructs_all_fields():
    result = parse_ocr_message(_full_ocr_message())

    assert result == OcrResult(
        owner_full_name="Ivanov Ivan",
        driver_full_name="Petrov Petr",
        policyholder_full_name="Petrov Petr",
        passport_number="AB1234567",
        citizenship="Georgia",
        category="passenger_car",
        registration_number="AA001AA",
        vin="JTMBR12345678901",
        chassis_number=None,
        manufacturer="Toyota",
        model="Camry",
    )


def test_parse_ocr_message_rejects_text_without_our_prefix():
    with pytest.raises(ReplyParseError):
        parse_ocr_message("Просто случайное сообщение в чате")


def test_parse_ocr_message_rejects_truncated_message():
    truncated = "Распознано:\n\nСобственник: Ivanov Ivan\n"
    with pytest.raises(ReplyParseError):
        parse_ocr_message(truncated)


# ---- parse_correction_reply ----


def test_parse_correction_reply_accepts_partial_fields():
    corrections = parse_correction_reply("Марка: Honda\nМодель: Accord")
    assert corrections == {"manufacturer": "Honda", "model": "Accord"}


def test_parse_correction_reply_accepts_full_field_block():
    corrections = parse_correction_reply(_full_ocr_message().split("\n\n", 1)[1])
    assert corrections["manufacturer"] == "Toyota"
    assert corrections["registration_number"] == "AA001AA"
    assert corrections["chassis_number"] is None
    assert corrections["passport_number"] == "AB1234567"
    assert corrections["citizenship"] == "Georgia"


def test_parse_correction_reply_accepts_passport_number_correction():
    corrections = parse_correction_reply("Номер паспорта: XY9998887")
    assert corrections == {"passport_number": "XY9998887"}


def test_parse_correction_reply_accepts_citizenship_correction():
    corrections = parse_correction_reply("Гражданство: Russia Federation")
    assert corrections == {"citizenship": "Russia Federation"}


def test_parse_correction_reply_not_recognized_marker_clears_passport_and_citizenship():
    corrections = parse_correction_reply("Номер паспорта: не распознано\nГражданство: не распознано")
    assert corrections == {"passport_number": None, "citizenship": None}


def test_parse_correction_reply_not_recognized_marker_clears_field():
    corrections = parse_correction_reply("VIN: не распознано")
    assert corrections == {"vin": None}


def test_parse_correction_reply_rejects_invalid_category():
    with pytest.raises(ReplyParseError, match="Категория"):
        parse_correction_reply("Категория: легковой")


def test_parse_correction_reply_accepts_all_three_valid_categories():
    for value in ("passenger_car", "motorcycle", "trailer"):
        assert parse_correction_reply(f"Категория: {value}") == {"category": value}


def test_parse_correction_reply_rejects_text_with_no_recognizable_fields():
    with pytest.raises(ReplyParseError):
        parse_correction_reply("случайный текст без меток")


def test_parse_correction_reply_error_message_lists_expected_format():
    with pytest.raises(ReplyParseError) as exc_info:
        parse_correction_reply("абракадабра")
    assert "pay" in str(exc_info.value)
    assert "Марка" in str(exc_info.value)


# ---- apply_corrections ----


def test_apply_corrections_overrides_only_given_fields():
    original = parse_ocr_message(_full_ocr_message())
    corrections = {"manufacturer": "Honda", "model": "Accord"}

    updated = apply_corrections(original, corrections)

    assert updated.manufacturer == "Honda"
    assert updated.model == "Accord"
    # остальное — из исходного сообщения, без изменений
    assert updated.owner_full_name == original.owner_full_name
    assert updated.registration_number == original.registration_number
    assert updated.category == original.category


def test_apply_corrections_can_clear_a_field_with_not_recognized_marker():
    original = parse_ocr_message(_full_ocr_message())
    corrections = parse_correction_reply("VIN: не распознано")

    updated = apply_corrections(original, corrections)

    assert updated.vin is None
    assert updated.chassis_number is None  # уже было None в исходном сообщении


def test_apply_corrections_with_no_overrides_returns_equivalent_result():
    original = parse_ocr_message(_full_ocr_message())
    updated = apply_corrections(original, {})
    assert updated == original


def test_apply_corrections_can_fix_passport_number_and_citizenship():
    original = parse_ocr_message(_full_ocr_message())
    corrections = parse_correction_reply("Номер паспорта: XY9998887\nГражданство: Russia Federation")

    updated = apply_corrections(original, corrections)

    assert updated.passport_number == "XY9998887"
    assert updated.citizenship == "Russia Federation"
    # остальное не изменилось
    assert updated.manufacturer == original.manufacturer
