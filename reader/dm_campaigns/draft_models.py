"""Structured Output схема ЛС-черновика (см. reader/dm_campaigns/
draft_service.py) и серверная проверка/нормализация результата модели:
Pydantic-валидность ограничивает только ТИПЫ полей, не их смысл."""

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

EvidenceStrength = Literal["none", "single_report", "several_consistent", "contradictory"]


class DmDraftOutput(BaseModel):
    should_generate: bool
    skip_reason: str | None
    primary_message: str | None
    follow_up_message: str | None
    evidence_strength: EvidenceStrength
    used_context_refs: list[str]


@dataclass(frozen=True)
class DraftDecision:
    """Итог проверки: kind="draft" — сохранить черновик; "filtered" —
    модель сама отказалась (should_generate=false); "invalid" — ответ
    модели нарушает правила (error объясняет, какое)."""

    kind: Literal["draft", "filtered", "invalid"]
    primary_message: str | None = None
    follow_up_message: str | None = None
    evidence_strength: str = "none"
    used_context_refs: tuple[str, ...] = ()
    error: str | None = None


_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{4,32})")
_TME_RE = re.compile(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,32})", re.IGNORECASE)


def _handles(text: str) -> set[str]:
    return {h.lower() for h in _MENTION_RE.findall(text or "")} | {h.lower() for h in _TME_RE.findall(text or "")}


def normalize_draft(
    output: DmDraftOutput,
    *,
    follow_up_enabled: bool,
    valid_refs: frozenset[str],
    fresh_context_used: bool,
    n_distinct_senders: int,
    allowed_resources: tuple[str, ...],
) -> DraftDecision:
    if not output.should_generate:
        return DraftDecision(kind="filtered")

    primary = (output.primary_message or "").strip()
    if not primary:
        return DraftDecision(kind="invalid", error="empty_primary_message")

    follow_up = (output.follow_up_message or "").strip() or None
    if not follow_up_enabled:
        follow_up = None

    refs = tuple(dict.fromkeys(ref.strip() for ref in output.used_context_refs if ref and ref.strip()))
    unknown = [ref for ref in refs if ref not in valid_refs]
    if unknown:
        return DraftDecision(kind="invalid", error=f"unknown_context_refs:{','.join(unknown[:5])}")

    # Рекламировать можно только ресурсы из настроек кампании.
    allowed = {r.strip().lstrip("@").lower() for r in allowed_resources}
    allowed |= {_TME_RE.search(r).group(1).lower() for r in allowed_resources if _TME_RE.search(r)}
    mentioned = _handles(primary) | _handles(follow_up or "")
    unapproved = sorted(mentioned - allowed)
    if unapproved:
        return DraftDecision(kind="invalid", error=f"unapproved_resources:{','.join(unapproved[:5])}")

    # Сила свидетельств — не выше того, что реально есть: число сообщений
    # одного человека не превращается в число независимых источников.
    strength = output.evidence_strength
    if not fresh_context_used or n_distinct_senders == 0:
        strength = "none"
    elif n_distinct_senders == 1 and strength in ("several_consistent", "contradictory"):
        strength = "single_report"

    return DraftDecision(
        kind="draft", primary_message=primary, follow_up_message=follow_up,
        evidence_strength=strength, used_context_refs=refs,
    )
