"""DmDraftService — генерация ЛС-черновика через OpenAI Responses API
(Structured Outputs). Тот же приём, что и reader/lead_ai/service.py::
LeadAiService: AsyncOpenAI, responses.parse с Pydantic text_format, один
ретрай только для транзиентных ошибок/5xx, без экспоненциального backoff.
Общий ключ OPENAI_API_KEY (settings.ocr.openai_api_key) — второй не
заводится. Текст сообщений/контекста в лог не пишется."""

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

from reader.dm_campaigns.draft_models import DmDraftOutput
from reader.dm_campaigns.draft_prompt import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_MAX_RETRIES = 1
_RETRY_DELAY_SECONDS = 1.0


class DmDraftServiceError(Exception):
    """Любой сбой генерации (сеть/API/пустой structured output)."""


class DmDraftService:
    def __init__(self, *, api_key: str, model: str, client: AsyncOpenAI | None = None):
        self._client = client if client is not None else AsyncOpenAI(api_key=api_key)
        self._model = model

    async def generate(self, user_text: str) -> DmDraftOutput:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.responses.parse(
                    model=self._model,
                    instructions=SYSTEM_PROMPT,
                    input=[{"role": "user", "content": user_text}],
                    text_format=DmDraftOutput,
                )
                parsed = response.output_parsed
                if parsed is None:
                    raise DmDraftServiceError("model did not return the expected structured output")
                return parsed
            except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
                if attempt > _MAX_RETRIES:
                    logger.warning("dm_outreach: транзиентная ошибка OpenAI после ретрая (%s)", type(exc).__name__)
                    raise DmDraftServiceError("transient failure after retry") from exc
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
            except APIStatusError as exc:
                if exc.status_code >= 500 and attempt <= _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_DELAY_SECONDS)
                    continue
                logger.warning("dm_outreach: OpenAI status error (%s, status=%s)", type(exc).__name__, exc.status_code)
                raise DmDraftServiceError("API status error") from exc
            except OpenAIError as exc:
                logger.warning("dm_outreach: OpenAI provider error (%s)", type(exc).__name__)
                raise DmDraftServiceError("provider error") from exc
