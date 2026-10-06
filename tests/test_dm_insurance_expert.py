"""insurance — тон эксперта по утверждённым фактам кампании: без оговорок,
без пересказа группы, без ненужных уточнений, ответ по существу + наши
ресурсы (обязательны, когда разрешены маршрутизацией). Серверная проверка
(normalize_draft(expert=True)) и prompt; реальные вопросы из групп.
fuel/border_queue (текущая обстановка) — прежний prompt и прежние правила.
Без OpenAI/Telegram."""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from test_dm_outreach_drafts import _builder, _FakeService, _out

from reader.dm_campaigns.context import DraftContext
from reader.dm_campaigns.draft_models import DmDraftOutput, DmDraftOutputExpert, normalize_draft
from reader.dm_campaigns.draft_prompt import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_BORDER,
    SYSTEM_PROMPT_EXPERT,
    build_user_text,
    system_prompt_for,
)
from reader.dm_campaigns.outreach_repository import STATUS_DRAFT, STATUS_FAILED, DmOutreachRepository
from reader.dm_campaigns.processor import DmDraftProcessor
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.resources import PROTOCOL_GE, PROTOCOL_TR, TPLGEE, allowed_resources

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
CAMPAIGN_RESOURCES = (TPLGEE, PROTOCOL_GE, PROTOCOL_TR)
GE_REQUIRED = (TPLGEE, PROTOCOL_GE)

# Реальные вопросы из групп (GE: Верхний Ларс).
REAL_QUESTIONS = [
    "Добрый день, подскажите мед страховка нужна?",
    "Страховку спрашивают на границе Грузии?",
    "На русской границе Ларса в сторону РФ проверяют страховку на российских номерах?",
    "Проверяют ли страховку в сторону РФ?",
    "Как оплатить штраф за отсутствие страховки онлайн?",
]
# Целевой стиль (из постановки) — должен проходить серверную проверку.
# Целевой стиль: живой менеджер, 2 коротких предложения, факты своими словами.
TARGET_ANSWERS = [
    "Да, с 1 января 2026 года медицинская страховка для въезда в Грузию обязательна. Если едете на машине, "
    "автостраховку можно оформить через @tplgee, а штрафы отслеживать через @ProtocolGEbot.",
    "Для машины на иностранных номерах автостраховка должна действовать весь срок пребывания в Грузии, штраф "
    "за её отсутствие — 100 лари. Полис можно оформить через @tplgee, а штрафы отслеживать через @ProtocolGEbot.",
    "На российской стороне грузинскую автостраховку не оформляют и не контролируют — это требование действует "
    "в Грузии. Полис можно оформить через @tplgee, штрафы отслеживать через @ProtocolGEbot.",
    "В сторону РФ грузинскую автостраховку на российской стороне не проверяют как требование — оно действует "
    "только в Грузии. Полис можно оформить через @tplgee, а штрафы отслеживать через @ProtocolGEbot.",
    "Штраф за отсутствие автостраховки для легковой машины — 100 лари. Его появление можно отслеживать через "
    "@ProtocolGEbot, а автостраховку оформить через @tplgee.",
]


def _norm(primary: str, *, expert=True, allowed=GE_REQUIRED, intent_text=""):
    output = DmDraftOutputExpert(should_generate=True, skip_reason=None, primary_message=primary,
                                 follow_up_message=None, evidence_strength="none", used_context_refs=[],
                                 intent="QUESTION")
    return normalize_draft(output, follow_up_enabled=False, valid_refs=frozenset(), fresh_context_used=False,
                           n_distinct_senders=0, allowed_resources=allowed, expert=expert, intent_text=intent_text)


# ---------------- маршрутизация на реальных вопросах ----------------


@pytest.mark.parametrize("question", REAL_QUESTIONS)
def test_real_ge_questions_get_tplgee_and_protocol_ge(question):
    assert allowed_resources(CAMPAIGN_RESOURCES, resource_region="ge", intent_text=question,
                             campaign_key="insurance") == GE_REQUIRED


