"""Универсальная обёртка над RapidOCR (движок на onnxruntime, модели —
PP-OCRv6, изначально обученные в PaddleOCR и сконвертированные в ONNX) —
использована ВМЕСТО PaddleOCR/PaddlePaddle, т.к. PaddlePaddle не публикует
колёса под текущий Python (см. отчёт диагностики совместимости: `pip
install paddlepaddle` не находит ни одной версии для cp314-win_amd64ы,
последний релиз 3.3.1 поддерживает только cp39-cp313). RapidOCR даёт те же
предобученные модели без зависимости от фреймворка paddle.

Универсальный OCR-инструмент (см. reader/turkey_bot/ocr/
rapid_ocr_diagnostic.py — offline-оценка качества распознавания на
обычном тексте). Также используется как один из движков
CaptchaSolver (reader/turkey_bot/captcha_solver.py, включается
через OCR_ENGINE=rapidocr) для автоматического решения CAPTCHA
GIB/Avrasya/KGM в reader/turkey_bot/conversation.py — см. design
report про _MAX_AUTO_CAPTCHA_ATTEMPTS и явное решение продолжать
автоматический обход CAPTCHA в этом экспериментальном test-clone.

reader/turkey_bot -> НЕ трогается, все ONNX-модели идут в комплекте с
пакетом rapidocr (см. requirements.txt) — сетевых запросов при инференсе
не требуется.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OcrResult:
    text: str
    confidence: float


class RapidOcrService:
    """Ленивая инициализация движка — тяжёлая загрузка ONNX-моделей
    происходит при первом вызове recognize(), а не при создании сервиса
    (нужно, чтобы честно измерить cold-start отдельно от времени
    импорта/конструирования, см. rapid_ocr_diagnostic.py). Движок
    создаётся один раз и переиспользуется всеми последующими вызовами
    recognize() на этом же экземпляре сервиса."""

    def __init__(self) -> None:
        self._engine = None

    def _get_engine(self):
        if self._engine is None:
            from rapidocr import RapidOCR

            # См. задачу "reduce Turkey OCR memory pressure" — READ-ONLY
            # диагностика production OOM (turkeybot, ~2.9 GB peak на 3.7
            # GiB VPS без swap) установила: RapidOCR — process-wide
            # singleton (см. докстрок класса), поэтому его onnxruntime
            # config.yaml по умолчанию отдаёт enable_cpu_mem_arena: false —
            # КАЖДЫЙ session.run() (до 35 CAPTCHA-попыток × 3 провайдера,
            # повторяется на каждый check за весь lifetime процесса) идёт
            # через голый OS/CRT allocator вместо переиспользуемого arena,
            # что фрагментирует и раздувает heap процесса без единого
            # "утёкшего" Python-объекта. Единственный override — ТОЛЬКО
            # этот один ключ (params, не отдельный config-файл — apply
            # НАД пакетным default'ом, см. rapidocr.RapidOCR.__init__ /
            # ParseParams.update_batch) — модели/thresholds/preprocessing/
            # распознавание и CAPTCHA retry-логика этим не затронуты вовсе.
            self._engine = RapidOCR(
                params={"EngineConfig.onnxruntime.enable_cpu_mem_arena": True},
            )
        return self._engine

    def recognize(self, image: str | Path | bytes | np.ndarray) -> list[OcrResult]:
        """image — путь к файлу, bytes с содержимым PNG/JPEG, Path, либо
        уже декодированный numpy-массив (например, после собственного
        OpenCV-препроцессинга, см. CaptchaSolver._strip_kgm_background_noise).
        Любая ошибка (повреждённое изображение, сбой инференса и т.п.)
        логируется и возвращает пустой список — вызывающий код никогда
        не падает из-за проблем в этом движке."""
        try:
            engine = self._get_engine()
            output = engine(image)
        except Exception:
            logger.exception("RapidOCR: recognize() failed, returning empty result")
            return []

        if output.txts is None or output.scores is None:
            return []

        return [
            OcrResult(text=text, confidence=float(score))
            for text, score in zip(output.txts, output.scores)
        ]
