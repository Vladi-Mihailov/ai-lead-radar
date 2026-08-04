"""
Тесты UserRepository — локальный кэш пользователей Telegram: keywords
(add_keywords/get_keywords), access_hash/peer_type/peer_updated_at (для
восстановления InputPeerUser без @username) и автоматическая миграция для
баз, созданных до появления этих полей.
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


def test_migration_adds_peer_columns_to_legacy_database(tmp_path):
    db_path = tmp_path / "users.db"

    # База в формате до появления access_hash/peer_type/peer_updated_at
    # (но уже с keywords — промежуточная версия схемы).
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(
        """
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_bot INTEGER,
            keywords TEXT,
            last_seen_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    legacy_conn.execute(
        "INSERT INTO users (user_id, username, is_bot) VALUES (888, 'legacy_user2', 0)"
    )
    legacy_conn.commit()
    legacy_conn.close()

    repository = UserRepository(db_path)
    try:
        columns = {
            row[1] for row in repository._conn.execute("PRAGMA table_info(users)")
        }
        assert {"access_hash", "peer_type", "peer_updated_at"} <= columns

        existing_user = repository.get(888)
        assert existing_user is not None
        assert existing_user.access_hash is None
        assert existing_user.peer_type is None
        assert repository.get_peer_updated_at(888) is None

        repository.upsert(
            TelegramUserInfo(
                user_id=888, username="legacy_user2", first_name=None, last_name=None,
                access_hash=123456789, peer_type="User",
            )
        )
        updated_user = repository.get(888)
        assert updated_user.access_hash == 123456789
        assert updated_user.peer_type == "User"
        assert repository.get_peer_updated_at(888) is not None
    finally:
        repository.close()


# ---- access_hash / peer_type ----


def test_upsert_saves_access_hash_and_peer_type(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(
            TelegramUserInfo(
                user_id=901, username="ivan", first_name="Ivan", last_name=None,
                access_hash=111222333, peer_type="User",
            )
        )

        user = repository.get(901)
        assert user.access_hash == 111222333
        assert user.peer_type == "User"
        assert repository.get_peer_updated_at(901) is not None
    finally:
        repository.close()


def test_upsert_without_access_hash_does_not_overwrite_existing_one(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(
            TelegramUserInfo(
                user_id=902, username="ivan", first_name=None, last_name=None,
                access_hash=111222333, peer_type="User",
            )
        )
        first_peer_updated_at = repository.get_peer_updated_at(902)

        # Обычный upsert без access_hash (как делает большинство существующих
        # вызовов) — не должен затирать уже сохранённый хэш.
        repository.upsert(
            TelegramUserInfo(user_id=902, username="ivan_new", first_name=None, last_name=None)
        )

        user = repository.get(902)
        assert user.username == "ivan_new"
        assert user.access_hash == 111222333
        assert user.peer_type == "User"
        assert repository.get_peer_updated_at(902) == first_peer_updated_at
    finally:
        repository.close()


def test_upsert_updates_access_hash_when_it_changes(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(
            TelegramUserInfo(
                user_id=903, username="ivan", first_name=None, last_name=None,
                access_hash=111, peer_type="User",
            )
        )

        # access_hash может смениться (например, после смены сессии) — новый
        # upsert с новым значением должен его обновить, а не игнорировать.
        repository.upsert(
            TelegramUserInfo(
                user_id=903, username="ivan", first_name=None, last_name=None,
                access_hash=999, peer_type="User",
            )
        )

        assert repository.get(903).access_hash == 999
    finally:
        repository.close()


def test_get_returns_none_access_hash_and_peer_type_when_never_set(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(
            TelegramUserInfo(user_id=904, username="ivan", first_name=None, last_name=None)
        )

        user = repository.get(904)
        assert user.access_hash is None
        assert user.peer_type is None
        assert repository.get_peer_updated_at(904) is None
    finally:
        repository.close()
