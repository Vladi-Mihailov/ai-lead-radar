"""РАЗРАБОТЧЕСКИЙ диагностический инструмент, НЕ автотест (без префикса
test_) — полностью локальный, без HTTP-запросов и без CAPTCHA вообще.
Единственная цель: убедиться, что RapidOcrService реально работает на
этом окружении, и измерить cold-start (первый вызов, включает загрузку
ONNX-моделей) отдельно от повторного вызова (модели уже в памяти).

Использование:
    python -m reader.turkey_bot_test.ocr.rapid_ocr_diagnostic
"""

import io
import time

from PIL import Image, ImageDraw

from reader.turkey_bot_test.ocr.rapid_ocr import RapidOcrService

_TEST_TEXT = "Hello OCR 123"


def _make_test_image() -> bytes:
    img = Image.new("RGB", (300, 80), color="white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 25), _TEST_TEXT, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> None:
    image_bytes = _make_test_image()
    service = RapidOcrService()

    print(f"Test text drawn on image: {_TEST_TEXT!r}")
    print()

    start = time.perf_counter()
    cold_results = service.recognize(image_bytes)
    cold_elapsed = time.perf_counter() - start

    print("=== Cold-start run (первый вызов, включает загрузку ONNX-моделей) ===")
    for r in cold_results:
        print(f"  text={r.text!r} confidence={r.confidence:.4f}")
    print(f"  elapsed: {cold_elapsed:.3f}s")
    print()

    start = time.perf_counter()
    warm_results = service.recognize(image_bytes)
    warm_elapsed = time.perf_counter() - start

    print("=== Warm run (повторный вызов, модели уже загружены) ===")
    for r in warm_results:
        print(f"  text={r.text!r} confidence={r.confidence:.4f}")
    print(f"  elapsed: {warm_elapsed:.3f}s")
    print()

    print(f"Speedup (cold/warm): {cold_elapsed / warm_elapsed:.1f}x" if warm_elapsed else "n/a")


if __name__ == "__main__":
    main()
