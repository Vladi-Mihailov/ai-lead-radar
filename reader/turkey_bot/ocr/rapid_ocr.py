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

            # См. задачу "reduce Turkey OCR retained memory" — предыдущая
            # попытка (enable_cpu_mem_arena=True, задача "reduce Turkey
            # OCR memory pressure") была РЕВЕРТНУТА: контролируемый
            # offline A/B (см. отчёт) показал, что enable_cpu_mem_arena=
            # True удерживает ~600-640 MB sustained RSS после одинаковой
            # offline OCR-нагрузки против ~130-150 MB при False — арена
            # переиспользует блоки ВНУТРИ процесса, но НИКОГДА не
            # возвращает их ОС, а голый OS/CRT allocator (arena=False) для
            # крупных Det-тензоров (~19.5 MB, фиксированный [1,3,736,2208]
            # на КАЖДЫЙ вызов) реально освобождает страницы обратно ОС.
            # На 3.7 GiB VPS без swap это решающий фактор — явное False
            # (а не просто "не передавать params" = package default),
            # чтобы поведение не зависело от будущего изменения
            # package-default значения при апдейте rapidocr. Единственный
            # override — ТОЛЬКО этот один ключ (params, не отдельный
            # config-файл — apply НАД пакетным default'ом, см.
            # rapidocr.RapidOCR.__init__ / ParseParams.update_batch) —
            # модели/thresholds/preprocessing/распознавание и CAPTCHA
            # retry-логика этим не затронуты вовсе.
            self._engine = RapidOCR(
                params={"EngineConfig.onnxruntime.enable_cpu_mem_arena": False},
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
