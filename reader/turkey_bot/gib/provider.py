"""GibProvider — тонкая склейка GibSession (транспорт) и parser.py (разбор),
по тому же принципу, что и reader/fines/police_ge_provider.py: сюда не
добавляется никакой новой транспортной/парсинг-логики, только их
последовательный вызов. Единственная точка, которую позже (Stage 3) будет
вызывать reader/turkey_bot/conversation.py — сам ConversationController
никогда не обращается к GibSession напрямую.
"""

from reader.turkey_bot.gib.models import CaptchaChallenge, GibSubmitOutcome
from reader.turkey_bot.gib.parser import parse_submit_response
from reader.turkey_bot.gib.session import GibSession


class GibProvider:
    def __init__(self, session: GibSession):
        self._session = session

    async def start(self) -> CaptchaChallenge:
        """Новая GIB-сессия + первая CAPTCHA (см. GibSession.start)."""
        return await self._session.start()

    async def refresh_captcha(self) -> CaptchaChallenge:
        """Новая CAPTCHA В РАМКАХ той же сессии — вызывать после submit()
        с исходом "rejected" (см. models.py::GibSubmitOutcome), не start()
        заново: start() создал бы новую сессию/cookies без необходимости."""
        return await self._session.refresh_captcha()

    async def submit(
        self, *, plate: str, image_id: str, captcha_code: str,
    ) -> GibSubmitOutcome:
        raw = await self._session.submit(
            plate=plate, image_id=image_id, captcha_code=captcha_code,
        )
        return parse_submit_response(raw)
