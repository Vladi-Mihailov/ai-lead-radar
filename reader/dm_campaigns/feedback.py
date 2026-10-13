"""Feedback learning для ЛС-черновиков (без fine-tuning).

Запись: после УСПЕШНОЙ отправки (send_service: mark_sent) в dm_draft_feedback
сохраняется пара «что написал AI» -> «что ушло человеку» (правленая или нет).
Не отправленные (skipped / blocked / failed / draft) сюда не попадают.
UNIQUE(draft_id): повторный callback не создаёт вторую строку.

Использование (Reader, только при dm_outreach.feedback_learning_enabled):
перед генерацией берутся последние отправленные примеры ТОЙ ЖЕ кампании,
их source_text сравнивается с новым вопросом по OpenAI embedding (косинус в
Python, без отдельной vector DB), в prompt идут 3–5 ближайших. Embedding
примера считается лениво (Reader) и кэшируется в строке.

Примеры влияют только на стиль: факты — из утверждённых знаний кампании и
текущего контекста, ресурсы/приветствие/подвал — серверные правила. Поэтому
из текстов примеров убираются предложения с @-ресурсами."""

import json
import logging
import math
import re
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from reader.dm_campaigns.outreach_repository import DmOutreach
from reader.dm_campaigns.recent_messages import format_time

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS dm_draft_feedback (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        draft_id               INTEGER NOT NULL UNIQUE,
        campaign               TEXT NOT NULL,
        intent                 TEXT,
        source_chat_identifier TEXT,
        source_text            TEXT NOT NULL,
        original_draft_text    TEXT NOT NULL,
        final_draft_text       TEXT NOT NULL,
        was_edited             INTEGER NOT NULL,
        changed_ratio          REAL NOT NULL,
        editor_user_id         INTEGER,
        sent_at                TEXT NOT NULL,
        created_at             TEXT NOT NULL,
        embedding_model        TEXT,
        embedding_json         TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dm_draft_feedback_campaign ON dm_draft_feedback (campaign, sent_at)",
)

_MENTION_SENTENCE_RE = re.compile(r"[^.!?\n]*@\w{3,}[^.!?\n]*[.!?]?")


@dataclass(frozen=True)
class FeedbackExample:
    id: int
    campaign: str
    source_text: str
    original_draft_text: str
    final_draft_text: str
    was_edited: bool
    embedding_model: str | None = None
    embedding: tuple[float, ...] | None = None


def changed_ratio(original: str, final: str) -> float:
    return round(1.0 - SequenceMatcher(None, original, final).ratio(), 3)


def _intent(context_json: str | None) -> str | None:
    try:
        data = json.loads(context_json or "{}")
    except ValueError:
        return None
    return data.get("final_intent") or data.get("intent") if isinstance(data, dict) else None


def strip_resource_sentences(text: str) -> str:
    """Без предложений с @-ресурсами: их наличие задают правила, а не примеры."""
    cleaned = _MENTION_SENTENCE_RE.sub("", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class DmFeedbackRepository:
    def __init__(self, db_path: Path | str):
        self._conn = sqlite3.connect(Path(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        for statement in _SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def record_sent(self, item: DmOutreach, *, campaign_key: str, sent_at: datetime, now: datetime) -> bool:
        """Только для строки, уже отмеченной sent. False — уже записано."""
        final = (item.primary_text or "").strip()
        original = (item.original_primary_text or final).strip()
        if not final:
            return False
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO dm_draft_feedback (draft_id, campaign, intent, source_chat_identifier, "
            "source_text, original_draft_text, final_draft_text, was_edited, changed_ratio, editor_user_id, "
            "sent_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item.id, campaign_key, _intent(item.context_json), item.source_chat_identifier, item.source_text,
             original, final, int(original != final), changed_ratio(original, final),
             item.edited_by if original != final else None, format_time(sent_at), format_time(now)),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def recent(self, campaign_key: str, *, limit: int) -> list[FeedbackExample]:
        rows = self._conn.execute(
            "SELECT id, campaign, source_text, original_draft_text, final_draft_text, was_edited, embedding_model, "
            "embedding_json FROM dm_draft_feedback WHERE campaign = ? ORDER BY sent_at DESC, id DESC LIMIT ?",
            (campaign_key, limit),
        ).fetchall()
        return [
            FeedbackExample(
                id=r[0], campaign=r[1], source_text=r[2], original_draft_text=r[3], final_draft_text=r[4],
                was_edited=bool(r[5]), embedding_model=r[6],
                embedding=tuple(json.loads(r[7])) if r[7] else None,
            )
            for r in rows
        ]

    def set_embedding(self, feedback_id: int, *, model: str, embedding: Sequence[float]) -> None:
        self._conn.execute(
            "UPDATE dm_draft_feedback SET embedding_model = ?, embedding_json = ? WHERE id = ?",
            (model, json.dumps([round(x, 6) for x in embedding]), feedback_id),
        )
        self._conn.commit()


class FeedbackRetriever:
    """Ближайшие по смыслу вопроса отправленные примеры ТОЙ ЖЕ кампании."""

    def __init__(
        self,
        repository: DmFeedbackRepository,
        embed: Callable[[list[str], str], Awaitable[list[list[float]]]],
        *,
        max_examples: int = 3,
        candidate_limit: int = 200,
        model: str = DEFAULT_EMBEDDING_MODEL,
    ):
        self._repo = repository
        self._embed = embed
        self._max = max_examples
        self._limit = candidate_limit
        self._model = model

    async def select(self, campaign_key: str, source_text: str) -> list[FeedbackExample]:
        candidates = [c for c in self._repo.recent(campaign_key, limit=self._limit) if c.campaign == campaign_key]
        if not candidates:
            return []
        missing = [c for c in candidates if c.embedding is None or c.embedding_model != self._model]
        vectors = await self._embed([source_text] + [c.source_text for c in missing], self._model)
        query, fresh = vectors[0], dict(zip((c.id for c in missing), vectors[1:]))
        for feedback_id, vector in fresh.items():
            self._repo.set_embedding(feedback_id, model=self._model, embedding=vector)
        scored = sorted(
            ((cosine(query, fresh.get(c.id) or c.embedding), c.id, c) for c in candidates),
            key=lambda s: (-s[0], -s[1]),
        )
        return [c for _, _, c in scored[: self._max]]


def format_examples_block(examples: Sequence[FeedbackExample]) -> str | None:
    if not examples:
        return None
    parts = [
        "OPERATOR-EDITED EXAMPLES\n"
        "These are examples of how operators corrected and approved similar replies.\n"
        "Learn tone, brevity, structure, ordering, phrasing and conversational style from them.\n"
        "Operator-edited examples are style/response examples, not a source of factual truth. Facts (prices, "
        "rules, terms, amounts, checkpoint status) must come ONLY from CAMPAIGN GUIDELINE / approved campaign "
        "knowledge and the current input/context above; if an example contradicts them, follow the approved "
        "knowledge. Resources, greeting and footer are governed by the rules above, not by these examples "
        "(sentences with @-resources are removed from the examples)."
    ]
    for number, example in enumerate(examples, 1):
        parts.append(
            f"Example {number}\n"
            f"User:\n{example.source_text[:600]}\n"
            f"AI originally wrote:\n{strip_resource_sentences(example.original_draft_text)[:800]}\n"
            f"Operator sent:\n{strip_resource_sentences(example.final_draft_text)[:800]}"
        )
    return "\n\n".join(parts)
