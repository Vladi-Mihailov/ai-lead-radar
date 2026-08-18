import asyncio
import logging

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
    RateLimitError,
)

from reader.lead_ai.models import LeadAiAnalysis, normalize_lead_ai_analysis
from reader.lead_ai.prompt import SYSTEM_PROMPT, build_user_text

logger = logging.getLogger(__name__)

# Тот же приём, что и reader/ocr/service.py::OcrService — один ретрай,
# только для транзиентных ошибок/5xx, без экспоненциального backoff.
_MAX_RETRIES = 1
_RETRY_DELAY_SECONDS = 1.0

# Текст сообщения-лида обычно короткий (это Telegram-сообщение, не
# документ) — ограничение только на случай аномально длинного сообщения,
# чтобы не раздувать токены/не отправлять модели лишний объём.
_MAX_TEXT_LENGTH = 2000


class LeadAiServiceError(Exception):
    """Любой сбой AI-анализа (сеть/API/некорректный ответ модели) —
    вызывающий код (см. reader/sinks/lead_ai_sink.py) НЕ должен ломать
    доставку самого лида из-за этой ошибки (см. задачу: AI — fail-open
    дополнение, а не часть основной доставки)."""


class LeadAiService:
    """AI-анализ лида через OpenAI Responses API (Structured Outputs),
    независимая реализация от reader/ocr/service.py::OcrService — общий
    только клиент/подход, prompt и назначение разные (см.
    reader/lead_ai/prompt.py)."""

    def __init__(self, *, api_key: str, model: str):
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def analyze(self, message_text: str) -> LeadAiAnalysis:
        text = message_text
        if len(text) > _MAX_TEXT_LENGTH:
            text = text[:_MAX_TEXT_LENGTH]

        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.responses.parse(
                    model=self._model,
                    instructions=SYSTEM_PROMPT,
                    input=[{"role": "user", "content": build_user_text(text)}],
                    text_format=LeadAiAnalysis,
                )
                parsed = response.output_parsed
                if parsed is None:
                    raise LeadAiServiceError(
                        "model did not return the expected structured output"
                    )
                return normalize_lead_ai_analysis(parsed)
            except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
                if attempt > _MAX_RETRIES:
                    logger.warning(
                        "lead_ai: транзиентная ошибка после ретрая (%s)", type(exc).__name__,
                    )
                    raise LeadAiServiceError("transient failure after retry") from exc
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                continue
            except APIStatusError as exc:
                if exc.status_code >= 500 and attempt <= _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_DELAY_SECONDS)
                    continue
                logger.warning(
                    "lead_ai: API status error (%s, status=%s)", type(exc).__name__, exc.status_code,
                )
                raise LeadAiServiceError("API status error") from exc
            except OpenAIError as exc:
                # НЕ логируем str(exc)/exc.body — может содержать текст
                # исходного сообщения лида (см. задачу).
                logger.warning("lead_ai: provider error (%s)", type(exc).__name__)
                raise LeadAiServiceError("provider error") from exc
