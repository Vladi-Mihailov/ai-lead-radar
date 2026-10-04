"""Тесты обнаружения кандидатов ЛС-кампаний (Phase 2): сценарий fuel,
страховой префильтр, сопоставление сценарий -> кампания, дедупликация,
сохранность существующего lead-потока. Временная БД, без Telegram/OpenAI."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.core.models import Message, ScenarioMatch
from reader.dm_campaigns.observer import DmOutreachObserver
from reader.dm_campaigns.outreach_repository import STATUS_FILTERED, STATUS_PENDING_CONTEXT, DmOutreachRepository
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.insurance_matching import is_insurance_text, ru_insurance_words
from reader.inviter import lead_pool
from reader.scenarios import KeywordMatcher, load_scenarios
from reader.users.keyword_matches import unique_keywords

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
SCENARIOS = load_scenarios(PROJECT_ROOT / "config" / "scenarios.yaml")
MATCHER = KeywordMatcher(SCENARIOS)


def names(text):
    return [m.scenario_name for m in MATCHER.match(text)]


# ---- fuel scenario (реальный config/scenarios.yaml) ----


@pytest.mark.parametrize("text", [
    "Ребята, кто ехал сегодня по М4? Как там с бензином?",
    "Где лучше заправиться перед границей?",
    "Есть АЗС после Владикавказа?",
    "С топливом проблемы на трассе?",
    "Солярка есть на заправках?",
    "Где заправляться дешевле?",
])
def test_fuel_positive(text):
    assert "fuel" in names(text)


@pytest.mark.parametrize("text", [
    "Цена 95 тысяч, дорого",
    "92 года выпуска, дешево",
    "Какая погода в Тбилиси?",
    "Что сейчас на Ларсе?",
])
def test_fuel_negative(text):
    assert "fuel" not in names(text)


def test_fuel_is_dm_only_not_forwarded_and_not_in_user_keywords():
    fuel = next(s for s in SCENARIOS if s.name == "fuel")
    assert fuel.forward_leads is False
    matches = MATCHER.match("Есть бензин на М4?")
    assert [m.forward_leads for m in matches] == [False]
    assert unique_keywords(matches) == []
    # Существующие сценарии по-прежнему forward_leads=True.
    assert all(s.forward_leads for s in SCENARIOS if s.name != "fuel")


def test_existing_scenarios_order_and_border_detection_unchanged():
    assert [s.name for s in SCENARIOS] == ["insurance", "car_border_crossing", "fuel"]
    assert names("Что сейчас на Ларсе?") == ["car_border_crossing"]
    border = next(s for s in SCENARIOS if s.name == "car_border_crossing")
    assert "кпп" in border.context_keywords and "кпп" not in border.keywords
    assert names("Как прошли КПП?") == []  # context_keywords не влияют на detection


# ---- insurance helper (общий для инвайтера и ЛС) ----


def test_inviter_uses_shared_insurance_helper():
    assert lead_pool._ru_insurance_words is ru_insurance_words


@pytest.mark.parametrize("text,expected", [
    ("Где сделать страховку на Грузию?", True),
    ("Нужно застраховать машину", True),
    ("Где оформить ОСАГО?", True),
    ("зелёная карта нужна?", True),
    ("Продаю авто, онлайн оформление документов", False),
    ("Обмен рублей на лари", False),
    ("на свой страх и риск поехали", False),
    ("лучше перестраховаться", False),
    ("из Астрахани едем", False),
])
def test_insurance_prefilter(text, expected):
    assert is_insurance_text(text) is expected


# ---- observer ----


class _Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach, recent = DmCampaignRepository(path), DmOutreachRepository(path), RecentMessageRepository(path)
    clock = _Clock()
    observer = DmOutreachObserver(campaigns, outreach, recent, context_wait_seconds=180, clock=clock, monotonic=lambda: 0.0)
    yield campaigns, outreach, recent, observer, clock
    for repo in (campaigns, outreach, recent):
        repo.close()


def _fresh_observer(env):
    campaigns, outreach, recent, _, clock = env
    return DmOutreachObserver(campaigns, outreach, recent, context_wait_seconds=180, clock=clock, monotonic=lambda: 0.0)


def _msg(text, *, message_id=1, ident="VerhniyLars", sender_id=555, username="ivan"):
    return Message(
        id=message_id, chat_id=-1001, chat_title="Верхний Ларс", sender_id=sender_id,
        sender_username=username, sender_name="Ivan", text=text, date=T0, link=None,
        chat_identifier=ident, reply_to_msg_id=None,
    )


def _observe(observer, message):
    observer.observe(message, MATCHER.match(message.text))


def test_all_disabled_means_no_buffer_no_candidate(env):
    _, outreach, recent, observer, _ = env
    _observe(observer, _msg("Есть бензин на М4?"))
    assert outreach.list_all() == [] and recent.count() == 0


def test_enabled_campaign_creates_pending_candidate_with_wait(env):
    campaigns, outreach, recent, _, _ = env
    campaigns.set_enabled(campaigns.get_campaign_by_key("fuel").id, True)
    _observe(_fresh_observer(env), _msg("Есть бензин на М4?"))
    rows = outreach.list_all()
    assert len(rows) == 1
    row = rows[0]
    assert (row.status, row.draft_after_at, row.recipient_username, row.source_chat_identifier) == (
        STATUS_PENDING_CONTEXT, T0 + timedelta(seconds=180), "ivan", "VerhniyLars")
    assert recent.count() == 1


def test_disabled_campaign_gets_no_candidate_even_if_other_enabled(env):
    campaigns, outreach, recent, _, _ = env
    campaigns.set_enabled(campaigns.get_campaign_by_key("insurance").id, True)
    _observe(_fresh_observer(env), _msg("Есть бензин на М4?"))  # fuel выключен
    assert outreach.list_all() == []
    assert recent.count() == 1  # буфер пишется, раз включена хоть одна кампания


def test_wrong_source_group_is_filtered(env):
    campaigns, outreach, _, _, _ = env
    fuel = campaigns.get_campaign_by_key("fuel")
    campaigns.set_enabled(fuel.id, True)
    campaigns.update_source_chats(fuel.id, ["Sadahlo"])
    _observe(_fresh_observer(env), _msg("Есть бензин на М4?", ident="VerhniyLars"))
    row = outreach.list_all()[0]
    assert (row.status, row.filter_reason) == (STATUS_FILTERED, "source_chat_not_allowed")


def test_source_group_match_is_case_insensitive(env):
    campaigns, outreach, _, _, _ = env
    fuel = campaigns.get_campaign_by_key("fuel")
    campaigns.set_enabled(fuel.id, True)
    campaigns.update_source_chats(fuel.id, ["@verhniylars"])
    _observe(_fresh_observer(env), _msg("Есть бензин на М4?", ident="VerhniyLars"))
    assert outreach.list_all()[0].status == STATUS_PENDING_CONTEXT


def test_insurance_false_positive_rejected_true_lead_accepted(env):
    campaigns, outreach, _, _, _ = env
    campaigns.set_enabled(campaigns.get_campaign_by_key("insurance").id, True)
    observer = _fresh_observer(env)
    _observe(observer, _msg("Продаю авто, срочно", message_id=1))
    _observe(observer, _msg("Где сделать страховку на Грузию?", message_id=2))
    rows = {r.source_message_id: r for r in outreach.list_all()}
    assert (rows[1].status, rows[1].filter_reason) == (STATUS_FILTERED, "insurance_prefilter_rejected")
    assert rows[2].status == STATUS_PENDING_CONTEXT


def test_border_existing_scenario_positive(env):
    campaigns, outreach, _, _, _ = env
    campaigns.set_enabled(campaigns.get_campaign_by_key("border_queue").id, True)
    _observe(_fresh_observer(env), _msg("Что сейчас на Ларсе? Сколько очередь?"))
    row = outreach.list_all()[0]
    assert (row.status, row.campaign_id) == (STATUS_PENDING_CONTEXT, campaigns.get_campaign_by_key("border_queue").id)


def test_multi_match_gives_exactly_one_candidate_in_scenario_order(env):
    campaigns, outreach, _, _, _ = env
    for key in ("fuel", "border_queue", "insurance"):
        campaigns.set_enabled(campaigns.get_campaign_by_key(key).id, True)
    text = "Где заправиться перед Ларсом и где сделать страховку?"
    assert names(text) == ["insurance", "car_border_crossing", "fuel"]
    _observe(_fresh_observer(env), _msg(text))
    rows = outreach.list_all()
    assert len(rows) == 1
    assert rows[0].campaign_id == campaigns.get_campaign_by_key("insurance").id


def test_multi_match_falls_through_rejected_insurance_to_next_scenario(env):
    campaigns, outreach, _, _, _ = env
    for key in ("border_queue", "insurance"):
        campaigns.set_enabled(campaigns.get_campaign_by_key(key).id, True)
    text = "Как граница, авто много?"  # insurance (по "авто") без страхового слова + граница
    assert names(text)[:2] == ["insurance", "car_border_crossing"]
    _observe(_fresh_observer(env), _msg(text))
    rows = outreach.list_all()
    assert len(rows) == 1
    assert (rows[0].status, rows[0].campaign_id) == (STATUS_PENDING_CONTEXT, campaigns.get_campaign_by_key("border_queue").id)


def test_same_event_twice_gives_one_candidate(env):
    campaigns, outreach, recent, _, _ = env
    campaigns.set_enabled(campaigns.get_campaign_by_key("fuel").id, True)
    observer = _fresh_observer(env)
    _observe(observer, _msg("Есть бензин на М4?"))
    _observe(observer, _msg("Есть бензин на М4?"))
    _observe(_fresh_observer(env), _msg("Есть бензин на М4?"))  # "рестарт"
    assert len(outreach.list_all()) == 1 and recent.count() == 1


@pytest.mark.parametrize("sender_id,username,reason", [(None, "ivan", "no_sender_id"), (555, None, "no_username")])
def test_sender_filters(env, sender_id, username, reason):
    campaigns, outreach, _, _, _ = env
    campaigns.set_enabled(campaigns.get_campaign_by_key("fuel").id, True)
    _observe(_fresh_observer(env), _msg("Есть бензин на М4?", sender_id=sender_id, username=username))
    row = outreach.list_all()[0]
    assert (row.status, row.filter_reason, row.draft_after_at) == (STATUS_FILTERED, reason, None)


def test_campaign_cache_refreshes(env):
    campaigns, outreach, _, _, clock = env
    ticks = {"t": 0.0}
    observer = DmOutreachObserver(campaigns, outreach, env[2], context_wait_seconds=180, clock=clock,
                                  monotonic=lambda: ticks["t"])
    _observe(observer, _msg("Есть бензин?", message_id=1))
    campaigns.set_enabled(campaigns.get_campaign_by_key("fuel").id, True)
    _observe(observer, _msg("Есть бензин?", message_id=2))  # кэш ещё старый
    ticks["t"] = 16.0
    _observe(observer, _msg("Есть бензин?", message_id=3))
    assert [r.source_message_id for r in outreach.list_all()] == [3]


def test_unmatched_message_only_buffered(env):
    campaigns, outreach, recent, _, _ = env
    campaigns.set_enabled(campaigns.get_campaign_by_key("fuel").id, True)
    _fresh_observer(env).observe(_msg("Просто привет"), [])
    assert outreach.list_all() == [] and recent.count() == 1


def test_scenario_match_default_forward_leads_true():
    assert ScenarioMatch(scenario_name="x", matched_keywords=["y"]).forward_leads is True
