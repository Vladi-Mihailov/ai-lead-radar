import logging

from reader.core.models import Message, ScenarioMatch
from reader.scenarios import KeywordMatcher

logger = logging.getLogger(__name__)


class MatchEngine:
    def __init__(self, matcher: KeywordMatcher):
        self._matcher = matcher

    def evaluate(self, message: Message) -> list[ScenarioMatch]:
        matches = self._matcher.match(message.text)
        if matches:
            logger.debug(
                "Сообщение %s в '%s' совпало со сценариями: %s",
                message.id,
                message.chat_title,
                [m.scenario_name for m in matches],
            )
        return matches
