"""Сбор ограниченного контекста для ЛС-черновика — ТОЛЬКО из локального
буфера group_messages_recent (никаких запросов истории в Telegram):

- обсуждение в исходной группе: до N сообщений ДО исходного (не старше
  часа), до N ПОСЛЕ (не позже draft_after_at), цепочка ответов до глубины 2;
- свежие сообщения по теме (только для сценариев, где важна текущая
  ситуация — граница, бензин): окно fresh_context_hours, только
  отслеживаемые группы кампании, только сообщения, совпавшие с терминами
  сценария из config/scenarios.yaml (keywords + context_keywords); для
  border_queue — только сообщения о проезде (очередь, время, открыт/закрыт,
  дорога, погода) или о соседней теме, о которой спросил сам USER.

Авторы обезличены: автор исходного сообщения — USER, остальные — P1, P2…
Ни user_id, ни username в контекст не попадают. Метаданные свежих
сообщений (сколько, сколько разных авторов, возраст) считаются здесь, а не
угадываются моделью."""

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from reader.dm_campaigns.models import DmCampaign, same_chat_identifier
from reader.dm_campaigns.outreach_repository import DmOutreach
from reader.dm_campaigns.recent_messages import RecentMessage, RecentMessageRepository
from reader.dm_campaigns.border import checkpoint_evidence_ok, requested_checkpoints
from reader.dm_campaigns.relevance import border_evidence_relevant, side_topics
from reader.scenarios import KeywordMatcher, Scenario
from reader.time_display import to_tbilisi

# Сценарии, для которых важна ТЕКУЩАЯ ситуация (свежие сообщения из групп).
FRESH_CONTEXT_SCENARIOS = frozenset({"car_border_crossing", "fuel"})

REPLY_CHAIN_DEPTH = 2
BEFORE_WINDOW = timedelta(hours=1)
ITEM_MAX_CHARS = 400
EVIDENCE_MAX_TOTAL_CHARS = 6000
# Сколько последних сообщений буфера просматривается при отборе по теме.
EVIDENCE_SCAN_LIMIT = 2000

SOURCE_AUTHOR = "USER"
BORDER_QUEUE = "border_queue"


@dataclass(frozen=True)
class ContextItem:
    ref: str
    chat: str
    author: str
    time_tbilisi: str
    age_minutes: int
    text: str
    note: str | None = None


@dataclass(frozen=True)
class EvidenceMetadata:
    n_messages: int
    n_distinct_senders: int
    newest_age_minutes: int | None
    oldest_age_minutes: int | None
    window_hours: float


@dataclass(frozen=True)
class DraftContext:
    discussion: list[ContextItem]
    evidence: list[ContextItem]
    fresh_context_used: bool
    metadata: EvidenceMetadata | None
    refs: frozenset[str] = field(default_factory=frozenset)

    def to_json(self, **extra) -> str:
        return json.dumps(
            {
                "discussion": [asdict(item) for item in self.discussion],
                "evidence": [asdict(item) for item in self.evidence],
                "fresh_context_used": self.fresh_context_used,
                "metadata": asdict(self.metadata) if self.metadata else None,
                **extra,
            },
            ensure_ascii=False,
        )


