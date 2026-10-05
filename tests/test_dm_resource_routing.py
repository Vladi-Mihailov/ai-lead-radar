"""Phase 3A: country-aware ресурсы ЛС-черновика — @ProtocolGEbot только для
GE-группы и вопроса про штрафы, @ProtocolTRbot только для TR-группы и
штрафов/платных дорог/HGS; unknown — ни один; @tplgee — как раньше.
Проверка на сервере (normalize_draft) + сквозной путь processor.
Без реального OpenAI/Telegram."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from test_dm_outreach_drafts import _builder, _FakeService, _out

from reader.dm_campaigns.draft_models import normalize_draft
from reader.dm_campaigns.outreach_repository import (
    STATUS_DRAFT,
    STATUS_FAILED,
    DmOutreachRepository,
)
from reader.dm_campaigns.processor import DmDraftProcessor
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.resources import PROTOCOL_GE, PROTOCOL_TR, allowed_resources
from reader.groups import GroupLoadError, load_groups

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
CAMPAIGN_RESOURCES = ("@tplgee", "@ProtocolGEbot")  # как в production-настройках insurance
FINE_Q = "Пришёл ли штраф за скорость? Как проверить штрафы?"
TOLL_Q = "Как оплатить платную дорогу, нужен HGS?"
INSURANCE_Q = "Сколько стоит страховка и где оформить ОСАГО?"


# ---- allowed_resources ----


@pytest.mark.parametrize("country,text,expected", [
    ("ge", FINE_Q, ("@tplgee", PROTOCOL_GE)),
    ("ge", INSURANCE_Q, ("@tplgee",)),
    ("ge", TOLL_Q, ("@tplgee",)),                     # платные дороги — не грузинский бот
    ("tr", FINE_Q, ("@tplgee", PROTOCOL_TR)),
    ("tr", TOLL_Q, ("@tplgee", PROTOCOL_TR)),
    ("tr", "Пришло начисление за toll на мосту", ("@tplgee", PROTOCOL_TR)),
    ("tr", INSURANCE_Q, ("@tplgee",)),
    ("am", FINE_Q, ("@tplgee",)),
    ("unknown", FINE_Q, ("@tplgee",)),
    (None, FINE_Q, ("@tplgee",)),
], ids=["ge_fine", "ge_insurance", "ge_toll", "tr_fine", "tr_toll_hgs", "tr_toll_word", "tr_insurance",
        "am_fine", "unknown_fine", "none_fine"])
def test_allowed_resources(country, text, expected):
    assert allowed_resources(CAMPAIGN_RESOURCES, resource_region=country, intent_text=text) == expected


def test_campaign_listed_protocol_bot_is_not_a_blanket_permission():
    assert PROTOCOL_GE not in allowed_resources(("@ProtocolGEbot",), resource_region="ge", intent_text=INSURANCE_Q)
    assert PROTOCOL_GE not in allowed_resources(("@ProtocolGEbot",), resource_region="tr", intent_text=FINE_Q)


# ---- server-side validation ----


def _norm(primary, allowed):
    return normalize_draft(
        _out(primary_message=primary, used_context_refs=[], evidence_strength="none"),
        follow_up_enabled=False, valid_refs=frozenset(), fresh_context_used=False, n_distinct_senders=0,
        allowed_resources=allowed,
    )


@pytest.mark.parametrize("country,text,primary", [
    ("ge", FINE_Q, "Проверить можно в @ProtocolTRbot."),       # GE -> TR-бот
    ("tr", FINE_Q, "Проверить можно в @ProtocolGEbot."),       # TR -> GE-бот
    ("unknown", FINE_Q, "Проверить можно в @ProtocolGEbot."),  # unknown -> ни один
    ("unknown", TOLL_Q, "Посмотрите @ProtocolTRbot."),
    ("ge", INSURANCE_Q, "Оформить можно у @tplgee, штрафы — в @ProtocolGEbot."),  # не про штрафы
    ("tr", INSURANCE_Q, "Оформить можно у @tplgee, а @ProtocolTRbot пришлёт начисления."),
])
def test_wrong_protocol_bot_makes_the_draft_invalid(country, text, primary):
    decision = _norm(primary, allowed_resources(CAMPAIGN_RESOURCES, resource_region=country, intent_text=text))
    assert decision.kind == "invalid" and decision.error.startswith("unapproved_resources:")


@pytest.mark.parametrize("country,text,primary", [
    ("ge", FINE_Q, "@ProtocolGEbot может прислать штраф, как только он появится в базе."),
    ("tr", TOLL_Q, "@ProtocolTRbot пришлёт начисления по платным дорогам, как только они появятся в базе."),
    ("ge", INSURANCE_Q, "Точную стоимость сейчас не подскажу. Рассчитать можно у @tplgee."),
    ("unknown", INSURANCE_Q, "Оформить можно у @tplgee."),
])
def test_right_resource_passes(country, text, primary):
    decision = _norm(primary, allowed_resources(CAMPAIGN_RESOURCES, resource_region=country, intent_text=text))
    assert (decision.kind, decision.primary_message) == ("draft", primary)


# ---- end-to-end through the processor ----


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach, recent = DmCampaignRepository(path), DmOutreachRepository(path), RecentMessageRepository(path)
    insurance = campaigns.get_campaign_by_key("insurance")
    campaigns.update_resources(insurance.id, list(CAMPAIGN_RESOURCES))
    insurance = campaigns.set_enabled(insurance.id, True)
    yield campaigns, outreach, recent, insurance
    for repo in (campaigns, outreach, recent):
        repo.close()


def _run(env, *, ident, text, output):
    campaigns, outreach, recent, insurance = env
    oid = outreach.insert_candidate(
        campaign_id=insurance.id, source_chat_id=-1001, source_chat_identifier=ident, source_chat_title=ident,
        source_message_id=7, source_message_at=T0, source_link=None, source_reply_to_msg_id=None,
        recipient_user_id=555, recipient_username=None, source_text=text, status="pending_context", now=T0,
        draft_after_at=T0 + timedelta(minutes=3),
    )
    service = _FakeService(output)
    processor = DmDraftProcessor(
        outreach, campaigns, recent, _builder(recent), service, interval_seconds=1, drafting_recovery_seconds=600,
        retention_hours=48, clock=lambda: T0 + timedelta(minutes=4), monotonic=lambda: 0.0,
        group_resource_regions={"VerhniyLars": "ge", "turkey_group": "tr", "krayzemlige": "unknown"},
    )
    import asyncio

    asyncio.run(processor.run_once())
    return outreach.get(oid), service.calls[0]


def test_ge_fine_question_offers_protocol_ge_bot(env):
    row, prompt = _run(env, ident="VerhniyLars", text=FINE_Q,
                       output=_out(primary_message="@ProtocolGEbot может прислать штраф, как только он появится в базе.",
                                   used_context_refs=[], evidence_strength="none"))
    assert "@ProtocolGEbot — может прислать штраф" in prompt and "@ProtocolTRbot" not in prompt
    assert row.status == STATUS_DRAFT


def test_ge_insurance_question_hides_protocol_bot_from_model_and_rejects_it(env):
    row, prompt = _run(env, ident="VerhniyLars", text=INSURANCE_Q,
                       output=_out(primary_message="Оформить можно у @tplgee, а штрафы — в @ProtocolGEbot.",
                                   used_context_refs=[], evidence_strength="none"))
    assert "@tplgee" in prompt and "ProtocolGEbot" not in prompt.split("ALLOWED PROMOTED RESOURCES")[1].split("\n\n")[0]
    assert (row.status, row.error_kind) == (STATUS_FAILED, "ai_invalid_output")


def test_tr_toll_question_offers_protocol_tr_bot(env):
    row, prompt = _run(env, ident="turkey_group", text=TOLL_Q,
                       output=_out(primary_message="@ProtocolTRbot пришлёт начисления по платным дорогам.",
                                   used_context_refs=[], evidence_strength="none"))
    assert "@ProtocolTRbot — может прислать штрафы и начисления" in prompt
    assert row.status == STATUS_DRAFT


def test_unknown_country_gets_no_protocol_bot(env):
    row, prompt = _run(env, ident="krayzemlige", text=FINE_Q,
                       output=_out(primary_message="Проверьте в @ProtocolGEbot.", used_context_refs=[],
                                   evidence_strength="none"))
    resources_block = prompt.split("ALLOWED PROMOTED RESOURCES")[1].split("\n\n")[0]
    assert "Protocol" not in resources_block and "@tplgee" in resources_block
    assert row.status == STATUS_FAILED


def test_tplgee_still_works_for_insurance(env):
    row, _ = _run(env, ident="VerhniyLars", text=INSURANCE_Q,
                  output=_out(primary_message="Точную стоимость сейчас не подскажу. Рассчитать можно у @tplgee.",
                              used_context_refs=[], evidence_strength="none"))
    assert (row.status, row.primary_text) == (STATUS_DRAFT, "Точную стоимость сейчас не подскажу. Рассчитать можно у @tplgee.")


# ---- groups.yaml country ----


def _groups(tmp_path, body):
    path = tmp_path / "groups.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_group_country_parsed_and_defaults_to_unknown(tmp_path):
    groups = load_groups(_groups(tmp_path, 'groups:\n  - username: "a"\n    resource_region: "GE"\n  - username: "b"\n'))
    assert [(g.username, g.resource_region) for g in groups] == [("a", "ge"), ("b", "unknown")]


def test_invalid_group_country_is_rejected(tmp_path):
    with pytest.raises(GroupLoadError):
        load_groups(_groups(tmp_path, 'groups:\n  - username: "a"\n    resource_region: "georgia"\n'))


def test_production_groups_yaml_country_mapping():
    groups = {str(g.identifier): g.resource_region for g in load_groups(PROJECT_ROOT / "config" / "groups.yaml")}
    assert groups["VerhniyLars"] == "ge" and groups["banks_ge"] == "ge"
    # Решения по спорным группам: Sadahlo — маршрут в Армению, sarpi_ge —
    # турецкий маршрут, krayzemlige — без явного назначения.
    assert (groups["Sadahlo"], groups["sarpi_ge"], groups["krayzemlige"]) == ("am", "tr", "unknown")
