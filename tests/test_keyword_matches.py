"""Тесты unique_keywords() — общей сборки уникальных ключевых слов из
ScenarioMatch, используемой и Pipeline (reader/main.py), и history_sync.py
(sync_users.py)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.core.models import ScenarioMatch  # noqa: E402
from reader.users.keyword_matches import unique_keywords  # noqa: E402


def test_unique_keywords_flattens_multiple_matches():
    matches = [
        ScenarioMatch(scenario_name="osago", matched_keywords=["осаго"]),
        ScenarioMatch(scenario_name="obmen", matched_keywords=["обмен", "оформить"]),
    ]

    assert unique_keywords(matches) == ["осаго", "обмен", "оформить"]


def test_unique_keywords_deduplicates_preserving_first_occurrence_order():
    matches = [
        ScenarioMatch(scenario_name="osago", matched_keywords=["осаго", "страховка"]),
        ScenarioMatch(scenario_name="obmen", matched_keywords=["страховка", "обмен"]),
    ]

    assert unique_keywords(matches) == ["осаго", "страховка", "обмен"]


def test_unique_keywords_with_no_matches_returns_empty_list():
    assert unique_keywords([]) == []
