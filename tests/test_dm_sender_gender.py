"""Форма речи от первого лица в ЛС-черновике — по полу ОЖИДАЕМОГО
отправителя (первый по порядку выбора, как в preview): серверный подвал
(«я вчера оформлял/оформляла тут: @tplgee») и блок SENDER в prompt. Пол
неизвестен — нейтрально. Без OpenAI/Telegram."""

import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from test_dm_outreach_drafts import _builder, _FakeService, _out

from reader.dm_campaigns.draft_prompt import SenderPersona, build_user_text, sender_block
from reader.dm_campaigns.outreach_repository import STATUS_DRAFT, DmOutreachRepository
from reader.dm_campaigns.processor import DmDraftProcessor
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.reply_style import resource_footer, tplgee_line
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.resources import PROTOCOL_GE, PROTOCOL_TR, TPLGEE
from reader.dm_campaigns.sender_selection import SenderSelector
from reader.dm_campaigns.sender_state import DmSenderStateRepository
from reader.inviter.repository import TelegramAccountRepository
from reader.settings import DmOutreachSettings

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 6)  # «вчера» относительно оформления 2026-10-05
FEMALE = SenderPersona(username="@alenaogir", gender="female")
MALE = SenderPersona(username="@vladimihailov", gender="male")
ANSWER = ("Да, автостраховка нужна, без неё штраф 100 лари. Оформить можно тут: @tplgee, "
          "а штрафы — через @ProtocolGEbot.")


# ---------------- подвал ----------------


@pytest.mark.parametrize("gender,expected", [("female", "я вчера оформляла"), ("male", "я вчера оформлял"),
                                             (None, "мне вчера оформили")])
def test_footer_personal_line_matches_sender_gender(gender, expected):
    for seed in range(3):
        line = tplgee_line("Как очередь?", (), seed=seed, today=TODAY, sender_gender=gender)
        assert expected in line and TPLGEE in line
    footer = resource_footer("Как очередь?", (), campaign_resources=(TPLGEE, PROTOCOL_GE), seed=0, today=TODAY,
                             sender_gender=gender)
    assert len(footer) == 2 and expected in footer[0] and PROTOCOL_GE in footer[1]


def test_neutral_footer_has_no_gendered_past_tense():
    for seed in range(6):
        line = tplgee_line("Еду из Грузии в РФ", (), seed=seed, today=TODAY)
        assert "оформлял" not in line


# ---------------- блок SENDER ----------------


def test_sender_block_female_and_male():
    female, male = sender_block(FEMALE), sender_block(MALE)
    assert "The reply will be sent from @alenaogir." in female and "Sender gender: female." in female
    assert "я оформляла" in female and "Prefer gender-neutral phrasing" in female
    assert "Sender gender: male." in male and "я оформлял" in male and "оформляла" not in male


@pytest.mark.parametrize("persona", [None, SenderPersona(username="@someone", gender=None)])
def test_sender_block_unknown_is_neutral(persona):
    block = sender_block(persona)
    assert "gender is unknown" in block and "gender-neutrally" in block and "Sender gender" not in block


def test_prompt_without_sender_resolution_is_unchanged():
    outreach = SimpleNamespace(source_text="Нужна страховка?", source_chat_title="Ларс", source_chat_identifier="x")
    campaign = SimpleNamespace(key="insurance", title="Страховка", resources=(TPLGEE,), ai_guideline="факты",
                               follow_up_enabled=False, follow_up_guideline=None)
    context = SimpleNamespace(discussion=[], evidence=[], fresh_context_used=False, metadata=None)
    plain = build_user_text(outreach, campaign, context)
    assert "SENDER" not in plain
    with_sender = build_user_text(outreach, campaign, context, sender=FEMALE)
    assert with_sender.startswith(plain) and with_sender[len(plain):].startswith("\n\nSENDER\n")


def test_default_personas_from_settings():
    personas = DmOutreachSettings().sender_personas
    assert (personas[14].gender, personas[1].gender, personas[15].gender) == ("female", "male", "male")


# ---------------- генерация ----------------


