"""Тесты extract_car_numbers() — распознавание и нормализация российских
автомобильных госномеров (A111AA111: буква, 3 цифры, 2 буквы, регион 2
или 3 цифры), см. reader/users/car_numbers.py."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.users.car_numbers import extract_car_numbers  # noqa: E402


# ---- позитивные варианты написания одного и того же номера ----


def test_extracts_plain_cyrillic_uppercase():
    assert extract_car_numbers("А111АА111") == ["A111AA111"]


def test_extracts_plain_latin_uppercase():
    assert extract_car_numbers("A111AA77") == ["A111AA77"]


def test_extracts_lowercase_mixed_with_spaces():
    assert extract_car_numbers("а 123 вс 77") == ["A123BC77"]


def test_extracts_with_hyphen_separators():
    assert extract_car_numbers("Х-777-ХХ-197") == ["X777XX197"]


def test_extracts_mixed_cyrillic_and_latin_letters():
    assert extract_car_numbers("А111AA111") == ["A111AA111"]


def test_extracts_with_space_separators_two_digit_region():
    assert extract_car_numbers("А 111 АА 77") == ["A111AA77"]


def test_extracts_lowercase_no_separators():
    assert extract_car_numbers("х777хх197") == ["X777XX197"]


# ---- негативные случаи ----


def test_rejects_letter_outside_allowed_set():
    assert extract_car_numbers("Z111ZZ77") == []


def test_rejects_region_with_single_digit():
    assert extract_car_numbers("A123AB1") == []


def test_rejects_region_with_four_digits():
    assert extract_car_numbers("A123AB1234") == []


def test_rejects_digits_only():
    assert extract_car_numbers("123456789") == []


def test_does_not_extract_as_substring_of_longer_alnum_sequence():
    assert extract_car_numbers("abcA123BC77xyz") == []


def test_empty_text_returns_empty_list():
    assert extract_car_numbers("") == []


def test_text_without_any_plate_returns_empty_list():
    assert extract_car_numbers("привет, как дела? встретимся в 18:00") == []


# ---- несколько номеров / дедупликация / детерминированный порядок ----


def test_multiple_plates_in_one_message():
    text = "Видел А111АА77 и Х777ХХ197 на парковке"
    assert extract_car_numbers(text) == ["A111AA77", "X777XX197"]


def test_same_plate_in_cyrillic_and_latin_variants_deduplicates_to_one_value():
    text = "номер А111АА77, а не A111AA77 или а111аа77"
    assert extract_car_numbers(text) == ["A111AA77"]


def test_result_is_sorted_regardless_of_appearance_order():
    text = "сначала Х777ХХ197, потом А111АА77"
    assert extract_car_numbers(text) == ["A111AA77", "X777XX197"]
