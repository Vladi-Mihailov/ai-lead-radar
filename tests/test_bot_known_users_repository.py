"""
Тесты BotKnownUsersRepository (reader/public_bot/known_users_repository.py) —
включая count_total()/count_first_seen_since(), добавленные для "📊
Статистика" (см. reader/public_bot/statistics_service.py). До этой задачи
у репозитория не было отдельного файла тестов вовсе.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402


def test_record_seen_creates_new_known_user(tmp_path):
    repo = BotKnownUsersRepository(tmp_path / "users.db")
    try:
        repo.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username="alice")

        user = repo.get(1)
        assert user is not None
        assert user.telegram_user_id == 1
        assert user.telegram_username == "alice"
        assert repo.is_known(1) is True
    finally:
        repo.close()


def test_is_known_false_for_unseen_user(tmp_path):
    repo = BotKnownUsersRepository(tmp_path / "users.db")
    try:
        assert repo.is_known(999) is False
        assert repo.get(999) is None
    finally:
        repo.close()


def test_record_seen_again_does_not_change_first_seen_at(tmp_path):
    """Ключевой инвариант для "📊 Статистика": first_seen_at выставляется
    ОДИН раз, повторные record_seen() (последующие визиты) его не
    трогают — иначе "новых" пользователей нельзя было бы честно
    посчитать по реальной дате первого появления."""
    repo = BotKnownUsersRepository(tmp_path / "users.db")
    try:
        repo.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username="alice")
        first = repo.get(1)

        repo.record_seen(telegram_user_id=1, telegram_chat_id=2, telegram_username="alice_new")
        second = repo.get(1)

        assert second.first_seen_at == first.first_seen_at
        assert second.telegram_chat_id == 2  # last_seen-подобные поля обновляются
        assert second.telegram_username == "alice_new"
        assert second.last_seen_at >= first.last_seen_at
    finally:
        repo.close()


# ---- count_total()/count_first_seen_since() — "📊 Статистика" ----


def test_count_total_zero_for_empty_database(tmp_path):
    repo = BotKnownUsersRepository(tmp_path / "users.db")
    try:
        assert repo.count_total() == 0
    finally:
        repo.close()


def test_count_total_counts_unique_users(tmp_path):
    repo = BotKnownUsersRepository(tmp_path / "users.db")
    try:
        repo.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)
        repo.record_seen(telegram_user_id=2, telegram_chat_id=2, telegram_username=None)
        repo.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)  # повтор

        assert repo.count_total() == 2
    finally:
        repo.close()


def test_count_first_seen_since_zero_for_empty_database(tmp_path):
    repo = BotKnownUsersRepository(tmp_path / "users.db")
    try:
        assert repo.count_first_seen_since(datetime.now(timezone.utc) - timedelta(days=1)) == 0
    finally:
        repo.close()


def test_count_first_seen_since_excludes_users_before_boundary(tmp_path):
    """Пользователь, появившийся ДО границы, не считается "новым" —
    подтверждает, что подсчёт идёт по first_seen_at, а не по count_total()."""
    repo = BotKnownUsersRepository(tmp_path / "users.db")
    try:
        repo.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)

        future_boundary = datetime.now(timezone.utc) + timedelta(days=1)
        assert repo.count_first_seen_since(future_boundary) == 0
    finally:
        repo.close()


def test_count_first_seen_since_includes_users_after_boundary(tmp_path):
    repo = BotKnownUsersRepository(tmp_path / "users.db")
    try:
        repo.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)

        past_boundary = datetime.now(timezone.utc) - timedelta(days=1)
        assert repo.count_first_seen_since(past_boundary) == 1
    finally:
        repo.close()


def test_repeated_activity_does_not_count_old_user_as_new(tmp_path):
    """Явное требование задачи: "duplicate/update activity does not count
    old user as new" — старый пользователь, снова написавший боту
    (record_seen() повторно) СЕГОДНЯ, не должен попасть в "новых
    сегодня", потому что first_seen_at у него — из прошлого."""
    repo = BotKnownUsersRepository(tmp_path / "users.db")
    try:
        # Симулируем "давнего" пользователя: пишем строку напрямую с
        # first_seen_at в прошлом (record_seen() всегда пишет "сейчас" —
        # для честного теста границы нужен контроль над датой).
        repo._conn.execute(
            "INSERT INTO bot_known_users "
            "(telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at) "
            "VALUES (1, 1, 'old_user', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        repo._conn.commit()

        # Пользователь снова написал боту "сегодня" — last_seen_at
        # обновляется, first_seen_at — нет (см. test выше).
        repo.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username="old_user")

        today_start = datetime.now(timezone.utc) - timedelta(hours=1)
        assert repo.count_first_seen_since(today_start) == 0  # НЕ новый
        assert repo.count_total() == 1
    finally:
        repo.close()
