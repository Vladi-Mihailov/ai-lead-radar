import logging

from reader.core.engine import MatchEngine
from reader.core.models import LeadEvent, Message
from reader.sinks.base import BaseSink
from reader.sources.base import BaseSource

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, source: BaseSource, engine: MatchEngine, sinks: list[BaseSink]):
        self._source = source
        self._engine = engine
        self._sinks = sinks

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

    async def _process(self, message: Message) -> None:
        matches = self._engine.evaluate(message)

        if not matches:
            return

        event = LeadEvent(message=message, matches=matches)

        for sink in self._sinks:
            sink_name = type(sink).__name__
            try:
                await sink.handle(event)
            except Exception:
                logger.exception("Sink %s не смог обработать событие", sink_name)
