"""Тесты reader/checkout/parser.py — обратный разбор "Распознано: ..." и
строгий разбор исправленных полей оператора (включая два bool-флага
"... = страхователь"). Никакого реального OCR/Telegram здесь нет — чистый
текстовый парсинг."""

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
        "Страхователь: Ivanov Ivan\n"
        "Номер паспорта: AB1234567\n"
        "Гражданство: Georgia\n"
        "Категория: passenger_car\n"
        "Марка: Toyota\n"
        "Модель: Camry\n"
        "VIN: JTMBR12345678901\n"
        "Номер шасси: не распознано\n"
        "Госномер: AA001AA\n"
        "Email: tplgee@mail.ru\n"
        "Телефон: 925000000000\n\n"
        "Водитель = страхователь: -\n"
        "Водитель: Petrov Petr\n\n"
        "Владелец = страхователь: +\n"
        "Владелец: \n\n"
        "Проверь данные."
    )


def _full_ocr_result() -> OcrResult:
    return OcrResult(
        policyholder_full_name="Ivanov Ivan",
        driver_same_as_policyholder=False,
        driver_full_name="Petrov Petr",
        owner_same_as_policyholder=True,
        owner_full_name=None,
        passport_number="AB1234567",
        citizenship="Georgia",
        category="passenger_car",
        manufacturer="Toyota",
        model="Camry",
        vin="JTMBR12345678901",
        chassis_number=None,
        registration_number="AA001AA",
        email="tplgee@mail.ru",
        phone="925000000000",
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
    assert result == _full_ocr_result()


def test_parse_ocr_message_rejects_text_without_our_prefix():
    with pytest.raises(ReplyParseError):
        parse_ocr_message("Просто случайное сообщение в чате")


def test_parse_ocr_message_rejects_truncated_message():
    truncated = "Распознано:\n\nСтрахователь: Ivanov Ivan\n"
    with pytest.raises(ReplyParseError):
        parse_ocr_message(truncated)


def test_parse_ocr_message_parses_both_flags_as_bool():
    result = parse_ocr_message(_full_ocr_message())
    assert result.owner_same_as_policyholder is True
    assert result.driver_same_as_policyholder is False


def test_parse_ocr_message_treats_empty_owner_value_as_none():
    """Реальный формат бота теперь оставляет "Владелец:"/"Водитель:" пустыми
    (не "не распознано"), когда роль совпадает со страхователем — парсер
    должен воспринимать пустое значение так же, как None."""
    result = parse_ocr_message(_full_ocr_message())
    assert result.owner_full_name is None


# ---- parse_correction_reply: обычные текстовые поля ----


def test_parse_correction_reply_accepts_partial_fields():
    corrections = parse_correction_reply("Марка: Honda\nМодель: Accord")
    assert corrections == {"manufacturer": "Honda", "model": "Accord"}


def test_parse_correction_reply_accepts_passport_number_correction():
    corrections = parse_correction_reply("Номер паспорта: XY9998887")
    assert corrections == {"passport_number": "XY9998887"}


def test_parse_correction_reply_accepts_citizenship_correction():
    corrections = parse_correction_reply("Гражданство: Russia Federation")
    assert corrections == {"citizenship": "Russia Federation"}


def test_parse_correction_reply_accepts_email_and_phone_correction():
    corrections = parse_correction_reply("Email: new@example.com\nТелефон: 599111222")
    assert corrections == {"email": "new@example.com", "phone": "599111222"}


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


# ---- parse_correction_reply: флаги "Водитель/Владелец = страхователь" ----


def test_parse_correction_reply_accepts_driver_flag_minus_with_full_name():
    corrections = parse_correction_reply("Водитель = страхователь: -\nВодитель: Petrov Petr")
    assert corrections == {"driver_same_as_policyholder": False, "driver_full_name": "Petrov Petr"}


def test_parse_correction_reply_accepts_driver_flag_plus_alone():
    corrections = parse_correction_reply("Водитель = страхователь: +")
    assert corrections == {"driver_same_as_policyholder": True}


def test_parse_correction_reply_accepts_owner_flag_minus_with_full_name():
    corrections = parse_correction_reply("Владелец = страхователь: -\nВладелец: Sidorov Petr")
    assert corrections == {"owner_same_as_policyholder": False, "owner_full_name": "Sidorov Petr"}


def test_parse_correction_reply_accepts_owner_flag_plus_alone():
    corrections = parse_correction_reply("Владелец = страхователь: +")
    assert corrections == {"owner_same_as_policyholder": True}


@pytest.mark.parametrize("raw_value", ["yes", "no", "true", "false", "1", "0", "", "х"])
def test_parse_correction_reply_rejects_invalid_flag_values(raw_value):
    """Только "+"/"-" — никаких yes/no/true/false."""
    with pytest.raises(ReplyParseError, match="Водитель = страхователь"):
        parse_correction_reply(f"Водитель = страхователь: {raw_value}")


def test_parse_correction_reply_invalid_flag_error_names_allowed_values():
    with pytest.raises(ReplyParseError) as exc_info:
        parse_correction_reply("Владелец = страхователь: yes")
    assert "+" in str(exc_info.value)
    assert "-" in str(exc_info.value)


# ---- parse_correction_reply: unknown/duplicate field ----


def test_parse_correction_reply_rejects_unknown_field():
    with pytest.raises(ReplyParseError, match="Неизвестное поле"):
        parse_correction_reply("Дата рождения: 01.01.1990")


def test_parse_correction_reply_rejects_duplicate_field():
    with pytest.raises(ReplyParseError, match="более одного раза"):
        parse_correction_reply("Марка: Toyota\nМарка: Honda")


def test_parse_correction_reply_rejects_duplicate_flag_field():
    with pytest.raises(ReplyParseError, match="более одного раза"):
        parse_correction_reply("Водитель = страхователь: -\nВодитель = страхователь: +")


# ---- apply_corrections ----


def test_apply_corrections_overrides_only_given_fields():
    original = parse_ocr_message(_full_ocr_message())
    corrections = {"manufacturer": "Honda", "model": "Accord"}

    updated = apply_corrections(original, corrections)

    assert updated.manufacturer == "Honda"
    assert updated.model == "Accord"
    # остальное — из исходного сообщения, без изменений
    assert updated.policyholder_full_name == original.policyholder_full_name
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


def test_apply_corrections_can_switch_driver_flag_to_same_as_policyholder():
    original = parse_ocr_message(_full_ocr_message())  # driver_same_as_policyholder=False
    corrections = parse_correction_reply("Водитель = страхователь: +")

    updated = apply_corrections(original, corrections)

    assert updated.driver_same_as_policyholder is True
    # driver_full_name из исходного сообщения не трогаем — оператор его не менял.
    assert updated.driver_full_name == original.driver_full_name


def test_apply_corrections_can_switch_owner_flag_to_not_same_as_policyholder():
    original = parse_ocr_message(_full_ocr_message())  # owner_same_as_policyholder=True
    corrections = parse_correction_reply("Владелец = страхователь: -\nВладелец: Sidorov Petr")

    updated = apply_corrections(original, corrections)

    assert updated.owner_same_as_policyholder is False
    assert updated.owner_full_name == "Sidorov Petr"


def test_apply_corrections_can_change_email_and_phone():
    original = parse_ocr_message(_full_ocr_message())
    corrections = parse_correction_reply("Email: new@example.com\nТелефон: 599111222")

    updated = apply_corrections(original, corrections)

    assert updated.email == "new@example.com"
    assert updated.phone == "599111222"


# ---- correction-reply как authoritative source поверх доверенности из OCR ----


def _ocr_result_with_power_of_attorney_owner() -> OcrResult:
    """OCR уже распознал отдельного владельца из доверенности — имитирует
    reader/ocr/service.py::_derive_name_roles с power_of_attorney_owner_full_name."""
    return OcrResult(
        policyholder_full_name="Ivanov Ivan",
        driver_same_as_policyholder=True, driver_full_name=None,
        owner_same_as_policyholder=False, owner_full_name="Sidorov Semyon",
        passport_number="AB1234567", citizenship="Georgia",
        category="passenger_car", manufacturer="Toyota", model="Camry",
        vin="JTMBR12345678901", chassis_number=None, registration_number="AA001AA",
        email="tplgee@mail.ru", phone="925000000000",
    )


def test_correction_reply_can_override_power_of_attorney_owner_name_from_ocr():
    """OCR — только initial draft: оператор может заменить owner_full_name,
    распознанный из доверенности, своим значением через correction-reply —
    исправленный reply authoritative для конкретного checkout."""
    original = _ocr_result_with_power_of_attorney_owner()
    corrections = parse_correction_reply("Владелец: Sidorova Anna")

    updated = apply_corrections(original, corrections)

    assert updated.owner_full_name == "Sidorova Anna"
    assert updated.owner_same_as_policyholder is False  # флаг оператор не менял


def test_correction_reply_can_clear_power_of_attorney_owner_by_switching_flag_to_plus():
    original = _ocr_result_with_power_of_attorney_owner()
    corrections = parse_correction_reply("Владелец = страхователь: +")

    updated = apply_corrections(original, corrections)

    assert updated.owner_same_as_policyholder is True