@pytest.mark.parametrize("question,expected", [
    ("Где оформить страховку на машину для въезда в Турцию?", (TPLGEE,)),
    ("Пришёл штраф в Турции, где проверить?", (TPLGEE, PROTOCOL_TR)),
    ("Как оплатить HGS?", (TPLGEE, PROTOCOL_TR)),
])
def test_turkey_routing_unchanged(question, expected):
    resources = allowed_resources(CAMPAIGN_RESOURCES, resource_region="tr", intent_text=question,
                                  campaign_key="insurance")
    assert resources == expected and PROTOCOL_GE not in resources


def test_armenia_and_unknown_get_no_protocol_bot():
    for region in ("am", "unknown"):
        assert allowed_resources(CAMPAIGN_RESOURCES, resource_region=region, intent_text=REAL_QUESTIONS[4],
                                 campaign_key="insurance") == (TPLGEE,)


# ---------------- серверная проверка: целевой стиль ----------------


@pytest.mark.parametrize("question,answer", list(zip(REAL_QUESTIONS, TARGET_ANSWERS)))
def test_target_style_answers_pass(question, answer):
    decision = _norm(answer, intent_text=question)
    assert (decision.kind, decision.primary_message) == ("draft", answer)


@pytest.mark.parametrize("bad", [
    "В группе пишут, что медстраховку не проверяют. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "По сообщениям в группе страховку не спрашивают. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "Участники группы говорят, что не проверяют. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "Точно подтвердить не могу. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "Не могу подтвердить это. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "Не хочу вводить в заблуждение. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "Ориентируйтесь на официальные требования. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "Уточните в официальных источниках. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "Попробуйте уточнить на границе. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "Возможно, проверяют. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "Наверное, не проверяют. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "Точную стоимость сейчас не подскажу. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
])
def test_hedging_and_group_references_are_rejected(bad):
    assert (_norm(bad).kind, _norm(bad).error) == ("invalid", "expert_hedging_or_group_reference")


@pytest.mark.parametrize("bad", [
    "Уточните, пожалуйста, какую границу вы имеете в виду? Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "Какое направление вас интересует? Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "В какую сторону едете? Полис — через @tplgee, штрафы — @ProtocolGEbot.",
])
def test_unnecessary_direction_question_is_rejected(bad):
    assert _norm(bad).error == "unnecessary_clarification"


def test_ad_only_answer_is_rejected():
    assert _norm("Обратитесь в @tplgee и @ProtocolGEbot.").error == "ad_only_answer"


def test_missing_relevant_resource_is_rejected():
    answer = "Нет, на российской стороне страховку не проверяют. Полис можно оформить через @tplgee."
    assert _norm(answer).error == "missing_resource:@ProtocolGEbot"


def test_dynamic_campaigns_keep_previous_rules():
    """border/fuel — не экспертный режим: «в свежих сообщениях группы» допустимо,
    ресурсы не обязательны."""
    text = "В свежих сообщениях группы пишут, что очередь большая."
    assert _norm(text, expert=False, allowed=()).kind == "draft"


# ---------------- prompt ----------------


def test_insurance_uses_expert_prompt_others_keep_dynamic_prompt():
    assert system_prompt_for("insurance") is SYSTEM_PROMPT_EXPERT
    assert system_prompt_for("border_queue") is SYSTEM_PROMPT_BORDER and system_prompt_for("fuel") is SYSTEM_PROMPT
    for rule in ("утверждённые факты кампании важнее любых сообщений группы", "«точно подтвердить не могу»",
                 "«в группе пишут»", "не переспрашивай", "Ответ только рекламой", "КАЖДЫЙ из них"):
        assert rule in SYSTEM_PROMPT_EXPERT
    # в экспертном prompt нет прежних подсказок-оговорок
    assert "Точно подтвердить не могу.»" not in SYSTEM_PROMPT_EXPERT and "в группе пишут, что…" not in SYSTEM_PROMPT_EXPERT


