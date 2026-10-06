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
источника (и страховой префильтр для сценария insurance) и относящаяся к
теме кампании (relevance.py; если ни одна сработавшая не по теме — другая
включённая fuel/border_queue по теме). Одно сообщение — не более одной
строки dm_outreach (глобальный UNIQUE в БД)."""

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from reader.core.models import Message, ScenarioMatch
from reader.dm_campaigns.border import place_hint
from reader.dm_campaigns.models import DmCampaign, source_chat_allowed
from reader.dm_campaigns.outreach_repository import (
    STATUS_FILTERED,
    STATUS_PENDING_CONTEXT,
    DmOutreachRepository,
)
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.relevance import ROUTABLE_CAMPAIGNS, campaign_relevant
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.sendability import assess_sendability
from reader.insurance_matching import is_insurance_text

logger = logging.getLogger(__name__)

INSURANCE_SCENARIO = "insurance"

FILTER_SOURCE_CHAT = "source_chat_not_allowed"
FILTER_INSURANCE_PREFILTER = "insurance_prefilter_rejected"
# Только медицинская/туристическая страховка без автомобильного намерения —
# не целевой лид insurance; отсекается до OpenAI.
FILTER_NON_AUTO_INSURANCE = "non_auto_insurance"
FILTER_NO_SENDER_ID = "no_sender_id"
# У этого человека в этой кампании уже есть свежий черновик (одна дискуссия —
# один черновик; первый автоматически не переписывается).
FILTER_ACTIVE_DRAFT = "active_draft_exists"
ACTIVE_DRAFT_WINDOW = timedelta(hours=2)
# Больше не присваивается (Phase 2.6: @username не обязателен); остаётся
# как значение filter_reason у строк, созданных раньше.
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
        passing: list[DmCampaign] = []
        first_rejection: tuple[DmCampaign, str] | None = None
        for match in matches:
            for campaign in campaigns:
                if campaign.scenario_name != match.scenario_name or campaign in passing:
                    continue
                reason = self._rejection(campaign, message)
                if reason is None:
                    passing.append(campaign)
                elif first_rejection is None:
                    first_rejection = (campaign, reason)

        # Одно сообщение — одна кампания: первая сработавшая И по теме (см.
        # relevance.py). Ни одна сработавшая не по теме — другая включённая
        # fuel/border_queue по теме («очереди на АЗС с АИ-95» — fuel); иначе
        # первая сработавшая (processor отфильтрует campaign_not_relevant).
        replies = self._reply_texts(message)
        hint = place_hint(message.chat_identifier, replies)
        chosen = next((c for c in passing if campaign_relevant(c.key, message.text, replies, place_hint=hint)), None)
        if chosen is None and passing:
            chosen = next(
                (c for c in campaigns if c.key in ROUTABLE_CAMPAIGNS and c not in passing
                 and self._rejection(c, message) is None
                 and campaign_relevant(c.key, message.text, replies, place_hint=hint)),
                passing[0],
            )

        if chosen is None:
            if first_rejection is not None:
                self._insert(message, first_rejection[0], STATUS_FILTERED, first_rejection[1])
            return

        # Минимальная пригодность: recipient_user_id + исходное сообщение
        # (source_chat_id/source_message_id есть всегда). @username НЕ
        # обязателен: без него отправляющий аккаунт адресует автора через
        # исходное сообщение (см. reader/dm_campaigns/sendability.py).
        if message.sender_id is None:
            self._insert(message, chosen, STATUS_FILTERED, FILTER_NO_SENDER_ID)
        elif self._outreach.active_draft_exists(
            campaign_id=chosen.id, recipient_user_id=message.sender_id, recipient_username=message.sender_username,
            since=self._clock() - ACTIVE_DRAFT_WINDOW,
        ):
            self._insert(message, chosen, STATUS_FILTERED, FILTER_ACTIVE_DRAFT)
        else:
            self._insert(message, chosen, STATUS_PENDING_CONTEXT, None)

    def _reply_texts(self, message: Message) -> tuple[str, ...]:
        if message.reply_to_msg_id is None:
            return ()
        target = self._recent.get(message.chat_id, message.reply_to_msg_id)
        return (target.text,) if target is not None and target.text else ()

    @staticmethod
    def _rejection(campaign: DmCampaign, message: Message) -> str | None:
        if not source_chat_allowed(campaign, message.chat_identifier):
            return FILTER_SOURCE_CHAT
        if campaign.scenario_name == INSURANCE_SCENARIO and not is_insurance_text(message.text):
            return FILTER_INSURANCE_PREFILTER
        # Вопрос только о медицинской страховке больше НЕ отсекается: у кампании
        # есть утверждённый ответ (медстраховка — см. CAMPAIGN GUIDELINE).
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
            sendability=assess_sendability(
                username=message.sender_username, recipient_user_id=message.sender_id,
                source_chat_id=message.chat_id, source_message_id=message.id,
                contact_require_premium=message.sender_contact_require_premium,
            ),
            contact_require_premium=message.sender_contact_require_premium,
        )
        if outreach_id is None:
            return
        logger.info(
            "dm_outreach candidate id=%s campaign=%s chat=%s message=%s status=%s reason=%s",
            outreach_id, campaign.key, message.chat_id, message.id, status, filter_reason,
        )
