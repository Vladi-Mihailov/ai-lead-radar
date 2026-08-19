"""Тесты config/scenarios.yaml + reader/scenarios.py::KeywordMatcher —
загружают РЕАЛЬНЫЙ (не тестовый) конфиг сценариев, чтобы проверить
фактические ключевые слова проекта, а не их копию в тесте.

Покрывает регрессии из задачи: люди, которые собираются ехать на машине
через границу (даже без прямого упоминания страховки/штрафа/перевода),
должны попадать в pipeline через новый сценарий car_border_crossing —
но НЕ любой разговор, где мельком упомянуто что-то похожее по одному
общему слову (граница/обстановка/сколько/можно)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.scenarios import KeywordMatcher, load_scenarios  # noqa: E402
from reader.users.keyword_matches import CAR_BORDER_CROSSING_TAG, unique_keywords  # noqa: E402

_SCENARIOS_PATH = PROJECT_ROOT / "config" / "scenarios.yaml"
_CAR_BORDER_SCENARIO = "car_border_crossing"


def _match(text: str):
    scenarios = load_scenarios(_SCENARIOS_PATH)
    matcher = KeywordMatcher(scenarios)
    return matcher.match(text)


def _matched_scenario_names(text: str) -> set[str]:
    return {match.scenario_name for match in _match(text)}


def _is_car_border_lead(text: str) -> bool:
    return _CAR_BORDER_SCENARIO in _matched_scenario_names(text)


def test_car_border_crossing_scenario_is_enabled():
    scenarios = load_scenarios(_SCENARIOS_PATH)
    names = {s.name for s in scenarios}
    assert _CAR_BORDER_SCENARIO in names


# ---- positive: recall для автомобильных пограничных сообщений (см. задачу) ----


def test_how_is_the_border_today_is_a_lead():
    assert _is_car_border_lead("Как граница сегодня?")


def test_what_is_the_situation_on_the_border_is_a_lead():
    assert _is_car_border_lead("Какая обстановка на границе?")


def test_can_i_leave_now_in_border_context_is_a_lead():
    """Раздельные слова "можно"/"выезжать" в реальной фразе часто разделены
    другими словами ("сейчас") — стем "выезжа" должен ловить это,
    независимо от точного порядка/вставок."""
    assert _is_car_border_lead("Стоим у границы, можно сейчас выезжать?")


def test_how_long_until_i_cross_the_border_right_now_is_a_lead():
    assert _is_car_border_lead("За сколько сейчас проеду границу?")


def test_queue_at_lars_is_a_lead():
    assert _is_car_border_lead("Какая очередь на Ларсе?")


def test_road_through_the_pass_is_a_lead():
    assert _is_car_border_lead("Как дорога через перевал?")


def test_is_the_pass_open_is_a_lead():
    assert _is_car_border_lead("Перевал открыт?")


# ---- дополнительный recall: явные автомобильные пограничные фразы из задачи ----


def test_driving_to_georgia_by_car_mentions_upper_lars():
    assert _is_car_border_lead("Планируем ехать через Верхний Ларс, как сейчас там?")


def test_border_is_open_phrase_is_a_lead():
    assert _is_car_border_lead("Подскажите, открыта граница сегодня?")


def test_documents_question_with_explicit_border_context_is_a_lead():
    assert _is_car_border_lead("Какая обстановка на границе, есть очередь?")


# ---- negative: не ловим слишком общее (см. задачу) ----


def test_situation_in_the_city_does_not_match_just_because_of_situation_word():
    """"обстановка" сама по себе НЕ должна матчить — только в связке с
    границей/дорогой/перевалом (см. car_border_crossing keywords)."""
    assert not _is_car_border_lead("Какая обстановка в городе?")


def test_selling_price_question_is_not_a_border_lead():
    assert not _is_car_border_lead("За сколько продашь?")


def test_bare_can_question_is_not_a_lead():
    assert not _is_car_border_lead("Можно?")


def test_bare_how_much_question_is_not_a_border_lead():
    """"сколько" само по себе не должно быть якорем — без границы/дороги/
    очереди/перевала рядом это не сигнал автомобильной поездки."""
    assert not _is_car_border_lead("Сколько время?")


def test_unrelated_past_tense_border_mention_without_question_words_is_not_a_lead():
    """Разговор о ГРАНИЦЕ постфактум (не вопрос "как сейчас"/"что там"), без
    привязки к устойчивым фразам сценария — не должен матчиться просто
    потому, что где-то упомянуто слово "граница" (см. задачу: "не ловить
    совсем любой разговор про границу")."""
    assert not _is_car_border_lead("Вчера ночевали недалеко от границы, красивые виды.")


# ---- синтетический тег car_border_crossing поверх реального конфига (см. задачу) ----


def test_real_car_border_crossing_match_yields_synthetic_tag_via_unique_keywords():
    """Сквозная проверка: реальные сценарии из config/scenarios.yaml +
    unique_keywords() дают синтетический тег car_border_crossing для
    любого совпавшего сообщения — независимо от того, какая именно из
    десятков фраз сценария сработала (см. reader/users/keyword_matches.py)."""
    matches = _match("Какая очередь на Ларсе?")

    keywords = unique_keywords(matches)

    assert "ларс" in keywords
    assert "очередь" in keywords
    assert CAR_BORDER_CROSSING_TAG in keywords


def test_real_insurance_only_match_does_not_yield_car_border_crossing_tag():
    matches = _match("Нужна страховка на машину")

    keywords = unique_keywords(matches)

    assert CAR_BORDER_CROSSING_TAG not in keywords
