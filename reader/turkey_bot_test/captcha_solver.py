import io
import logging
import os

import cv2
import numpy as np
import pytesseract
from PIL import Image

from reader.turkey_bot_test.ocr.rapid_ocr import RapidOcrService

logger = logging.getLogger(__name__)

_VALID_ENGINES = ("tesseract", "rapidocr")
_DEFAULT_ENGINE = "tesseract"

_rapid_ocr_service: RapidOcrService | None = None


def _get_rapid_ocr_service() -> RapidOcrService:
    """Один экземпляр RapidOcrService на процесс — модели загружаются один
    раз при первом обращении и переиспользуются всеми последующими
    вызовами (см. RapidOcrService — ленивая инициализация движка внутри
    самого сервиса)."""
    global _rapid_ocr_service
    if _rapid_ocr_service is None:
        _rapid_ocr_service = RapidOcrService()
    return _rapid_ocr_service


def _read_ocr_engine() -> str:
    """OCR_ENGINE читается заново при каждом вызове (не кэшируется на
    импорте) — можно переключать движок через .env/EnvironmentFile без
    пересборки кода. По умолчанию (переменная не задана ИЛИ содержит
    нераспознанное значение) — 'tesseract', то есть ТЕКУЩЕЕ
    production-поведение не меняется, пока кто-то явно не укажет
    OCR_ENGINE=rapidocr."""
    engine = os.environ.get("OCR_ENGINE", _DEFAULT_ENGINE).strip().lower()
    if engine not in _VALID_ENGINES:
        logger.warning(
            "OCR_ENGINE=%r не распознан (ожидается один из %s), используется %r",
            engine, _VALID_ENGINES, _DEFAULT_ENGINE,
        )
        return _DEFAULT_ENGINE
    return engine


class CaptchaSolver:
    """Класс для автоматического распознавания капчи"""

    @staticmethod
    def solve_captcha(captcha_img_bytes: bytes) -> str | None:
        """Распознает капчу для всех провайдеров (универсальный метод).
        Движок выбирается через OCR_ENGINE (tesseract — по умолчанию,
        rapidocr — только явным включением, см. _read_ocr_engine)."""
        engine = _read_ocr_engine()
        if engine == "rapidocr":
            return CaptchaSolver._solve_with_rapidocr(captcha_img_bytes)
        return CaptchaSolver._solve_with_tesseract(captcha_img_bytes)

    @staticmethod
    def _solve_with_tesseract(captcha_img_bytes: bytes) -> str | None:
        """ТЕКУЩАЯ (неизменённая) реализация — CLAHE + adaptive threshold +
        morphology close, whitelist A-Z0-9, --psm 8. Это то же самое
        поведение, что было production-поведением ДО добавления
        OCR_ENGINE — трогать этот метод не входит в задачу переключения
        движка."""
        try:
            # Конвертируем байты в изображение PIL
            img = Image.open(io.BytesIO(captcha_img_bytes))

            # Конвертируем в numpy array для обработки OpenCV
            img_array = np.array(img)

            # Если изображение цветное (3 канала), конвертируем в градации серого
            if len(img_array.shape) == 3:
                gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
            else:
                gray = img_array

            # Увеличиваем контрастность
            clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
            enhanced = clahe.apply(gray)

            # Применяем адаптивное пороговое значение
            thresh = cv2.adaptiveThreshold(
                enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY_INV, 11, 2
            )

            # Удаляем шум
            kernel = np.ones((2, 2), np.uint8)
            processed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

            # Распознаем текст с помощью Tesseract
            # Ограничиваем символы только буквами и цифрами
            text = pytesseract.image_to_string(
                processed,
                config='--psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
            ).strip().upper()

            # Фильтруем результат - оставляем только буквы и цифры
            filtered_text = ''.join(filter(str.isalnum, text))

            # Проверяем, что результат не пустой и имеет разумную длину (обычно 4-6 символов)
            if filtered_text and 4 <= len(filtered_text) <= 6:
                logger.info(f"Распознана капча (tesseract): {filtered_text}")
                return filtered_text

            return None

        except Exception as e:
            logger.error(f"Ошибка при распознавании капчи (tesseract): {e}")
            return None

    @staticmethod
    def _solve_with_rapidocr(captcha_img_bytes: bytes) -> str | None:
        """Экспериментальный путь через RapidOcrService — включается
        ТОЛЬКО через OCR_ENGINE=rapidocr. Без собственного OpenCV
        preprocessing (RapidOCR получает исходное изображение как есть,
        см. reader/turkey_bot_test/ocr/rapid_ocr.py)."""
        try:
            service = _get_rapid_ocr_service()
            results = service.recognize(captcha_img_bytes)
            # НЕ .upper() — реальные GIB CAPTCHA строчные (см. вручную
            # размеченные образцы в reader/turkey_bot_test/ocr_samples/),
            # а проверка кода на сервере, вероятно, регистрозависима.
            text = "".join(r.text for r in results)
            filtered_text = ''.join(filter(str.isalnum, text))

            if filtered_text and 4 <= len(filtered_text) <= 6:
                logger.info(f"Распознана капча (rapidocr): {filtered_text}")
                return filtered_text

            return None

        except Exception as e:
            logger.error(f"Ошибка при распознавании капчи (rapidocr): {e}")
            return None
