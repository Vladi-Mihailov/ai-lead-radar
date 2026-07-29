from dataclasses import dataclass
from pathlib import Path

import yaml


class GroupLoadError(Exception):
    """Ошибка загрузки списка групп."""


@dataclass(frozen=True)
class Group:
    id: int | None
    username: str | None
    title: str | None

    @property
    def identifier(self) -> int | str:
        if self.username:
            return self.username
        if self.id is not None:
            return self.id
        raise GroupLoadError("У группы не указан ни id, ни username")


def load_groups(path: Path) -> list[Group]:
    path = Path(path)
    if not path.exists():
        raise GroupLoadError(f"Файл со списком групп не найден: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("groups") or []
    if not entries:
        raise GroupLoadError(f"В {path} не указано ни одной группы")

    groups = []
    for entry in entries:
        if entry.get("enabled", True) is False:
            continue

        group_id = entry.get("id")
        username = entry.get("username")
        if group_id is None and not username:
            raise GroupLoadError(f"Группа без id и username: {entry}")

        groups.append(
            Group(
                id=group_id,
                username=username,
                title=entry.get("title"),
            )
        )

    if not groups:
        raise GroupLoadError(f"В {path} нет ни одной активной (enabled) группы")

    return groups
