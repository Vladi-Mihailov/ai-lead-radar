"""DmOutreachObserver — точка входа ЛС-кампаний в существующий Reader
(вызывается из Pipeline._process для КАЖДОГО сообщения отслеживаемой
группы, после MatchEngine). Нового детектора нет: кандидат появляется
только из совпадений существующего KeywordMatcher.

Пока НИ ОДНА кампания не включена (dm_campaigns.enabled=0 — состояние
после деплоя) — ничего не делает вовсе: ни буфера сообщений, ни
кандидатов, ни OpenAI.

Выбор кампании для сообщения детерминирован: совпадения перебираются в
порядке KeywordMatcher (= порядок сценариев в config/scenarios.yaml),
берётся первая ВКЛЮЧЁННАЯ кампания этого сценария, прошедшая фильтр
источника (и страховой префильтр для сценария insurance). Одно сообщение —
не более одной строки dm_outreach (глобальный UNIQUE в БД)."""

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from reader.core.models import Message, ScenarioMatch
from reader.dm_campaigns.models import DmCampaign, source_chat_allowed
from reader.dm_campaigns.outreach_repository import (
    STATUS_FILTERED,
    STATUS_PENDING_CONTEXT,
    DmOutreachRepository,
)
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.insurance_matching import is_insurance_text

logger = logging.getLogger(__name__)

INSURANCE_SCENARIO = "insurance"

FILTER_SOURCE_CHAT = "source_chat_not_allowed"
FILTER_INSURANCE_PREFILTER = "insurance_prefilter_rejected"
FILTER_NO_SENDER_ID = "no_sender_id"
FILTER_NO_USERNAME = "no_username"

# Настройки кампаний перечитываются не чаще раза в N секунд — не SELECT на
# каждое сообщение группы; включение/выключение подхватывается быстро.
_CAMPAIGN_CACHE_SECONDS = 15.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DmOutreachObserver:
    def __init__(
        self,
        campaign_repository: DmCampaignRepository,
        outreach_repository: DmOutreachRepository,
        recent_repository: RecentMessageRepository,
        *,
        context_wait_seconds: int,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._campaigns = campaign_repository
        self._outreach = outreach_repository
        self._recent = recent_repository
        self._context_wait = timedelta(seconds=context_wait_seconds)
        self._clock = clock
        self._monotonic = monotonic
        self._cached: list[DmCampaign] = []
        self._cached_at: float | None = None

    def _enabled_campaigns(self) -> list[DmCampaign]:
        now = self._monotonic()
        if self._cached_at is None or now - self._cached_at >= _CAMPAIGN_CACHE_SECONDS:
            self._cached = [c for c in self._campaigns.list_campaigns() if c.enabled]
            self._cached_at = now
        return self._cached

    def observe(self, message: Message, matches: list[ScenarioMatch]) -> None:
        campaigns = self._enabled_campaigns()
        if not campaigns:
            return
        self._recent.add(
            chat_id=message.chat_id, chat_identifier=message.chat_identifier,
            message_id=message.id, sender_id=message.sender_id, text=message.text,
            date=message.date, reply_to_msg_id=message.reply_to_msg_id,
        )
        if matches:
            self._detect(message, matches, campaigns)

    def _detect(self, message: Message, matches: list[ScenarioMatch], campaigns: list[DmCampaign]) -> None:
        chosen: DmCampaign | None = None
        first_rejection: tuple[DmCampaign, str] | None = None
        for match in matches:
            for campaign in campaigns:
                if campaign.scenario_name != match.scenario_name:
                    continue
                reason = self._rejection(campaign, message)
                if reason is None:
                    chosen = campaign
                    break
                if first_rejection is None:
                    first_rejection = (campaign, reason)
            if chosen is not None:
                break

        if chosen is None:
            if first_rejection is not None:
                self._insert(message, first_rejection[0], STATUS_FILTERED, first_rejection[1])
            return

        if message.sender_id is None:
            self._insert(message, chosen, STATUS_FILTERED, FILTER_NO_SENDER_ID)
        elif not message.sender_username:
            # Без @username другой (отправляющий) аккаунт в Phase 3 не сможет
            # надёжно найти получателя — черновик бесполезен, OpenAI не зовём.
            self._insert(message, chosen, STATUS_FILTERED, FILTER_NO_USERNAME)
        else:
            self._insert(message, chosen, STATUS_PENDING_CONTEXT, None)

    @staticmethod
    def _rejection(campaign: DmCampaign, message: Message) -> str | None:
        if not source_chat_allowed(campaign, message.chat_identifier):
            return FILTER_SOURCE_CHAT
        if campaign.scenario_name == INSURANCE_SCENARIO and not is_insurance_text(message.text):
            return FILTER_INSURANCE_PREFILTER
        return None

    def _insert(self, message: Message, campaign: DmCampaign, status: str, filter_reason: str | None) -> None:
        now = self._clock()
        outreach_id = self._outreach.insert_candidate(
            campaign_id=campaign.id, source_chat_id=message.chat_id,
            source_chat_identifier=message.chat_identifier, source_chat_title=message.chat_title,
            source_message_id=message.id, source_message_at=message.date, source_link=message.link,
            source_reply_to_msg_id=message.reply_to_msg_id, recipient_user_id=message.sender_id,
            recipient_username=message.sender_username, source_text=message.text, status=status,
            now=now, draft_after_at=now + self._context_wait if status == STATUS_PENDING_CONTEXT else None,
            filter_reason=filter_reason,
        )
        if outreach_id is None:
            return
        logger.info(
            "dm_outreach candidate id=%s campaign=%s chat=%s message=%s status=%s reason=%s",
            outreach_id, campaign.key, message.chat_id, message.id, status, filter_reason,
        )