@pytest.fixture
def penv(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach, recent = DmCampaignRepository(path), DmOutreachRepository(path), RecentMessageRepository(path)
    campaign = campaigns.get_campaign_by_key("insurance")
    campaigns.update_resources(campaign.id, [TPLGEE, PROTOCOL_GE, PROTOCOL_TR])
    campaigns.set_enabled(campaign.id, True)
    yield SimpleNamespace(path=path, campaigns=campaigns, outreach=outreach, recent=recent)
    for r in (campaigns, outreach, recent):
        r.close()


def _run(penv, *, persona=None, resolver=True, primary=ANSWER, resolver_fn=None):
    oid = penv.outreach.insert_candidate(
        campaign_id=penv.campaigns.get_campaign_by_key("insurance").id, source_chat_id=-1001,
        source_chat_identifier="VerhniyLars", source_chat_title="Верхний Ларс", source_message_id=7,
        source_message_at=T0, source_link=None, source_reply_to_msg_id=None, recipient_user_id=555,
        recipient_username=None, source_text="Нужна ли страховка на машину?", status="pending_context", now=T0,
        draft_after_at=T0 + timedelta(minutes=3),
    )
    service = _FakeService(_out(primary_message=primary, used_context_refs=[], evidence_strength="none"))
    processor = DmDraftProcessor(
        penv.outreach, penv.campaigns, penv.recent, _builder(penv.recent), service, interval_seconds=1,
        drafting_recovery_seconds=600, retention_hours=48, clock=lambda: T0 + timedelta(minutes=4),
        monotonic=lambda: 0.0, group_resource_regions={"VerhniyLars": "ge"},
        sender_persona=resolver_fn or ((lambda item: persona) if resolver else None),
    )
    asyncio.run(processor.run_once())
    return penv.outreach.get(oid), service


def test_female_sender_prompt_footer_and_audit(penv):
    row, service = _run(penv, persona=FEMALE)
    assert "Sender gender: female." in service.calls[0]
    assert row.status == STATUS_DRAFT
    audit = json.loads(row.context_json)
    assert (audit["expected_sender"], audit["sender_gender"]) == ("@alenaogir", "female")


def test_male_sender_prompt(penv):
    _, service = _run(penv, persona=MALE)
    assert "Sender gender: male." in service.calls[0]


def test_female_first_person_answer_is_accepted(penv):
    answer = ("Да, автостраховка нужна, без неё штраф 100 лари. Я сама вчера оформляла тут: @tplgee, "
              "а штрафы проверяю через @ProtocolGEbot.")
    row, _ = _run(penv, persona=FEMALE, primary=answer)
    assert row.status == STATUS_DRAFT and "оформляла" in row.primary_text


def test_unknown_sender_gets_neutral_instruction(penv):
    row, service = _run(penv, persona=None)
    assert "gender is unknown" in service.calls[0]
    assert json.loads(row.context_json)["expected_sender"] is None


def test_no_resolver_keeps_prompt_and_resources(penv):
    row, service = _run(penv, resolver=False)
    assert "SENDER" not in service.calls[0] and "expected_sender" not in json.loads(row.context_json)
    assert row.status == STATUS_DRAFT and TPLGEE in row.primary_text and PROTOCOL_GE in row.primary_text


def test_resolver_error_falls_back_to_neutral(penv):
    def broken(item):
        raise RuntimeError("db")
    row, service = _run(penv, resolver_fn=broken)
    assert "gender is unknown" in service.calls[0] and row.status == STATUS_DRAFT
    assert "мне вчера оформили" in row.primary_text or "оформлял" not in row.primary_text


# ---------------- ожидаемый отправитель = порядок preview ----------------


def test_expected_sender_follows_selection_order_and_filters(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach = DmCampaignRepository(path), DmOutreachRepository(path)
    accounts, states = TelegramAccountRepository(path), DmSenderStateRepository(path)
    try:
        insurance = campaigns.get_campaign_by_key("insurance")
        alena = accounts.create(name="@alenaogir", phone="", session_name="a", session_path="data/sessions/a",
                                enabled=False, can_send_dm=True, is_premium=True)
        vladi = accounts.create(name="@vladimihailov", phone="", session_name="v", session_path="data/sessions/v",
                                enabled=False, can_send_dm=True, is_premium=False)
        for account in (alena, vladi):
            campaigns.set_campaign_account_enabled(insurance.id, account.id, True)
        item = SimpleNamespace(campaign_id=insurance.id, sendability="source_message")
        selector = SenderSelector(outreach, campaigns, accounts, states, session_exists=lambda a: True)
        assert selector.expected_sender(item, T0, cap=5).id == alena.id  # меньший id при равных отправках
        states.block(alena.id, reason="peer_flood", now=T0)
        assert selector.expected_sender(item, T0, cap=5).id == vladi.id
        premium_item = SimpleNamespace(campaign_id=insurance.id, sendability="premium_required")
        assert selector.expected_sender(premium_item, T0, cap=5) is None  # Premium только у заблокированной
    finally:
        for r in (campaigns, outreach, accounts, states):
            r.close()
