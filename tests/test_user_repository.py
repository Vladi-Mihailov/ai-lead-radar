"""
Тесты UserRepository — локальный кэш пользователей Telegram, в частности
новое поле keywords (add_keywords/get_keywords) и автоматическая миграция
для баз, созданных до его появления.
"""

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.users.models import TelegramUserInfo  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402


def test_add_keywords_creates_user_when_absent(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.add_keywords(111, ["осаго", "страховка"])

        assert repository.get(111) is not None
        assert repository.get_keywords(111) == ["осаго", "страховка"]
    finally:
        repository.close()


def test_add_keywords_on_existing_user_preserves_identity_fields(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(
            TelegramUserInfo(user_id=222, username="ivan", first_name="Ivan", last_name=None)
        )

        repository.add_keywords(222, ["осаго"])

        user = repository.get(222)
        assert user.username == "ivan"
        assert user.first_name == "Ivan"
        assert repository.get_keywords(222) == ["осаго"]
    finally:
        repository.close()


def test_add_keywords_merges_without_duplicates_preserving_order(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.add_keywords(333, ["осаго", "страховка"])
        repository.add_keywords(333, ["страховка", "обмен"])

        assert repository.get_keywords(333) == ["осаго", "страховка", "обмен"]
    finally:
        repository.close()


def test_add_keywords_repeated_call_with_same_keywords_is_noop(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.add_keywords(444, ["осаго"])
        repository.add_keywords(444, ["осаго"])

        assert repository.get_keywords(444) == ["осаго"]
    finally:
        repository.close()


def test_add_keywords_with_empty_list_is_noop(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.add_keywords(555, [])

        assert repository.get(555) is None
        assert repository.get_keywords(555) == []
    finally:
        repository.close()


def test_get_keywords_returns_empty_list_for_unknown_user(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        assert repository.get_keywords(999999) == []
    finally:
        repository.close()


def test_migration_adds_keywords_column_to_legacy_database(tmp_path):
    db_path = tmp_path / "users.db"

    # База в старом формате — без колонки keywords, как до этой задачи.
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(
        """
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_bot INTEGER,
            last_seen_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    legacy_conn.execute(
        "INSERT INTO users (user_id, username, is_bot) VALUES (777, 'legacy_user', 0)"
    )
    legacy_conn.commit()
    legacy_conn.close()

    # UserRepository должен открыть эту базу без удаления/пересоздания и
    # добавить недостающую колонку автоматически.
    repository = UserRepository(db_path)
    try:
        existing_user = repository.get(777)
        assert existing_user is not None
        assert existing_user.username == "legacy_user"
        assert repository.get_keywords(777) == []

        repository.add_keywords(777, ["осаго"])
        assert repository.get_keywords(777) == ["осаго"]
    finally:
        repository.close()
