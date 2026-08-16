import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Literal

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
    """Wire-schema для OpenAI Structured Outputs (см. reader/ocr/models.py::
    OcrResult и reader/ocr/prompt.py про источники каждого поля). Модель
    возвращает три СЫРЫХ значения ФИО (по одному на документ-источник) —
    сравнение/вывод policyholder/driver/owner/same_as-флагов делает код
    (см. _derive_name_roles), не модель. Не добавлять сюда поле, не
    обновив одновременно prompt.py и OcrResult — schema (в strict-режиме
    Structured Outputs) это единственный механизм, ограничивающий, что
    вообще может вернуть модель."""

    registration_owner_full_name: str | None
    passport_full_name: str | None
    # ФИО отдельного владельца/доверителя ТОЛЬКО из доверенности — null,
    # если доверенности нет среди изображений, либо модель не может
    # уверенно определить нужное лицо (в доверенности может быть несколько
    # людей, см. reader/ocr/prompt.py). Не переиспользуем techpassport для
    # этого поля — источник только доверенность.
    power_of_attorney_owner_full_name: str | None
    # Номер паспорта/ID СТРАХОВАТЕЛЯ — только из паспорта/ID, отдельного от
    # техпаспорта документа (см. reader/ocr/prompt.py и
    # reader/ocr/models.py::OcrResult про источник и назначение — checkout
    # tpl.ge). Свободная строка (не Literal) — в отличие от category,
    # значений здесь не 3 штуки, а произвольный номер документа.
    passport_number: str | None
    # Свободная строка (название страны на английском, см.
    # reader/ocr/prompt.py) — сопоставление со справочником tpl.ge
    # происходит вне OCR (см. reader/checkout/reference_data.py), поэтому
    # здесь не Literal с фиксированным списком стран.
    citizenship: str | None
    # Литерал, а не str — "никаких произвольных значений category" (см.
    # задачу) обеспечивается самой schema (strict-режим Structured
    # Outputs): модель структурно не может вернуть ничего, кроме этих трёх
    # значений или null, никакая валидация после факта не нужна.
    category: Literal["passenger_car", "motorcycle", "trailer"] | None
    registration_number: str | None
    vin: str | None
    chassis_number: str | None
    manufacturer: str | None
    model: str | None


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

        roles = _derive_name_roles(
            registration_owner_full_name=_clean(parsed.registration_owner_full_name),
            passport_full_name=_clean(parsed.passport_full_name),
            power_of_attorney_owner_full_name=_clean(parsed.power_of_attorney_owner_full_name),
        )
        return OcrResult(
            policyholder_full_name=roles.policyholder_full_name,
            driver_same_as_policyholder=roles.driver_same_as_policyholder,
            driver_full_name=roles.driver_full_name,
            owner_same_as_policyholder=roles.owner_same_as_policyholder,
            owner_full_name=roles.owner_full_name,
            passport_number=_clean(parsed.passport_number),
            citizenship=_clean(parsed.citizenship),
            # category — уже ограничен enum'ом на уровне schema (см.
            # _VehicleFieldsSchema.category), _clean() ему не нужен: это не
            # свободный текст, который может прийти с лишними пробелами.
            category=parsed.category,
            manufacturer=_clean(parsed.manufacturer),
            model=_clean(parsed.model),
            vin=_clean(parsed.vin),
            chassis_number=_clean(parsed.chassis_number),
            registration_number=_clean(parsed.registration_number),
            # Не из OCR — попадают в Telegram-draft как default-значения (см.
            # reader/commands/insurance_ocr.py), не заставляем модель их
            # угадывать.
            email=None,
            phone=None,
            payment_bank=None,
            policy_period=None,
            period_start=None,
        )


@dataclass(frozen=True)
class _NameRoles:
    policyholder_full_name: str | None
    driver_full_name: str | None
    driver_same_as_policyholder: bool
    owner_full_name: str | None
    owner_same_as_policyholder: bool


def _derive_name_roles(
    *,
    registration_owner_full_name: str | None,
    passport_full_name: str | None,
    power_of_attorney_owner_full_name: str | None,
) -> _NameRoles:
    """Страхователь — ТОЛЬКО из техпаспорта (см. reader/ocr/models.py про
    правила источников); без него не придумываем страхователя из одного
    паспорта/прав. Если оба ФИО распознаны и отличаются — водитель отдельный
    (из паспорта/прав). Иначе (совпадают, или доступен только техпаспорт) —
    водитель = страхователь по умолчанию (~99% бизнес-случаев).

    Владелец — независимо от driver-логики: доверенность с уверенно
    определённым лицом имеет приоритет над default owner_same_as_
    policyholder=True (см. reader/ocr/models.py); техпаспорт для отдельного
    владельца повторно не используется."""
    if power_of_attorney_owner_full_name is not None:
        owner_full_name = power_of_attorney_owner_full_name
        owner_same_as_policyholder = False
    else:
        owner_full_name = None
        owner_same_as_policyholder = True

    if registration_owner_full_name is None:
        return _NameRoles(
            policyholder_full_name=None, driver_full_name=None, driver_same_as_policyholder=True,
            owner_full_name=owner_full_name, owner_same_as_policyholder=owner_same_as_policyholder,
        )

    if passport_full_name is None or _names_match(registration_owner_full_name, passport_full_name):
        return _NameRoles(
            policyholder_full_name=registration_owner_full_name,
            driver_full_name=None,
            driver_same_as_policyholder=True,
            owner_full_name=owner_full_name,
            owner_same_as_policyholder=owner_same_as_policyholder,
        )

    return _NameRoles(
        policyholder_full_name=registration_owner_full_name,
        driver_full_name=passport_full_name,
        driver_same_as_policyholder=False,
        owner_full_name=owner_full_name,
        owner_same_as_policyholder=owner_same_as_policyholder,
    )


def _names_match(a: str, b: str) -> bool:
    return _normalize_name(a) == _normalize_name(b)


def _normalize_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None
