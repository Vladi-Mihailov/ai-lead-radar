"""
Тесты reader/groups.py — загрузка списка отслеживаемых Telegram-групп из
config/groups.yaml. Только сам загрузчик (без TelegramClient/сети) плюс
regression-проверки реального config/groups.yaml проекта (см. задачу про
добавление групп про Турцию).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.groups import Group, GroupLoadError, load_groups  # noqa: E402

_CONFIG_GROUPS_PATH = PROJECT_ROOT / "config" / "groups.yaml"

_NEW_TURKEY_USERNAMES = {
    "turtsiab",
    "russiansinturkey_antalya",
    "russiansinturkey_stambul",
    "russiansinturkey_all",
    "istanbul_ru",
    "stambuli",
    "chat_istambul_help",
    "nedvizhkaalaniya",
    "russianturkiyeruss",
    "turkey_ourantalya",
}

_PRE_EXISTING_USERNAMES = {
    "VerhniyLars",
    "krayzemlige",
    "Sadahlo",
    "sarpi_ge",
    "verkhniy_lars_dariali",
    "geolars",
    "tbilisi14",
    "mytbilisi_chat",
    "grusia1",
    "expats_georgia",
    "saburtalo_tbi",
    "vake_tbi",
    "svoi_tbilisi",
    "mybatumi_chat",
    "batumi_people",
    "georgia_auto",
}


def _write_groups_yaml(tmp_path, content: str) -> Path:
    path = tmp_path / "groups.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_groups_returns_only_enabled_entries(tmp_path):
    path = _write_groups_yaml(
        tmp_path,
        """
groups:
  - username: "one"
    title: "One"
    enabled: true
  - username: "two"
    title: "Two"
    enabled: false
""",
    )

    groups = load_groups(path)

    assert [g.username for g in groups] == ["one"]


def test_load_groups_defaults_enabled_to_true_when_omitted(tmp_path):
    """enabled не указан вовсе — группа всё равно активна (см.
    entry.get("enabled", True) в reader/groups.py)."""
    path = _write_groups_yaml(
        tmp_path,
        """
groups:
  - username: "one"
""",
    )

    groups = load_groups(path)

    assert [g.username for g in groups] == ["one"]


def test_load_groups_supports_private_group_by_numeric_id(tmp_path):
    path = _write_groups_yaml(
        tmp_path,
        """
groups:
  - id: -1001234567890
    title: "Приватная группа"
    enabled: true
""",
    )

    [group] = load_groups(path)

    assert group.id == -1001234567890
    assert group.username is None
    assert group.identifier == -1001234567890


def test_load_groups_title_is_optional(tmp_path):
    """title можно не указывать — резолвится позже самим TelegramSource из
    live entity (см. reader/sources/telegram_source.py::_resolve_groups),
    а не придумывается на этапе конфига."""
    path = _write_groups_yaml(
        tmp_path,
        """
groups:
  - username: "no_title_here"
    enabled: true
""",
    )

    [group] = load_groups(path)

    assert group.title is None
    assert group.identifier == "no_title_here"


def test_load_groups_raises_when_file_missing(tmp_path):
    with pytest.raises(GroupLoadError):
        load_groups(tmp_path / "does-not-exist.yaml")


def test_load_groups_raises_when_no_groups_key(tmp_path):
    path = _write_groups_yaml(tmp_path, "groups: []\n")

    with pytest.raises(GroupLoadError):
        load_groups(path)


def test_load_groups_raises_when_all_groups_disabled(tmp_path):
    path = _write_groups_yaml(
        tmp_path,
        """
groups:
  - username: "one"
    enabled: false
""",
    )

    with pytest.raises(GroupLoadError):
        load_groups(path)


def test_load_groups_raises_when_entry_has_neither_id_nor_username(tmp_path):
    path = _write_groups_yaml(
        tmp_path,
        """
groups:
  - title: "Без идентификатора"
    enabled: true
""",
    )

    with pytest.raises(GroupLoadError):
        load_groups(path)


def test_group_identifier_prefers_username_over_id():
    group = Group(id=123, username="somebody", title=None)

    assert group.identifier == "somebody"


# ---- regression: реальный config/groups.yaml проекта (см. задачу про -----
# ---- добавление 10 турецких групп/каналов) --------------------------------


def test_project_groups_yaml_loads_without_errors():
    groups = load_groups(_CONFIG_GROUPS_PATH)

    assert len(groups) >= len(_PRE_EXISTING_USERNAMES) + len(_NEW_TURKEY_USERNAMES)


def test_project_groups_yaml_contains_all_new_turkey_usernames():
    groups = load_groups(_CONFIG_GROUPS_PATH)
    usernames = {g.username for g in groups if g.username}

    assert _NEW_TURKEY_USERNAMES <= usernames


def test_project_groups_yaml_preserves_pre_existing_usernames():
    """Существующие группы не удалены и не переименованы (см. задачу)."""
    groups = load_groups(_CONFIG_GROUPS_PATH)
    usernames = {g.username for g in groups if g.username}

    assert _PRE_EXISTING_USERNAMES <= usernames


def test_project_groups_yaml_has_no_duplicate_usernames():
    groups = load_groups(_CONFIG_GROUPS_PATH)
    usernames = [g.username for g in groups if g.username]

    assert len(usernames) == len(set(usernames))
