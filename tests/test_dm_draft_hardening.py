"""Phase 2.6 draft hardening: служебные ref-метки контекста и выдуманная
техподдержка не попадают в видимый пользователю текст (prompt + серверная
проверка normalize_draft); все sendability доходят до черновика; sender не
назначается; настройка источников insurance — явной командой, не миграцией.
Без реального OpenAI/Telegram."""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from test_dm_outreach_drafts import _builder, _FakeService, _out

from reader.core.models import Message
from reader.dm_campaigns import manage
from reader.dm_campaigns.draft_models import normalize_draft
from reader.dm_campaigns.draft_prompt import SYSTEM_PROMPT, build_user_text
from reader.dm_campaigns.observer import DmOutreachObserver
from reader.dm_campaigns.outreach_repository import (
    STATUS_DRAFT,
    STATUS_FAILED,
    DmOutreachRepository,
)
from reader.dm_campaigns.processor import DmDraftProcessor
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.sendability import (
    SENDABILITY_PREMIUM_REQUIRED,
    SENDABILITY_SOURCE_MESSAGE,
    SENDABILITY_USERNAME,
)
from reader.scenarios import KeywordMatcher, load_scenarios

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
MATCHER = KeywordMatcher(load_scenarios(PROJECT_ROOT / "config" / "scenarios.yaml"))
REFS = frozenset({"B1", "B2", "B3", "B4", "B5", "A1", "A2"})
GROUPS = ["VerhniyLars", "krayzemlige", "Sadahlo", "sarpi_ge", "tbilisi14"]


def _norm(primary, *, follow_up=None, follow_up_enabled=False, refs=REFS, used=()):
    return normalize_draft(
        _out(primary_message=primary, follow_up_message=follow_up, used_context_refs=list(used),
             evidence_strength="none"),
        follow_up_enabled=follow_up_enabled, valid_refs=refs, fresh_context_used=False,
        n_distinct_senders=0, allowed_resources=("@tplgee", "@ProtocolGEbot"),
    )


# ---- internal context refs ----


@pytest.mark.parametrize("primary,expected", [
    ("Свежих сообщений про это нет (B1–B5). Оформить можно у @tplgee.",
     "Свежих сообщений про это нет. Оформить можно у @tplgee."),
    ("В группе пишут, что нужна [A2].", "В группе пишут, что нужна."),
    ("Да, нужна (см. B1, B3).", "Да, нужна."),
])
def test_bracketed_ref_groups_are_stripped_from_primary(primary, expected):
    decision = _norm(primary, used=("B1",))
    assert decision.kind == "draft"
    assert decision.primary_message == expected
    assert decision.used_context_refs == ("B1",)  # метки остаются только в метаданных


@pytest.mark.parametrize("primary", [
    "В обсуждении B1–B5 об этом ничего нет.",
    "Как пишет участник в A2, штраф 100 лари.",
    "Судя по B3, да.",
])
def test_bare_refs_in_primary_reject_the_draft(primary):
    decision = _norm(primary)
    assert (decision.kind, decision.error) == ("invalid", "internal_ref_leak")


def test_refs_in_follow_up_reject_the_draft():
    decision = _norm("Да, нужна.", follow_up="Ещё см. A1.", follow_up_enabled=True)
    assert (decision.kind, decision.error) == ("invalid", "internal_ref_leak")


@pytest.mark.parametrize("primary", [
    "В переданном контексте нет информации о распечатке.",
    "Точно подтвердить не могу — в переданных данных нет информации о распечатке.",
    "В предоставленной информации об этом ничего нет.",
    "По контексту сказать сложно.",
])
def test_context_mentions_reject_the_draft(primary):
    assert _norm(primary).error == "context_mention"


def test_trailing_spaces_are_trimmed_per_line():
    decision = _norm("Точную стоимость сейчас не подскажу. \n\nРассчитать можно через @tplgee.  ")
    assert decision.primary_message == "Точную стоимость сейчас не подскажу.\n\nРассчитать можно через @tplgee."


@pytest.mark.parametrize("primary", [
    "Трасса S1 сейчас свободна.",           # S1 — не метка этого контекста
    "Полис формата A4 можно распечатать.",  # A4 — тоже не метка
    "Посмотрите (S1) на карте.",
])
def test_ordinary_text_that_looks_like_a_ref_is_untouched(primary):
    decision = _norm(primary)
    assert (decision.kind, decision.primary_message) == ("draft", primary)


# ---- invented troubleshooting ----


@pytest.mark.parametrize("primary", [
    "Попробуйте очистить кэш браузера и зайти снова.",
    "Откройте tpl.ge в другом браузере.",
    "Попробуйте ввести VIN вместо модели.",
    "Напишите в поддержку tpl.ge через форму на сайте.",
])
def test_unsupported_troubleshooting_rejects_the_draft(primary):
    assert _norm(primary).error == "unsupported_troubleshooting"


def test_expected_tpl_ge_answer_passes():
    primary = ("Похоже, этой модели нет в списке на tpl.ge. Если нужно оформить полис, напишите @tplgee — "
               "подскажут по машине и оформлению.")
    decision = _norm(primary)
    assert (decision.kind, decision.primary_message) == ("draft", primary)


def test_cash_is_not_treated_as_browser_cache():
    assert _norm("Оплатить можно кэшем или картой, оформить — у @tplgee.").kind == "draft"


def test_prompt_states_the_rules():
    for rule in ("ref-метки", "used_context_refs", "очистить кэш", "сменить браузер", "ввести VIN",
                 "Точно подтвердить не могу", "Точную стоимость сейчас не подскажу", "1–3 коротких абзаца"):
        assert rule in SYSTEM_PROMPT


