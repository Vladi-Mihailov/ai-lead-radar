import io
import logging
import os
import re

import cv2
import numpy as np
import pytesseract
from PIL import Image

from reader.turkey_bot_test.ocr.rapid_ocr import RapidOcrService

logger = logging.getLogger(__name__)

_VALID_ENGINES = ("tesseract", "rapidocr")
_DEFAULT_ENGINE = "tesseract"

# KGM CAPTCHA — не случайный код, а математическая задача + хвостовой
# код, например "16+6 03713" (операнды до 2 цифр — по образцам, реально
# увиденным в проде: 16+6, 7+7, 18+9, 20+11). Знак операции — второе
# "значение": +/-/×/÷ в разных начертаниях OCR.
_KGM_EXPRESSION_RE = re.compile(r"(\d{1,2})\s*([+\-xX×÷*/])\s*(\d{1,2})")

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

    @staticmethod
    def solve_kgm_captcha(captcha_img_bytes: bytes) -> str | None:
        """KGM CAPTCHA имеет ДРУГОЙ формат, чем GIB/Avrasya — это не
        случайный код, а математическая задача + хвостовой код, например
        "16+6 03713": первое и третье распознанные значения — операнды,
        второе — знак операции (+/-/×/÷), результат вычисляется и
        записывается вместо этих трёх значений, затем через пробел —
        все остальные распознанные значения ("22 03713"). Использует тот
        же OCR_ENGINE, что и solve_captcha(), но БЕЗ alnum-фильтрации на
        входе — оператор не буква/цифра."""
        engine = _read_ocr_engine()
        try:
            if engine == "rapidocr":
                raw_text = CaptchaSolver._raw_text_rapidocr(captcha_img_bytes)
            else:
                raw_text = CaptchaSolver._raw_text_tesseract_kgm(captcha_img_bytes)
        except Exception as e:
            logger.error(f"Ошибка OCR при решении KGM CAPTCHA: {e}")
            return None

        return CaptchaSolver._parse_kgm_expression(raw_text)

    @staticmethod
    def _strip_kgm_background_noise(captcha_img_bytes: bytes) -> np.ndarray:
        """KGM CAPTCHA заливает фон плотным оранжевым/жёлтым шумом (в
        отличие от GIB/Avrasya) — обычный OCR без препроцессинга на нём
        почти не читает текст (см. диагностику). Текст всегда тёмный
        (низкая V в HSV), шум — светлее и/или насыщенный оранжевый:
        порог по V отделяет текст от фона намного надёжнее, чем
        grayscale/adaptiveThreshold (испробованные для GIB — там они
        только вредили)."""
        arr = np.frombuffer(captcha_img_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        value_channel = hsv[:, :, 2]
        text_mask = value_channel < 120
        result = np.full_like(img, 255)
        result[text_mask] = [0, 0, 0]
        return result

    @staticmethod
    def _raw_text_rapidocr(captcha_img_bytes: bytes) -> str:
        service = _get_rapid_ocr_service()
        processed = CaptchaSolver._strip_kgm_background_noise(captcha_img_bytes)
        results = service.recognize(processed)
        return " ".join(r.text for r in results)

    @staticmethod
    def _raw_text_tesseract_kgm(captcha_img_bytes: bytes) -> str:
        processed = CaptchaSolver._strip_kgm_background_noise(captcha_img_bytes)
        return pytesseract.image_to_string(
            processed, config="--psm 7 -c tessedit_char_whitelist=0123456789+-x*÷×/",
        )

    @staticmethod
    def _parse_kgm_expression(raw_text: str) -> str | None:
        match = _KGM_EXPRESSION_RE.search(raw_text)
        if not match:
            logger.info("KGM CAPTCHA: не удалось найти выражение в распознанном тексте %r", raw_text)
            return None

        left_str, op, right_str = match.groups()
        left, right = int(left_str), int(right_str)

        if op == "+":
            result = left + right
        elif op == "-":
            result = left - right
        elif op in ("x", "X", "×", "*"):
            result = left * right
        elif op in ("/", "÷"):
            if right == 0 or left % right != 0:
                logger.info("KGM CAPTCHA: деление %s/%s не даёт целого результата", left, right)
                return None
            result = left // right
        else:
            return None

        tail = raw_text[match.end():]
        tail_value = "".join(ch for ch in tail if ch.isalnum())
        if not tail_value:
            logger.info("KGM CAPTCHA: не найден хвостовой код после выражения в %r", raw_text)
            return None

        code = f"{result} {tail_value}"
        logger.info(f"Решена KGM CAPTCHA: {code!r}")
        return code
