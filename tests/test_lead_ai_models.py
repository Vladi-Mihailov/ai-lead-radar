"""Тесты reader/lead_ai/models.py::normalize_lead_ai_analysis — инварианты
между relevant/lead_type/suggested_reply должны выполняться даже если
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
        suggested_reply="Подскажите сумму и в какой валюте хотите получить?",
    )

    result = normalize_lead_ai_analysis(analysis)

    assert result == analysis


def test_consistent_irrelevant_result_is_unchanged():
    analysis = LeadAiAnalysis(
        relevant=False,
        lead_type="irrelevant",
        reason="спрашивает только про обменник",
        suggested_reply="",
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
        suggested_reply="какой-то ответ",
    )

    result = normalize_lead_ai_analysis(analysis)

    assert result.relevant is False
    assert result.lead_type == "irrelevant"
    assert result.suggested_reply == ""


def test_relevant_false_with_non_irrelevant_lead_type_is_normalized():
    analysis = LeadAiAnalysis(
        relevant=False,
        lead_type="fine_payment",
        reason="неоднозначно",
        suggested_reply="",
    )

    result = normalize_lead_ai_analysis(analysis)

    assert result.relevant is False
    assert result.lead_type == "irrelevant"
    assert result.suggested_reply == ""


def test_relevant_false_with_non_empty_suggested_reply_is_cleared():
    analysis = LeadAiAnalysis(
        relevant=False,
        lead_type="irrelevant",
        reason="не лид",
        suggested_reply="этого быть не должно",
    )

    result = normalize_lead_ai_analysis(analysis)

    assert result.relevant is False
    assert result.suggested_reply == ""
