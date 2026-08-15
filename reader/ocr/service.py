import asyncio
import base64
import logging

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
    RateLimitError,
)
from pydantic import BaseModel

from reader.ocr.models import OcrResult
from reader.ocr.prompt import SYSTEM_PROMPT, USER_TEXT

logger = logging.getLogger(__name__)

# По аналогии с auto-insurance (app/ocr/provider.py) — один ретрай, только
# для транзиентных ошибок/5xx, без экспоненциального backoff.
_MAX_RETRIES = 1
_RETRY_DELAY_SECONDS = 1.0

_ALLOWED_MIME_TYPES = ("image/jpeg", "image/png", "image/webp")


class OcrServiceError(Exception):
    """Любой сбой распознавания (сеть/API/некорректный ответ модели) —
    вызывающий код (см. reader/commands/insurance_ocr.py) показывает
    оператору только generic-сообщение и НЕ логирует str(exc)/тело
    ответа/содержимое документа (см. задачу)."""


class _VehicleFieldsSchema(BaseModel):
    """Wire-schema для OpenAI Structured Outputs — по аналогии с
    auto-insurance (app/ocr/provider.py::_VehicleFieldsSchema), плюс
    full_name (см. reader/ocr/models.py::OcrResult и reader/ocr/prompt.py
    про сознательное отличие от auto-insurance). Не добавлять сюда поле,
    не обновив одновременно prompt.py и OcrResult — schema (в strict-режиме
    Structured Outputs) это единственный механизм, ограничивающий, что
    вообще может вернуть модель."""

    registration_number: str | None
    vin: str | None
    chassis_number: str | None
    manufacturer: str | None
    model: str | None
    full_name: str | None


class OcrService:
    """Распознавание документов через OpenAI Responses API (Structured
    Outputs, text_format=<pydantic model>) — тот же подход, что и
    auto-insurance/app/ocr/provider.py::OpenAIVisionOcrProvider, но:
    - AsyncOpenAI вместо синхронного OpenAI: ai-lead-radar работает в одном
      asyncio event loop (Telethon + scheduler + command dispatcher'ы, см.
      reader/main.py) — синхронный сетевой вызов застопорил бы весь процесс
      на время ответа OpenAI, в отличие от auto-insurance (FastAPI, sync-
      route в threadpool);
    - произвольное число изображений за один вызов (auto-insurance — всегда
      ровно одно) — нужно для Telegram-альбомов (см. задачу).

    Ничего не импортируется из auto-insurance — независимая реализация."""

    def __init__(self, *, api_key: str, model: str):
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def extract(self, images: list[tuple[bytes, str]]) -> OcrResult:
        """images — список (bytes, mime_type), непустой (проверяется
        вызывающим кодом — reader/commands/insurance_ocr.py решает, что
        показать оператору, если изображений нет вовсе). Работает только с
        байтами в памяти — ни один temp-файл здесь не создаётся (см. задачу)."""
        content: list[dict] = [{"type": "input_text", "text": USER_TEXT}]
        for data, mime_type in images:
            media_type = mime_type if mime_type in _ALLOWED_MIME_TYPES else "image/jpeg"
            image_b64 = base64.b64encode(data).decode("ascii")
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{media_type};base64,{image_b64}",
                    "detail": "auto",
                }
            )

        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.responses.parse(
                    model=self._model,
                    instructions=SYSTEM_PROMPT,
                    input=[{"role": "user", "content": content}],
                    text_format=_VehicleFieldsSchema,
                )
                return self._to_result(response)
            except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
                if attempt > _MAX_RETRIES:
                    logger.warning(
                        "OCR: транзиентная ошибка после ретрая (%s)", type(exc).__name__,
                    )
                    raise OcrServiceError("transient failure after retry") from exc
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                continue
            except APIStatusError as exc:
                if exc.status_code >= 500 and attempt <= _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_DELAY_SECONDS)
                    continue
                logger.warning(
                    "OCR: API status error (%s, status=%s)", type(exc).__name__, exc.status_code,
                )
                raise OcrServiceError("API status error") from exc
            except OpenAIError as exc:
                # НЕ логируем str(exc)/exc.body — может содержать данные из
                # запроса/ответа (см. задачу про содержимое документов).
                logger.warning("OCR: provider error (%s)", type(exc).__name__)
                raise OcrServiceError("provider error") from exc

    def _to_result(self, response) -> OcrResult:
        parsed = response.output_parsed
        if parsed is None:
            raise OcrServiceError("model did not return the expected structured output")
        return OcrResult(
            registration_number=_clean(parsed.registration_number),
            vin=_clean(parsed.vin),
            chassis_number=_clean(parsed.chassis_number),
            manufacturer=_clean(parsed.manufacturer),
            model=_clean(parsed.model),
            full_name=_clean(parsed.full_name),
        )


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None
