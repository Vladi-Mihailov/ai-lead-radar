"""CaptchaResolver — единственная точка, где UnifiedTurkeyCheckService
касается CAPTCHA (см. design report решение п.7: "captcha_code — входной
технический параметр", "не переносить OCR retry loops в
UnifiedTurkeyCheckService/MonitoringService"). Ровно ОДНА попытка
распознавания на challenge — БЕЗ повторов/refresh здесь: если провайдеру
досталась "неудачная" картинка, соответствующий ProviderCheckResult
становится ERROR(error_type="captcha_unavailable") — это уже задача
вызывающего кода (check_service.py) решить, что делать дальше (сейчас —
ничего, просто ERROR, см. решение п.7).

Сам OCR (CaptchaSolver/RapidOcrService, OCR_ENGINE) НЕ дублируется и НЕ
меняется здесь — это тонкая обёртка под протокол CaptchaResolver."""

from __future__ import annotations

from typing import Protocol

from reader.turkey_bot_test.captcha_solver import CaptchaSolver


class CaptchaResolver(Protocol):
    async def resolve(self, *, provider: str, image_png: bytes) -> str | None: ...


class DefaultCaptchaResolver:
    """Обёртка над уже существующим CaptchaSolver — используется как
    default в UnifiedTurkeyCheckService, но полностью заменяема (см.
    Protocol выше) — например, фейком в тестах, без единого реального
    OCR-вызова (см. задачу п.10: "никаких реальных запросов")."""

    async def resolve(self, *, provider: str, image_png: bytes) -> str | None:
        if provider == "kgm":
            return CaptchaSolver.solve_kgm_captcha(image_png)
        return CaptchaSolver.solve_captcha(image_png)
