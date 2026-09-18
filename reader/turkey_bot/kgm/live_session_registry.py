"""In-memory (НИКОГДА не persisted) реестр живых KGM-проверок по chat_id —
структурная копия reader/turkey_bot/avrasya/live_session_registry.py::
LiveAvrasyaSessionRegistry (см. design report: "Сделай по существующему
GİB/Avrasya pattern... restart-safe behavior аналогично текущим
providers") — ОТДЕЛЬНЫЙ, но структурно идентичный реестр, не общий
базовый класс (тот же принцип "do not force KGM into GIB/Avrasya-specific
abstractions", уже применённый к Avrasya).

Этот реестр НАМЕРЕННО не имеет собственного lock_for() — единый per-chat
lock на ВСЕ ТРИ провайдера обеспечивает reader/turkey_bot/conversation.py,
переиспользуя LiveGibSessionRegistry.lock_for() (см. design report Stage
2B: тот же принцип, уже применённый к Avrasya, теперь распространяется и
на KGM)."""

import logging
import time
from dataclasses import dataclass, field

import httpx

from reader.turkey_bot.kgm.provider import KgmProvider

logger = logging.getLogger(__name__)


@dataclass
class LiveKgmCheck:
    """Один живой цикл проверки одного номера — client ПРИНАДЛЕЖИТ этой
    записи: закрывать его обязан тот, кто удаляет запись из реестра (см.
    LiveKgmSessionRegistry.pop_and_close/close_all), больше никто (см.
    reader/turkey_bot/avrasya/live_session_registry.py::LiveAvrasyaCheck —
    тот же принцип)."""

    client: httpx.AsyncClient
    provider: KgmProvider
    plate: str
    created_at: float = field(default_factory=time.monotonic)
    # Только для turkey_toll_checks.captcha_attempts (см.
    # reader/turkey_bot/toll_check_repository.py), ни на что другое не
    # влияет.
    submit_attempts: int = 0


class LiveKgmSessionRegistry:
    """Один процесс — один экземпляр (создаётся в
    reader/turkey_bot/main.py::run(), передаётся в ConversationController).
    Структурная копия LiveAvrasyaSessionRegistry (см. модуль docstring)."""

    def __init__(self, *, idle_ttl_seconds: float = 900.0):
        self._checks: dict[int, LiveKgmCheck] = {}
        self._idle_ttl = idle_ttl_seconds

    async def get(self, chat_id: int) -> LiveKgmCheck | None:
        """None — либо для этого chat_id никогда не было живой KGM-
        проверки, либо она устарела (idle_ttl) и уже закрыта здесь же,
        либо процесс только что перезапустился — вызывающий код
        (conversation.py) не различает эти три случая и не должен (тот
        же принцип, что и у LiveAvrasyaSessionRegistry.get)."""
        check = self._checks.get(chat_id)
        if check is None:
            return None
        if time.monotonic() - check.created_at > self._idle_ttl:
            await self.pop_and_close(chat_id)
            return None
        return check

    async def put(self, chat_id: int, check: LiveKgmCheck) -> None:
        """Закрывает и заменяет любую предыдущую запись этого chat_id —
        никогда не оставляет два httpx.AsyncClient открытыми для одного
        chat_id одновременно."""
        await self.pop_and_close(chat_id)
        self._checks[chat_id] = check

    async def pop_and_close(self, chat_id: int) -> None:
        check = self._checks.pop(chat_id, None)
        if check is None:
            return
        try:
            await check.client.aclose()
        except Exception:
            logger.warning(
                "Turkey bot: не удалось закрыть httpx.AsyncClient (KGM) для chat_id=%s",
                chat_id, exc_info=True,
            )

    async def close_all(self) -> None:
        """Вызывается ИСКЛЮЧИТЕЛЬНО при штатном завершении процесса (см.
        reader/turkey_bot/main.py::run(), finally) — безопасно вызывать
        при пустом реестре (no-op)."""
        for chat_id in list(self._checks.keys()):
            await self.pop_and_close(chat_id)

    def count_active(self) -> int:
        """Только для диагностики/логов, ни на что не влияет."""
        return len(self._checks)
