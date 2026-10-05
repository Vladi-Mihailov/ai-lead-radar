from dataclasses import dataclass
from pathlib import Path

import yaml


class GroupLoadError(Exception):
    """Ошибка загрузки списка групп."""


# resource_region — ЯВНОЕ значение из config/groups.yaml (ge/tr/am): какие
# страновые ресурсы (например, @ProtocolGEbot/@ProtocolTRbot) разрешены для
# лидов ЭТОЙ группы. Это НЕ физическая страна группы и никогда не
# угадывается по её названию в runtime. Нет поля — "unknown" (старые
# конфиги работают как раньше; страновые ресурсы не разрешаются).
REGION_UNKNOWN = "unknown"
RESOURCE_REGIONS = frozenset({"ge", "tr", "am"})


@dataclass(frozen=True)
class Group:
    id: int | None
    username: str | None
    title: str | None
    resource_region: str = REGION_UNKNOWN

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

        raw_region = entry.get("resource_region")
        region = str(raw_region).strip().lower() if raw_region else REGION_UNKNOWN
        if region != REGION_UNKNOWN and region not in RESOURCE_REGIONS:
            raise GroupLoadError(
                f"Недопустимый resource_region {raw_region!r} у группы {username or group_id} "
                f"(допустимо: {', '.join(sorted(RESOURCE_REGIONS))})"
            )

        groups.append(
            Group(
                id=group_id,
                username=username,
                title=entry.get("title"),
                resource_region=region,
            )
        )

    if not groups:
        raise GroupLoadError(f"В {path} нет ни одной активной (enabled) группы")

    return groups