def test_insurance_campaign_rules_no_longer_suggest_disclaimers():
    campaign = SimpleNamespace(key="insurance", title="🛡 Страховка", ai_guideline="ФАКТ: 100 лари.", resources=(),
                               follow_up_enabled=False, follow_up_guideline=None)
    outreach = SimpleNamespace(source_chat_title="Верхний Ларс", source_chat_identifier="VerhniyLars",
                               source_text=REAL_QUESTIONS[4])
    context = DraftContext(discussion=[], evidence=[], fresh_context_used=False, metadata=None, refs=frozenset())
    text = build_user_text(outreach, campaign, context, allowed_resources=GE_REQUIRED)
    assert "не подскажу" not in text and "ФАКТ: 100 лари." in text
    assert "@ProtocolGEbot — отслеживание грузинских штрафов" in text


# ---------------- сквозной путь processor ----------------


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach, recent = DmCampaignRepository(path), DmOutreachRepository(path), RecentMessageRepository(path)
    for key in ("insurance", "border_queue"):
        campaign = campaigns.get_campaign_by_key(key)
        campaigns.update_resources(campaign.id, list(CAMPAIGN_RESOURCES))
        campaigns.set_enabled(campaign.id, True)
    yield campaigns, outreach, recent
    for repo in (campaigns, outreach, recent):
        repo.close()


def _process(env, *, key, text, primary, message_id=7):
    campaigns, outreach, recent = env
    oid = outreach.insert_candidate(
        campaign_id=campaigns.get_campaign_by_key(key).id, source_chat_id=-1001, source_chat_identifier="VerhniyLars",
        source_chat_title="Верхний Ларс", source_message_id=message_id, source_message_at=T0, source_link=None,
        source_reply_to_msg_id=None, recipient_user_id=555, recipient_username=None, source_text=text,
        status="pending_context", now=T0, draft_after_at=T0 + timedelta(minutes=3),
    )
    service = _FakeService(_out(primary_message=primary, used_context_refs=[], evidence_strength="none"))
    processor = DmDraftProcessor(
        outreach, campaigns, recent, _builder(recent), service, interval_seconds=1, drafting_recovery_seconds=600,
        retention_hours=48, clock=lambda: T0 + timedelta(minutes=4), monotonic=lambda: 0.0,
        group_resource_regions={"VerhniyLars": "ge"},
    )
    asyncio.run(processor.run_once())
    return outreach.get(oid), service


@pytest.mark.parametrize("question,answer", list(zip(REAL_QUESTIONS, TARGET_ANSWERS)))
def test_real_questions_end_to_end_with_expert_prompt(env, question, answer):
    row, service = _process(env, key="insurance", text=question, primary=answer)
    assert (row.status, row.primary_text) == (STATUS_DRAFT, answer)
    assert service.instructions == [SYSTEM_PROMPT_EXPERT]
    block = service.calls[0].split("ALLOWED PROMOTED RESOURCES" + chr(10))[1].split(chr(10) * 2)[0]
    assert TPLGEE in block and PROTOCOL_GE in block and PROTOCOL_TR not in block


def test_real_question_hedged_model_answer_is_not_stored_as_draft(env):
    row, _ = _process(env, key="insurance", text=REAL_QUESTIONS[2],
                      primary="Точно подтвердить не могу — в группе пишут по-разному. Полис — через @tplgee, "
                              "штрафы — @ProtocolGEbot.")
    assert (row.status, row.error_kind, row.error) == (STATUS_FAILED, "ai_invalid_output",
                                                        "expert_hedging_or_group_reference")


def test_border_queue_uses_border_prompt_end_to_end(env):
    row, service = _process(env, key="border_queue", text="Что сейчас на Ларсе? Большая очередь?",
                            primary="Верхний Ларс открыт, критичных очередей сейчас нет.")
    assert row.status == STATUS_DRAFT and service.instructions == [SYSTEM_PROMPT_BORDER]


