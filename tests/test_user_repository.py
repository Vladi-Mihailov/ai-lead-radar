"""
Тесты UserRepository — локальный кэш пользователей Telegram: keywords
(add_keywords/get_keywords), car_numbers (add_car_numbers/get_car_numbers),
access_hash/peer_type/peer_updated_at (для восстановления InputPeerUser без
@username) и автоматическая миграция для баз, созданных до появления этих
полей.
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


# ---- car_numbers (add_car_numbers/get_car_numbers) ----


def test_add_car_numbers_creates_user_when_absent(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.add_car_numbers(111, ["A111AA77", "X777XX197"])

        assert repository.get(111) is not None
        assert repository.get_car_numbers(111) == ["A111AA77", "X777XX197"]
    finally:
        repository.close()


def test_add_car_numbers_on_existing_user_preserves_identity_fields(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(
            TelegramUserInfo(user_id=222, username="ivan", first_name="Ivan", last_name=None)
        )

        repository.add_car_numbers(222, ["A111AA77"])

        user = repository.get(222)
        assert user.username == "ivan"
        assert user.first_name == "Ivan"
        assert repository.get_car_numbers(222) == ["A111AA77"]
    finally:
        repository.close()


def test_add_car_numbers_merges_without_duplicates_sorted(tmp_path):
    """В отличие от add_keywords() (порядок первого появления) — car_numbers
    хранятся сортированными, детерминированно, независимо от порядка
    добавления (см. задачу)."""
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.add_car_numbers(333, ["X777XX197"])
        repository.add_car_numbers(333, ["A111AA77", "X777XX197"])

        assert repository.get_car_numbers(333) == ["A111AA77", "X777XX197"]
    finally:
        repository.close()


def test_add_car_numbers_repeated_call_with_same_numbers_is_noop(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.add_car_numbers(444, ["A111AA77"])
        repository.add_car_numbers(444, ["A111AA77"])

        assert repository.get_car_numbers(444) == ["A111AA77"]
    finally:
        repository.close()


def test_add_car_numbers_with_empty_list_is_noop(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.add_car_numbers(555, [])

        assert repository.get(555) is None
        assert repository.get_car_numbers(555) == []
    finally:
        repository.close()


def test_get_car_numbers_returns_empty_list_for_unknown_user(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        assert repository.get_car_numbers(999999) == []
    finally:
        repository.close()


def test_car_numbers_do_not_affect_keywords_and_vice_versa(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.add_keywords(666, ["осаго"])
        repository.add_car_numbers(666, ["A111AA77"])

        assert repository.get_keywords(666) == ["осаго"]
        assert repository.get_car_numbers(666) == ["A111AA77"]
    finally:
        repository.close()


# ---- find_by_car_number() ----


def test_find_by_car_number_returns_matching_user(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.add_car_numbers(111, ["A111AA77"])

        found = repository.find_by_car_number("A111AA77")

        assert [u.user_id for u in found] == [111]
    finally:
        repository.close()


def test_find_by_car_number_returns_empty_list_when_not_found(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.add_car_numbers(111, ["A111AA77"])

        assert repository.find_by_car_number("X999XX99") == []
    finally:
        repository.close()


def test_find_by_car_number_returns_empty_list_when_no_users_have_car_numbers(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        assert repository.find_by_car_number("A111AA77") == []
    finally:
        repository.close()


def test_find_by_car_number_does_not_match_substring_of_another_plate(tmp_path):
    """Регрессия против наивного SQL LIKE по car_numbers: "A111AA77" не
    должен считаться найденным только потому, что он является подстрокой
    другого сохранённого номера."""
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.add_car_numbers(111, ["XA111AA779"])

        assert repository.find_by_car_number("A111AA77") == []
        assert [u.user_id for u in repository.find_by_car_number("XA111AA779")] == [111]
    finally:
        repository.close()


def test_find_by_car_number_matches_regardless_of_position_in_stored_list(tmp_path):
    """car_numbers хранится как "N1, N2, N3" (см. _format_car_numbers) —
    номер должен находиться независимо от того, первый он, последний или
    посередине списка конкретного пользователя."""
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.add_car_numbers(111, ["A111AA77", "B222BB77", "C333CC77"])

        assert [u.user_id for u in repository.find_by_car_number("A111AA77")] == [111]
        assert [u.user_id for u in repository.find_by_car_number("B222BB77")] == [111]
        assert [u.user_id for u in repository.find_by_car_number("C333CC77")] == [111]
    finally:
        repository.close()


def test_find_by_car_number_returns_all_users_when_ambiguous(tmp_path):
    """add_car_numbers() не гарантирует уникальность car_number между
    разными user_id — два разных Telegram-пользователя вполне могут
    упомянуть один и тот же номер в истории чата (см. докстрок
    find_by_car_number). Метод должен вернуть ОБОИХ, а не молча выбрать
    одного."""
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.add_car_numbers(111, ["A111AA77"])
        repository.add_car_numbers(222, ["A111AA77"])

        found = repository.find_by_car_number("A111AA77")

        assert sorted(u.user_id for u in found) == [111, 222]
    finally:
        repository.close()


def test_find_by_car_number_returns_full_user_info(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(
            TelegramUserInfo(
                user_id=111, username="ivan_petrov", first_name="Иван", last_name="Петров",
            )
        )
        repository.add_car_numbers(111, ["A111AA77"])

        [found] = repository.find_by_car_number("A111AA77")

        assert found.user_id == 111
        assert found.username == "ivan_petrov"
        assert found.first_name == "Иван"
        assert found.last_name == "Петров"
    finally:
        repository.close()


# ---- find_by_username() ----


def test_find_by_username_returns_matching_user(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(TelegramUserInfo(user_id=111, username="ivan_petrov", first_name=None, last_name=None))

        found = repository.find_by_username("ivan_petrov")

        assert found is not None
        assert found.user_id == 111
    finally:
        repository.close()


def test_find_by_username_returns_none_when_not_found(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        assert repository.find_by_username("unknown") is None
    finally:
        repository.close()


def test_find_by_username_is_case_insensitive(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(TelegramUserInfo(user_id=111, username="Ivan_Petrov", first_name=None, last_name=None))

        assert repository.find_by_username("ivan_petrov").user_id == 111
        assert repository.find_by_username("IVAN_PETROV").user_id == 111
        assert repository.find_by_username("Ivan_Petrov").user_id == 111
    finally:
        repository.close()


def test_find_by_username_does_not_strip_at_itself(tmp_path):
    """find_by_username() ожидает username уже БЕЗ ведущего '@' (вызывающий
    код, reader/commands/fine.py, сам его убирает) — с '@' совпадений не
    найдёт, потому что в БД username хранится без него (см. upsert())."""
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(TelegramUserInfo(user_id=111, username="ivan_petrov", first_name=None, last_name=None))

        assert repository.find_by_username("@ivan_petrov") is None
        assert repository.find_by_username("ivan_petrov") is not None
    finally:
        repository.close()


def test_migration_adds_car_numbers_column_to_legacy_database(tmp_path):
    db_path = tmp_path / "users.db"

    # База в формате до появления car_numbers (но уже с keywords/access_hash
    # и т.п. — промежуточная версия схемы, как на реальном сервере).
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
            access_hash INTEGER,
            peer_type TEXT,
            peer_updated_at TIMESTAMP,
            last_seen_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    legacy_conn.execute(
        "INSERT INTO users (user_id, username, is_bot, keywords) "
        "VALUES (999, 'legacy_user3', 0, 'осаго')"
    )
    legacy_conn.commit()
    legacy_conn.close()

    # UserRepository должен открыть эту базу без удаления/пересоздания и
    # добавить недостающую колонку car_numbers автоматически, без потери
    # уже существующих данных (keywords/username и т.п.).
    repository = UserRepository(db_path)
    try:
        columns = {row[1] for row in repository._conn.execute("PRAGMA table_info(users)")}
        assert "car_numbers" in columns

        existing_user = repository.get(999)
        assert existing_user is not None
        assert existing_user.username == "legacy_user3"
        assert repository.get_keywords(999) == ["осаго"]
        assert repository.get_car_numbers(999) == []

        repository.add_car_numbers(999, ["A111AA77"])
        assert repository.get_car_numbers(999) == ["A111AA77"]
        # keywords не затронуты добавлением car_numbers.
        assert repository.get_keywords(999) == ["осаго"]
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


# ---- update_access_hash() — резолв кандидата ЛИЧНО инвайтящим аккаунтом
# (reader/inviter/service.py: InviterService._resolve_input_peer) ----


def test_update_access_hash_updates_when_value_changed(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(
            TelegramUserInfo(user_id=905, username="ivan", first_name=None, last_name=None, access_hash=111)
        )

        updated = repository.update_access_hash(905, 999)

        assert updated is True
        assert repository.get(905).access_hash == 999
    finally:
        repository.close()


def test_update_access_hash_updates_username_when_it_changed(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(
            TelegramUserInfo(user_id=906, username="old_name", first_name=None, last_name=None, access_hash=111)
        )

        updated = repository.update_access_hash(906, 111, username="new_name")

        assert updated is True
        assert repository.get(906).username == "new_name"
    finally:
        repository.close()


def test_update_access_hash_ignores_empty_or_unchanged_username(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(
            TelegramUserInfo(user_id=907, username="ivan", first_name=None, last_name=None, access_hash=111)
        )

        # Тот же username — не считается изменением.
        repository.update_access_hash(907, 222, username="ivan")
        assert repository.get(907).username == "ivan"
        assert repository.get(907).access_hash == 222

        # Пустая строка — как "username не передан", не затирает текущий.
        repository.update_access_hash(907, 333, username="")
        assert repository.get(907).username == "ivan"
        assert repository.get(907).access_hash == 333
    finally:
        repository.close()


def test_update_access_hash_skips_write_when_nothing_changed(tmp_path):
    """Если access_hash совпадает, а username пуст либо совпадает — лишнего
    UPDATE быть не должно. updated_at (секундная точность CURRENT_TIMESTAMP)
    ненадёжен для этой проверки в рамках одного теста — вместо этого следим
    за sqlite3.Connection.total_changes (счётчик реально изменённых строк)."""
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(
            TelegramUserInfo(user_id=908, username="ivan", first_name=None, last_name=None, access_hash=111)
        )

        changes_before = repository._conn.total_changes
        updated = repository.update_access_hash(908, 111, username="ivan")

        assert updated is False
        assert repository._conn.total_changes == changes_before
    finally:
        repository.close()


def test_update_access_hash_returns_false_for_unknown_user(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        assert repository.update_access_hash(999999, 111) is False
    finally:
        repository.close()


def test_update_access_hash_does_not_touch_other_fields(tmp_path):
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(
            TelegramUserInfo(
                user_id=909, username="ivan", first_name="Ivan", last_name="Petrov",
                access_hash=111, is_bot=False,
            )
        )
        repository.add_keywords(909, ["осаго"])

        repository.update_access_hash(909, 222)

        user = repository.get(909)
        assert user.first_name == "Ivan"
        assert user.last_name == "Petrov"
        assert repository.get_keywords(909) == ["осаго"]
    finally:
        repository.close()


def test_update_access_hash_advances_peer_updated_at_when_it_actually_updates(tmp_path):
    """access_hash реально меняется (в т.ч. с "никогда не было" на
    заданное) -> peer_updated_at должен обновиться — время получения
    Telegram peer-данных, то же поле, что ставит upsert()."""
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(
            TelegramUserInfo(user_id=910, username="ivan", first_name=None, last_name=None)
        )
        assert repository.get_peer_updated_at(910) is None

        updated = repository.update_access_hash(910, 111)

        assert updated is True
        assert repository.get_peer_updated_at(910) is not None
    finally:
        repository.close()


def test_update_access_hash_leaves_peer_updated_at_untouched_when_skipped(tmp_path):
    """Ничего не изменилось (access_hash совпадает, username пуст/совпадает)
    -> update_access_hash() возвращает False и НЕ трогает peer_updated_at,
    как и остальные поля."""
    repository = UserRepository(tmp_path / "users.db")
    try:
        repository.upsert(
            TelegramUserInfo(
                user_id=911, username="ivan", first_name=None, last_name=None, access_hash=111,
            )
        )
        before = repository.get_peer_updated_at(911)
        assert before is not None

        updated = repository.update_access_hash(911, 111, username="ivan")

        assert updated is False
        assert repository.get_peer_updated_at(911) == before
    finally:
        repository.close()
