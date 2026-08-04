"""Сборка уникального списка ключевых слов из ScenarioMatch — общая функция
для reader/main.py (Pipeline, по новым сообщениям) и
reader/users/history_sync.py (sync_users.py, по истории), чтобы не
дублировать её в обоих местах.
"""

from reader.core.models import ScenarioMatch


def unique_keywords(matches: list[ScenarioMatch]) -> list[str]:
    """Ключевые слова из всех совпавших сценариев, без дублей, с
    сохранением порядка первого появления."""
    seen: set[str] = set()
    result: list[str] = []
    for match in matches:
        for keyword in match.matched_keywords:
            if keyword not in seen:
                seen.add(keyword)
                result.append(keyword)
    return result
