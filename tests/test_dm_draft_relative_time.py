"""Ручной режим: черновик отправляется после проверки оператором, поэтому
вычисленный относительный возраст сообщений ("35 минут назад", "за
последний час") в видимом тексте недопустим — устареет. Модели возраст не
передаётся, а серверная проверка отклоняет такой черновик. Плюс регрессия
@tplgee: его описание не про очереди/бензин. Без OpenAI/Telegram."""

import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.dm_campaigns.context import ContextItem, DraftContext, EvidenceMetadata
from reader.dm_campaigns.draft_models import DmDraftOutput, normalize_draft
from reader.dm_campaigns.draft_prompt import SYSTEM_PROMPT, build_user_text
from reader.dm_campaigns.resources import RESOURCE_HINTS, TPLGEE


def _norm(primary: str):
    output = DmDraftOutput(should_generate=True, skip_reason=None, primary_message=primary, follow_up_message=None,
                           evidence_strength="several_consistent", used_context_refs=[])
    return normalize_draft(output, follow_up_enabled=False, valid_refs=frozenset({"S1"}), fresh_context_used=True,
                           n_distinct_senders=2, allowed_resources=())


# Формулировки из реального dry-run (2026-10-05) + варианты.
@pytest.mark.parametrize("primary", [
    "В группе пишут, что 95-й есть — последнее сообщение примерно 35 минут назад.",
    "Дизель брали около часа назад.",
    "По сообщениям за последние полтора часа несколько участников пишут о наличии.",
    "По свежим сообщениям за последние ~20–50 минут ситуация противоречивая.",
    "Один пишет, что прошли за 40 минут (20 мин назад), другой — что стоят 5 часов.",
    "В группе три свежих отзыва за последние ~70 минут.",
    "Сообщения за последний час расходятся.",
    "Писали полчаса назад, что очередь большая.",
    "Последний отзыв был 2 ч. назад.",
])
def test_relative_time_is_rejected(primary):
    decision = _norm(primary)
    assert (decision.kind, decision.error) == ("invalid", "relative_time")


@pytest.mark.parametrize("primary", [
    "В свежих сообщениях группы пишут, что очередь большая и движение медленное.",
    "Недавно в группе писали, что 95-й на выезде из Владикавказа есть.",
    "Сейчас в обсуждении есть сообщения, что прошли за 40 минут, и есть — что стоят 5 часов.",
    "Российскую сторону прошли за 4 часа, грузинскую — примерно за 20 минут.",
    "Свежие сообщения расходятся: одни пишут, что прошли быстро, другие — что стоят долго.",
])
def test_neutral_wording_passes(primary):
    decision = _norm(primary)
    assert (decision.kind, decision.primary_message) == ("draft", primary)


def test_prompt_does_not_carry_message_age():
    evidence = [ContextItem(ref="S1", chat="Верхний Ларс", author="P1", time_tbilisi="14:05", age_minutes=35,
                            text="очередь большая")]
    context = DraftContext(discussion=[], evidence=evidence, fresh_context_used=True,
                           metadata=EvidenceMetadata(n_messages=1, n_distinct_senders=1, newest_age_minutes=35,
                                                     oldest_age_minutes=35, window_hours=3.0),
                           refs=frozenset({"S1"}))
    campaign = SimpleNamespace(key="border_queue", title="🚧 Очереди на границе", ai_guideline=None, resources=(),
                               follow_up_enabled=False, follow_up_guideline=None)
    outreach = SimpleNamespace(source_chat_title="Верхний Ларс", source_chat_identifier="VerhniyLars",
                               source_text="Что сейчас на Ларсе?")
    text = build_user_text(outreach, campaign, context, allowed_resources=())
    assert "мин назад" not in text and "35" not in text and "age_minutes" not in text
    assert "S1 | Верхний Ларс | 14:05 | P1: очередь большая" in text


def test_system_prompt_forbids_relative_time_and_no_longer_asks_for_it():
    assert "минут назад…" not in SYSTEM_PROMPT and "за последний час…" not in SYSTEM_PROMPT
    assert "НЕ указывай, сколько минут или часов назад" in SYSTEM_PROMPT
    assert "в свежих сообщениях группы" in SYSTEM_PROMPT


def test_tplgee_hint_is_insurance_only():
    hint = RESOURCE_HINTS[TPLGEE]
    assert "ОСАГО" in hint and "НЕ источник" in hint and "очеред" in hint and "топлив" in hint
