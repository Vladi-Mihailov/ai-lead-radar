"""Feedback learning ЛС-черновиков: запись примера только после успешной
отправки, неизменный исходный AI-текст, выбор примеров только своей
кампании, блок примеров в prompt только при включённом флаге, приоритет
утверждённых фактов и серверных проверок. Без OpenAI/Telegram."""

import asyncio
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from telethon import errors
from test_dm_outreach_drafts import _builder, _FakeService, _out
from test_dm_send import ADMIN, T0, click_and_confirm, confirm, draft, env, press  # noqa: F401  (env — fixture)

from reader.dm_campaigns.draft_prompt import SenderPersona, build_user_text
from reader.dm_campaigns.feedback import (
    DmFeedbackRepository,
    FeedbackRetriever,
    format_examples_block,
    strip_resource_sentences,
)
from reader.dm_campaigns.outreach_repository import STATUS_DRAFT, STATUS_FAILED, DmOutreachRepository
from reader.dm_campaigns.processor import DmDraftProcessor
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.resources import PROTOCOL_GE, PROTOCOL_TR, TPLGEE
from reader.inviter_admin_bot import dm_draft_callbacks as cb
from reader.settings import DmOutreachSettings

AI_TEXT = "Оформить ОСАГО можно онлайн у @tplgee."


def feedback_rows(path):
    import sqlite3
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM dm_draft_feedback ORDER BY id")]
    finally:
        conn.close()


@pytest.fixture
def fb(env):  # noqa: F811
    repo = DmFeedbackRepository(env.path)
    env.service._feedback = repo
    yield env
    repo.close()


# ---------------- запись: только после успешной отправки ----------------


def test_edited_and_sent_creates_feedback(fb):
    row_id = draft(fb)
    assert fb.outreach.edit_primary_text(row_id, "Да, ОСАГО нужно — оформите онлайн.", operator_id=ADMIN, now=T0)
    click_and_confirm(fb, row_id)
    (row,) = feedback_rows(fb.path)
    assert (row["draft_id"], row["campaign"], row["was_edited"], row["editor_user_id"]) == (row_id, "insurance", 1,
                                                                                            ADMIN)
    assert (row["original_draft_text"], row["final_draft_text"]) == (AI_TEXT, "Да, ОСАГО нужно — оформите онлайн.")
    assert row["source_text"] == "Где оформить ОСАГО?" and 0 < row["changed_ratio"] <= 1 and row["sent_at"]


def test_unedited_sent_draft_is_positive_example(fb):
    row_id = draft(fb)
    click_and_confirm(fb, row_id)
    (row,) = feedback_rows(fb.path)
    assert (row["was_edited"], row["changed_ratio"], row["editor_user_id"]) == (0, 0.0, None)
    assert row["original_draft_text"] == row["final_draft_text"] == AI_TEXT


def test_edited_but_not_sent_has_no_feedback(fb):
    row_id = draft(fb)
    fb.outreach.edit_primary_text(row_id, "Правка без отправки", operator_id=ADMIN, now=T0)
    press(fb, cb.ACTION_APPROVE, row_id)  # только preview, без «Да, отправить»
    assert feedback_rows(fb.path) == []


@pytest.mark.parametrize("effect", [errors.RPCError(request=None, message="X"), ConnectionError("down")])
def test_failed_or_uncertain_send_has_no_feedback(fb, effect):
    row_id = draft(fb)
    fb.tg.send_effects = [effect]
    click_and_confirm(fb, row_id)
    assert fb.outreach.get(row_id).status == "send_failed" and feedback_rows(fb.path) == []


def test_skipped_and_blocked_have_no_feedback(fb):
    skipped = draft(fb)
    press(fb, cb.ACTION_SKIP, skipped)
    fb.tg.chats.clear()
    unreadable = draft(fb, sendability="source_message", username=None, message_id=777)
    click_and_confirm(fb, unreadable)
    assert fb.outreach.get(skipped).status == "skipped" and feedback_rows(fb.path) == []


def test_repeated_send_callback_creates_one_feedback_row(fb):
    row_id = draft(fb)
    click_and_confirm(fb, row_id)
    confirm(fb, row_id)  # повторный «✅ Да, отправить»
    assert len(fb.tg.sent) == 1 and len(feedback_rows(fb.path)) == 1
    item = fb.outreach.get(row_id)
    assert fb.service._feedback.record_sent(item, campaign_key="insurance", sent_at=T0, now=T0) is False


