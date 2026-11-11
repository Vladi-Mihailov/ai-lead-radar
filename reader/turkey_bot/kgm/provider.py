"""KgmProvider — тонкая склейка KgmSession (транспорт) и parser.py
(разбор) — см. reader/turkey_bot/avrasya/provider.py::AvrasyaProvider,
тот же принцип: сюда не добавляется никакой новой транспортной/
парсинг-логики, только их последовательный вызов.

Единственное добавление (см. задачу "KGM diagnostic logging") — одна
строка лога на каждый kind == "unexpected": безопасная диагностика
parser.py (KgmDiagnostics), время ответа сервера (KgmSession.
last_submit_seconds) и размер ответа. Госномер, CAPTCHA, cookies, message
(может нести текст сайта) и сам ответ в лог не пишутся; результат проверки
возвращается без изменений."""

import logging

from reader.turkey_bot.kgm.models import KgmCaptchaChallenge, KgmSubmitOutcome
from reader.turkey_bot.kgm.parser import parse_submit_response
from reader.turkey_bot.kgm.session import KgmSession

logger = logging.getLogger(__name__)


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
        outcome = parse_submit_response(response_text)
        if outcome.kind == "unexpected":
            diagnostics = outcome.diagnostics
            server_seconds = self._session.last_submit_seconds
            logger.warning(
                "KGM unexpected response: category=%s code=%s detail=%s server_seconds=%s response_chars=%d",
                diagnostics.category if diagnostics else "unknown",
                diagnostics.code if diagnostics else "unknown",
                (diagnostics.detail if diagnostics else "") or "-",
                f"{server_seconds:.2f}" if server_seconds is not None else "-",
                len(response_text),
            )
        return outcome
