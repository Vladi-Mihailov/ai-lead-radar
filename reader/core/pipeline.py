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
                # ---- ВРЕМЕННАЯ ДИАГНОСТИКА ----
                logger.debug(
                    "Message received | message_id=%s chat_id=%s", message.id, message.chat_id
                )
                # --------------------------------
                await self._process(message)
        finally:
            await self._source.stop()

    async def _process(self, message: Message) -> None:
        # ---- ВРЕМЕННАЯ ДИАГНОСТИКА ----
        logger.debug("Pipeline entered | message_id=%s", message.id)
        # --------------------------------

        matches = self._engine.evaluate(message)

        # ---- ВРЕМЕННАЯ ДИАГНОСТИКА ----
        logger.debug("Engine evaluated | message_id=%s matches=%d", message.id, len(matches))
        # --------------------------------

        if not matches:
            return

        event = LeadEvent(message=message, matches=matches)

        # ---- ВРЕМЕННАЯ ДИАГНОСТИКА ----
        logger.debug("LeadEvent created | message_id=%s", message.id)
        # --------------------------------

        # ---- ВРЕМЕННАЯ ДИАГНОСТИКА ----
        logger.debug(
            "Configured sinks: %s",
            [type(s).__name__ for s in self._sinks],
        )
        # --------------------------------

        for sink in self._sinks:
            sink_name = type(sink).__name__

            # ---- ВРЕМЕННАЯ ДИАГНОСТИКА ----
            logger.debug("Sink %s entered | message_id=%s", sink_name, message.id)
            # --------------------------------

            try:
                await sink.handle(event)

                # ---- ВРЕМЕННАЯ ДИАГНОСТИКА ----
                logger.debug("Sink %s finished | message_id=%s", sink_name, message.id)
                # --------------------------------
            except Exception:
                logger.exception("Sink %s не смог обработать событие", sink_name)