def test_feedback_failure_does_not_change_send_outcome(fb, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(fb.service._feedback, "record_sent", boom)
    row_id = draft(fb)
    click_and_confirm(fb, row_id)
    assert fb.outreach.get(row_id).status == "sent" and len(fb.tg.sent) == 1


def test_original_ai_text_survives_multiple_edits(fb):
    row_id = draft(fb)
    for text in ("Первая правка", "Вторая правка", "Третья правка"):
        assert fb.outreach.edit_primary_text(row_id, text, operator_id=ADMIN, now=T0)
    item = fb.outreach.get(row_id)
    assert (item.original_primary_text, item.primary_text) == (AI_TEXT, "Третья правка")
    click_and_confirm(fb, row_id)
    assert feedback_rows(fb.path)[0]["original_draft_text"] == AI_TEXT


def test_regenerated_draft_does_not_keep_stale_original(tmp_path):
    repo = DmOutreachRepository(tmp_path / "users.db")
    campaigns = DmCampaignRepository(tmp_path / "users.db")
    try:
        oid = repo.insert_candidate(
            campaign_id=campaigns.get_campaign_by_key("insurance").id, source_chat_id=-1, source_chat_identifier="x",
            source_chat_title="x", source_message_id=1, source_message_at=T0, source_link=None,
            source_reply_to_msg_id=None, recipient_user_id=1, recipient_username="u", source_text="q",
            status="pending_context", now=T0, draft_after_at=T0,
        )
        repo.claim(oid, T0)
        repo.mark_draft(oid, now=T0, context_json="{}", primary_text="AI v1", follow_up_text=None,
                        evidence_strength="none", used_context_refs=[])
        repo.edit_primary_text(oid, "оператор", operator_id=ADMIN, now=T0)
        repo._conn.execute("UPDATE dm_outreach SET status='pending_context' WHERE id=?", (oid,))
        repo.claim(oid, T0)
        repo.mark_draft(oid, now=T0, context_json="{}", primary_text="AI v2", follow_up_text=None,
                        evidence_strength="none", used_context_refs=[])
        item = repo.get(oid)
        assert (item.primary_text, item.original_primary_text, item.edited_by) == ("AI v2", None, None)
    finally:
        repo.close()
        campaigns.close()


# ---------------- выбор примеров ----------------

_TOPICS = ("бензин", "страх", "очеред", "дизел")


async def _fake_embed(texts, model, calls=None):
    if calls is not None:
        calls.append(list(texts))
    return [[float(t.lower().count(k)) for k in _TOPICS] + [0.1] for t in texts]


def _store(repo, draft_id, campaign, source, final, *, original=None):
    item = SimpleNamespace(id=draft_id, primary_text=final, original_primary_text=original, edited_by=ADMIN,
                           context_json=json.dumps({"final_intent": "QUESTION"}), source_chat_identifier="VerhniyLars",
                           source_text=source)
    assert repo.record_sent(item, campaign_key=campaign, sent_at=T0 + timedelta(minutes=draft_id), now=T0)


@pytest.fixture
def repo(tmp_path):
    r = DmFeedbackRepository(tmp_path / "fb.db")
    yield r
    r.close()


def test_insurance_never_retrieves_fuel_examples(repo):
    _store(repo, 1, "fuel", "Где бензин? бензин бензин страх", "Бензин есть.")
    _store(repo, 2, "insurance", "Нужна ли страховка?", "Да, нужна.")
    found = asyncio.run(FeedbackRetriever(repo, _fake_embed, max_examples=5).select("insurance", "бензин страх"))
    assert [e.id for e in found] == [2] and all(e.campaign == "insurance" for e in found)


def test_max_examples_respected_and_most_similar_first(repo):
    for i, source in enumerate(["очередь на ларсе", "страховка нужна?", "страховка страховка на границе",
                                "дизель где", "страховка штраф"], start=1):
        _store(repo, i, "insurance", source, f"ответ {i}")
    found = asyncio.run(FeedbackRetriever(repo, _fake_embed, max_examples=2).select("insurance", "страховка"))
    assert len(found) == 2 and {e.id for e in found} <= {2, 3, 5}  # только про страховку


def test_embeddings_are_cached_between_selections(repo):
    _store(repo, 1, "insurance", "страховка", "да")
    calls = []

    async def embed(texts, model):
        return await _fake_embed(texts, model, calls)
    retriever = FeedbackRetriever(repo, embed, max_examples=3)
    asyncio.run(retriever.select("insurance", "страховка?"))
    asyncio.run(retriever.select("insurance", "страховка?"))
    assert calls == [["страховка?", "страховка"], ["страховка?"]]


def test_settings_default_disabled_and_bounded():
    s = DmOutreachSettings()
    assert (s.feedback_learning_enabled, s.feedback_learning_max_examples) == (False, 3)
    with pytest.raises(ValueError):
        DmOutreachSettings(feedback_learning_max_examples=6)


# ---------------- prompt и генерация ----------------


def test_examples_block_strips_resources_and_marks_facts_as_non_authoritative(repo):
    _store(repo, 1, "fuel", "Сколько дизель?", "Дизель 4.8 GEL. Штрафы — через @ProtocolGEbot.",
           original="Дизель около 4.8 лари. Страховка тут: @tplgee.")
    block = format_examples_block(repo.recent("fuel", limit=5))
    assert block.startswith("OPERATOR-EDITED EXAMPLES")
    assert "not a source of factual truth" in block and "follow the approved knowledge" in block
    assert "@" not in block.split("Example 1")[1] and "Дизель 4.8 GEL." in block
    assert strip_resource_sentences("Да. Оформить тут: @tplgee. Ещё совет.") == "Да. Ещё совет."


@pytest.fixture
def penv(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach, recent = DmCampaignRepository(path), DmOutreachRepository(path), RecentMessageRepository(path)
    for key in ("insurance", "fuel"):
        campaign = campaigns.get_campaign_by_key(key)
        campaigns.update_resources(campaign.id, [TPLGEE, PROTOCOL_GE, PROTOCOL_TR])
        campaigns.set_enabled(campaign.id, True)
    feedback = DmFeedbackRepository(path)
    yield SimpleNamespace(campaigns=campaigns, outreach=outreach, recent=recent, feedback=feedback)
    for r in (campaigns, outreach, recent, feedback):
        r.close()


def _run(penv, *, primary, retriever=None, key="insurance", text="Нужна ли страховка на машину?",
         sender_persona=None):
    oid = penv.outreach.insert_candidate(
        campaign_id=penv.campaigns.get_campaign_by_key(key).id, source_chat_id=-1001,
        source_chat_identifier="VerhniyLars", source_chat_title="Верхний Ларс", source_message_id=7,
        source_message_at=T0, source_link=None, source_reply_to_msg_id=None, recipient_user_id=555,
        recipient_username=None, source_text=text, status="pending_context", now=T0,
        draft_after_at=T0 + timedelta(minutes=3),
    )
    service = _FakeService(_out(primary_message=primary, used_context_refs=[], evidence_strength="none"))
    processor = DmDraftProcessor(
        penv.outreach, penv.campaigns, penv.recent, _builder(penv.recent), service, interval_seconds=1,
        drafting_recovery_seconds=600, retention_hours=48, clock=lambda: T0 + timedelta(minutes=4),
        monotonic=lambda: 0.0, group_resource_regions={"VerhniyLars": "ge"}, feedback_retriever=retriever,
        sender_persona=sender_persona,
    )
    asyncio.run(processor.run_once())
    return penv.outreach.get(oid), service


GOOD = "Да, автостраховка нужна, без неё штраф 100 лари. Оформить можно тут: @tplgee, а штрафы — через @ProtocolGEbot."


def test_learning_disabled_keeps_prompt_unchanged(penv):
    _store(penv.feedback, 1, "insurance", "страховка нужна?", "Да, нужна.")
    row, service = _run(penv, primary=GOOD)
    assert row.status == STATUS_DRAFT and "OPERATOR-EDITED EXAMPLES" not in service.calls[0]
    assert "feedback_example_ids" not in json.loads(row.context_json)


def test_build_user_text_without_examples_is_identical():
    outreach = SimpleNamespace(source_text="Нужна страховка?", source_chat_title="Ларс", source_chat_identifier="x")
    campaign = SimpleNamespace(key="insurance", title="Страховка", resources=(TPLGEE,), ai_guideline="факты",
                               follow_up_enabled=False, follow_up_guideline=None)
    context = SimpleNamespace(discussion=[], evidence=[], fresh_context_used=False, metadata=None)
    assert build_user_text(outreach, campaign, context) == build_user_text(outreach, campaign, context,
                                                                           feedback_examples=())


def test_learning_enabled_inserts_examples_after_rules_and_records_ids(penv, caplog):
    _store(penv.feedback, 1, "insurance", "страховка нужна?", "Да, нужна, штраф 100 лари.", original="Нужна.")
    _store(penv.feedback, 2, "fuel", "бензин есть?", "Есть.")
    retriever = FeedbackRetriever(penv.feedback, _fake_embed, max_examples=3)
    with caplog.at_level(logging.INFO):
        row, service = _run(penv, primary=GOOD, retriever=retriever)
    prompt = service.calls[0]
    assert row.status == STATUS_DRAFT
    assert prompt.index("CAMPAIGN GUIDELINE") < prompt.index("OPERATOR-EDITED EXAMPLES")
    assert "Operator sent:\nДа, нужна, штраф 100 лари." in prompt and "Есть." not in prompt.split("EXAMPLES")[1]
    assert json.loads(row.context_json)["feedback_example_ids"] == [1]
    log = " ".join(r.getMessage() for r in caplog.records)
    assert "feedback_examples_used=1" in log and "feedback_example_ids=[1]" in log
    assert "страховка нужна?" not in log  # текст пользователя в лог не пишется


def test_validators_and_resources_still_apply_with_examples(penv):
    _store(penv.feedback, 1, "insurance", "страховка нужна?", "Да, нужна.")  # пример без ресурсов
    retriever = FeedbackRetriever(penv.feedback, _fake_embed, max_examples=3)
    ok, _ = _run(penv, primary=GOOD, retriever=retriever)
    assert ok.status == STATUS_DRAFT and TPLGEE in ok.primary_text and PROTOCOL_GE in ok.primary_text
    assert ok.primary_text.startswith("Здравствуйте!")  # серверное приветствие


def test_validator_still_rejects_hedging_with_examples(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach, recent = DmCampaignRepository(path), DmOutreachRepository(path), RecentMessageRepository(path)
    feedback = DmFeedbackRepository(path)
    try:
        campaign = campaigns.get_campaign_by_key("insurance")
        campaigns.update_resources(campaign.id, [TPLGEE, PROTOCOL_GE, PROTOCOL_TR])
        campaigns.set_enabled(campaign.id, True)
        _store(feedback, 1, "insurance", "страховка нужна?", "Да, нужна.")
        penv = SimpleNamespace(campaigns=campaigns, outreach=outreach, recent=recent, feedback=feedback)
        bad, _ = _run(penv, primary="Точно подтвердить не могу — в группе пишут по-разному. Полис — через "
                                    "@tplgee, штрафы — @ProtocolGEbot.",
                      retriever=FeedbackRetriever(feedback, _fake_embed, max_examples=3))
        assert bad.status == STATUS_FAILED and bad.error_kind == "ai_invalid_output"
    finally:
        for r in (campaigns, outreach, recent, feedback):
            r.close()


def test_retriever_failure_falls_back_to_plain_generation(penv):
    async def broken(texts, model):
        raise RuntimeError("embeddings down")
    _store(penv.feedback, 1, "insurance", "страховка нужна?", "Да, нужна.")
    row, service = _run(penv, primary=GOOD, retriever=FeedbackRetriever(penv.feedback, broken))
    assert row.status == STATUS_DRAFT and "OPERATOR-EDITED EXAMPLES" not in service.calls[0]
    assert json.loads(row.context_json)["feedback_example_ids"] == []


def test_sender_block_and_feedback_examples_coexist(penv):
    """Feedback learning + ожидаемый отправитель @alenaogir (female) + история:
    в prompt оба блока, ни один не перетирает другой; аудит — оба поля."""
    _store(penv.feedback, 1, "insurance", "страховка нужна?", "Да, нужна, штраф 100 лари.", original="Нужна.")
    retriever = FeedbackRetriever(penv.feedback, _fake_embed, max_examples=3)
    persona = SenderPersona(username="@alenaogir", gender="female")
    row, service = _run(penv, primary=GOOD, retriever=retriever, sender_persona=lambda item: persona)
    prompt = service.calls[0]
    sender_at, examples_at = prompt.index("\n\nSENDER\n"), prompt.index("\n\nOPERATOR-EDITED EXAMPLES\n")
    assert sender_at < examples_at
    sender_section = prompt[sender_at:examples_at]
    assert "The reply will be sent from @alenaogir." in sender_section and "Sender gender: female." in sender_section
    assert "я оформляла" in sender_section and "Operator sent" not in sender_section
    examples_section = prompt[examples_at:]
    assert "Operator sent:\nДа, нужна, штраф 100 лари." in examples_section and "SENDER" not in examples_section
    audit = json.loads(row.context_json)
    assert (audit["expected_sender"], audit["sender_gender"], audit["feedback_example_ids"]) == (
        "@alenaogir", "female", [1])
    assert row.status == STATUS_DRAFT