def test_expert_mode_drops_non_message_refs_instead_of_rejecting():
    """Реальный ответ модели (dry-run): в used_context_refs попали названия
    секций — в экспертном режиме они отбрасываются, черновик сохраняется."""
    output = DmDraftOutputExpert(should_generate=True, skip_reason=None, primary_message=TARGET_ANSWERS[4],
                                 follow_up_message=None, evidence_strength="none",
                                 used_context_refs=["CAMPAIGN_GUIDELINE", "ORIGINAL_MESSAGE"], intent="QUESTION")
    decision = normalize_draft(output, follow_up_enabled=False, valid_refs=frozenset(), fresh_context_used=False,
                               n_distinct_senders=0, allowed_resources=GE_REQUIRED, expert=True)
    assert (decision.kind, decision.used_context_refs) == ("draft", ())
    # fuel/border_queue: чужие метки тоже отбрасываются, но черновик, который
    # заявляет свежие сведения без единого реального сообщения, — отклоняется.
    claimed = output.model_copy(update={"evidence_strength": "several_consistent"})
    dynamic = normalize_draft(claimed, follow_up_enabled=False, valid_refs=frozenset({"B1"}), fresh_context_used=True,
                              n_distinct_senders=2, allowed_resources=GE_REQUIRED)
    assert (dynamic.kind, dynamic.error) == ("invalid", "unknown_context_refs:CAMPAIGN_GUIDELINE,ORIGINAL_MESSAGE")


def test_expert_prompt_requires_resources_in_every_answer_and_short_answers():
    for rule in ("КАЖДЫЙ из них в КАЖДОМ ответе", "Обычно 2 коротких предложения, максимум 3",
                 "не пересказывай все факты подряд", "названия секций", "Не копируй юридические формулировки",
                 "«для машины на иностранных"):
        assert rule in SYSTEM_PROMPT_EXPERT


@pytest.mark.parametrize("bad", [
    "Медстраховка обязательна, но на практике её не проверяют. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "Медстраховку пока не проверяют. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "Страховку никогда не спрашивают. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "При выезде из Грузии оформляют штраф 100 лари. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
    "Штраф 100 лари оформляют при выезде. Полис — через @tplgee, штрафы — @ProtocolGEbot.",
])
def test_retracted_claims_are_rejected(bad):
    assert _norm(bad).error == "unapproved_claim"


def test_approved_russian_side_wording_passes():
    """Разрешённая формулировка про российскую сторону — не «отозванное утверждение»."""
    assert _norm(TARGET_ANSWERS[2]).kind == "draft"


@pytest.mark.parametrize("bad", [
    "Для автомобиля, зарегистрированного за пределами Грузии, нужен полис. Полис — через @tplgee, штрафы — "
    "@ProtocolGEbot.",
    "Полис должен действовать весь период нахождения в Грузии. Оформить — через @tplgee, штрафы — @ProtocolGEbot.",
    "По грузинскому законодательству полис нужен. Оформить — через @tplgee, штрафы — @ProtocolGEbot.",
])
def test_verbatim_legal_wording_is_rejected(bad):
    assert _norm(bad).error == "legalese"


def test_more_than_three_sentences_is_rejected():
    long = ("Да, нужна. Это требование Грузии. Полис нужен на весь срок поездки. Оформить можно через @tplgee, "
            "а штрафы отслеживать через @ProtocolGEbot.")
    assert _norm(long).error == "too_long"


@pytest.mark.parametrize("answer", TARGET_ANSWERS)
def test_target_answers_are_short_and_keep_approved_meaning(answer):
    sentences = [s for s in answer.replace("!", ".").split(". ") if s.strip()]
    assert 2 <= len(sentences) <= 3 and len(answer) <= 260
    assert "гражданск" not in answer and "зарегистрированн" not in answer


def test_target_answers_keep_fact_meaning():
    medical, border, russia, _, fine = TARGET_ANSWERS
    assert "медицинская страховка" in medical and "2026" in medical and "обязательна" in medical
    assert "@tplgee" in medical.split("автостраховку")[1]  # @tplgee — про автостраховку, не про медицинскую
    assert "иностранных номерах" in border and "100 лари" in border and "весь срок" in border
    assert "не оформляют и не контролируют" in russia and "в Грузии" in russia
    assert "100 лари" in fine and "оплат" not in fine  # способа оплаты среди фактов нет


def test_soft_style_preference_is_not_a_hard_rejection():
    """«гражданская автостраховка» — шероховатость стиля (правило prompt), а не
    повод терять лид: черновик проходит."""
    text = "Для машины на иностранных номерах нужна гражданская автостраховка на весь срок в Грузии. Полис — через @tplgee, штрафы — через @ProtocolGEbot."
    assert _norm(text).kind == "draft"
