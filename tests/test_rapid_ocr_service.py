"""Unit-тесты RapidOcrService (reader/turkey_bot_test/ocr/rapid_ocr.py) —
полностью локальные, без HTTP, без CAPTCHA/GIB/Avrasya/KGM. Используют
синтетические изображения с текстом, сгенерированные PIL в памяти."""

import io

from PIL import Image, ImageDraw

from reader.turkey_bot_test.ocr.rapid_ocr import OcrResult, RapidOcrService


def _draw_text_png(text: str) -> bytes:
    img = Image.new("RGB", (300, 80), color="white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 25), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_recognize_returns_ocr_results_with_text_and_confidence():
    service = RapidOcrService()
    image_bytes = _draw_text_png("HELLO 123")

    results = service.recognize(image_bytes)

    assert len(results) >= 1
    assert all(isinstance(r, OcrResult) for r in results)
    assert all(isinstance(r.text, str) and r.text for r in results)
    assert all(0.0 <= r.confidence <= 1.0 for r in results)

    combined_text = " ".join(r.text for r in results).upper()
    assert "HELLO" in combined_text
    assert "123" in combined_text


def test_recognize_on_blank_image_returns_empty_list():
    service = RapidOcrService()
    blank = Image.new("RGB", (100, 40), color="white")
    buf = io.BytesIO()
    blank.save(buf, format="PNG")

    results = service.recognize(buf.getvalue())

    assert results == []


def test_engine_is_lazily_initialized_and_reused():
    service = RapidOcrService()
    assert service._engine is None

    service.recognize(_draw_text_png("REUSE"))
    engine_after_first_call = service._engine
    assert engine_after_first_call is not None

    service.recognize(_draw_text_png("REUSE"))
    assert service._engine is engine_after_first_call
