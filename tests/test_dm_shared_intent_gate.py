"""Общий intent-gate для ВСЕХ ЛС-кампаний (insurance, fuel, border_queue):
один классификатор (один prompt, одна schema, одно серверное правило
reply-to-other) до генерации; генераторы и знания — у каждой кампании свои.
Без OpenAI/Telegram: классификатор и генератор — фейки."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from test_dm_outreach_drafts import _builder

from reader.core.models import Message
from reader.dm_campaigns.draft_models import DmDraftOutput, DmIntentOutput
from reader.dm_campaigns.draft_prompt import INTENT_CLASSIFIER_PROMPT, SYSTEM_PROMPT, system_prompt_for
from reader.dm_campaigns.observer import FILTER_ACTIVE_DRAFT, DmOutreachObserver
from reader.dm_campaigns.outreach_repository import (
    STATUS_DRAFT,
    STATUS_FILTERED,
    STATUS_PENDING_CONTEXT,
    DmOutreachRepository,
)
from reader.dm_campaigns.processor import DmDraftProcessor
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.scenarios import KeywordMatcher, load_scenarios

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
MATCHER = KeywordMatcher(load_scenarios(PROJECT_ROOT / "config" / "scenarios.yaml"))
GUIDELINE_MARKER = "СЕКРЕТНЫЙ ФАКТ КАМПАНИИ"


class TwoStepService:
    """Шаг 1 — фиксированная классификация; шаг 2 — фиксированный черновик."""

    def __init__(self, intent, *, has_own_problem=True, draft="Ответ по делу."):
        self.intent, self.has_own_problem, self.draft = intent, has_own_problem, draft
        self.classification_inputs, self.classification_prompts, self.generation_prompts = [], [], []

    async def generate(self, user_text, *, instructions=None, text_format=None):
        if text_format is DmIntentOutput:
            self.classification_inputs.append(user_text)
            self.classification_prompts.append(instructions)
            return DmIntentOutput(intent=self.intent, has_own_problem=self.has_own_problem, reason="test")
        self.generation_prompts.append(instructions)
        return DmDraftOutput(should_generate=True, skip_reason=None, primary_message=self.draft,
                             follow_up_message=None, evidence_strength="none", used_context_refs=[])


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach, recent = DmCampaignRepository(path), DmOutreachRepository(path), RecentMessageRepository(path)
    for key in ("fuel", "border_queue", "insurance"):
        campaign = campaigns.get_campaign_by_key(key)
        campaigns.set_enabled(campaign.id, True)
        campaigns.update_guideline(campaign.id, f"{GUIDELINE_MARKER} {key}")
        campaigns.update_resources(campaign.id, ["@tplgee", "@ProtocolGEbot"])
    yield campaigns, outreach, recent
    for repo in (campaigns, outreach, recent):
        repo.close()


def _candidate(env, key, text, *, msg_id=1, sender=555, reply_to=None, at=T0):
    campaigns, outreach, _ = env
    return outreach.insert_candidate(
        campaign_id=campaigns.get_campaign_by_key(key).id, source_chat_id=-1001, source_chat_identifier="VerhniyLars",
        source_chat_title="Верхний Ларс", source_message_id=msg_id, source_message_at=at, source_link=None,
        source_reply_to_msg_id=reply_to, recipient_user_id=sender, recipient_username=None, source_text=text,
        status=STATUS_PENDING_CONTEXT, now=at, draft_after_at=at)


def _run(env, service, at=T0 + timedelta(minutes=4)):
    campaigns, outreach, recent = env
    processor = DmDraftProcessor(outreach, campaigns, recent, _builder(recent), service, interval_seconds=1,
                                 drafting_recovery_seconds=600, retention_hours=48, clock=lambda: at,
                                 monotonic=lambda: 0.0, group_resource_regions={"VerhniyLars": "ge"})
    asyncio.run(processor.run_once())


# ---------------- общая архитектура ----------------


@pytest.mark.parametrize("key", ["insurance", "fuel", "border_queue"])
def test_one_shared_classifier_for_every_campaign_without_campaign_knowledge(env, key):
    text = {"insurance": "Где оформить страховку на машину?", "fuel": "Где заправиться бензином?",
            "border_queue": "Какая сейчас очередь на Ларсе?"}[key]
    oid = _candidate(env, key, text)
    service = TwoStepService("QUESTION")
    _run(env, service)
    assert service.classification_prompts == [INTENT_CLASSIFIER_PROMPT]
    (text,) = service.classification_inputs
    for leaked in (GUIDELINE_MARKER, "@tplgee", "@ProtocolGEbot", "ALLOWED PROMOTED RESOURCES", "CAMPAIGN"):
        assert leaked not in text
    # генератор — своей кампании (у insurance возможна одна исправляющая попытка тем же prompt)
    assert service.generation_prompts and set(service.generation_prompts) == {system_prompt_for(key)}
    if key != "insurance":
        assert env[1].get(oid).status == STATUS_DRAFT


def test_classifier_prompt_is_topic_agnostic():
    for word in ("бензин", "заправка", "очередь", "граница", "перевал", "страховка"):
        assert word in INTENT_CLASSIFIER_PROMPT
    for example in ("Где лучше заправиться после Ларса?", "Какая сейчас очередь?", "Стоим два часа и не двигаемся",
                    "проехали границу за 40 минут", "заправляйтесь после границы"):
        assert example in INTENT_CLASSIFIER_PROMPT


# ---------------- fuel ----------------


@pytest.mark.parametrize("text,intent", [
    ("Я вчера заправлялся там, всё было нормально", "STORY"),
    ("Я вчера заправлялся там, всё было нормально", "ANSWER"),
    ("Заправляйтесь после границы, там дешевле", "ADVICE"),
    ("Там бензин хороший", "OPINION"),
])
def test_fuel_not_lead_no_generation(env, text, intent):
    oid = _candidate(env, "fuel", text)
    service = TwoStepService(intent)
    _run(env, service)
    row = env[1].get(oid)
    assert (row.status, row.filter_reason) == (STATUS_FILTERED, f"not_lead_intent:{intent.lower()}")
    assert service.generation_prompts == []


@pytest.mark.parametrize("text,intent", [
    ("Где лучше заправиться после Ларса?", "QUESTION"),
    ("Бензина почти не осталось, а до границы далеко", "PERSONAL_PROBLEM"),
    ("Подскажите, где заправить 95", "HELP_REQUEST"),
])
def test_fuel_lead_generates(env, text, intent):
    oid = _candidate(env, "fuel", text)
    service = TwoStepService(intent)
    _run(env, service)
    row = env[1].get(oid)
    assert row.status == STATUS_DRAFT and service.generation_prompts == [SYSTEM_PROMPT]
    audit = json.loads(row.context_json)
    assert (audit["model_intent"], audit["final_intent"], audit["has_own_problem"]) == (intent, intent, True)


# ---------------- border_queue ----------------


@pytest.mark.parametrize("text,intent", [
    ("Проехали границу за 40 минут", "STORY"),
    ("Езжайте сейчас, очереди почти нет", "ADVICE"),
    ("Очереди почти нет", "ANSWER"),
])
def test_border_not_lead_no_generation(env, text, intent):
    oid = _candidate(env, "border_queue", text)
    service = TwoStepService(intent)
    _run(env, service)
    assert env[1].get(oid).filter_reason == f"not_lead_intent:{intent.lower()}" and service.generation_prompts == []


@pytest.mark.parametrize("text,intent", [
    ("Какая сейчас очередь?", "QUESTION"),
    ("Стоим два часа и не двигаемся", "PERSONAL_PROBLEM"),
])
def test_border_lead_generates(env, text, intent):
    oid = _candidate(env, "border_queue", text)
    service = TwoStepService(intent)
    _run(env, service)
    assert env[1].get(oid).status == STATUS_DRAFT
    assert service.generation_prompts == [system_prompt_for("border_queue")]


# ---------------- reply-to-other для fuel и border ----------------


@pytest.mark.parametrize("key,question,reply", [
    ("fuel", "Где заправиться после границы?", "Я всегда заправляюсь на первой заправке справа."),
    ("border_queue", "Какая очередь сейчас?", "Проехали за час."),
])
def test_reply_to_other_without_own_problem_is_answer(env, key, question, reply):
    _, outreach, recent = env
    recent.add(chat_id=-1001, chat_identifier="VerhniyLars", message_id=10, sender_id=111, text=question, date=T0,
               reply_to_msg_id=None)
    oid = _candidate(env, key, reply, msg_id=11, sender=222, reply_to=10, at=T0 + timedelta(minutes=1))
    service = TwoStepService("QUESTION", has_own_problem=False)  # модель ошиблась
    _run(env, service, at=T0 + timedelta(minutes=5))
    row = outreach.get(oid)
    assert (row.status, row.filter_reason) == (STATUS_FILTERED, "not_lead_intent:answer")
    assert service.generation_prompts == []
    audit = json.loads(row.context_json)
    assert (audit["model_intent"], audit["final_intent"], audit["has_own_problem"]) == ("QUESTION", "ANSWER", False)


@pytest.mark.parametrize("key,question,reply", [
    ("fuel", "Где заправиться после границы?",
     "Я тоже там заправлялся, но сейчас сам еду обратно и не знаю, будет ли ночью открыта эта АЗС. Кто знает?"),
    ("border_queue", "Какая очередь сейчас?",
     "Мы вчера прошли быстро, а сегодня родители едут обратно — какая сейчас очередь?"),
])
def test_reply_with_own_need_still_generates(env, key, question, reply):
    _, outreach, recent = env
    recent.add(chat_id=-1001, chat_identifier="VerhniyLars", message_id=20, sender_id=111, text=question, date=T0,
               reply_to_msg_id=None)
    oid = _candidate(env, key, reply, msg_id=21, sender=222, reply_to=20, at=T0 + timedelta(minutes=1))
    service = TwoStepService("QUESTION", has_own_problem=True)
    _run(env, service, at=T0 + timedelta(minutes=5))
    assert outreach.get(oid).status == STATUS_DRAFT and len(service.generation_prompts) == 1


# ---------------- dedup — по кампании ----------------


def _observe(env, text, *, msg_id, sender=555, at=T0):
    campaigns, outreach, recent = env
    observer = DmOutreachObserver(campaigns, outreach, recent, context_wait_seconds=180, clock=lambda: at,
                                  monotonic=lambda: 0.0)
    message = Message(id=msg_id, chat_id=-1001, chat_title="Верхний Ларс", sender_id=sender, sender_username=None,
                      sender_name="Ivan", text=text, date=at, link=None, chat_identifier="VerhniyLars",
                      reply_to_msg_id=None)
    observer.observe(message, MATCHER.match(text))
    return max(outreach.list_all(), key=lambda r: r.id)


def _as_draft(env, oid):
    outreach = env[1]
    outreach.claim(oid, T0)
    outreach.mark_draft(oid, now=T0, context_json="{}", primary_text="x", follow_up_text=None,
                        evidence_strength="none", used_context_refs=[])


def test_dedup_is_per_campaign(env):
    campaigns = env[0]
    insurance_row = _candidate(env, "insurance", "Где оформить страховку на машину?", msg_id=30)
    _as_draft(env, insurance_row)
    fuel = _observe(env, "Есть бензин на заправках?", msg_id=31, at=T0 + timedelta(minutes=5))
    assert fuel.campaign_id == campaigns.get_campaign_by_key("fuel").id
    assert fuel.status == STATUS_PENDING_CONTEXT  # черновик insurance не мешает fuel
    _as_draft(env, fuel.id)
    second = _observe(env, "А дизель на заправках есть, бензин?", msg_id=32, at=T0 + timedelta(minutes=8))
    assert second.campaign_id == fuel.campaign_id
    assert (second.status, second.filter_reason) == (STATUS_FILTERED, FILTER_ACTIVE_DRAFT)


def test_one_message_goes_to_one_campaign_only(env):
    """Текущая маршрутизация: одно сообщение — одна кампания (первая сработавшая
    по порядку сценариев; глобальный UNIQUE по сообщению). «Ларс» — слово
    границы, поэтому смешанный вопрос уходит в border_queue, а не в fuel."""
    campaigns, outreach, _ = env
    row = _observe(env, "Какая очередь на Ларсе и где перед границей лучше заправиться бензином?", msg_id=40)
    assert len([r for r in outreach.list_all() if r.source_message_id == 40]) == 1
    assert row.campaign_id == campaigns.get_campaign_by_key("border_queue").id


# ---------------- тон ----------------


@pytest.mark.parametrize("key", ["fuel", "border_queue", "insurance"])
def test_polite_you_rule_in_every_generator(key):
    assert "на «вы»" in system_prompt_for(key)


def test_dynamic_prompt_keeps_fresh_context_rules():
    """fuel/border_queue — прежние правила свежих сведений не тронуты."""
    for rule in ("FRESH EVIDENCE", "сообщения противоречивые", "НЕ придумывай цены",
                 "Сообщения-вопросы («а как сейчас на границе?») — не сведения о ситуации."):
        assert rule in SYSTEM_PROMPT
