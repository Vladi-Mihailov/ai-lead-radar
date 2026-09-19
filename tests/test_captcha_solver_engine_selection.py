"""Тесты выбора OCR-движка (OCR_ENGINE) в
reader/turkey_bot_test/captcha_solver.py::CaptchaSolver. Полностью
локальные — без HTTP, без реальных CAPTCHA/GIB/Avrasya/KGM."""

import io

import pytest
from PIL import Image, ImageDraw

from reader.turkey_bot_test import captcha_solver as captcha_solver_module
from reader.turkey_bot_test.captcha_solver import CaptchaSolver
from reader.turkey_bot_test.ocr.rapid_ocr import OcrResult


def _draw_text_png(text: str) -> bytes:
    img = Image.new("RGB", (200, 60), color="white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 15), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _reset_rapid_ocr_singleton(monkeypatch):
    """Каждый тест начинает с чистого module-level синглтона сервиса."""
    monkeypatch.setattr(captcha_solver_module, "_rapid_ocr_service", None)
    yield
    monkeypatch.setattr(captcha_solver_module, "_rapid_ocr_service", None)


# ---- 1. Выбор движка через конфиг (OCR_ENGINE) ----

def test_default_engine_is_tesseract_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("OCR_ENGINE", raising=False)
    calls = {"tesseract": 0, "rapidocr": 0}
    monkeypatch.setattr(CaptchaSolver, "_solve_with_tesseract", staticmethod(lambda b: calls.__setitem__("tesseract", calls["tesseract"] + 1) or "TESS"))
    monkeypatch.setattr(CaptchaSolver, "_solve_with_rapidocr", staticmethod(lambda b: calls.__setitem__("rapidocr", calls["rapidocr"] + 1) or "RAPID"))

    result = CaptchaSolver.solve_captcha(b"irrelevant")

    assert result == "TESS"
    assert calls == {"tesseract": 1, "rapidocr": 0}


def test_engine_switches_to_rapidocr_when_explicitly_set(monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "rapidocr")
    calls = {"tesseract": 0, "rapidocr": 0}
    monkeypatch.setattr(CaptchaSolver, "_solve_with_tesseract", staticmethod(lambda b: calls.__setitem__("tesseract", calls["tesseract"] + 1) or "TESS"))
    monkeypatch.setattr(CaptchaSolver, "_solve_with_rapidocr", staticmethod(lambda b: calls.__setitem__("rapidocr", calls["rapidocr"] + 1) or "RAPID"))

    result = CaptchaSolver.solve_captcha(b"irrelevant")

    assert result == "RAPID"
    assert calls == {"tesseract": 0, "rapidocr": 1}


def test_unrecognized_engine_value_falls_back_to_tesseract(monkeypatch, caplog):
    monkeypatch.setenv("OCR_ENGINE", "some_typo")
    calls = {"tesseract": 0, "rapidocr": 0}
    monkeypatch.setattr(CaptchaSolver, "_solve_with_tesseract", staticmethod(lambda b: calls.__setitem__("tesseract", calls["tesseract"] + 1) or "TESS"))
    monkeypatch.setattr(CaptchaSolver, "_solve_with_rapidocr", staticmethod(lambda b: calls.__setitem__("rapidocr", calls["rapidocr"] + 1) or "RAPID"))

    with caplog.at_level("WARNING"):
        result = CaptchaSolver.solve_captcha(b"irrelevant")

    assert result == "TESS"
    assert calls == {"tesseract": 1, "rapidocr": 0}
    assert "some_typo" in caplog.text


# ---- 2. RapidOCR initialization (singleton, переиспользуется) ----

def test_rapid_ocr_service_singleton_is_created_once_and_reused(monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "rapidocr")
    created = []

    class _FakeService:
        def __init__(self):
            created.append(self)

        def recognize(self, image):
            return [OcrResult(text="AB12", confidence=0.9)]

    monkeypatch.setattr(captcha_solver_module, "RapidOcrService", _FakeService)

    CaptchaSolver.solve_captcha(b"one")
    CaptchaSolver.solve_captcha(b"two")

    assert len(created) == 1  # движок создан один раз, а не при каждом вызове


# ---- 3. recognize success (реальная интеграция, без моков) ----

def test_solve_captcha_with_rapidocr_recognizes_real_synthetic_image(monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "rapidocr")
    image_bytes = _draw_text_png("XY9K3")

    result = CaptchaSolver.solve_captcha(image_bytes)

    assert result is not None
    assert result.upper() == "XY9K3"


# ---- 4. recognize failure — не должно ронять приложение ----

def test_solve_captcha_with_rapidocr_returns_none_on_engine_failure(monkeypatch, caplog):
    monkeypatch.setenv("OCR_ENGINE", "rapidocr")

    class _FailingService:
        def recognize(self, image):
            raise RuntimeError("simulated engine crash")

    monkeypatch.setattr(captcha_solver_module, "_rapid_ocr_service", _FailingService())

    with caplog.at_level("ERROR"):
        result = CaptchaSolver.solve_captcha(b"irrelevant")

    assert result is None
    assert "simulated engine crash" in caplog.text


# ---- 5. fallback/rollback: переключение обратно на tesseract без изменений кода ----

def test_switching_env_var_back_restores_tesseract_behavior(monkeypatch):
    calls = {"tesseract": 0, "rapidocr": 0}
    monkeypatch.setattr(CaptchaSolver, "_solve_with_tesseract", staticmethod(lambda b: calls.__setitem__("tesseract", calls["tesseract"] + 1) or "TESS"))
    monkeypatch.setattr(CaptchaSolver, "_solve_with_rapidocr", staticmethod(lambda b: calls.__setitem__("rapidocr", calls["rapidocr"] + 1) or "RAPID"))

    monkeypatch.setenv("OCR_ENGINE", "rapidocr")
    assert CaptchaSolver.solve_captcha(b"x") == "RAPID"

    monkeypatch.delenv("OCR_ENGINE", raising=False)  # rollback: просто убрать/поменять env var
    assert CaptchaSolver.solve_captcha(b"x") == "TESS"

    assert calls == {"tesseract": 1, "rapidocr": 1}
