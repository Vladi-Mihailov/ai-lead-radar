"""
Тесты TurkeyBotKnownUsersRepository (reader/turkey_bot/known_users_repository.py) —
своя таблица (turkey_bot_known_users), отдельная от Георгии bot_known_users
(см. design report Stage 1: этот факт per-bot, не глобальный).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.known_users_repository import (
    TurkeyBotKnownUsersRepository,  # noqa: E402
)


def test_record_seen_creates_new_known_user(tmp_path):
    repo = TurkeyBotKnownUsersRepository(tmp_path / "users.db")
    try:
        repo.record_seen(telegram_user_id=1, telegram_chat_id=100, telegram_username="alice")

        assert repo.is_known(1) is True
    finally:
        repo.close()


def test_is_known_false_for_unseen_user(tmp_path):
    repo = TurkeyBotKnownUsersRepository(tmp_path / "users.db")
    try:
        assert repo.is_known(999) is False
    finally:
        repo.close()


def test_record_seen_upserts_username_with_coalesce(tmp_path):
    repo = TurkeyBotKnownUsersRepository(tmp_path / "users.db")
    try:
        repo.record_seen(telegram_user_id=1, telegram_chat_id=100, telegram_username="alice")
        repo.record_seen(telegram_user_id=1, telegram_chat_id=100, telegram_username=None)

        row = repo._conn.execute(
            "SELECT telegram_username FROM turkey_bot_known_users WHERE telegram_user_id = 1"
        ).fetchone()
        assert row[0] == "alice"  # None не затирает уже сохранённый username
    finally:
        repo.close()


def test_uses_its_own_table_not_georgian_bots_table(tmp_path):
    """См. design report Stage 1: этот факт per-bot — своя таблица, не
    reader/public_bot/known_users_repository.py::bot_known_users."""
    repo = TurkeyBotKnownUsersRepository(tmp_path / "users.db")
    try:
        tables = {
            row[0]
            for row in repo._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "turkey_bot_known_users" in tables
        assert "bot_known_users" not in tables
    finally:
        repo.close()
