"""Phase 3A: вопрос ТОЛЬКО про медицинскую/туристическую страховку — не
автомобильный insurance-лид: фильтруется до OpenAI (non_auto_insurance).
Смешанные вопросы с ОСАГО/страховкой машины остаются кандидатами. Matcher
инвайтера (ru_insurance_words/is_insurance_text) не меняется."""

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.core.models import Message
from reader.dm_campaigns.observer import FILTER_NON_AUTO_INSURANCE, DmOutreachObserver
from reader.dm_campaigns.outreach_repository import (
    STATUS_FILTERED,
    STATUS_PENDING_CONTEXT,
    DmOutreachRepository,
)
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.insurance_matching import (
    is_insurance_text,
    is_non_auto_insurance_text,
    ru_insurance_words,
)
from reader.scenarios import KeywordMatcher, load_scenarios

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
MATCHER = KeywordMatcher(load_scenarios(PROJECT_ROOT / "config" / "scenarios.yaml"))

PURE_MEDICAL = [
    "Добрый день, подскажите мед страховка нужна?",
    "Нужна ли медицинская страховка при въезде в Грузию?",
    "Где купить медстраховку на месяц?",
    "Подскажите туристическую страховку для поездки",
    "Нужна страховка здоровья для ВНЖ?",
    "Where to buy travel health insurance?",
]
AUTO_OR_MIXED = [
    "Нужно ли ОСАГО и медстраховка?",
    "Страховка на машину и медицинская страховка — где оформить?",
    "Подскажите страховку на авто и на себя, надо распечатать?",
    "Где оформить ОСАГО на границе?",
    "Нужна ли зелёная карта и мед страховка?",
    "Сколько стоит страховка автомобиля?",
    "Нужна страховка?",  # без медицины — остаётся кандидатом
]


@pytest.mark.parametrize("text", PURE_MEDICAL)
def test_pure_medical_is_non_auto(text):
    assert is_non_auto_insurance_text(text) is True


@pytest.mark.parametrize("text", AUTO_OR_MIXED)
def test_auto_or_mixed_is_not_filtered(text):
    assert is_non_auto_insurance_text(text) is False


def test_inviter_matcher_is_unchanged():
    # Правило инвайтера по-прежнему видит страховое слово в медицинском вопросе.
    assert ru_insurance_words("подскажите мед страховка нужна?", "страх") == ["страховка"]
    assert is_insurance_text("Добрый день, подскажите мед страховка нужна?") is True


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach, recent = DmCampaignRepository(path), DmOutreachRepository(path), RecentMessageRepository(path)
    campaigns.set_enabled(campaigns.get_campaign_by_key("insurance").id, True)
    observer = DmOutreachObserver(campaigns, outreach, recent, context_wait_seconds=180, clock=lambda: T0,
                                  monotonic=lambda: 0.0)
    yield outreach, observer
    for repo in (campaigns, outreach, recent):
        repo.close()


def _observe(observer, text, message_id):
    message = Message(id=message_id, chat_id=-1001, chat_title="Верхний Ларс", sender_id=555, sender_username=None,
                      sender_name="Ivan", text=text, date=T0, link=None, chat_identifier="VerhniyLars")
    observer.observe(message, MATCHER.match(text))


@pytest.mark.parametrize("text,status,reason", [
    ("Добрый день, подскажите мед страховка нужна?", STATUS_FILTERED, FILTER_NON_AUTO_INSURANCE),
    ("Нужно ли ОСАГО и медстраховка?", STATUS_PENDING_CONTEXT, None),
    ("Страховка на машину и медицинская страховка — где оформить?", STATUS_PENDING_CONTEXT, None),
    ("Где оформить ОСАГО на границе?", STATUS_PENDING_CONTEXT, None),
])
def test_observer_filters_pure_medical_before_openai(env, text, status, reason):
    outreach, observer = env
    _observe(observer, text, message_id=1)
    (row,) = outreach.list_all()
    assert (row.status, row.filter_reason) == (status, reason)
    if status == STATUS_FILTERED:
        assert row.draft_after_at is None  # не уйдёт в обработчик / OpenAI
