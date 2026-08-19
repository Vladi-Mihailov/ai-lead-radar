"""Тесты unique_keywords() — общей сборки уникальных ключевых слов из
ScenarioMatch, используемой и Pipeline (reader/main.py), и history_sync.py
(sync_users.py). Также покрывает синтетический тег
CAR_BORDER_CROSSING_TAG (см. задачу): единая метка для ВСЕХ пользователей,
пойманных сценарием car_border_crossing, независимо от того, какая именно
из десятков его ключевых фраз совпала — чтобы одна inviter-кампания с
keyword=car_border_crossing подхватывала их всех сразу."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.core.models import ScenarioMatch  # noqa: E402
from reader.users.keyword_matches import (  # noqa: E402
    CAR_BORDER_CROSSING_TAG,
    unique_keywords,
)


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


# ---- синтетический тег car_border_crossing (см. задачу) ----


def test_car_border_crossing_match_adds_synthetic_tag_after_real_keywords():
    """Реальные matched_keywords сохраняются как есть (см. задачу:
    "существующие matched keywords можно сохранить дополнительно") —
    синтетический тег добавляется ПОСЛЕ них, не заменяя их."""
    matches = [
        ScenarioMatch(scenario_name="car_border_crossing", matched_keywords=["ларс", "как граница"]),
    ]

    assert unique_keywords(matches) == ["ларс", "как граница", CAR_BORDER_CROSSING_TAG]


def test_car_border_crossing_tag_added_regardless_of_which_specific_keyword_matched():
    """Ровно та же метка независимо от конкретной сработавшей фразы — тег
    один и тот же вне зависимости от того, "ларс" это, "как граница" или
    любая из десятков других фраз сценария (см. задачу)."""
    matches_a = [ScenarioMatch(scenario_name="car_border_crossing", matched_keywords=["ларс"])]
    matches_b = [ScenarioMatch(scenario_name="car_border_crossing", matched_keywords=["как дорога"])]
    matches_c = [ScenarioMatch(scenario_name="car_border_crossing", matched_keywords=["проеду"])]

    assert CAR_BORDER_CROSSING_TAG in unique_keywords(matches_a)
    assert CAR_BORDER_CROSSING_TAG in unique_keywords(matches_b)
    assert CAR_BORDER_CROSSING_TAG in unique_keywords(matches_c)


def test_car_border_crossing_tag_not_added_for_unrelated_scenarios():
    matches = [
        ScenarioMatch(scenario_name="insurance", matched_keywords=["страховка"]),
    ]

    assert unique_keywords(matches) == ["страховка"]
    assert CAR_BORDER_CROSSING_TAG not in unique_keywords(matches)


def test_car_border_crossing_tag_added_once_alongside_other_scenario_matches():
    """Сообщение, совпавшее и с insurance, и с car_border_crossing одним
    ScenarioMatch-списком (обычный случай — одно сообщение может дать
    несколько ScenarioMatch, см. reader/core/engine.py) — тег появляется
    ровно один раз, реальные ключевые слова обоих сценариев сохраняются."""
    matches = [
        ScenarioMatch(scenario_name="insurance", matched_keywords=["авто"]),
        ScenarioMatch(scenario_name="car_border_crossing", matched_keywords=["ларс"]),
    ]

    result = unique_keywords(matches)

    assert result == ["авто", "ларс", CAR_BORDER_CROSSING_TAG]
    assert result.count(CAR_BORDER_CROSSING_TAG) == 1


def test_car_border_crossing_tag_is_not_duplicated_if_scenario_matches_twice():
    """Гипотетический край: если бы car_border_crossing почему-то встретился
    в matches дважды отдельными ScenarioMatch — тег всё равно один."""
    matches = [
        ScenarioMatch(scenario_name="car_border_crossing", matched_keywords=["ларс"]),
        ScenarioMatch(scenario_name="car_border_crossing", matched_keywords=["как граница"]),
    ]

    result = unique_keywords(matches)

    assert result.count(CAR_BORDER_CROSSING_TAG) == 1
    assert result == ["ларс", "как граница", CAR_BORDER_CROSSING_TAG]


def test_car_border_crossing_tag_not_duplicated_if_already_present_as_a_real_keyword():
    """Гипотетический край: если бы синтетический тег случайно совпал с
    именем реального matched keyword — не дублируется."""
    matches = [
        ScenarioMatch(
            scenario_name="car_border_crossing",
            matched_keywords=["ларс", CAR_BORDER_CROSSING_TAG],
        ),
    ]

    result = unique_keywords(matches)

    assert result.count(CAR_BORDER_CROSSING_TAG) == 1
    assert result == ["ларс", CAR_BORDER_CROSSING_TAG]
