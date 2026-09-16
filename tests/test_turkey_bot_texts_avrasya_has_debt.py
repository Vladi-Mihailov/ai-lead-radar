"""
Тесты reader/turkey_bot/texts.py::format_avrasya_has_debt_message — см.
design report Stage 2C, точный production-пример (turkey_toll_checks.id=3,
2026-09-16, plate M295YB196).
"""

import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot import texts  # noqa: E402
from reader.turkey_bot.avrasya.models import AvrasyaDebtItem  # noqa: E402


def _item(*, principal, total, service_file_type="COLLECTION_FILE", penalty=None) -> AvrasyaDebtItem:
    return AvrasyaDebtItem(
        principal_amount=Decimal(str(principal)), total_amount=Decimal(str(total)),
        service_file_type=service_file_type, penalty_amount=Decimal(str(penalty)) if penalty is not None else None,
    )


def test_real_production_example_matches_expected_message_exactly():
    """См. задачу — точный ожидаемый вывод для M295YB196."""
    items = (
        _item(principal=330.0, total=330.0, service_file_type="EARLY_COLLECTION_FILE"),
        _item(principal=225.0, total=1125.0, penalty=900.0, service_file_type="COLLECTION_FILE"),
        _item(principal=225.0, total=1125.0, penalty=900.0, service_file_type="COLLECTION_FILE"),
    )

    message = texts.format_avrasya_has_debt_message("M295YB196", items)

    assert message == (
        "⚠️ M295YB196: найдены неоплаченные проезды по Avrasya Tüneli.\n"
        "\n"
        "Проездов: 3\n"
        "Стоимость проездов: 780 ₺\n"
        "Начисленные штрафы: 1 800 ₺\n"
        "Итого к оплате: 2 580 ₺"
    )


def test_message_includes_plate_and_item_count():
    items = (_item(principal=100.0, total=100.0),)

    message = texts.format_avrasya_has_debt_message("A123AA123", items)

    assert "A123AA123" in message
    assert "Проездов: 1" in message


def test_penalty_line_omitted_when_no_item_has_a_penalty():
    """См. design report: та же логика "не показывать поле вовсе, когда
    оно отсутствует", что и у GIB _format_fine_block про late_fee."""
    items = (_item(principal=100.0, total=100.0, penalty=None),)

    message = texts.format_avrasya_has_debt_message("A123AA123", items)

    assert "Начисленные штрафы" not in message


def test_penalty_line_shown_when_at_least_one_item_has_a_penalty():
    items = (
        _item(principal=100.0, total=100.0, penalty=None),
        _item(principal=100.0, total=300.0, penalty=200.0),
    )

    message = texts.format_avrasya_has_debt_message("A123AA123", items)

    assert "Начисленные штрафы: 200 ₺" in message


def test_thousands_separator_uses_space_not_comma():
    items = (_item(principal=780.0, total=2580.0, penalty=1800.0),)

    message = texts.format_avrasya_has_debt_message("A123AA123", items)

    assert "2 580 ₺" in message
    assert "2,580" not in message


def test_never_renders_masked_or_internal_fields():
    """См. задачу: явный запрет на ExitDate/ExitStation/DebtorContactFullName/
    UniqueId/сырой JSON — AvrasyaDebtItem структурно их не несёт (см.
    test_turkey_avrasya_parser.py), здесь дополнительно проверяется, что
    рендер тоже ничего такого не печатает, даже случайно через repr()."""
    items = (_item(principal=330.0, total=330.0, service_file_type="EARLY_COLLECTION_FILE"),)

    message = texts.format_avrasya_has_debt_message("M295YB196", items)

    for forbidden in ("ExitDate", "ExitStation", "DebtorContactFullName", "UniqueId", "IsAuthenticate", "*"):
        assert forbidden not in message


def test_never_renders_service_file_type_as_legal_terminology():
    """См. задачу: "Do not translate EARLY_COLLECTION_FILE /
    COLLECTION_FILE into user-facing legal terminology yet" — они не
    должны появляться в тексте пользователю ВООБЩЕ (ни как есть, ни
    "переведённые"), пока их точный смысл не подтверждён."""
    items = (
        _item(principal=100.0, total=100.0, service_file_type="EARLY_COLLECTION_FILE"),
        _item(principal=100.0, total=100.0, service_file_type="COLLECTION_FILE"),
    )

    message = texts.format_avrasya_has_debt_message("A123AA123", items)

    assert "EARLY_COLLECTION_FILE" not in message
    assert "COLLECTION_FILE" not in message


def test_whole_amounts_have_no_decimal_point():
    """См. задачу: пример показывает "780 ₺", не "780.00 ₺"."""
    items = (_item(principal=780.0, total=780.0),)

    message = texts.format_avrasya_has_debt_message("A123AA123", items)

    assert "780 ₺" in message
    assert "780.00" not in message
    assert "780,00" not in message
