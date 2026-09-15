"""
Тесты reader/turkey_bot/texts.py::format_statistics/format_user_list_messages.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot import texts  # noqa: E402
from reader.turkey_bot.statistics_service import TurkeyStatistics  # noqa: E402

_TELEGRAM_LIMIT = 4096


def _stats(**overrides) -> TurkeyStatistics:
    defaults = {
        "total_users": 16, "new_users_today": 2, "new_users_7d": 8, "new_users_30d": 16,
        "total_checks": 40, "checks_today": 3, "checks_7d": 12, "checks_30d": 40,
        "checks_has_debt": 5, "checks_no_debt": 35,
    }
    defaults.update(overrides)
    return TurkeyStatistics(**defaults)


def test_format_statistics_includes_all_core_numbers():
    text = texts.format_statistics(_stats())

    assert "16" in text  # total_users
    assert "Новых сегодня: 2" in text
    assert "Новых за 7 дней: 8" in text
    assert "Новых за 30 дней: 16" in text
    assert "Всего: 40" in text
    assert "Сегодня: 3" in text
    assert "За 7 дней: 12" in text
    assert "За 30 дней: 40" in text
    assert "С задолженностью: 5" in text
    assert "Без задолженности: 35" in text


def test_format_statistics_does_not_mention_georgian_only_concepts():
    """Явное требование задачи: "Do NOT show Georgian-only metrics: active
    subscriptions, stopped subscriptions, monitoring tasks"."""
    text = texts.format_statistics(_stats()).lower()

    for forbidden in ("подписк", "мониторинг", "active", "stopped"):
        assert forbidden not in text


def test_user_list_username_shown_with_at_sign():
    messages = texts.format_user_list_messages([(111, "alice")])

    assert "@alice" in messages[0]


def test_user_list_falls_back_to_id_when_username_missing():
    messages = texts.format_user_list_messages([(111, None)])

    assert "ID: 111" in messages[0]


def test_user_list_header_shows_total_count():
    messages = texts.format_user_list_messages([(1, "a"), (2, None), (3, "c")])

    assert "Пользователи (3)" in messages[0]


def test_user_list_empty_shows_zero_count_not_error():
    messages = texts.format_user_list_messages([])

    assert len(messages) == 1
    assert "Пользователи (0)" in messages[0]


def test_user_list_preserves_given_order():
    messages = texts.format_user_list_messages([(1, "alice"), (2, "bob")])
    full_text = "\n".join(messages)

    assert full_text.index("alice") < full_text.index("bob")


def test_long_user_list_splits_safely_and_keeps_every_user():
    users = [(i, f"user{i}") for i in range(500)]

    messages = texts.format_user_list_messages(users)

    assert len(messages) > 1
    for message in messages:
        assert len(message) <= _TELEGRAM_LIMIT

    full_text = "\n".join(messages)
    for i in range(500):
        assert f"user{i}" in full_text  # никто не потерян/не обрезан
