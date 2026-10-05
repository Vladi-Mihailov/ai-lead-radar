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


# Служебные ref-метки контекста (reader/dm_campaigns/context.py: R1/B1/A1/S1).
# Метка считается утечкой, только если она есть в valid_refs ЭТОГО черновика:
# обычный текст вроде "трасса S1" без такой метки в контексте не трогается.
_REF = r"[ABRS]\d{1,2}"
_REF_TOKEN_RE = re.compile(rf"(?<![\w@/.]){_REF}(?!\w)")
# "(B1–B5)", "[A2]", "(см. B1, B3)" — служебная вставка целиком, удаляется.
_REF_GROUP_RE = re.compile(
    rf"\s*[\(\[]\s*(?:см\.?\s*)?{_REF}(?:\s*(?:[,;/]|–|—|-|и)\s*{_REF})*\s*[\)\]]"
)
_CONTEXT_MENTION_RE = re.compile(
    r"(?:переданн|предоставленн)\w*\s+(?:мне\s+)?(?:контекст|данн|информаци|материал|сообщени)"
    r"|по\s+(?:переданному\s+)?контексту|в\s+контексте\s+[ABRS]\d|context[_\s]?refs?",
    re.IGNORECASE,
)
# Самодеятельная техподдержка сайта — ни в одном ресурсе кампании такого нет.
_TROUBLESHOOTING_RE = re.compile(
    r"очист\w*\s+(?:кэш|кеш)|(?:кэш|кеш)\s+браузер|браузер|cookie|"
    r"поддержк\w*\s+(?:сайта\s+)?tpl|(?:введ|ввест|указ)\w*\s+vin\b",
    re.IGNORECASE,
)


# Вычисленный относительный возраст ("35 минут назад", "за последний час") —
# устареет, пока оператор проверяет черновик: такой черновик отклоняется.
_RELATIVE_TIME_RE = re.compile(
    r"(?<!\w)(?:~?\s*\d+(?:\s*[–—-]\s*\d+)?\s*(?:мин\.?|минут\w*|ч\.|час\w*)|минут\w*|час(?:а|ов)?|полчаса"
    r"|полтора\s+часа)\s+назад"
    r"|за\s+последн\w*\s+[^.!?\n]{0,20}?(?:мин\.?|минут\w*|час\w*|полчаса|полтора\s+часа)",
    re.IGNORECASE,
)


# Экспертный режим (insurance): оговорки, пересказ группы и ссылки на неё как
# на источник, уточнение уже названного направления — черновик отклоняется.
_EXPERT_HEDGING_RE = re.compile(
    r"в\s+(?:этой\s+|нашей\s+|той\s+)?(?:группе|чате)\s+(?:пишут|писали|говорят|сообщают|советуют)"
    r"|сообщени\w*\s+(?:в|из)\s+(?:этой\s+)?(?:групп|чат)\w*"
    r"|участник\w*\s+(?:групп|чат)\w*|(?:люди|участники)\s+(?:пишут|говорят)"
    r"|точно\s+(?:подтвердить|сказать|ответить)\s+не\s+могу|не\s+могу\s+(?:точно\s+)?(?:подтвердить|гарантировать)"
    r"|не\s+хочу\s+вводить\s+в\s+заблуждение|ориентируйтесь\s+на\s+официальн"
    r"|(?:уточните|проверьте|сверьтесь)\s+(?:\w+\s+){0,2}(?:в|на|по|с)\s+официальн|официальн\w*\s+источник"
    r"|попробуйте\s+уточнить|(?<!\w)возможно(?!\w)|(?<!\w)наверное(?!\w)|не\s+подскажу",
    re.IGNORECASE,
)
_EXPERT_CLARIFY_RE = re.compile(
    r"какую\s+(?:именно\s+)?границ|какое\s+(?:именно\s+)?направлени|в\s+какую\s+сторону"
    r"|о\s+какой\s+(?:именно\s+)?границ|(?:уточните|подскажите),?\s+(?:пожалуйста,?\s+)?(?:какую|какое|куда|о\s+как)",
    re.IGNORECASE,
)


# Утверждения, которые бизнес отозвал как неподтверждённые ("на практике
# не проверяют", момент/место оформления штрафа): модель могла взять их из
# сообщений группы — такой черновик отклоняется.
_EXPERT_RETRACTED_RE = re.compile(
    r"(?:на\s+практике|пока|никогда|обычно|сейчас)\s+(?:\w+\s+){0,2}не\s+(?:проверя|спрашива|смотр)"
    r"|при\s+выезде\s+(?:\w+\s+){0,3}(?:оформля|выписыва|наклад)\w*\s+штраф"
    r"|штраф\w*\s+(?:\w+\s+){0,3}(?:оформля|выписыва)\w*\s+при\s+выезде",
    re.IGNORECASE,
)


