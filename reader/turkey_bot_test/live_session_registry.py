"""In-memory (НИКОГДА не persisted — см. design report Stage 3: "do not
pretend that an in-memory client can be reconstructed from a DB
session_token") реестр живых GIB-проверок по chat_id.

Один httpx.AsyncClient — одна in-flight проверка (см.
reader/turkey_bot_test/gib/session.py::GibSession) — здесь же ассоциация
chat_id -> эта проверка, чтобы submit()/refresh_captcha() ВСЕГДА
переиспользовали ТОТ ЖЕ клиент (и его cookies, включая ротирующийся
"TS...", см. design report Stage 1), а не пересоздавали GIB-сессию заново.

Архитектурный ответ на "что происходит при рестарте процесса, пока
пользователь вводит CAPTCHA" (см. design report Stage 3): НИЧЕГО здесь не
переживает рестарт — реестр стартует пустым. reader/turkey_bot_test/
conversation.py воспринимает "get(chat_id) вернул None" (будь то рестарт,
или естественная idle-TTL эвикция, или дважды использованный/устаревший
image_id) как ОДИН И ТОТ ЖЕ штатный сигнал: прошлый challenge
недействителен, нужно молча запросить новую CAPTCHA для номера, уже
сохранённого в turkey_bot_conversation_state.payload — а не пытаться
как-либо "восстановить" исходный httpx.AsyncClient/cookies, что в принципе
невозможно.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

from reader.turkey_bot_test.gib.provider import GibProvider

logger = logging.getLogger(__name__)


@dataclass
class LiveGibCheck:
    """Один живой цикл проверки одного номера — client ПРИНАДЛЕЖИТ этой
    записи: закрывать его обязан тот, кто удаляет запись из реестра (см.
    LiveGibSessionRegistry.pop_and_close/close_all), больше никто."""

    client: httpx.AsyncClient
    provider: GibProvider
    plate: str
    image_id: str
    created_at: float = field(default_factory=time.monotonic)
    # Сколько раз уже вызывался submit() для этого номера (включая
    # отклонённые попытки) — только для turkey_fine_checks.captcha_attempts
    # (см. reader/turkey_bot_test/check_repository.py), ни на что другое не влияет.
    submit_attempts: int = 0


class LiveGibSessionRegistry:
    """Один процесс — один экземпляр этого класса (создаётся в
    reader/turkey_bot_test/main.py::run(), передаётся в ConversationController).
    Не потокобезопасен и не рассчитан на несколько процессов — ровно так
    же, как и остальные in-memory структуры этого бота (один процесс на
    Turkey-бота, см. design report Stage 1: "own token/own process")."""

    def __init__(self, *, idle_ttl_seconds: float = 900.0):
        self._checks: dict[int, LiveGibCheck] = {}
        # Locks НИКОГДА не удаляются из словаря (см. докстрок lock_for) —
        # намеренно: удаление lock'а, который в этот момент удерживается
        # (async with) где-то выше по стеку, создало бы окно, где
        # конкурентный lock_for() того же chat_id вернул бы НОВЫЙ,
        # независимый Lock — то есть сериализация перестала бы работать
        # именно тогда, когда она нужнее всего. Цена — по одному маленькому
        # объекту asyncio.Lock на каждый когда-либо писавший боту chat_id
        # за всё время жизни процесса — принято сознательно.
        self._locks: dict[int, asyncio.Lock] = {}
        self._idle_ttl = idle_ttl_seconds

    def lock_for(self, chat_id: int) -> asyncio.Lock:
        """Сериализует обработку сообщений ОДНОГО chat_id (см. design
        report Stage 3: "per-chat locking") — защищает от гонки, если два
        входящих события того же чата (например, двойной тап или
        одновременно текст и callback) обрабатываются интерливингом вокруг
        await-точек сетевого запроса к GIB."""
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[chat_id] = lock
        return lock

    async def get(self, chat_id: int) -> LiveGibCheck | None:
        """None — либо для этого chat_id никогда не было живой проверки,
        либо она устарела (idle_ttl) и уже закрыта здесь же, либо процесс
        только что перезапустился (реестр пуст по построению) — вызывающий
        код (conversation.py) не различает эти три случая и не должен: во
        всех трёх реакция одна и та же (см. докстрок модуля)."""
        check = self._checks.get(chat_id)
        if check is None:
            return None
        if time.monotonic() - check.created_at > self._idle_ttl:
            await self.pop_and_close(chat_id)
            return None
        return check

    async def put(self, chat_id: int, check: LiveGibCheck) -> None:
        """Закрывает и заменяет любую предыдущую запись этого chat_id —
        никогда не оставляет два httpx.AsyncClient открытыми для одного
        chat_id одновременно (см. докстрок LiveGibCheck про владение
        client'ом)."""
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
                "Turkey bot: не удалось закрыть httpx.AsyncClient для chat_id=%s",
                chat_id, exc_info=True,
            )

    async def close_all(self) -> None:
        """Вызывается ИСКЛЮЧИТЕЛЬНО при штатном завершении процесса (см.
        reader/turkey_bot_test/main.py::run(), finally) — закрывает ВСЕ ещё
        открытые httpx.AsyncClient, чтобы ни одно соединение не осталось
        висеть после остановки бота. Безопасно вызывать при пустом
        реестре (no-op)."""
        for chat_id in list(self._checks.keys()):
            await self.pop_and_close(chat_id)

    def count_active(self) -> int:
        """Только для диагностики/логов, ни на что не влияет."""
        return len(self._checks)
