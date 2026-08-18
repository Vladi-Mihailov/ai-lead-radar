import logging

from reader.core.engine import MatchEngine
from reader.core.models import LeadEvent, Message
from reader.sinks.base import BaseSink
from reader.sources.base import BaseSource
from reader.users.car_numbers import extract_car_numbers
from reader.users.keyword_matches import unique_keywords
from reader.users.repository import UserRepository

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        source: BaseSource,
        engine: MatchEngine,
        sinks: list[BaseSink],
        user_repository: UserRepository,
    ):
        self._source = source
        self._engine = engine
        self._sinks = sinks
        self._user_repository = user_repository

    async def run(self) -> None:
        await self._source.start()
        for sink in self._sinks:
            await sink.start()
        logger.info("Reader запущен, ожидание новых сообщений...")
        try:
            async for message in self._source.messages():
                await self._process(message)
        finally:
            await self._source.stop()
            # sink.stop() — тот же приём изоляции ошибок, что и вокруг
            # sink.handle() в _process: сбой остановки одного sink'а (см.
            # LeadAiSink — дожидается/отменяет фоновые AI-задачи) не должен
            # мешать остановке остальных.
            for sink in self._sinks:
                try:
                    await sink.stop()
                except Exception:
                    logger.exception(
                        "Sink %s не смог корректно остановиться", type(sink).__name__
                    )

    async def _process(self, message: Message) -> None:
        matches = self._engine.evaluate(message)

        if matches and message.sender_id is not None:
            # Локальная база пользователей — побочный эффект, не часть
            # detection/forwarding: сбой здесь не должен мешать основной
            # обработке сообщения (см. try/except ниже, вокруг sink.handle).
            try:
                self._user_repository.add_keywords(
                    message.sender_id, unique_keywords(matches)
                )
            except Exception:
                logger.exception(
                    "Не удалось обновить keywords пользователя %s", message.sender_id
                )

        if message.sender_id is not None:
            # В отличие от keywords — независимо от того, совпал ли
            # сценарий (см. задачу: госномер ищется в ЛЮБОМ сообщении
            # пользователя, а не только в тех, что дали ScenarioMatch).
            car_numbers = extract_car_numbers(message.text)
            if car_numbers:
                try:
                    self._user_repository.add_car_numbers(message.sender_id, car_numbers)
                except Exception:
                    logger.exception(
                        "Не удалось обновить car_numbers пользователя %s", message.sender_id
                    )

        if not matches:
            return

        event = LeadEvent(message=message, matches=matches)

        for sink in self._sinks:
            sink_name = type(sink).__name__
            try:
                await sink.handle(event)
            except Exception:
                logger.exception("Sink %s не смог обработать событие", sink_name)