# ---- full pipeline per sendability; no sender; no access_hash ----


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach, recent = DmCampaignRepository(path), DmOutreachRepository(path), RecentMessageRepository(path)
    insurance = campaigns.set_enabled(campaigns.get_campaign_by_key("insurance").id, True)
    yield path, campaigns, outreach, recent, insurance
    for repo in (campaigns, outreach, recent):
        repo.close()


def _process_one(env, output, *, username, premium):
    _, campaigns, outreach, recent, _ = env
    observer = DmOutreachObserver(campaigns, outreach, recent, context_wait_seconds=180, clock=lambda: T0,
                                  monotonic=lambda: 0.0)
    text = "Где оформить страховку на авто для въезда в Грузию?"
    message = Message(id=7, chat_id=-1001, chat_title="Верхний Ларс", sender_id=555, sender_username=username,
                      sender_name="Ivan", text=text, date=T0, link=None, chat_identifier="VerhniyLars",
                      sender_contact_require_premium=premium)
    observer.observe(message, MATCHER.match(text))
    service = _FakeService(output)
    processor = DmDraftProcessor(
        outreach, campaigns, recent, _builder(recent), service, interval_seconds=1, drafting_recovery_seconds=600,
        retention_hours=48, clock=lambda: T0 + timedelta(minutes=4), monotonic=lambda: 0.0,
    )
    return outreach, service, processor


@pytest.mark.parametrize("username,premium,sendability", [
    ("ivan", False, SENDABILITY_USERNAME),
    (None, False, SENDABILITY_SOURCE_MESSAGE),
    (None, True, SENDABILITY_PREMIUM_REQUIRED),
])
async def test_every_sendability_reaches_draft_without_sender(env, username, premium, sendability):
    path = env[0]
    outreach, _, processor = _process_one(env, _out(used_context_refs=[]), username=username, premium=premium)
    assert await processor.run_once() == 1
    (row,) = outreach.list_all()
    assert (row.status, row.sendability) == (STATUS_DRAFT, sendability)
    conn = sqlite3.connect(path)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(dm_outreach)")}
    conn.close()
    assert not {"sender_account_id", "assigned_sender", "sender_session", "access_hash"} & (columns | set(vars(row)))


async def test_leaked_refs_from_the_model_never_become_a_stored_draft(env):
    env[3].add(chat_id=-1001, chat_identifier="VerhniyLars", message_id=6, sender_id=9, text="кто знает?",
               date=T0 - timedelta(minutes=1), reply_to_msg_id=None)  # -> в контексте есть метка B1
    leaked = _out(primary_message="В обсуждении B1 ничего про это нет.", used_context_refs=["B1"])
    outreach, _, processor = _process_one(env, leaked, username=None, premium=False)
    assert await processor.run_once() == 1
    (row,) = outreach.list_all()
    assert (row.status, row.error_kind, row.primary_text) == (STATUS_FAILED, "ai_invalid_output", None)
    assert "internal_ref_leak" in row.error


def test_build_user_text_still_labels_context_for_the_model(env):
    _, _, outreach, recent, insurance = env
    recent.add(chat_id=-1001, chat_identifier="VerhniyLars", message_id=6, sender_id=9, text="страховка нужна",
               date=T0 - timedelta(minutes=1), reply_to_msg_id=None)
    oid = outreach.insert_candidate(
        campaign_id=insurance.id, source_chat_id=-1001, source_chat_identifier="VerhniyLars", source_chat_title="Ларс",
        source_message_id=7, source_message_at=T0, source_link=None, source_reply_to_msg_id=None,
        recipient_user_id=555, recipient_username=None, source_text="Нужна страховка?", status="pending_context",
        now=T0, draft_after_at=T0 + timedelta(minutes=3),
    )
    context = _builder(recent).build(outreach.get(oid), insurance, T0 + timedelta(minutes=3))
    assert "B1 |" in build_user_text(outreach.get(oid), insurance, context)  # метки — только во входе модели


# ---- insurance source groups: explicit deploy command ----


@pytest.fixture
def repo(tmp_path):
    repository = DmCampaignRepository(tmp_path / "users.db")
    yield repository
    repository.close()


def test_set_source_chats_dry_run_changes_nothing(repo):
    out = manage.set_source_chats(repo, GROUPS, key="insurance", chats=["VerhniyLars", "sarpi_ge", "Sadahlo"], apply=False)
    assert out.startswith("DRY RUN")
    assert repo.get_campaign_by_key("insurance").source_chats == ()


def test_set_source_chats_apply_touches_only_insurance_sources(repo):
    before = {c.key: c for c in repo.list_campaigns()}
    manage.set_source_chats(repo, GROUPS, key="insurance", chats=["VerhniyLars", "sarpi_ge", "Sadahlo"], apply=True)
    after = {c.key: c for c in repo.list_campaigns()}
    assert after["insurance"].source_chats == ("VerhniyLars", "sarpi_ge", "Sadahlo")
    assert [c.enabled for c in after.values()] == [False, False, False]
    for key in ("fuel", "border_queue"):
        assert after[key] == before[key]
    assert (after["insurance"].resources, after["insurance"].ai_guideline) == (
        before["insurance"].resources, before["insurance"].ai_guideline)


def test_set_source_chats_refuses_enabled_campaign_and_unknown_group(repo):
    with pytest.raises(SystemExit):
        manage.set_source_chats(repo, GROUPS, key="insurance", chats=["VerhniyLars", "no_such_group"], apply=True)
    repo.set_enabled(repo.get_campaign_by_key("insurance").id, True)
    with pytest.raises(SystemExit):
        manage.set_source_chats(repo, GROUPS, key="insurance", chats=["VerhniyLars"], apply=True)
    assert repo.get_campaign_by_key("insurance").source_chats == ()
