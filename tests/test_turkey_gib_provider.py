"""
Тесты GibProvider - тонкая склейка сессии и парсера. Сессия здесь -
рукописный фейк (Protocol-подобный), реальная сеть не используется вовсе
(её уже проверяет tests/test_turkey_gib_session.py).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.gib.models import CaptchaChallenge  # noqa: E402
from reader.turkey_bot.gib.provider import GibProvider  # noqa: E402


class _FakeSession:
    def __init__(self, *, submit_response: dict | None = None):
        self.start_calls = 0
        self.refresh_calls = 0
        self.submit_calls: list[dict] = []
        self._submit_response = submit_response or {"data": []}

    async def start(self) -> CaptchaChallenge:
        self.start_calls += 1
        return CaptchaChallenge(image_id="cid-start", image_png=b"\x89PNG")

    async def refresh_captcha(self) -> CaptchaChallenge:
        self.refresh_calls += 1
        return CaptchaChallenge(image_id="cid-refresh", image_png=b"\x89PNG")

    async def submit(self, *, plate: str, image_id: str, captcha_code: str) -> dict:
        self.submit_calls.append(
            {"plate": plate, "image_id": image_id, "captcha_code": captcha_code}
        )
        return self._submit_response


async def test_start_delegates_to_session():
    session = _FakeSession()
    provider = GibProvider(session)

    challenge = await provider.start()

    assert challenge.image_id == "cid-start"
    assert session.start_calls == 1


async def test_refresh_captcha_delegates_to_session_not_start():
    session = _FakeSession()
    provider = GibProvider(session)

    challenge = await provider.refresh_captcha()

    assert challenge.image_id == "cid-refresh"
    assert session.refresh_calls == 1
    assert session.start_calls == 0


async def test_submit_passes_arguments_through_and_parses_result():
    session = _FakeSession(submit_response={"data": [{"x": 1}]})
    provider = GibProvider(session)

    outcome = await provider.submit(plate="A123AA123", image_id="cid-9", captcha_code="g8fyx")

    assert session.submit_calls == [
        {"plate": "A123AA123", "image_id": "cid-9", "captcha_code": "g8fyx"}
    ]
    assert outcome.kind == "has_debt"
    assert outcome.raw_data == [{"x": 1}]


async def test_submit_result_is_no_debt_for_the_observed_gib_no_debt_message():
    """См. design report Stage 2 (апдейт после первого live-запуска): GIB
    присылает "долг не найден" как type="ERROR" - GibProvider должен
    прозрачно пропускать это через parser.parse_submit_response, не считая
    его отклонением."""
    session = _FakeSession(
        submit_response={
            "messages": [
                {"type": "ERROR", "text": "Girdiğiniz plakaya ait borç bulunamadı."}
            ],
            "data": None,
        }
    )
    provider = GibProvider(session)

    outcome = await provider.submit(plate="A123AA123", image_id="cid-9", captcha_code="g8fyx")

    assert outcome.kind == "no_debt"


async def test_submit_result_is_unexpected_for_an_unrecognized_error_message():
    session = _FakeSession(
        submit_response={"messages": [{"type": "ERROR", "text": "bad code"}]}
    )
    provider = GibProvider(session)

    outcome = await provider.submit(plate="A123AA123", image_id="cid-9", captcha_code="wrong")

    assert outcome.kind == "unexpected"
    assert outcome.messages[0].text == "bad code"
