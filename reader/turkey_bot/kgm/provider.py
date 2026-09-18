"""KgmProvider — тонкая склейка KgmSession (транспорт) и parser.py
(разбор) — см. reader/turkey_bot/avrasya/provider.py::AvrasyaProvider,
тот же принцип: сюда не добавляется никакой новой транспортной/
парсинг-логики, только их последовательный вызов."""

from reader.turkey_bot.kgm.models import KgmCaptchaChallenge, KgmSubmitOutcome
from reader.turkey_bot.kgm.parser import parse_submit_response
from reader.turkey_bot.kgm.session import KgmSession


class KgmProvider:
    def __init__(self, session: KgmSession):
        self._session = session

    async def start(self) -> KgmCaptchaChallenge:
        return await self._session.start()

    async def refresh_captcha(self) -> KgmCaptchaChallenge:
        """Вызывать после submit() с исходом "rejected" (см.
        models.py::KgmSubmitOutcome), не start() заново — start() создал
        бы новую сессию/cookies без необходимости (тот же приём, что и
        AvrasyaProvider.refresh_captcha)."""
        return await self._session.refresh_captcha()

    async def submit(self, *, plate: str, captcha_code: str) -> KgmSubmitOutcome:
        response_text = await self._session.submit(plate=plate, captcha_code=captcha_code)
        return parse_submit_response(response_text)
