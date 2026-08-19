"""Сборка уникального списка ключевых слов из ScenarioMatch — общая функция
для reader/main.py (Pipeline, по новым сообщениям) и
reader/users/history_sync.py (sync_users.py, по истории), чтобы не
дублировать её в обоих местах.
"""

from reader.core.models import ScenarioMatch

# Синтетический тег (см. задачу) — добавляется в users.keywords ДОПОЛНИТЕЛЬНО
# к реальным matched_keywords, если сработал сценарий car_border_crossing
# (см. config/scenarios.yaml), независимо от того, какая именно фраза внутри
# сценария совпала ("как граница", "ларс", "проеду" и т.д. — их десятки).
# Это позволяет завести ОДНУ inviter-кампанию с
# --keyword car_border_crossing (см. reader/inviter/manage.py), которая
# подхватывает ВСЕХ пользователей этого сценария сразу — вместо отдельной
# кампании на каждую конкретную ключевую фразу (campaign.keyword матчится
# точным токеном в users.keywords, см. reader/inviter/repository.py::
# _CANDIDATES_BASE_WHERE, а не подстрокой).
CAR_BORDER_CROSSING_SCENARIO_NAME = "car_border_crossing"
CAR_BORDER_CROSSING_TAG = CAR_BORDER_CROSSING_SCENARIO_NAME


def unique_keywords(matches: list[ScenarioMatch]) -> list[str]:
    """Ключевые слова из всех совпавших сценариев, без дублей, с
    сохранением порядка первого появления — плюс синтетический тег
    CAR_BORDER_CROSSING_TAG, если среди matches есть сценарий
    car_border_crossing (см. модуль-докстрок выше). Реальные matched_
    keywords при этом никуда не пропадают — тег добавляется ДОПОЛНИТЕЛЬНО,
    после них."""
    seen: set[str] = set()
    result: list[str] = []
    for match in matches:
        for keyword in match.matched_keywords:
            if keyword not in seen:
                seen.add(keyword)
                result.append(keyword)

    if (
        CAR_BORDER_CROSSING_TAG not in seen
        and any(match.scenario_name == CAR_BORDER_CROSSING_SCENARIO_NAME for match in matches)
    ):
        result.append(CAR_BORDER_CROSSING_TAG)

    return result
