import logging

from reader.core.models import LeadEvent
from reader.sinks.base import BaseSink

logger = logging.getLogger(__name__)


class ConsoleSink(BaseSink):
    async def handle(self, event: LeadEvent) -> None:
        message = event.message
        scenario_names = ", ".join(m.scenario_name for m in event.matches)
        keywords = ", ".join(sorted({kw for m in event.matches for kw in m.matched_keywords}))

        logger.info(
            "НАЙДЕН ЛИД | сценарии: %s | группа: %s | автор: %s | ключевые слова: %s\n"
            "    текст: %s\n"
            "    ссылка: %s",
            scenario_names,
            message.chat_title,
            message.sender_username or message.sender_name or message.sender_id or "неизвестно",
            keywords,
            message.text.replace("\n", " ")[:300],
            message.link or "-",
        )
