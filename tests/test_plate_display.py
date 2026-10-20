"""Тесты reader.plate_display.format_plate_for_telegram (задача "plate
readability improvement") — DISPLAY ONLY, см. модульный докстрок."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.plate_display import format_plate_for_telegram


def test_turkey_example_m606om35_markdown():
    assert format_plate_for_telegram("M606OM35") == "**M**606**OM**35"


def test_turkey_example_m6060m35_only_isolated_letters_bold():
    assert format_plate_for_telegram("M6060M35") == "**M**6060**M**35"


def test_georgia_example_p354hb190_markdown():
    assert format_plate_for_telegram("P354HB190") == "**P**354**HB**190"


def test_all_digits_unchanged():
    assert format_plate_for_telegram("123456") == "123456"


def test_all_letters_fully_bold_as_one_run():
    assert format_plate_for_telegram("ABCDEF") == "**ABCDEF**"


def test_empty_string_unchanged():
    assert format_plate_for_telegram("") == ""


def test_markdown_is_default_mode():
    assert format_plate_for_telegram("M606OM35") == format_plate_for_telegram("M606OM35", mode="markdown")


def test_html_mode_wraps_letters_in_bold_tag():
    assert format_plate_for_telegram("M606OM35", mode="html") == "<b>M</b>606<b>OM</b>35"


def test_html_mode_escapes_special_characters_safely():
    # Не настоящий номер — проверка defensive-поведения на произвольном
    # входе (см. задачу TESTS п.6), а не реального car_number.
    assert format_plate_for_telegram("A<B>&C", mode="html") == "<b>A</b>&lt;<b>B</b>&gt;&amp;<b>C</b>"


def test_markdown_mode_does_not_crash_on_non_canonical_input():
    # Канонический car_number — всегда [A-Z0-9] (см. normalize_car_number/
    # normalize_plate), но formatter сам по себе не должен падать/терять
    # символы на произвольном входе.
    assert format_plate_for_telegram("ab-12 cd") == "**ab**-12 **cd**"


def test_formatter_does_not_mutate_value_semantics():
    # Убрать разметку и получить оригинал обратно — должно совпасть с
    # исходной строкой (никакого upper()/strip()/перевода символов).
    raw = "m6O6om35"
    formatted = format_plate_for_telegram(raw)
    assert formatted.replace("**", "") == raw
