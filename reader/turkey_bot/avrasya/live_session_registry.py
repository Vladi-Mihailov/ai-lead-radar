"""In-memory (НИКОГДА не persisted — тот же принцип, что и
reader/turkey_bot/live_session_registry.py::LiveGibSessionRegistry) реестр
живых Avrasya-проверок по chat_id.

Один httpx.AsyncClient — одна in-flight проверка (см.
reader/turkey_bot/avrasya/session.py::AvrasyaSession) — здесь же
ассоциация chat_id -> эта проверка, чтобы submit()/refresh_captcha()
ВСЕГДА переиспользовали ТОТ ЖЕ клиент (и его cookies, включая
ASP.NET_SessionId), а не пересоздавали Avrasya-сессию заново.

Отдельный, СТРУКТУРНО параллельный (но не общий) реестр от
LiveGibSessionRegistry (см. design report Stage 1: "do not force Avrasya
into GIB-specific abstractions... reader/turkey_bot/gib/* остаётся
нетронутым") — LiveAvrasyaCheck не несёт image_id вовсе (у Avrasya его
нет, см. avrasya/models.py::AvrasyaCaptchaChallenge).

ВАЖНО (см. design report Stage 1, "per-chat locking"): этот реестр
НАМЕРЕННО не имеет собственного lock_for() — единый per-chat lock на ОБА
провайдера (GIB и Avrasya) обеспечивает reader/turkey_bot/conversation.py,
переиспользуя LiveGibSessionRegistry.lock_for() (сам по себе
провайдер-агностичный метод, не трогающий _checks/GibProvider) — так один
и тот же chat_id не может одновременно обрабатываться и GIB-, и
Avrasya-веткой (см. design report: "one common per-chat lock across GİB
and Avrasya so the two flows cannot race")."""

import logging
import time
from dataclasses import dataclass, field

import httpx

from reader.turkey_bot.avrasya.provider import AvrasyaProvider

logger = logging.getLogger(__name__)


@dataclass
class LiveAvrasyaCheck:
    """Один живой цикл проверки одного номера — client ПРИНАДЛЕЖИТ этой
    записи: закрывать его обязан тот, кто удаляет запись из реестра (см.
    LiveAvrasyaSessionRegistry.pop_and_close/close_all), больше никто."""

    client: httpx.AsyncClient
    provider: AvrasyaProvider
    plate: str
    created_at: float = field(default_factory=time.monotonic)
    # Сколько раз уже вызывался submit() для этого номера (включая
    # отклонённые попытки) — только для turkey_toll_checks.captcha_attempts
    # (см. reader/turkey_bot/toll_check_repository.py), ни на что другое
    # не влияет.
    submit_attempts: int = 0


class LiveAvrasyaSessionRegistry:
    """Один процесс — один экземпляр этого класса (создаётся в
    reader/turkey_bot/main.py::run(), передаётся в ConversationController).
    Структурная копия LiveGibSessionRegistry (см. модуль docstring про то,
    почему это отдельный, а не общий класс)."""

    def __init__(self, *, idle_ttl_seconds: float = 900.0):
        self._checks: dict[int, LiveAvrasyaCheck] = {}
        self._idle_ttl = idle_ttl_seconds

    async def get(self, chat_id: int) -> LiveAvrasyaCheck | None:
        """None — либо для этого chat_id никогда не было живой Avrasya-
        проверки, либо она устарела (idle_ttl) и уже закрыта здесь же,
        либо процесс только что перезапустился (реестр пуст по
        построению) — вызывающий код (conversation.py) не различает эти
        три случая и не должен (тот же принцип, что и у
        LiveGibSessionRegistry.get)."""
        check = self._checks.get(chat_id)
        if check is None:
            return None
        if time.monotonic() - check.created_at > self._idle_ttl:
            await self.pop_and_close(chat_id)
            return None
        return check

    async def put(self, chat_id: int, check: LiveAvrasyaCheck) -> None:
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
                "Turkey bot: не удалось закрыть httpx.AsyncClient (Avrasya) для chat_id=%s",
                chat_id, exc_info=True,
            )

    async def close_all(self) -> None:
        """Вызывается ИСКЛЮЧИТЕЛЬНО при штатном завершении процесса (см.
        reader/turkey_bot/main.py::run(), finally) — закрывает ВСЕ ещё
        открытые httpx.AsyncClient, чтобы ни одно соединение не осталось
        висеть после остановки бота. Безопасно вызывать при пустом
        реестре (no-op)."""
        for chat_id in list(self._checks.keys()):
            await self.pop_and_close(chat_id)

    def count_active(self) -> int:
        """Только для диагностики/логов, ни на что не влияет."""
        return len(self._checks)
