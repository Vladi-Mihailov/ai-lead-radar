"""Тесты сборки контекста, генерации и обработки ЛС-черновиков (Phase 2) —
без реального OpenAI/Telegram, только временная БД."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import openai
import pytest

from reader.core.engine import MatchEngine
from reader.core.models import Message
from reader.core.pipeline import Pipeline
from reader.dm_campaigns.context import DmContextBuilder
from reader.dm_campaigns.draft_models import DmDraftOutput, normalize_draft
from reader.dm_campaigns.draft_prompt import SYSTEM_PROMPT, build_user_text
from reader.dm_campaigns.draft_service import DmDraftService, DmDraftServiceError
from reader.dm_campaigns.observer import DmOutreachObserver
from reader.dm_campaigns.outreach_repository import (
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_FILTERED,
    STATUS_PENDING_CONTEXT,
    DmOutreachRepository,
)
from reader.dm_campaigns.processor import DmDraftProcessor
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.scenarios import KeywordMatcher, load_scenarios
from reader.sinks.base import BaseSink
from reader.sources.base import BaseSource
from reader.users.repository import UserRepository

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
SCENARIOS = load_scenarios(PROJECT_ROOT / "config" / "scenarios.yaml")
LARS, DARIALI = -1001, -1002


class _Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach, recent = DmCampaignRepository(path), DmOutreachRepository(path), RecentMessageRepository(path)
    yield path, campaigns, outreach, recent
    for repo in (campaigns, outreach, recent):
        repo.close()


def _buffer(recent, message_id, text, *, chat_id=LARS, ident="VerhniyLars", sender=1, at=T0, reply=None):
    recent.add(chat_id=chat_id, chat_identifier=ident, message_id=message_id, sender_id=sender,
               text=text, date=at, reply_to_msg_id=reply)


def _candidate(outreach, campaign, *, message_id=100, chat_id=LARS, ident="VerhniyLars", text="Что сейчас на Ларсе?",
               sender=555, reply=None, at=T0, wait=180):
    oid = outreach.insert_candidate(
        campaign_id=campaign.id, source_chat_id=chat_id, source_chat_identifier=ident,
        source_chat_title="Верхний Ларс", source_message_id=message_id, source_message_at=at, source_link=None,
        source_reply_to_msg_id=reply, recipient_user_id=sender, recipient_username="ivan", source_text=text,
        status=STATUS_PENDING_CONTEXT, now=at, draft_after_at=at + timedelta(seconds=wait),
    )
    return outreach.get(oid)


def _builder(recent, **kwargs):
    return DmContextBuilder(recent, SCENARIOS, group_titles={"VerhniyLars": "Верхний Ларс"}, **kwargs)


# ---- контекст ----


def test_discussion_before_after_reply_and_pseudonyms(env):
    _, campaigns, outreach, recent = env
    border = campaigns.get_campaign_by_key("border_queue")
    _buffer(recent, 90, "старое сообщение", at=T0 - timedelta(hours=2), sender=7)  # вне окна "до"
    _buffer(recent, 95, "Кто едет сегодня?", at=T0 - timedelta(minutes=10), sender=7)
    _buffer(recent, 98, "Мы выезжаем вечером", at=T0 - timedelta(minutes=5), sender=8, reply=95)
    _buffer(recent, 100, "Что сейчас на Ларсе?", at=T0, sender=555, reply=98)
    _buffer(recent, 101, "Стояли 2 часа утром", at=T0 + timedelta(minutes=1), sender=9, reply=100)
    _buffer(recent, 105, "поздно", at=T0 + timedelta(minutes=10), sender=9)  # после draft_after_at
    row = _candidate(outreach, border, reply=98)
    ctx = _builder(recent).build(row, border, T0 + timedelta(minutes=4))
    refs = [i.ref for i in ctx.discussion]
    assert refs == ["B1", "B2", "A1"]  # 98 уже в "до" — в цепочку не дублируется
    a1 = next(i for i in ctx.discussion if i.ref == "A1")
    assert (a1.author, a1.note, a1.age_minutes, a1.chat) == ("P3", "ответ на исходное сообщение USER", 3, "Верхний Ларс")
    payload = ctx.to_json()
    assert "555" not in payload and "ivan" not in payload


def test_reply_chain_depth_two(env):
    _, campaigns, outreach, recent = env
    border = campaigns.get_campaign_by_key("border_queue")
    _buffer(recent, 10, "корень", at=T0 - timedelta(minutes=50), sender=1)
    _buffer(recent, 20, "ответ 1", at=T0 - timedelta(minutes=40), sender=2, reply=10)
    _buffer(recent, 30, "ответ 2", at=T0 - timedelta(minutes=30), sender=3, reply=20)
    row = _candidate(outreach, border, reply=30)
    ctx = _builder(recent, max_context_messages=0).build(row, border, T0 + timedelta(minutes=4))
    assert [(i.ref, i.text) for i in ctx.discussion] == [("R1", "ответ 1"), ("R2", "ответ 2")]


def test_border_fresh_evidence_filters_and_metadata(env):
    _, campaigns, outreach, recent = env
    border = campaigns.get_campaign_by_key("border_queue")
    now = T0 + timedelta(minutes=4)
    _buffer(recent, 1, "На Ларсе очередь 2 часа", at=T0 - timedelta(minutes=30), sender=11)
    _buffer(recent, 2, "кпп Дариали стоим час", chat_id=DARIALI, ident="verkhniy_lars_dariali", at=T0 - timedelta(minutes=20), sender=12)
    _buffer(recent, 3, "кто продаёт шины?", at=T0 - timedelta(minutes=10), sender=13)  # не по теме
    _buffer(recent, 4, "очередь была вчера", at=T0 - timedelta(hours=4), sender=14)  # старше 3 часов
    _buffer(recent, 5, "Сколько очередь на Ларсе?", at=T0 - timedelta(minutes=5), sender=555)  # сам спрашивающий
    _buffer(recent, 100, "Что сейчас на Ларсе?", at=T0, sender=555)
    row = _candidate(outreach, border)
    ctx = _builder(recent).build(row, border, now)
    assert ctx.fresh_context_used is True
    assert [(i.ref, i.text) for i in ctx.evidence] == [("S1", "кпп Дариали стоим час"), ("S2", "На Ларсе очередь 2 часа")]
    meta = ctx.metadata
    assert (meta.n_messages, meta.n_distinct_senders, meta.newest_age_minutes, meta.oldest_age_minutes, meta.window_hours) == (2, 2, 24, 34, 3)


def test_fresh_context_chats_take_precedence_over_source_chats(env):
    _, campaigns, outreach, recent = env
    border = campaigns.get_campaign_by_key("border_queue")
    campaigns.update_source_chats(border.id, ["VerhniyLars"])
    campaigns.update_fresh_context_chats(border.id, ["verkhniy_lars_dariali"])
    border = campaigns.get_campaign(border.id)
    _buffer(recent, 1, "очередь на Ларсе большая", at=T0 - timedelta(minutes=30), sender=11)
    _buffer(recent, 2, "очередь на Дариали", chat_id=DARIALI, ident="verkhniy_lars_dariali", at=T0 - timedelta(minutes=20), sender=12)
    ctx = _builder(recent).build(_candidate(outreach, border), border, T0 + timedelta(minutes=4))
    assert [i.text for i in ctx.evidence] == ["очередь на Дариали"]


def test_same_sender_many_messages_counts_once_and_limits(env):
    _, campaigns, outreach, recent = env
    border = campaigns.get_campaign_by_key("border_queue")
    for i in range(1, 31):
        _buffer(recent, i, "очередь " + "х" * 600, at=T0 - timedelta(minutes=i), sender=42)
    ctx = _builder(recent).build(_candidate(outreach, border), border, T0 + timedelta(minutes=4))
    assert len(ctx.evidence) <= 15
    assert all(len(i.text) <= 401 for i in ctx.evidence)
    assert sum(len(i.text) for i in ctx.evidence) <= 6000
    assert ctx.metadata.n_distinct_senders == 1


def test_insurance_does_not_use_fresh_evidence_and_empty_buffer(env):
    _, campaigns, outreach, recent = env
    insurance = campaigns.get_campaign_by_key("insurance")
    ctx = _builder(recent).build(_candidate(outreach, insurance, text="Где сделать страховку?"), insurance, T0)
    assert (ctx.fresh_context_used, ctx.evidence, ctx.metadata, ctx.discussion) == (False, [], None, [])


def test_prompt_contains_no_identifiers(env):
    _, campaigns, outreach, recent = env
    border = campaigns.get_campaign_by_key("border_queue")
    campaigns.update_resources(border.id, ["@tplgee"])
    border = campaigns.get_campaign(border.id)
    row = _candidate(outreach, border)
    text = build_user_text(row, border, _builder(recent).build(row, border, T0))
    assert "555" not in text and "ivan" not in text
    assert "ALLOWED PROMOTED RESOURCES\n@tplgee" in text and "EVIDENCE METADATA" in text
    assert "should_generate" in SYSTEM_PROMPT


# ---- нормализация ответа модели ----


def _out(**kwargs):
    base = dict(should_generate=True, skip_reason=None, primary_message="Ответ по делу.", follow_up_message=None,
                evidence_strength="several_consistent", used_context_refs=["S1"])
    base.update(kwargs)
    return DmDraftOutput(**base)


def _norm(output, **kwargs):
    base = dict(follow_up_enabled=False, valid_refs=frozenset({"S1", "S2", "B1"}), fresh_context_used=True,
                n_distinct_senders=2, allowed_resources=("@tplgee", "@ProtocolGEbot"))
    base.update(kwargs)
    return normalize_draft(output, **base)


def test_normal_draft():
    d = _norm(_out(primary_message="  Есть два свежих отзыва. Страховку можно оформить через @tplgee  "))
    assert (d.kind, d.primary_message, d.evidence_strength, d.used_context_refs) == (
        "draft", "Есть два свежих отзыва. Страховку можно оформить через @tplgee", "several_consistent", ("S1",))


def test_follow_up_enabled_and_disabled():
    assert _norm(_out(follow_up_message="Второе"), follow_up_enabled=True).follow_up_message == "Второе"
    assert _norm(_out(follow_up_message="Второе"), follow_up_enabled=False).follow_up_message is None


def test_should_generate_false_is_filtered():
    assert _norm(_out(should_generate=False, primary_message=None, skip_reason="не про бензин")).kind == "filtered"


@pytest.mark.parametrize("claimed", ["several_consistent", "contradictory"])
def test_single_reporter_caps_strength(claimed):
    assert _norm(_out(evidence_strength=claimed), n_distinct_senders=1).evidence_strength == "single_report"


def test_contradictory_kept_with_several_senders_and_none_without_evidence():
    assert _norm(_out(evidence_strength="contradictory"), n_distinct_senders=3).evidence_strength == "contradictory"
    assert _norm(_out(), n_distinct_senders=0).evidence_strength == "none"
    assert _norm(_out(used_context_refs=[]), fresh_context_used=False).evidence_strength == "none"


@pytest.mark.parametrize("output,error_prefix", [
    (_out(used_context_refs=["S9"]), "unknown_context_refs"),
    (_out(primary_message="Пишите @some_other_bot"), "unapproved_resources"),
    (_out(primary_message="Смотрите t.me/random_channel"), "unapproved_resources"),
    (_out(primary_message="   "), "empty_primary_message"),
])
def test_invalid_outputs(output, error_prefix):
    d = _norm(output)
    assert d.kind == "invalid" and d.error.startswith(error_prefix)


def test_email_is_not_treated_as_mention():
    assert _norm(_out(primary_message="Почта tplgee@mail.ru")).kind == "draft"


# ---- DmDraftService (фейковый клиент) ----

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


class _Parsed:
    def __init__(self, value):
        self.output_parsed = value


class _FakeResponses:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    async def parse(self, **kwargs):
        self.calls += 1
        assert kwargs["text_format"] is DmDraftOutput
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Parsed(outcome)


class _FakeClient:
    def __init__(self, outcomes):
        self.responses = _FakeResponses(outcomes)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr("reader.dm_campaigns.draft_service._RETRY_DELAY_SECONDS", 0)


async def test_service_returns_parsed_output():
    client = _FakeClient([_out()])
    assert (await DmDraftService(api_key="x", model="m", client=client).generate("t")).primary_message == "Ответ по делу."


async def test_service_retries_once_on_transient_error():
    client = _FakeClient([openai.APIConnectionError(request=_REQUEST), _out()])
    await DmDraftService(api_key="x", model="m", client=client).generate("t")
    assert client.responses.calls == 2


async def test_service_final_failure_after_retry():
    client = _FakeClient([openai.APITimeoutError(request=_REQUEST), openai.APITimeoutError(request=_REQUEST)])
    with pytest.raises(DmDraftServiceError):
        await DmDraftService(api_key="x", model="m", client=client).generate("t")
    assert client.responses.calls == 2


async def test_service_5xx_retry_and_4xx_no_retry():
    err500 = openai.APIStatusError("boom", response=httpx.Response(500, request=_REQUEST), body=None)
    client = _FakeClient([err500, _out()])
    await DmDraftService(api_key="x", model="m", client=client).generate("t")
    assert client.responses.calls == 2
    err400 = openai.APIStatusError("bad", response=httpx.Response(400, request=_REQUEST), body=None)
    client = _FakeClient([err400])
    with pytest.raises(DmDraftServiceError):
        await DmDraftService(api_key="x", model="m", client=client).generate("t")
    assert client.responses.calls == 1


async def test_service_missing_structured_output():
    with pytest.raises(DmDraftServiceError):
        await DmDraftService(api_key="x", model="m", client=_FakeClient([None])).generate("t")


# ---- обработчик ----


class _FakeService:
    def __init__(self, output=None, *, error=None, delay=0.0):
        self.output = output if output is not None else _out(used_context_refs=[])
        self.error = error
        self.delay = delay
        self.calls = []

    async def generate(self, user_text):
        self.calls.append(user_text)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.output


def _processor(env, service, clock, **kwargs):
    _, campaigns, outreach, recent = env
    params = dict(interval_seconds=1, drafting_recovery_seconds=600, retention_hours=48, clock=clock, monotonic=lambda: 0.0)
    params.update(kwargs)
    return DmDraftProcessor(outreach, campaigns, recent, _builder(recent), service, **params)


async def test_processor_waits_for_context_then_drafts(env):
    _, campaigns, outreach, recent = env
    fuel = campaigns.set_enabled(campaigns.get_campaign_by_key("fuel").id, True)
    row = _candidate(outreach, fuel, text="Есть бензин на М4?")
    clock = _Clock(T0 + timedelta(minutes=2))
    service = _FakeService(_out(used_context_refs=[], evidence_strength="single_report"))
    proc = _processor(env, service, clock)
    assert await proc.run_once() == 0 and service.calls == []  # окно контекста ещё не прошло
    clock.now = T0 + timedelta(minutes=3)
    assert await proc.run_once() == 1
    done = outreach.get(row.id)
    assert (done.status, done.primary_text, done.evidence_strength) == (STATUS_DRAFT, "Ответ по делу.", "none")
    assert json.loads(done.context_json)["fresh_context_used"] is True
    assert len(service.calls) == 1
    assert await proc.run_once() == 0 and len(service.calls) == 1  # повторно не генерируется


async def test_campaign_disabled_before_generation(env):
    _, campaigns, outreach, recent = env
    fuel = campaigns.get_campaign_by_key("fuel")
    row = _candidate(outreach, fuel)  # кампания выключена к моменту генерации
    service = _FakeService()
    await _processor(env, service, _Clock(T0 + timedelta(minutes=5))).run_once()
    done = outreach.get(row.id)
    assert (done.status, done.filter_reason, service.calls) == (STATUS_FILTERED, "campaign_disabled_before_generation", [])


async def test_source_changed_before_generation(env):
    _, campaigns, outreach, recent = env
    fuel = campaigns.set_enabled(campaigns.get_campaign_by_key("fuel").id, True)
    row = _candidate(outreach, fuel)
    campaigns.update_source_chats(fuel.id, ["Sadahlo"])
    service = _FakeService()
    await _processor(env, service, _Clock(T0 + timedelta(minutes=5))).run_once()
    assert (outreach.get(row.id).filter_reason, service.calls) == ("source_chat_not_allowed_before_generation", [])


async def test_ai_not_suitable_is_filtered(env):
    _, campaigns, outreach, recent = env
    fuel = campaigns.set_enabled(campaigns.get_campaign_by_key("fuel").id, True)
    row = _candidate(outreach, fuel)
    service = _FakeService(_out(should_generate=False, primary_message=None, skip_reason="дизельный двигатель, не про топливо"))
    await _processor(env, service, _Clock(T0 + timedelta(minutes=5))).run_once()
    done = outreach.get(row.id)
    assert (done.status, done.filter_reason, done.primary_text) == (STATUS_FILTERED, "ai_not_suitable", None)
    assert json.loads(done.context_json)["ai_skip_reason"].startswith("дизельный")


@pytest.mark.parametrize("service,kind", [
    (_FakeService(error=DmDraftServiceError("transient failure after retry")), "ai_error"),
    (_FakeService(_out(used_context_refs=["S7"])), "ai_invalid_output"),
])
async def test_ai_failures(env, service, kind):
    _, campaigns, outreach, recent = env
    fuel = campaigns.set_enabled(campaigns.get_campaign_by_key("fuel").id, True)
    row = _candidate(outreach, fuel)
    await _processor(env, service, _Clock(T0 + timedelta(minutes=5))).run_once()
    done = outreach.get(row.id)
    assert (done.status, done.error_kind) == (STATUS_FAILED, kind)


async def test_ai_timeout(env):
    _, campaigns, outreach, recent = env
    fuel = campaigns.set_enabled(campaigns.get_campaign_by_key("fuel").id, True)
    row = _candidate(outreach, fuel)
    await _processor(env, _FakeService(delay=1.0), _Clock(T0 + timedelta(minutes=5)), generation_timeout_seconds=0.01).run_once()
    assert (outreach.get(row.id).status, outreach.get(row.id).error_kind) == (STATUS_FAILED, "ai_timeout")


async def test_recovery_and_attempt_limit(env):
    _, campaigns, outreach, recent = env
    fuel = campaigns.set_enabled(campaigns.get_campaign_by_key("fuel").id, True)
    row = _candidate(outreach, fuel)
    clock = _Clock(T0 + timedelta(minutes=5))
    for _ in range(3):  # трижды "упал" посреди генерации
        assert outreach.claim(row.id, clock.now)
        clock.now += timedelta(minutes=11)
        outreach.recover_stale(stale_before=clock.now - timedelta(minutes=10), now=clock.now)
    service = _FakeService()
    await _processor(env, service, clock).run_once()
    done = outreach.get(row.id)
    assert (done.status, done.error_kind, service.calls) == (STATUS_FAILED, "too_many_attempts", [])


async def test_processor_recovers_stale_drafting_then_drafts(env):
    _, campaigns, outreach, recent = env
    fuel = campaigns.set_enabled(campaigns.get_campaign_by_key("fuel").id, True)
    row = _candidate(outreach, fuel)
    outreach.claim(row.id, T0 + timedelta(minutes=3))
    await _processor(env, _FakeService(), _Clock(T0 + timedelta(minutes=20))).run_once()
    assert outreach.get(row.id).status == STATUS_DRAFT


async def test_processor_cleans_old_buffer(env):
    _, campaigns, outreach, recent = env
    _buffer(recent, 1, "старое", at=T0 - timedelta(hours=50))
    _buffer(recent, 2, "свежее", at=T0 - timedelta(hours=1))
    await _processor(env, _FakeService(), _Clock(T0)).run_once()
    assert recent.count() == 1


async def test_run_forever_survives_tick_errors(env):
    proc = _processor(env, _FakeService(), _Clock(T0), interval_seconds=0.001)
    calls = {"n": 0}

    async def broken():
        calls["n"] += 1
        if calls["n"] >= 3:
            raise asyncio.CancelledError
        raise RuntimeError("db down")

    proc.run_once = broken
    with pytest.raises(asyncio.CancelledError):
        await proc.run_forever()
    assert calls["n"] == 3


# ---- интеграция с существующим Pipeline ----


class _Source(BaseSource):
    def __init__(self, messages):
        self._messages = messages

    async def start(self):
        pass

    async def messages(self):
        for message in self._messages:
            yield message

    async def stop(self):
        pass


class _Sink(BaseSink):
    def __init__(self):
        self.events = []

    async def handle(self, event):
        self.events.append(event)


def _message(message_id, text, *, sender=555, username="ivan", at=T0, reply=None):
    return Message(id=message_id, chat_id=LARS, chat_title="Верхний Ларс", sender_id=sender, sender_username=username,
                   sender_name=None, text=text, date=at, link=None, chat_identifier="VerhniyLars", reply_to_msg_id=reply)


def _pipeline(env, messages, clock):
    path, campaigns, outreach, recent = env
    users = UserRepository(path)
    observer = DmOutreachObserver(campaigns, outreach, recent, context_wait_seconds=180, clock=clock, monotonic=lambda: 0.0)
    sink = _Sink()
    pipeline = Pipeline(_Source(messages), MatchEngine(KeywordMatcher(SCENARIOS)), [sink], users, dm_observer=observer)
    return pipeline, sink, users


async def test_pipeline_end_to_end_border_draft(env):
    _, campaigns, outreach, recent = env
    border = campaigns.set_enabled(campaigns.get_campaign_by_key("border_queue").id, True)
    campaigns.update_resources(border.id, ["@tplgee"])
    clock = _Clock()
    messages = [
        _message(1, "На Ларсе очередь часа два", sender=11, at=T0 - timedelta(minutes=40)),
        _message(2, "Подтверждаю, очередь большая", sender=12, at=T0 - timedelta(minutes=20)),
        _message(3, "Что сейчас на Ларсе? Сколько очередь?", at=T0),
        _message(4, "Утром было 2 часа", sender=13, at=T0 + timedelta(minutes=1), reply=3),
    ]
    pipeline, sink, users = _pipeline(env, messages, clock)
    await pipeline.run()
    users.close()

    # Существующий lead-поток не изменился: 3 совпадения car_border_crossing,
    # сообщение 4 ни с чем не совпало — только попадает в буфер контекста.
    assert [e.message.id for e in sink.events] == [1, 2, 3]
    rows = outreach.list_all()
    assert [(r.source_message_id, r.status) for r in rows] == [
        (1, STATUS_PENDING_CONTEXT), (2, STATUS_PENDING_CONTEXT), (3, STATUS_PENDING_CONTEXT)]
    assert recent.count() == 4

    service = _FakeService(_out(primary_message="Несколько участников пишут, что очередь около двух часов.",
                                evidence_strength="several_consistent", used_context_refs=["S1", "A1"]))
    clock.now = T0 + timedelta(minutes=4)
    await _processor(env, service, clock).run_once()
    done = next(r for r in outreach.list_all() if r.source_message_id == 3)
    done = outreach.get(done.id)
    assert done.status == STATUS_DRAFT
    assert done.evidence_strength == "several_consistent"
    assert done.used_context_refs == ("S1", "A1")
    prompt = next(text for text in service.calls if "Сколько очередь" in text.split("ORIGINAL MESSAGE (USER)\n")[1][:60])
    assert "n_distinct_senders=" in prompt and "555" not in prompt and "ivan" not in prompt
    # @tplgee стоит в ресурсах кампании, но вопрос не про страховку — в промпт он не попадает.
    resources_block = prompt.split("ALLOWED PROMOTED RESOURCES\n")[1].split("\n\n")[0]
    assert "@tplgee" not in resources_block


async def test_pipeline_campaigns_off_lead_flow_unchanged_no_dm(env):
    _, campaigns, outreach, recent = env
    pipeline, sink, users = _pipeline(env, [_message(1, "Где сделать страховку на Грузию?"), _message(2, "Есть бензин на М4?")], _Clock())
    await pipeline.run()
    users.close()
    assert [e.message.id for e in sink.events] == [1]  # страховка — как раньше; бензин (forward_leads=false) — нет
    assert outreach.list_all() == [] and recent.count() == 0
    service = _FakeService()
    await _processor(env, service, _Clock(T0 + timedelta(hours=1))).run_once()
    assert service.calls == []


async def test_pipeline_survives_observer_failure(env):
    path, *_ = env

    class _Broken:
        def observe(self, message, matches):
            raise RuntimeError("db locked")

    users = UserRepository(path)
    sink = _Sink()
    pipeline = Pipeline(_Source([_message(1, "Где сделать страховку?")]), MatchEngine(KeywordMatcher(SCENARIOS)), [sink], users,
                        dm_observer=_Broken())
    await pipeline.run()
    users.close()
    assert len(sink.events) == 1
