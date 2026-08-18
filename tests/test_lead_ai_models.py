"""Тесты reader/lead_ai/models.py::normalize_lead_ai_analysis — инварианты
между relevant/lead_type/suggested_messages должны выполняться даже если
модель вернула противоречивый результат (см. задачу)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.lead_ai.models import LeadAiAnalysis, normalize_lead_ai_analysis  # noqa: E402


def test_consistent_relevant_result_is_unchanged():
    analysis = LeadAiAnalysis(
        relevant=True,
        lead_type="money_transfer_ru_ge",
        reason="хочет перевести деньги из России в Грузию",
        suggested_messages=["Мы так переводили", "Подскажите сумму"],
    )

    result = normalize_lead_ai_analysis(analysis)

    assert result == analysis


def test_consistent_irrelevant_result_is_unchanged():
    analysis = LeadAiAnalysis(
        relevant=False,
        lead_type="irrelevant",
        reason="спрашивает только про обменник",
        suggested_messages=[],
    )

    result = normalize_lead_ai_analysis(analysis)

    assert result == analysis


def test_relevant_true_with_irrelevant_lead_type_is_normalized_to_not_a_lead():
    """Противоречие: relevant=True, но lead_type=irrelevant — нормализуется
    в безопасную сторону (не лид), а не в пользу relevant=True."""
    analysis = LeadAiAnalysis(
        relevant=True,
        lead_type="irrelevant",
        reason="неоднозначно",
        suggested_messages=["какое-то сообщение"],
    )

    result = normalize_lead_ai_analysis(analysis)

    assert result.relevant is False
    assert result.lead_type == "irrelevant"
    assert result.suggested_messages == []


def test_relevant_false_with_non_irrelevant_lead_type_is_normalized():
    analysis = LeadAiAnalysis(
        relevant=False,
        lead_type="fine_payment",
        reason="неоднозначно",
        suggested_messages=[],
    )

    result = normalize_lead_ai_analysis(analysis)

    assert result.relevant is False
    assert result.lead_type == "irrelevant"
    assert result.suggested_messages == []


def test_relevant_false_with_non_empty_suggested_messages_is_cleared():
    analysis = LeadAiAnalysis(
        relevant=False,
        lead_type="irrelevant",
        reason="не лид",
        suggested_messages=["этого быть не должно"],
    )

    result = normalize_lead_ai_analysis(analysis)

    assert result.relevant is False
    assert result.suggested_messages == []


# ---- relevant=True: suggested_messages содержит только непустые строки ----


def test_relevant_true_strips_whitespace_from_messages():
    analysis = LeadAiAnalysis(
        relevant=True,
        lead_type="fine_payment",
        reason="хочет оплатить штраф",
        suggested_messages=["  Добрый день  ", "В этой же группе помогают с оплатой штрафа"],
    )

    result = normalize_lead_ai_analysis(analysis)

    assert result.suggested_messages == ["Добрый день", "В этой же группе помогают с оплатой штрафа"]


def test_relevant_true_drops_empty_and_whitespace_only_messages():
    analysis = LeadAiAnalysis(
        relevant=True,
        lead_type="fine_check",
        reason="хочет проверить штрафы",
        suggested_messages=["Мы тут проверяли", "", "   ", "В этой же группе помогают с оплатой штрафа"],
    )

    result = normalize_lead_ai_analysis(analysis)

    assert result.suggested_messages == ["Мы тут проверяли", "В этой же группе помогают с оплатой штрафа"]


def test_relevant_true_with_empty_suggested_messages_is_left_as_is():
    """Модель может (например, при неоднозначности) вернуть relevant=True
    вообще без сообщений — normalize не обязана это чинить/придумывать
    контент, только следить за инвариантами относительно relevant/lead_type."""
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="insurance_general", reason="r", suggested_messages=[],
    )

    result = normalize_lead_ai_analysis(analysis)

    assert result.relevant is True
    assert result.suggested_messages == []
