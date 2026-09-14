"""
Тесты reader/turkey_bot/validation.py::normalize_plate — намеренно лёгкая
проверка (см. design report Stage 3: "не гадать полную грамматику турецких
номеров"), отсеивает только заведомый мусор.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.validation import normalize_plate  # noqa: E402


def test_accepts_plain_plate():
    assert normalize_plate("34ABC123") == "34ABC123"


def test_lowercases_input_is_uppercased():
    assert normalize_plate("34abc123") == "34ABC123"


def test_strips_surrounding_whitespace():
    assert normalize_plate("  34ABC123  ") == "34ABC123"


def test_removes_internal_spaces():
    assert normalize_plate("34 ABC 123") == "34ABC123"


def test_removes_dashes():
    assert normalize_plate("34-ABC-123") == "34ABC123"


def test_rejects_empty_string():
    assert normalize_plate("") is None


def test_rejects_whitespace_only():
    assert normalize_plate("   ") is None


def test_rejects_too_short():
    assert normalize_plate("A1") is None


def test_rejects_too_long():
    assert normalize_plate("A" * 20) is None


def test_rejects_non_latin_characters():
    assert normalize_plate("34АВС123") is None  # кириллица, не латиница


def test_rejects_special_characters_other_than_space_and_dash():
    assert normalize_plate("34ABC@123") is None
