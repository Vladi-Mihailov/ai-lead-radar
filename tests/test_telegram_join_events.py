"""
Тесты reader/diagnostics/telegram_join_events.py — только собственная
чистая логика (классификация события/разбор --chat), без единого мока
TelegramClient/событий: это диагностический read-only скрипт, а не
production-компонент, для него незачем поднимать половину проекта.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.diagnostics.telegram_join_events import (  # noqa: E402
    classify_membership_event,
    parse_chat_identifier,
)

# ---- classify_membership_event ----


def test_classify_user_joined():
    assert (
        classify_membership_event(
            user_joined=True, user_added=False, user_left=False, user_kicked=False,
        )
        == "joined"
    )


def test_classify_user_added():
    assert (
        classify_membership_event(
            user_joined=False, user_added=True, user_left=False, user_kicked=False,
        )
        == "added"
    )


def test_classify_user_left():
    assert (
        classify_membership_event(
            user_joined=False, user_added=False, user_left=True, user_kicked=False,
        )
        == "left"
    )


def test_classify_user_kicked_maps_to_removed():
    assert (
        classify_membership_event(
            user_joined=False, user_added=False, user_left=False, user_kicked=True,
        )
        == "removed"
    )


def test_classify_no_flags_is_unknown():
    """Ни один из флагов не выставлен — например, ChatAction про смену
    названия/фото чата, а не про участников."""
    assert (
        classify_membership_event(
            user_joined=False, user_added=False, user_left=False, user_kicked=False,
        )
        == "unknown"
    )


def test_classify_priority_when_multiple_flags_set():
    """На практике Telethon не должен одновременно выставлять несколько
    флагов сразу, но классификация детерминирована и не падает даже в
    таком (искусственном) случае — join имеет приоритет."""
    assert (
        classify_membership_event(
            user_joined=True, user_added=True, user_left=True, user_kicked=True,
        )
        == "joined"
    )


# ---- parse_chat_identifier ----


def test_parse_chat_identifier_numeric():
    assert parse_chat_identifier("-1001234567890") == -1001234567890
    assert isinstance(parse_chat_identifier("123"), int)


def test_parse_chat_identifier_username_with_at():
    assert parse_chat_identifier("@my_group") == "my_group"


def test_parse_chat_identifier_username_without_at():
    assert parse_chat_identifier("my_group") == "my_group"


def test_parse_chat_identifier_returns_str_type_for_username():
    assert isinstance(parse_chat_identifier("my_group"), str)
