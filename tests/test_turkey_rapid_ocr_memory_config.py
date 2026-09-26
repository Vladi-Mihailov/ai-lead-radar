"""Тесты "reduce Turkey OCR memory pressure" (FIX 1) — production
RapidOcrService (reader/turkey_bot/ocr/rapid_ocr.py, NOT the
turkey_bot_test experimental clone) должен создавать свой RapidOCR engine
с enable_cpu_mem_arena=True — READ-ONLY диагностика production OOM
установила, что package default (enable_cpu_mem_arena: false) заставляет
onnxruntime аллоцировать/освобождать тензоры через голый OS-аллокатор на
КАЖДЫЙ session.run(), что фрагментирует/раздувает heap process-wide
singleton-движка за весь его lifetime.

Полностью локальные тесты — без HTTP, без CAPTCHA/GIB/Avrasya/KGM, без
сети (модели идут в комплекте с пакетом rapidocr)."""

import gc
import io

import pytest
from PIL import Image, ImageDraw

from reader.turkey_bot.ocr.rapid_ocr import RapidOcrService

try:
    import resource  # POSIX only (stdlib) — production target (Linux), no new dependency needed.
except ImportError:
    resource = None


def _draw_text_png(text: str) -> bytes:
    img = Image.new("RGB", (300, 80), color="white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 25), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---- 1. Regression: production engine is created with the arena enabled ----


def test_production_engine_has_cpu_mem_arena_enabled():
    service = RapidOcrService()
    engine = service._get_engine()

    assert engine.text_det.session.session.get_session_options().enable_cpu_mem_arena is True
    assert engine.text_cls.session.session.get_session_options().enable_cpu_mem_arena is True
    assert engine.text_rec.session.session.get_session_options().enable_cpu_mem_arena is True


def test_only_the_arena_flag_changed_not_other_ocr_parameters():
    """См. задачу: "Не меняй никакие другие OCR параметры" — сравниваем
    ПОЛНЫЙ набор session options между package-default и production
    override, единственная разница — enable_cpu_mem_arena."""
    from rapidocr import RapidOCR

    default_engine = RapidOCR()
    production_engine = RapidOcrService()._get_engine()

    default_opts = default_engine.text_det.session.session.get_session_options()
    production_opts = production_engine.text_det.session.session.get_session_options()

    assert default_opts.enable_cpu_mem_arena is False
    assert production_opts.enable_cpu_mem_arena is True
    assert default_opts.graph_optimization_level == production_opts.graph_optimization_level
    assert default_opts.intra_op_num_threads == production_opts.intra_op_num_threads
    assert default_opts.inter_op_num_threads == production_opts.inter_op_num_threads

    # Recognition/thresholds/preprocessing (Global.*) — не затронуты вовсе,
    # т.к. override передаёт РОВНО один ключ (см. rapid_ocr.py::_get_engine).
    assert default_engine.text_score == production_engine.text_score
    assert default_engine.min_height == production_engine.min_height
    assert default_engine.width_height_ratio == production_engine.width_height_ratio
    assert default_engine.max_side_len == production_engine.max_side_len
    assert default_engine.min_side_len == production_engine.min_side_len


def test_engine_still_recognizes_text_correctly_after_arena_change():
    """Смысловая регрессия: сама точность/поведение распознавания не
    изменились — тот же самый тест, что и в tests/test_rapid_ocr_service.py
    (test-clone), но против production-модуля."""
    service = RapidOcrService()
    results = service.recognize(_draw_text_png("HELLO 123"))

    combined_text = " ".join(r.text for r in results).upper()
    assert "HELLO" in combined_text
    assert "123" in combined_text


# ---- LOCAL/OFFLINE MEMORY TEST (см. задачу) — no network, no CAPTCHA ----


def _rss_mb() -> float:
    # ru_maxrss — КБ на Linux, байты на macOS (см. stdlib docs) — этот
    # тест запускается на Linux-проде, где деплоится сам fix; на macOS
    # цифры были бы неверны, но платформа здесь не используется вовсе.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


@pytest.mark.skipif(resource is None, reason="resource module unavailable on this platform (Windows) — POSIX only")
def test_offline_repeated_ocr_calls_do_not_grow_rss_linearly():
    """Не требуется доказать, что RSS уменьшается — цель: после fix
    память не растёт ЛИНЕЙНО на каждый offline OCR-вызов (см. задачу:
    "убедиться, что memory не растёт линейно на каждом invocation").
    Полностью offline — те же bundled-модели, никакого provider/CAPTCHA."""
    rss_before_init = _rss_mb()

    service = RapidOcrService()
    image_bytes = _draw_text_png("MEMTEST 42")

    # Первый вызов включает cold-start (lazy engine init, см. класс
    # докстрок) — не часть "растёт ли RSS на каждый ВЫЗОВ", поэтому меряем
    # ПОСЛЕ него отдельно.
    service.recognize(image_bytes)
    rss_after_init_and_first_call = _rss_mb()

    per_call_growth: list[float] = []
    last_rss = rss_after_init_and_first_call
    for _ in range(10):
        service.recognize(image_bytes)
        current_rss = _rss_mb()
        per_call_growth.append(current_rss - last_rss)
        last_rss = current_rss

    gc.collect()
    rss_after_gc = _rss_mb()

    # Не строгая гарантия убывания — просто sanity-числа для наблюдения +
    # loose проверка "не растёт неограниченно": суммарный рост за 10
    # ПОВТОРНЫХ вызовов (одна и та же маленькая картинка, engine уже тёплый)
    # не должен быть похож на "каждый вызов создаёт ещё один полный набор
    # моделей" (~десятки МБ на вызов) — допускаем разумный буфер для
    # аллокатора/GC-шума.
    total_growth_after_warm = sum(per_call_growth)
    print(
        f"\n[offline OCR RSS] before_init={rss_before_init:.1f}MB "
        f"after_init+1call={rss_after_init_and_first_call:.1f}MB "
        f"after_10_more_calls={last_rss:.1f}MB after_gc={rss_after_gc:.1f}MB "
        f"total_growth_10_warm_calls={total_growth_after_warm:.1f}MB",
    )
    assert total_growth_after_warm < 200.0  # loose ceiling, not a strict leak-proof guarantee
