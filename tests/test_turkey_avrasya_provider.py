"""
Тесты AvrasyaProvider — тонкая склейка сессии и парсера. Сессия здесь —
рукописный фейк, реальная сеть не используется вовсе (её уже проверяет
tests/test_turkey_avrasya_session.py).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.turkey_bot.avrasya.models import AvrasyaCaptchaChallenge  # noqa: E402
from reader.turkey_bot.avrasya.provider import AvrasyaProvider  # noqa: E402
from reader.turkey_bot.avrasya.session import AvrasyaRateLimitedError  # noqa: E402


class _FakeSession:
    def __init__(self, *, submit_result: tuple[int, object] | None = None, submit_raises=None):
        self.start_calls = 0
        self.refresh_calls = 0
        self.submit_calls: list[dict] = []
        self._submit_result = submit_result or (200, {})
        self._submit_raises = submit_raises

    async def start(self) -> AvrasyaCaptchaChallenge:
        self.start_calls += 1
        return AvrasyaCaptchaChallenge(image_png=b"\xff\xd8start")

    async def refresh_captcha(self) -> AvrasyaCaptchaChallenge:
        self.refresh_calls += 1
        return AvrasyaCaptchaChallenge(image_png=b"\xff\xd8refresh")

    async def submit(self, *, plate: str, captcha_code: str) -> tuple[int, object]:
        self.submit_calls.append({"plate": plate, "captcha_code": captcha_code})
        if self._submit_raises is not None:
            raise self._submit_raises
        return self._submit_result


async def test_start_delegates_to_session():
    session = _FakeSession()
    provider = AvrasyaProvider(session)

    challenge = await provider.start()

    assert challenge.image_png == b"\xff\xd8start"
    assert session.start_calls == 1


async def test_refresh_captcha_delegates_to_session_not_start():
    session = _FakeSession()
    provider = AvrasyaProvider(session)

    challenge = await provider.refresh_captcha()

    assert challenge.image_png == b"\xff\xd8refresh"
    assert session.refresh_calls == 1
    assert session.start_calls == 0


async def test_submit_does_not_pass_image_id_the_way_gib_provider_does():
    """Архитектурное отличие от GibProvider (см. design report Stage 1) —
    AvrasyaProvider.submit сигнатурно не принимает image_id вовсе."""
    session = _FakeSession(submit_result=(400, {"Messages": []}))
    provider = AvrasyaProvider(session)

    await provider.submit(plate="A123AA123", captcha_code="g8fyx")

    assert session.submit_calls == [{"plate": "A123AA123", "captcha_code": "g8fyx"}]


async def test_submit_parses_the_real_observed_captcha_rejected_response():
    session = _FakeSession(
        submit_result=(
            400,
            {
                "Messages": [
                    {
                        "PropertyName": "Captcha",
                        "ErrorMessage": "Güvenlik kodunu doğru girdiğinizden emin olunuz.",
                    }
                ]
            },
        )
    )
    provider = AvrasyaProvider(session)

    outcome = await provider.submit(plate="A123AA123", captcha_code="000000")

    assert outcome.kind == "rejected"


async def test_submit_result_is_unexpected_for_unobserved_response_shapes():
    session = _FakeSession(submit_result=(200, {"Subcriptions": []}))
    provider = AvrasyaProvider(session)

    outcome = await provider.submit(plate="A123AA123", captcha_code="123456")

    assert outcome.kind == "unexpected"


async def test_submit_propagates_rate_limited_error_without_catching_it():
    """См. design report Stage 1/задача: "429 remains a transport/rate-
    limit error" — provider не должен превращать это в AvrasyaSubmitOutcome."""
    session = _FakeSession(submit_raises=AvrasyaRateLimitedError("rate limited"))
    provider = AvrasyaProvider(session)

    with pytest.raises(AvrasyaRateLimitedError):
        await provider.submit(plate="A123AA123", captcha_code="123456")