# Стиль живого менеджера: тяжёлые юридические обороты из фактов и раздутые
# ответы (больше 3 предложений) до оператора не доходят. Мягкие предпочтения
# ("автостраховка" вместо "гражданская автостраховка") — только в prompt:
# отклонённый черновик — потерянный лид.
_EXPERT_LEGALESE_RE = re.compile(
    r"зарегистрированн\w*\s+за\s+пределами"
    r"|(?:весь|всего)\s+период\w*\s+(?:нахождени|пребывани)|в\s+соответствии\s+с\s+законодательств"
    r"|по\s+грузинскому\s+законодательству",
    re.IGNORECASE,
)
MAX_EXPERT_SENTENCES = 3


def _expert_problem(text: str) -> str | None:
    if _EXPERT_HEDGING_RE.search(text):
        return "expert_hedging_or_group_reference"
    if _EXPERT_RETRACTED_RE.search(text):
        return "unapproved_claim"
    if _EXPERT_CLARIFY_RE.search(text):
        return "unnecessary_clarification"
    if _EXPERT_LEGALESE_RE.search(text):
        return "legalese"
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    if len(sentences) > MAX_EXPERT_SENTENCES:
        return "too_long"
    if sentences and all("@" in s for s in sentences):
        return "ad_only_answer"
    return None


def _strip_ref_groups(text: str, valid_refs: frozenset[str]) -> str:
    def drop(match: re.Match) -> str:
        refs = re.findall(_REF, match.group(0))
        return "" if refs and all(ref in valid_refs for ref in refs) else match.group(0)

    cleaned = _REF_GROUP_RE.sub(drop, text)
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return re.sub(r"[ \t]+\n", "\n", cleaned).strip()


def _text_problem(text: str, valid_refs: frozenset[str]) -> str | None:
    if any(ref in valid_refs for ref in _REF_TOKEN_RE.findall(text)):
        return "internal_ref_leak"
    if _CONTEXT_MENTION_RE.search(text):
        return "context_mention"
    if _TROUBLESHOOTING_RE.search(text):
        return "unsupported_troubleshooting"
    if _RELATIVE_TIME_RE.search(text):
        return "relative_time"
    return None


def normalize_draft(
    output: DmDraftOutput,
    *,
    follow_up_enabled: bool,
    valid_refs: frozenset[str],
    fresh_context_used: bool,
    n_distinct_senders: int,
    allowed_resources: tuple[str, ...],
    expert: bool = False,
) -> DraftDecision:
    if not output.should_generate:
        return DraftDecision(kind="filtered")

    primary = (output.primary_message or "").strip()
    if not primary:
        return DraftDecision(kind="invalid", error="empty_primary_message")

    follow_up = (output.follow_up_message or "").strip() or None
    if not follow_up_enabled:
        follow_up = None

    # Видимый пользователю текст: служебные вставки "(B1–B5)" вырезаются,
    # любая оставшаяся метка/упоминание контекста/выдуманная техподдержка —
    # черновик отклоняется (метки живут только в used_context_refs).
    primary = _strip_ref_groups(primary, valid_refs)
    follow_up = _strip_ref_groups(follow_up, valid_refs) or None if follow_up else None
    for text in (primary, follow_up):
        problem = _text_problem(text, valid_refs) if text else None
        if problem:
            return DraftDecision(kind="invalid", error=problem)
    if not primary:
        return DraftDecision(kind="invalid", error="empty_primary_message")

    refs = tuple(dict.fromkeys(ref.strip() for ref in output.used_context_refs if ref and ref.strip()))
    if expert:
        # Экспертный режим отвечает по фактам кампании: ссылки на сообщения —
        # лишь метаданные; чужие метки ("CAMPAIGN_GUIDELINE") отбрасываются, а
        # не губят черновик (в самом тексте метки по-прежнему запрещены выше).
        refs = tuple(ref for ref in refs if ref in valid_refs)
    unknown = [ref for ref in refs if ref not in valid_refs]
    if unknown:
        return DraftDecision(kind="invalid", error=f"unknown_context_refs:{','.join(unknown[:5])}")

    # Экспертный режим (insurance): без оговорок/пересказа группы, не только
    # реклама, и КАЖДЫЙ разрешённый ресурс упомянут (наши ресурсы обязательны).
    if expert:
        for text in (primary, follow_up):
            problem = _expert_problem(text) if text else None
            if problem:
                return DraftDecision(kind="invalid", error=problem)
        handles = _handles(primary)
        missing = [r for r in allowed_resources if r.strip().lstrip("@").lower() not in handles]
        if missing:
            return DraftDecision(kind="invalid", error=f"missing_resource:{','.join(missing[:5])}")

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
