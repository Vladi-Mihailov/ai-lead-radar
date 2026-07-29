from dataclasses import dataclass
from pathlib import Path

import yaml

from reader.core.models import ScenarioMatch


class ScenarioLoadError(Exception):
    """Ошибка загрузки сценариев."""


@dataclass(frozen=True)
class Scenario:
    name: str
    enabled: bool
    keywords: tuple[str, ...]


def load_scenarios(path: Path) -> list[Scenario]:
    path = Path(path)
    if not path.exists():
        raise ScenarioLoadError(f"Файл сценариев не найден: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("scenarios") or []
    if not entries:
        raise ScenarioLoadError(f"В {path} не описано ни одного сценария")

    scenarios = []
    seen_names = set()
    for entry in entries:
        name = entry.get("name")
        if not name:
            raise ScenarioLoadError(f"Сценарий без имени: {entry}")
        if name in seen_names:
            raise ScenarioLoadError(f"Повторяющееся имя сценария: {name}")
        seen_names.add(name)

        keywords = entry.get("keywords") or []
        if not keywords:
            raise ScenarioLoadError(f"У сценария '{name}' не задано ни одного ключевого слова")

        scenarios.append(
            Scenario(
                name=name,
                enabled=entry.get("enabled", True),
                keywords=tuple(keywords),
            )
        )

    enabled_scenarios = [s for s in scenarios if s.enabled]
    if not enabled_scenarios:
        raise ScenarioLoadError(f"В {path} нет ни одного активного (enabled) сценария")

    return enabled_scenarios


def _normalize(text: str) -> str:
    return text.lower().strip()


class KeywordMatcher:
    def __init__(self, scenarios: list[Scenario]):
        self._scenarios = scenarios

    def match(self, text: str) -> list[ScenarioMatch]:
        if not text:
            return []

        normalized = _normalize(text)
        results = []
        for scenario in self._scenarios:
            hits = [kw for kw in scenario.keywords if _normalize(kw) in normalized]
            if hits:
                results.append(ScenarioMatch(scenario_name=scenario.name, matched_keywords=hits))
        return results
