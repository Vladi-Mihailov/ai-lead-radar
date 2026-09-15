"""
Тесты reader/turkey_bot/validation.py::normalize_plate — намеренно лёгкая
проверка (см. design report Stage 3: "не гадать полную грамматику
номеров"), отсеивает только заведомый мусор. Целевая аудитория —
русскоязычные водители с российскими номерами (см. задачу про
А123АА123/A123AA123) — поэтому поддерживаются и кириллица, и латиница.
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


def test_rejects_special_characters_other_than_space_and_dash():
    assert normalize_plate("34ABC@123") is None


# ---- Российские номера: кириллица -> латиница (см. задачу) ----


def test_cyrillic_russian_plate_normalizes_to_latin_9_chars():
    assert normalize_plate("А123АА123") == "A123AA123"


def test_cyrillic_russian_plate_normalizes_to_latin_8_chars():
    assert normalize_plate("А123АА77") == "A123AA77"


def test_latin_russian_style_plate_is_unchanged():
    assert normalize_plate("A123AA123") == "A123AA123"


def test_lowercase_cyrillic_input_is_uppercased_and_translated():
    assert normalize_plate("а123аа123") == "A123AA123"


def test_cyrillic_plate_with_spaces_and_hyphens_is_normalized():
    assert normalize_plate("А 123 АА 123") == "A123AA123"
    assert normalize_plate("А-123-АА-123") == "A123AA123"


def test_all_twelve_supported_cyrillic_letters_translate_correctly():
    # А В Е К М Н -> A B E K M H, О Р С Т У Х -> O P C T Y X (см. задачу).
    # Разбито на 2 строки по 6 букв, чтобы уместиться в допустимую длину
    # номера (5-9 символов) — сама 12-буквенная таблица проверяется целиком.
    assert normalize_plate("АВЕКМН") == "ABEKMH"
    assert normalize_plate("ОРСТУХ") == "OPCTYX"


def test_unsupported_cyrillic_letter_is_rejected():
    """Только 12 конкретных букв (см. задачу: "Do NOT convert arbitrary
    Cyrillic letters") — любая другая кириллическая буква остаётся
    кириллицей после .translate() и не проходит [A-Z0-9]."""
    assert normalize_plate("Б123АА123") is None
    assert normalize_plate("Ф123АА123") is None
    assert normalize_plate("Щ123АА123") is None
