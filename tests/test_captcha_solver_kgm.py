"""Тесты математического решателя KGM CAPTCHA
(reader/turkey_bot_test/captcha_solver.py::CaptchaSolver.solve_kgm_captcha/
_parse_kgm_expression) — полностью локальные, без HTTP/submit."""

import io

from PIL import Image, ImageDraw, ImageFont

from reader.turkey_bot_test.captcha_solver import CaptchaSolver


def test_parse_addition_expression():
    assert CaptchaSolver._parse_kgm_expression("16+6 03713") == "22 03713"


def test_parse_subtraction_expression():
    assert CaptchaSolver._parse_kgm_expression("20-11 17341") == "9 17341"


def test_parse_multiplication_expression_x_letter():
    assert CaptchaSolver._parse_kgm_expression("7x7 82245") == "49 82245"


def test_parse_multiplication_expression_asterisk():
    assert CaptchaSolver._parse_kgm_expression("18*9 33324") == "162 33324"


def test_parse_division_expression_exact():
    assert CaptchaSolver._parse_kgm_expression("20/4 17341") == "5 17341"


def test_parse_division_expression_inexact_returns_none():
    # 20/3 не целое — небезопасно гадать формат дробного ответа
    assert CaptchaSolver._parse_kgm_expression("20/3 17341") is None


def test_parse_division_by_zero_returns_none():
    assert CaptchaSolver._parse_kgm_expression("20/0 17341") is None


def test_parse_no_expression_returns_none():
    assert CaptchaSolver._parse_kgm_expression("hello world") is None


def test_parse_expression_without_tail_returns_none():
    assert CaptchaSolver._parse_kgm_expression("16+6") is None


def test_parse_tolerates_ocr_noise_around_expression():
    # реальный OCR иногда добавляет мусорные символы вокруг цифр/знака
    assert CaptchaSolver._parse_kgm_expression("*16+6* 03713") == "22 03713"


def test_solve_kgm_captcha_end_to_end_with_rapidocr(monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "rapidocr")
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    img = Image.new("RGB", (300, 80), color="white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 20), "5+3 999", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    result = CaptchaSolver.solve_kgm_captcha(buf.getvalue())

    assert result is not None
    assert result.startswith("8 ")
    assert "999" in result
