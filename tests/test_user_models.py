"""Тесты reader/users/models.py::TelegramUserInfo.from_telethon_user —
общий маппинг Telethon entity -> TelegramUserInfo, которым пользуется
reader/commands/fine.py (резолв @username, которого нет в локальной БД,
см. задачу), чтобы не дублировать этот маппинг ещё раз."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon.tl.types import User as TelethonUser  # noqa: E402

from reader.users.models import TelegramUserInfo  # noqa: E402


def test_from_telethon_user_maps_all_available_fields():
    entity = TelethonUser(
        id=555, username="santinorussia", first_name="Santino", last_name="Russia",
        access_hash=999888, bot=False,
    )

    info = TelegramUserInfo.from_telethon_user(entity)

    assert info.user_id == 555
    assert info.username == "santinorussia"
    assert info.first_name == "Santino"
    assert info.last_name == "Russia"
    assert info.access_hash == 999888
    assert info.is_bot is False
    assert info.peer_type == "User"


def test_from_telethon_user_defaults_missing_optional_fields_to_none():
    entity = TelethonUser(id=42)

    info = TelegramUserInfo.from_telethon_user(entity)

    assert info.user_id == 42
    assert info.username is None
    assert info.first_name is None
    assert info.last_name is None
    assert info.access_hash is None
    assert info.is_bot is False
    assert info.peer_type == "User"


def test_from_telethon_user_detects_bot_flag():
    entity = TelethonUser(id=999, username="some_bot", bot=True)

    info = TelegramUserInfo.from_telethon_user(entity)

    assert info.is_bot is True