def _truncate(text: str, limit: int = ITEM_MAX_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


class _Authors:
    def __init__(self, source_sender_id: int | None):
        self._source = source_sender_id
        self._labels: dict[int | None, str] = {}

    def label(self, sender_id: int | None) -> str:
        if sender_id is not None and sender_id == self._source:
            return SOURCE_AUTHOR
        if sender_id not in self._labels:
            self._labels[sender_id] = f"P{len(self._labels) + 1}"
        return self._labels[sender_id]


class DmContextBuilder:
    def __init__(
        self,
        recent_repository: RecentMessageRepository,
        scenarios: Iterable[Scenario],
        *,
        group_titles: dict[str, str] | None = None,
        max_context_messages: int = 5,
        fresh_context_hours: float = 3.0,
        max_evidence_messages: int = 15,
    ):
        self._recent = recent_repository
        self._scenarios = {s.name: s for s in scenarios}
        self._group_titles = {k.lstrip("@").lower(): v for k, v in (group_titles or {}).items()}
        self._max_context = max_context_messages
        self._fresh_window = timedelta(hours=fresh_context_hours)
        self._fresh_hours = fresh_context_hours
        self._max_evidence = max_evidence_messages

    def _chat_label(self, chat_identifier: str | None, fallback: str | None = None) -> str:
        if chat_identifier:
            return self._group_titles.get(chat_identifier.lstrip("@").lower(), chat_identifier)
        return fallback or "группа"

    def _item(self, ref: str, msg: RecentMessage, authors: _Authors, now: datetime, note: str | None = None) -> ContextItem:
        return ContextItem(
            ref=ref, chat=self._chat_label(msg.chat_identifier), author=authors.label(msg.sender_id),
            time_tbilisi=to_tbilisi(msg.date).strftime("%H:%M"),
            age_minutes=max(int((now - msg.date).total_seconds() // 60), 0),
            text=_truncate(msg.text), note=note,
        )

    def build(self, outreach: DmOutreach, campaign: DmCampaign, now: datetime) -> DraftContext:
        authors = _Authors(outreach.recipient_user_id)
        chat_id, source_id = outreach.source_chat_id, outreach.source_message_id
        source_at = outreach.source_message_at or outreach.created_at
        until = min(outreach.draft_after_at or now, now)

        before = self._recent.before(chat_id, source_id, since=source_at - BEFORE_WINDOW, limit=self._max_context)
        after = self._recent.after(chat_id, source_id, until=until, limit=self._max_context)

        # Цепочка ответов — ВСЕГДА как R ("сообщение, на которое отвечает
        # USER"), даже если то же сообщение попало в окно "до": по ней модель
        # понимает роль сообщения (свой вопрос или ответ другому участнику).
        chain: list[RecentMessage] = []
        seen = {source_id}
        parent_id = outreach.source_reply_to_msg_id
        while parent_id is not None and len(chain) < REPLY_CHAIN_DEPTH:
            parent = self._recent.get(chat_id, parent_id)
            if parent is None:
                break
            if parent.message_id not in seen:
                chain.append(parent)
                seen.add(parent.message_id)
            parent_id = parent.reply_to_msg_id
        before = [m for m in before if m.message_id not in seen]
        after = [m for m in after if m.message_id not in seen]

        discussion: list[ContextItem] = []
        for index, msg in enumerate(reversed(chain), start=1):
            discussion.append(self._item(f"R{index}", msg, authors, now, note="сообщение, на которое отвечает USER"))
        for index, msg in enumerate(before, start=1):
            discussion.append(self._item(f"B{index}", msg, authors, now))
        for index, msg in enumerate(after, start=1):
            note = "ответ на исходное сообщение USER" if msg.reply_to_msg_id == source_id else None
            discussion.append(self._item(f"A{index}", msg, authors, now, note=note))

        fresh_used = campaign.scenario_name in FRESH_CONTEXT_SCENARIOS
        evidence: list[ContextItem] = []
        metadata: EvidenceMetadata | None = None
        if fresh_used:
            evidence, metadata = self._evidence(outreach, campaign, authors, now, [m.text for m in chain])

        refs = frozenset(item.ref for item in discussion) | frozenset(item.ref for item in evidence)
        return DraftContext(
            discussion=discussion, evidence=evidence, fresh_context_used=fresh_used,
            metadata=metadata, refs=refs,
        )

    def _evidence_chats(self, campaign: DmCampaign) -> tuple[str, ...]:
        """fresh_context_chats, иначе source_chats, иначе — все
        отслеживаемые группы (пустой кортеж = без фильтра; в буфере и так
        только группы из groups.yaml)."""
        return campaign.fresh_context_chats or campaign.source_chats

    def _topic_matcher(self, campaign: DmCampaign) -> KeywordMatcher | None:
        scenario = self._scenarios.get(campaign.scenario_name or "")
        if scenario is None:
            return None
        terms = tuple(scenario.keywords) + tuple(scenario.context_keywords)
        return KeywordMatcher([Scenario(name=scenario.name, enabled=True, keywords=terms)])

    def _evidence(
        self, outreach: DmOutreach, campaign: DmCampaign, authors: _Authors, now: datetime,
        reply_texts: Iterable[str] = (),
    ) -> tuple[list[ContextItem], EvidenceMetadata]:
        matcher = self._topic_matcher(campaign)
        chats = self._evidence_chats(campaign)
        selected: list[RecentMessage] = []
        border = campaign.key == BORDER_QUEUE
        asked = side_topics(outreach.source_text) if border else frozenset()
        # border_queue: только запрошенный КПП (вопрос о Ларсе — без Сарпи/Вале).
        checkpoints = (requested_checkpoints(outreach.source_text, reply_texts, outreach.source_chat_identifier)
                       if border else frozenset())
        if matcher is not None:
            for msg in self._recent.recent(since=now - self._fresh_window, until=now, limit=EVIDENCE_SCAN_LIMIT):
                if msg.chat_id == outreach.source_chat_id and msg.message_id == outreach.source_message_id:
                    continue
                # Сам спрашивающий — не источник сведений о ситуации.
                if outreach.recipient_user_id is not None and msg.sender_id == outreach.recipient_user_id:
                    continue
                if chats and not (msg.chat_identifier and any(same_chat_identifier(msg.chat_identifier, c) for c in chats)):
                    continue
                if not matcher.match(msg.text):
                    continue
                if border and not border_evidence_relevant(msg.text, asked):
                    continue
                if border and not checkpoint_evidence_ok(msg.text, msg.chat_identifier, checkpoints):
                    continue
                selected.append(msg)
                if len(selected) >= self._max_evidence:
                    break

        items: list[ContextItem] = []
        kept: list[RecentMessage] = []
        total = 0
        for index, msg in enumerate(selected, start=1):
            item = self._item(f"S{index}", msg, authors, now)
            if total + len(item.text) > EVIDENCE_MAX_TOTAL_CHARS:
                break
            total += len(item.text)
            items.append(item)
            kept.append(msg)

        senders = {msg.sender_id for msg in kept}
        metadata = EvidenceMetadata(
            n_messages=len(items),
            n_distinct_senders=len(senders),
            newest_age_minutes=min((i.age_minutes for i in items), default=None),
            oldest_age_minutes=max((i.age_minutes for i in items), default=None),
            window_hours=self._fresh_hours,
        )
        return items, metadata
