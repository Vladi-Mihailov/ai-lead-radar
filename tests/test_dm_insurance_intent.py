"""insurance: кому вообще писать (роль сообщения), транзит через Грузию,
ответ только по релевантной части, без утечки темы медстраховки, и один
черновик на человека в одной дискуссии. Реальные production-примеры
(dm_outreach #66–#101, 2026-10-05). Без OpenAI/Telegram: проверяется
серверная часть (normalize_draft, маршрутизация, dedup) и prompt."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from test_dm_outreach_drafts import _builder, _FakeService, _out

from reader.core.models import Message
from reader.dm_campaigns.draft_models import (
    INSUFFICIENT_INTENT,
    LEAD_INTENTS,
    DmDraftOutputExpert,
    DmIntentOutput,
    MessageIntent,
    normalize_draft,
    resolve_intent,
)
from reader.dm_campaigns.draft_prompt import (
    INTENT_CLASSIFIER_PROMPT,
    SYSTEM_PROMPT_EXPERT,
    build_classification_text,
    reply_to_other,
)
from reader.dm_campaigns.observer import ACTIVE_DRAFT_WINDOW, FILTER_ACTIVE_DRAFT, DmOutreachObserver
from reader.dm_campaigns.outreach_repository import (
    STATUS_DRAFT,
    STATUS_FILTERED,
    STATUS_PENDING_CONTEXT,
    DmOutreachRepository,
)
from reader.dm_campaigns.processor import DmDraftProcessor
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.resources import PROTOCOL_GE, PROTOCOL_TR, TPLGEE, allowed_resources, georgia_transit
from reader.scenarios import KeywordMatcher, load_scenarios

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
RESOURCES = (TPLGEE, PROTOCOL_GE, PROTOCOL_TR)
GE = (TPLGEE, PROTOCOL_GE)
MATCHER = KeywordMatcher(load_scenarios(PROJECT_ROOT / "config" / "scenarios.yaml"))

# ---- реальные production-сообщения ----
Q66 = "Мед страховку спрашивают на границе?"
Q86 = "Всем привет. Скажите профукал страховку на неделю. Как быть и что сейчас на выезде будет?"
Q88 = "Автостраховку?"
Q91 = ("В любом случае спросите на кпп когда будете выезжать. Оплатить можно прям там в банке. Но страховку "
       "сделайте обязательно. На выезде они проверяют и штраф (100 лари) выпишут точно, если раньше не поймает патруль.")
Q92 = "Я сейчас буду выезжать и только сейчас понял что нет страховки"
Q95 = "Врядли дадут выехать без страховки,,, как проедете напишите пожалуйста,,,,"
Q96 = "Думаю, что штраф за отсутствие страховки нужно будет оплатить, там есть окошко на прием платежей"
Q98 = ("Выпустят без страховки, штраф 100 лари. На данный момент этот штраф в размере не увеличивается. "
       "Можно сделать на 15 дней и выехать без штрафа.")
Q100 = ("Здравствуйте, подскажите пожалуйста кто знает, хочу поехать из Армении в Россию, на своей машине, у меня есть "
        "и Армянские права и Российские, где на каких правах сделать страховку и на таможне какие права показывать?")
Q101 = ("Это в Грузии ? Просто если не ошибаюсь у кого российские права в России не имеют право на других прав ездить, "
        "хочу еще узнать, когда делают страховку как указывают права ? Для Грузи и Росси?")

AUTO_ANSWER = ("За езду без автостраховки предусмотрен штраф 100 лари. Лучше оформить полис сейчас через @tplgee, "
               "а штрафы проверять через @ProtocolGEbot.")
MEDICAL_ANSWER = ("Да, с 1 января 2026 года медицинская страховка для въезда в Грузию обязательна. Если едете на машине, "
                  "автостраховку можно оформить через @tplgee, а штрафы отслеживать через @ProtocolGEbot.")
TRANSIT_ANSWER = ("Если едете из Армении в Россию через Грузию, на время нахождения машины в Грузии нужна действующая "
                  "автостраховка. Её можно оформить через @tplgee, а грузинские штрафы отслеживать через @ProtocolGEbot.")


def _decide(intent, primary=AUTO_ANSWER, *, question="", should_generate=True, skip_reason=None, allowed=GE):
    output = DmDraftOutputExpert(should_generate=should_generate, skip_reason=skip_reason, primary_message=primary,
                                 follow_up_message=None, evidence_strength="none", used_context_refs=[], intent=intent)
    return normalize_draft(output, follow_up_enabled=False, valid_refs=frozenset(), fresh_context_used=False,
                           n_distinct_senders=0, allowed_resources=allowed, expert=True, intent_text=question)


# ---------------- схема и prompt ----------------


def test_intent_schema_has_exactly_the_eight_categories():
    assert set(get_args(MessageIntent)) == {"QUESTION", "HELP_REQUEST", "PERSONAL_PROBLEM", "ADVICE", "ANSWER",
                                            "STORY", "OPINION", "OTHER"}
    assert LEAD_INTENTS == {"QUESTION", "HELP_REQUEST", "PERSONAL_PROBLEM"}
    assert "intent" in DmDraftOutputExpert.model_json_schema()["required"]


def test_classifier_prompt_is_separate_from_generation_prompt():
    for rule in ("QUESTION", "HELP_REQUEST", "PERSONAL_PROBLEM", "ANSWER", "ADVICE", "STORY", "OPINION",
                 "не делают сообщение", "сообщение ДРУГОГО участника", "has_own_problem"):
        assert rule in INTENT_CLASSIFIER_PROMPT
    assert "Определи intent" not in SYSTEM_PROMPT_EXPERT  # роль решается до генерации, отдельно
    for rule in ("Маршрут через Грузию", "Медицинскую страховку упоминай ТОЛЬКО", "не факты",
                 "@tplgee никогда не предлагай как медицинскую страховку", "не чек-лист"):
        assert rule in SYSTEM_PROMPT_EXPERT


# ---------------- LEAD: должны остаться ----------------


@pytest.mark.parametrize("question,intent,answer", [
    (Q66, "QUESTION", MEDICAL_ANSWER),
    (Q86, "PERSONAL_PROBLEM", AUTO_ANSWER),
    (Q92, "PERSONAL_PROBLEM", AUTO_ANSWER),
])
def test_real_leads_generate(question, intent, answer):
    decision = _decide(intent, answer, question=question)
    assert (decision.kind, decision.intent) == ("draft", intent)


# ---------------- NOT LEAD: советы, мнения, ответы ----------------


@pytest.mark.parametrize("question,intent", [
    (Q91, "ADVICE"), (Q95, "ADVICE"), (Q96, "OPINION"), (Q98, "ADVICE"),
    ("Да, у меня так же было, выпустили спокойно.", "STORY"),
    ("Страховку на выезде проверяют.", "ANSWER"),
])
def test_advice_opinion_story_answer_never_become_drafts(question, intent):
    # даже если модель «хочет» ответить и написала хороший текст
    decision = _decide(intent, AUTO_ANSWER, question=question)
    assert (decision.kind, decision.filter_reason) == ("filtered", f"not_lead_intent:{intent.lower()}")


def test_keyword_alone_is_not_a_lead_other():
    decision = _decide("OTHER", AUTO_ANSWER, question="Страховка, штраф, полис.")
    assert decision.filter_reason == "not_lead_intent:other"


# ---------------- AMBIGUOUS #88 ----------------


def test_short_reply_with_clear_own_context_generates():
    decision = _decide("QUESTION", AUTO_ANSWER, question=f"{Q88} {Q86}")
    assert decision.kind == "draft"


def test_short_reply_without_enough_context_is_rejected():
    decision = _decide("OTHER", None, question=Q88, should_generate=False, skip_reason=INSUFFICIENT_INTENT)
    assert (decision.kind, decision.filter_reason) == ("filtered", INSUFFICIENT_INTENT)


# ---------------- TRANSIT / PARTIAL RELEVANCE #100/#101 ----------------


@pytest.mark.parametrize("text", [Q100, Q101, "Еду из Турции в Россию на машине, какая страховка нужна?",
                                  "Из России в Армению на авто, что со страховкой?", "Едем через Ларс в Армению"])
def test_georgia_transit_is_detected(text):
    assert georgia_transit(text)


@pytest.mark.parametrize("text", ["Страховка для поездки по Армении", "Где оформить ОСАГО в Турции?",
                                  "Нужна страховка в России"])
def test_no_transit_without_route_through_georgia(text):
    assert not georgia_transit(text)


@pytest.mark.parametrize("region", ["am", "unknown", "ge"])
def test_transit_question_gets_georgia_resources_from_any_group(region):
    assert allowed_resources(RESOURCES, resource_region=region, intent_text=Q100, campaign_key="insurance") == GE


def test_turkey_fines_keep_tr_bot_even_when_route_mentions_georgia():
    text = "Еду из Турции в Россию через Сарпи, пришёл штраф в Турции"
    assert allowed_resources(RESOURCES, resource_region="tr", intent_text=text,
                             campaign_key="insurance") == (TPLGEE, PROTOCOL_TR)


def test_transit_answer_is_partial_useful_and_without_medical():
    decision = _decide("QUESTION", TRANSIT_ANSWER, question=Q100)
    assert decision.kind == "draft"
    for unknown in ("права", "российск", "таможн", "ОСАГО РФ", "медицин"):
        assert unknown not in decision.primary_message.lower()


def test_transit_answer_with_medical_leak_is_rejected():
    leaked = ("С 1 января 2026 года медстраховка для въезда в Грузию обязательна. Автостраховку можно оформить через "
              "@tplgee, а штрафы отслеживать через @ProtocolGEbot.")
    assert _decide("QUESTION", leaked, question=Q100).error == "topic_leak_medical"


# ---------------- TOPIC LEAK #92 ----------------


def test_auto_only_question_must_not_mention_medical():
    bad = ("Нужно оформить страховки: с 1 января 2026 медстраховка для въезда в Грузию обязательна. Полис — через "
           "@tplgee, штрафы — через @ProtocolGEbot.")
    assert _decide("PERSONAL_PROBLEM", bad, question=Q92).error == "topic_leak_medical"
    assert _decide("PERSONAL_PROBLEM", AUTO_ANSWER, question=Q92).kind == "draft"


def test_medical_question_may_mention_medical():
    assert _decide("QUESTION", MEDICAL_ANSWER, question=Q66).kind == "draft"


# ---------------- ACTIVE DRAFT DEDUP ----------------


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach, recent = DmCampaignRepository(path), DmOutreachRepository(path), RecentMessageRepository(path)
    insurance = campaigns.set_enabled(campaigns.get_campaign_by_key("insurance").id, True)
    campaigns.update_resources(insurance.id, list(RESOURCES))
    yield campaigns, outreach, recent, insurance
    for repo in (campaigns, outreach, recent):
        repo.close()


def _message(msg_id, text, *, sender=555, username="lead_person", at=T0):
    return Message(id=msg_id, chat_id=-1001, chat_title="Верхний Ларс", sender_id=sender, sender_username=username,
                   sender_name="Ivan", text=text, date=at, link=None, chat_identifier="VerhniyLars",
                   reply_to_msg_id=None)


def _make_draft(outreach, insurance, *, msg_id, sender=555, at=T0):
    oid = outreach.insert_candidate(
        campaign_id=insurance.id, source_chat_id=-1001, source_chat_identifier="VerhniyLars",
        source_chat_title="Верхний Ларс", source_message_id=msg_id, source_message_at=at, source_link=None,
        source_reply_to_msg_id=None, recipient_user_id=sender, recipient_username="lead_person",
        source_text="Страховку спрашивают?", status=STATUS_PENDING_CONTEXT, now=at, draft_after_at=at)
    outreach.claim(oid, at)
    outreach.mark_draft(oid, now=at, context_json="{}", primary_text=AUTO_ANSWER, follow_up_text=None,
                        evidence_strength="none", used_context_refs=[])
    return oid


def _observe(env, message, now):
    campaigns, outreach, recent, _ = env
    observer = DmOutreachObserver(campaigns, outreach, recent, context_wait_seconds=180, clock=lambda: now,
                                  monotonic=lambda: 0.0)
    observer.observe(message, MATCHER.match(message.text))
    return max(outreach.list_all(), key=lambda r: r.id)


def test_second_candidate_of_same_person_within_two_hours_is_filtered(env):
    _, outreach, _, insurance = env
    _make_draft(outreach, insurance, msg_id=31)  # как #31
    row = _observe(env, _message(33, "Проверяют ли страховку на машину в сторону рф", at=T0 + timedelta(minutes=12)),
                   T0 + timedelta(minutes=12))  # как #33
    assert (row.status, row.filter_reason) == (STATUS_FILTERED, FILTER_ACTIVE_DRAFT)
    first = outreach.get(1)
    assert (first.status, first.primary_text) == (STATUS_DRAFT, AUTO_ANSWER)  # первый не переписан


def test_after_two_hours_new_candidate_is_allowed(env):
    _, outreach, _, insurance = env
    _make_draft(outreach, insurance, msg_id=31)
    later = T0 + ACTIVE_DRAFT_WINDOW + timedelta(minutes=1)
    row = _observe(env, _message(40, "Где оформить страховку на машину?", at=later), later)
    assert row.status == STATUS_PENDING_CONTEXT


def test_other_person_or_other_campaign_is_not_affected(env):
    campaigns, outreach, _, insurance = env
    _make_draft(outreach, insurance, msg_id=31)
    row = _observe(env, _message(41, "Где оформить страховку на машину?", sender=777, username="someone_else"),
                   T0 + timedelta(minutes=5))
    assert row.status == STATUS_PENDING_CONTEXT
    fuel = campaigns.get_campaign_by_key("fuel")
    assert not outreach.active_draft_exists(campaign_id=fuel.id, recipient_user_id=555, recipient_username=None,
                                            since=T0 - timedelta(hours=1))


def test_same_username_different_user_id_is_not_deduplicated(env):
    _, outreach, _, insurance = env
    _make_draft(outreach, insurance, msg_id=31, sender=555)
    assert not outreach.active_draft_exists(campaign_id=insurance.id, recipient_user_id=999,
                                            recipient_username="lead_person", since=T0 - timedelta(hours=1))


def test_processor_filters_when_draft_appeared_while_waiting_for_context(env):
    campaigns, outreach, recent, insurance = env
    pending = outreach.insert_candidate(
        campaign_id=insurance.id, source_chat_id=-1001, source_chat_identifier="VerhniyLars",
        source_chat_title="Верхний Ларс", source_message_id=50, source_message_at=T0, source_link=None,
        source_reply_to_msg_id=None, recipient_user_id=555, recipient_username="lead_person",
        source_text="А штраф большой?", status=STATUS_PENDING_CONTEXT, now=T0, draft_after_at=T0)
    _make_draft(outreach, insurance, msg_id=51)  # черновик появился, пока кандидат ждал
    service = _FakeService(_out(primary_message=AUTO_ANSWER, used_context_refs=[], evidence_strength="none"))
    processor = DmDraftProcessor(outreach, campaigns, recent, _builder(recent), service, interval_seconds=1,
                                 drafting_recovery_seconds=600, retention_hours=48,
                                 clock=lambda: T0 + timedelta(minutes=4), monotonic=lambda: 0.0)
    asyncio.run(processor.run_once())
    row = outreach.get(pending)
    assert (row.status, row.filter_reason) == (STATUS_FILTERED, FILTER_ACTIVE_DRAFT) and service.calls == []


# ---------------- сквозной путь: роль хранится как причина фильтра ----------------


def test_processor_stores_not_lead_intent_reason(env):
    campaigns, outreach, recent, insurance = env
    oid = outreach.insert_candidate(
        campaign_id=insurance.id, source_chat_id=-1001, source_chat_identifier="VerhniyLars",
        source_chat_title="Верхний Ларс", source_message_id=98, source_message_at=T0, source_link=None,
        source_reply_to_msg_id=None, recipient_user_id=31, recipient_username=None, source_text=Q98,
        status=STATUS_PENDING_CONTEXT, now=T0, draft_after_at=T0)
    service = _FakeService(_out(primary_message=AUTO_ANSWER, used_context_refs=[], evidence_strength="none",
                                intent="ADVICE", skip_reason="совет другим участникам", should_generate=False))
    processor = DmDraftProcessor(outreach, campaigns, recent, _builder(recent), service, interval_seconds=1,
                                 drafting_recovery_seconds=600, retention_hours=48,
                                 clock=lambda: T0 + timedelta(minutes=4), monotonic=lambda: 0.0,
                                 group_resource_regions={"VerhniyLars": "ge"})
    asyncio.run(processor.run_once())
    row = outreach.get(oid)
    assert (row.status, row.filter_reason, row.primary_text) == (STATUS_FILTERED, "not_lead_intent:advice", None)
    assert json.loads(row.context_json)["intent"] == "ADVICE"


# ---------------- исправляющая попытка ----------------


class _SequenceService:
    """Шаг 1 (классификация) — intent/has_own_problem; шаг 2 — outputs по очереди."""

    def __init__(self, outputs, *, intent="QUESTION", has_own_problem=True):
        self.outputs, self.calls, self.classifications = list(outputs), [], []
        self.intent, self.has_own_problem = intent, has_own_problem

    async def generate(self, user_text, *, instructions=None, text_format=None):
        if text_format is DmIntentOutput:
            self.classifications.append(user_text)
            return DmIntentOutput(intent=self.intent, has_own_problem=self.has_own_problem, reason="test")
        self.calls.append(user_text)
        return self.outputs.pop(0)


def _run_transit(env, outputs, **service_kwargs):
    campaigns, outreach, recent, insurance = env
    oid = outreach.insert_candidate(
        campaign_id=insurance.id, source_chat_id=-1001, source_chat_identifier="VerhniyLars",
        source_chat_title="Верхний Ларс", source_message_id=100, source_message_at=T0, source_link=None,
        source_reply_to_msg_id=None, recipient_user_id=32, recipient_username=None, source_text=Q100,
        status=STATUS_PENDING_CONTEXT, now=T0, draft_after_at=T0)
    service = _SequenceService(outputs, **service_kwargs)
    processor = DmDraftProcessor(outreach, campaigns, recent, _builder(recent), service, interval_seconds=1,
                                 drafting_recovery_seconds=600, retention_hours=48,
                                 clock=lambda: T0 + timedelta(minutes=4), monotonic=lambda: 0.0,
                                 group_resource_regions={"VerhniyLars": "ge"})
    asyncio.run(processor.run_once())
    return outreach.get(oid), service


def _expert_out(primary, intent="QUESTION"):
    return DmDraftOutputExpert(should_generate=True, skip_reason=None, primary_message=primary, follow_up_message=None,
                               evidence_strength="none", used_context_refs=[], intent=intent)


LEAKY = ("С 1 января 2026 года медстраховка для въезда в Грузию обязательна. Автостраховку можно оформить через "
         "@tplgee, а штрафы отслеживать через @ProtocolGEbot.")


def test_fixable_rejection_gets_one_corrective_retry(env):
    row, service = _run_transit(env, [_expert_out(LEAKY), _expert_out(TRANSIT_ANSWER)])
    assert row.status == STATUS_DRAFT and row.primary_text.endswith(" " + TRANSIT_ANSWER)  # + приветствие
    assert len(service.calls) == 2 and "медицинская страховка, а USER о ней не спрашивал" in service.calls[1]


def test_corrective_retry_happens_only_once(env):
    row, service = _run_transit(env, [_expert_out(LEAKY), _expert_out(LEAKY)])
    assert (row.status, row.error) == ("failed", "topic_leak_medical") and len(service.calls) == 2


def test_not_lead_intent_stops_before_generation(env):
    row, service = _run_transit(env, [_expert_out(TRANSIT_ANSWER)], intent="ADVICE")
    assert row.filter_reason == "not_lead_intent:advice"
    assert len(service.classifications) == 1 and service.calls == []  # генерация не запускалась


def test_prompts_cover_asking_others_opinion_and_no_approved_answer():
    for rule in ("ЧУЖУЮ ситуацию", "«Автостраховку?»", "вряд ли дадут выехать"):
        assert rule in INTENT_CLASSIFIER_PROMPT
    for rule in ("no_approved_answer", "не подменяй ответ общим фактом"):
        assert rule in SYSTEM_PROMPT_EXPERT


def test_reply_to_another_participant_is_flagged_in_prompt():
    from types import SimpleNamespace

    from reader.dm_campaigns.context import ContextItem, DraftContext
    from reader.dm_campaigns.draft_prompt import build_user_text
    campaign = SimpleNamespace(key="insurance", title="🛡 Страховка", ai_guideline="факты", resources=(),
                               follow_up_enabled=False, follow_up_guideline=None)
    outreach = SimpleNamespace(source_chat_title="Верхний Ларс", source_chat_identifier="VerhniyLars", source_text=Q88)
    other = ContextItem(ref="R1", chat="Верхний Ларс", author="P1", time_tbilisi="20:52", age_minutes=6, text=Q86,
                        note="сообщение, на которое отвечает USER")
    ctx = DraftContext(discussion=[other], evidence=[], fresh_context_used=False, metadata=None, refs=frozenset({"R1"}))
    text = build_user_text(outreach, campaign, ctx, allowed_resources=GE)
    assert "REPLY TARGET" in text and "ДРУГОГО участника (P1, R1)" in text
    own = ContextItem(ref="R1", chat="Верхний Ларс", author="USER", time_tbilisi="20:52", age_minutes=6, text=Q86,
                      note="сообщение, на которое отвечает USER")
    text = build_user_text(outreach, campaign, DraftContext(discussion=[own], evidence=[], fresh_context_used=False,
                                                            metadata=None, refs=frozenset({"R1"})), allowed_resources=GE)
    assert "своё собственное сообщение" in text
    border = SimpleNamespace(**{**vars(campaign), "key": "border_queue"})
    assert "REPLY TARGET" not in build_user_text(outreach, border, ctx, allowed_resources=())



# ---------------- шаг 1: отдельная классификация ----------------


@pytest.mark.parametrize("intent", ["QUESTION", "HELP_REQUEST", "PERSONAL_PROBLEM"])
def test_lead_intents_pass_resolution(intent):
    out = DmIntentOutput(intent=intent, has_own_problem=True, reason="")
    assert resolve_intent(out, reply_to_other=False) == intent


@pytest.mark.parametrize("intent", ["QUESTION", "HELP_REQUEST", "PERSONAL_PROBLEM"])
def test_reply_to_other_without_own_problem_is_forced_to_answer(intent):
    out = DmIntentOutput(intent=intent, has_own_problem=False, reason="")
    assert resolve_intent(out, reply_to_other=True) == "ANSWER"


def test_reply_to_other_with_own_problem_stays_lead():
    out = DmIntentOutput(intent="PERSONAL_PROBLEM", has_own_problem=True, reason="")
    assert resolve_intent(out, reply_to_other=True) == "PERSONAL_PROBLEM"


@pytest.mark.parametrize("intent", ["ANSWER", "ADVICE", "STORY", "OPINION", "OTHER"])
def test_non_lead_intents_are_kept(intent):
    out = DmIntentOutput(intent=intent, has_own_problem=True, reason="")
    assert resolve_intent(out, reply_to_other=False) == intent


def test_reply_to_other_forced_answer_skips_generation_end_to_end(env):
    """#88-подобный случай: модель сказала QUESTION, но USER отвечает на чужое
    сообщение и своей проблемы нет — сервер делает ANSWER, генерации нет."""
    campaigns, outreach, recent, insurance = env
    from reader.dm_campaigns.recent_messages import RecentMessage  # noqa: F401
    recent.add(chat_id=-1001, chat_identifier="VerhniyLars", message_id=86, sender_id=26, text=Q86, date=T0,
               reply_to_msg_id=None)
    oid = outreach.insert_candidate(
        campaign_id=insurance.id, source_chat_id=-1001, source_chat_identifier="VerhniyLars",
        source_chat_title="Верхний Ларс", source_message_id=88, source_message_at=T0 + timedelta(minutes=6),
        source_link=None, source_reply_to_msg_id=86, recipient_user_id=24, recipient_username=None,
        source_text=Q88, status=STATUS_PENDING_CONTEXT, now=T0 + timedelta(minutes=6),
        draft_after_at=T0 + timedelta(minutes=6))
    service = _SequenceService([_expert_out(AUTO_ANSWER)], intent="QUESTION", has_own_problem=False)
    processor = DmDraftProcessor(outreach, campaigns, recent, _builder(recent), service, interval_seconds=1,
                                 drafting_recovery_seconds=600, retention_hours=48,
                                 clock=lambda: T0 + timedelta(minutes=10), monotonic=lambda: 0.0,
                                 group_resource_regions={"VerhniyLars": "ge"})
    asyncio.run(processor.run_once())
    row = outreach.get(oid)
    assert (row.status, row.filter_reason) == (STATUS_FILTERED, "not_lead_intent:answer")
    assert service.calls == [] and len(service.classifications) == 1
    stored = json.loads(row.context_json)
    assert (stored["intent"], stored["model_intent"]) == ("ANSWER", "QUESTION")
    assert "ДРУГОГО участника" in service.classifications[0]  # REPLY TARGET дошёл до классификатора


def test_classification_input_has_no_campaign_facts_or_resources():
    from types import SimpleNamespace

    from reader.dm_campaigns.context import DraftContext
    outreach = SimpleNamespace(source_chat_title="Верхний Ларс", source_chat_identifier="VerhniyLars", source_text=Q92)
    text = build_classification_text(outreach, DraftContext(discussion=[], evidence=[], fresh_context_used=False,
                                                            metadata=None, refs=frozenset()))
    assert Q92 in text and "самостоятельное сообщение" in text
    assert "ALLOWED PROMOTED RESOURCES" not in text and "CAMPAIGN GUIDELINE" not in text
    assert not reply_to_other(DraftContext(discussion=[], evidence=[], fresh_context_used=False, metadata=None,
                                           refs=frozenset()))


def test_expert_prompt_requires_formal_you():
    assert "на «вы»" in SYSTEM_PROMPT_EXPERT
