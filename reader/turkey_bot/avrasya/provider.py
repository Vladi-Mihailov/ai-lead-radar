"""AvrasyaProvider — тонкая склейка AvrasyaSession (транспорт) и
parser.py (разбор), тот же принцип, что и
reader/turkey_bot/gib/provider.py/reader/fines/police_ge_provider.py:
сюда не добавляется никакой новой транспортной/парсинг-логики, только их
последовательный вызов.

Единственное архитектурное отличие от GibProvider (см. design report
Stage 1, models.py/session.py в этом же пакете): submit() здесь не
принимает image_id — у Avrasya его нет вовсе (см. models.py::
AvrasyaCaptchaChallenge).

AvrasyaRateLimitedError/AvrasyaTransportError (см. session.py) здесь
СОЗНАТЕЛЬНО не перехватываются — это транспортное/операционное условие,
не бизнес-исход (см. задачу: "429 remains a transport/rate-limit error"),
вызывающий код (manual_test.py, позже — Stage 2B conversation.py) должен
обрабатывать их отдельно от AvrasyaSubmitOutcome."""

from reader.turkey_bot.avrasya.models import (
    AvrasyaCaptchaChallenge,
    AvrasyaSubmitOutcome,
)
from reader.turkey_bot.avrasya.parser import parse_submit_response
from reader.turkey_bot.avrasya.session import AvrasyaSession


class AvrasyaProvider:
    def __init__(self, session: AvrasyaSession):
        self._session = session

    async def start(self) -> AvrasyaCaptchaChallenge:
        """Новая Avrasya-сессия + первая CAPTCHA (см. AvrasyaSession.start)."""
        return await self._session.start()

    async def refresh_captcha(self) -> AvrasyaCaptchaChallenge:
        """Новая CAPTCHA в рамках той же сессии — вызывать после submit()
        с исходом "rejected" (см. models.py::AvrasyaSubmitOutcome), не
        start() заново: start() создал бы новую сессию/cookies без
        необходимости (тот же приём, что и GibProvider.refresh_captcha)."""
        return await self._session.refresh_captcha()

    async def submit(self, *, plate: str, captcha_code: str) -> AvrasyaSubmitOutcome:
        status_code, body = await self._session.submit(plate=plate, captcha_code=captcha_code)
        return parse_submit_response(status_code, body)
