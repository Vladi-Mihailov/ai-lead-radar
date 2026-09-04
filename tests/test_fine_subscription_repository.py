"""
Тесты FineSubscriptionRepository — SQLite-репозиторий клиентских подписок
на мониторинг (@GEShtrafbot foundation, см. design report). Только сама
таблица/репозиторий — без Telegram-бота/handlers (их пока нет).
"""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.subscription_repository import (  # noqa: E402
    DuplicateActiveSubscriptionError,
    FineSubscriptionRepository,
)

_CHAT_ID = -100999
_USER_ID = 111
_BOT_CHAT_ID_A = 1001
_BOT_CHAT_ID_B = 2002


def _make_task(db_path, car_number="B957MA09") -> int:
    """Реальная задача мониторинга — нужна из-за FOREIGN KEY на
    fine_monitoring_subscriptions (тот же приём, что и
    tests/test_detected_fine_repository.py::_make_task)."""
    task_repo = FineMonitoringTaskRepository(db_path)
    try:
        task = task_repo.create(
            car_number=car_number, label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        return task.id
    finally:
        task_repo.close()


def _make_repo(tmp_path) -> FineSubscriptionRepository:
    return FineSubscriptionRepository(tmp_path / "users.db")


def test_create_and_get_roundtrip(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        sub = repo.create(
            monitoring_task_id=task_id,
            car_number="B957MA09",
            telegram_user_id=42,
            telegram_chat_id=42,
            telegram_username="client_one",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 12, 1),
        )

        assert sub.id is not None
        assert sub.monitoring_task_id == task_id
        assert sub.car_number == "B957MA09"
        assert sub.telegram_user_id == 42
        assert sub.telegram_chat_id == 42
        assert sub.telegram_username == "client_one"
        assert sub.status == "active"
        assert sub.start_date == date(2026, 9, 1)
        assert sub.end_date == date(2026, 12, 1)
        assert sub.source == "geshtrafbot"
        assert sub.stopped_at is None

        assert repo.get(sub.id) == sub
    finally:
        repo.close()


def test_create_without_username_is_allowed(tmp_path):
    """telegram_username — контактный атрибут, НЕ identity (см. design) —
    его отсутствие не должно мешать созданию подписки."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        sub = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=42, telegram_chat_id=42, telegram_username=None,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )
        assert sub.telegram_username is None
    finally:
        repo.close()


# ---- duplicate active subscription ----


def test_duplicate_active_subscription_is_rejected(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=42, telegram_chat_id=42, telegram_username="client",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        with pytest.raises(DuplicateActiveSubscriptionError):
            repo.create(
                monitoring_task_id=task_id, car_number="B957MA09",
                telegram_user_id=42, telegram_chat_id=42, telegram_username="client",
                start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            )
    finally:
        repo.close()


def test_new_subscription_allowed_after_previous_one_stopped(tmp_path):
    """Частичный уникальный индекс покрывает только status='active' —
    после остановки прежней подписки того же (task, user) новую заводить
    можно (например, клиент повторно "Добавить авто" после "Остановить
    мониторинг")."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        first = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=42, telegram_chat_id=42, telegram_username="client",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )
        assert repo.stop_by_owner_or_creator(first.id, telegram_user_id=42) is True

        second = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=42, telegram_chat_id=42, telegram_username="client",
            start_date=date(2026, 9, 1), end_date=date(2027, 1, 1),
        )
        assert second.id != first.id
        assert second.status == "active"
    finally:
        repo.close()


# ---- несколько клиентов на одном авто / один клиент на нескольких авто ----


def test_multiple_clients_on_same_car(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path, car_number="B957MA09")

    repo = FineSubscriptionRepository(db_path)
    try:
        first = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="alice",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )
        second = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=2, telegram_chat_id=2, telegram_username="bob",
            start_date=date(2026, 9, 1), end_date=date(2027, 3, 1),
        )

        subscribers = repo.list_active_subscribers_for_car("B957MA09", today=date(2026, 9, 15))

        assert {s.id for s in subscribers} == {first.id, second.id}
    finally:
        repo.close()


def test_one_client_on_multiple_cars(tmp_path):
    db_path = tmp_path / "users.db"
    task_a = _make_task(db_path, car_number="AA001AA")
    task_b = _make_task(db_path, car_number="BB002BB")

    repo = FineSubscriptionRepository(db_path)
    try:
        sub_a = repo.create(
            monitoring_task_id=task_a, car_number="AA001AA",
            telegram_user_id=99, telegram_chat_id=99, telegram_username="driver",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )
        sub_b = repo.create(
            monitoring_task_id=task_b, car_number="BB002BB",
            telegram_user_id=99, telegram_chat_id=99, telegram_username="driver",
            start_date=date(2026, 9, 1), end_date=date(2027, 6, 1),
        )

        subscriptions = repo.list_by_user(99)

        assert {s.id for s in subscriptions} == {sub_a.id, sub_b.id}
        assert repo.get_active_for_user_and_car(99, "AA001AA", today=date(2026, 9, 15)).id == sub_a.id
        assert repo.get_active_for_user_and_car(99, "BB002BB", today=date(2026, 9, 15)).id == sub_b.id
        assert repo.get_active_for_user_and_car(99, "CC003CC", today=date(2026, 9, 15)) is None
    finally:
        repo.close()


# ---- stop не затрагивает чужую подписку ----


def test_stop_by_owner_or_creator_does_not_affect_another_users_subscription(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path, car_number="B957MA09")

    repo = FineSubscriptionRepository(db_path)
    try:
        alice = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="alice",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )
        bob = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=2, telegram_chat_id=2, telegram_username="bob",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        stopped = repo.stop_by_owner_or_creator(alice.id, telegram_user_id=1)

        assert stopped is True
        assert repo.get(alice.id).status == "stopped"
        assert repo.get(alice.id).stopped_at is not None
        assert repo.get(bob.id).status == "active"  # чужая подписка не тронута

        remaining = repo.list_active_subscribers_for_car("B957MA09", today=date(2026, 9, 15))
        assert [s.id for s in remaining] == [bob.id]
    finally:
        repo.close()


def test_stop_by_owner_or_creator_rejects_wrong_owner(tmp_path):
    """Ключевая защита от "чужого авто через подделанный id" (см. design
    про forwarded-кнопки/callback_data) — repository-уровень отказывает,
    даже если вызывающий код (будущие bot-хендлеры) ошибся бы с
    owner-check."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path, car_number="B957MA09")

    repo = FineSubscriptionRepository(db_path)
    try:
        alice = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="alice",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        stopped = repo.stop_by_owner_or_creator(alice.id, telegram_user_id=999)

        assert stopped is False
        assert repo.get(alice.id).status == "active"  # не изменилась
    finally:
        repo.close()


def test_stop_by_owner_or_creator_returns_false_for_unknown_id(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        assert repo.stop_by_owner_or_creator(999999, telegram_user_id=1) is False
    finally:
        repo.close()


# ---- expired subscription не считается active ----


def test_subscription_past_end_date_is_excluded_from_active_queries_even_before_expire_elapsed(tmp_path):
    """Ключевое свойство lifecycle (см. design): фильтр по end_date
    применяется НА ЧТЕНИИ, независимо от того, вызывался ли expire_elapsed()."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path, car_number="B957MA09")

    repo = FineSubscriptionRepository(db_path)
    try:
        sub = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="alice",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        # status в БД всё ещё 'active' — expire_elapsed() ни разу не вызывался.
        assert repo.get(sub.id).status == "active"

        today = date(2026, 9, 1)  # далеко после end_date
        assert repo.get_active_for_user_and_car(1, "B957MA09", today=today) is None
        assert repo.list_active_subscribers_for_car("B957MA09", today=today) == []
    finally:
        repo.close()


def test_expire_elapsed_flips_status_and_returns_count(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path, car_number="B957MA09")

    repo = FineSubscriptionRepository(db_path)
    try:
        expired_candidate = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="alice",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        still_active = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=2, telegram_chat_id=2, telegram_username="bob",
            start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
        )

        updated_count = repo.expire_elapsed(today=date(2026, 9, 1))

        assert updated_count == 1
        assert repo.get(expired_candidate.id).status == "expired"
        assert repo.get(still_active.id).status == "active"
    finally:
        repo.close()


def test_expire_elapsed_does_not_touch_already_stopped_subscriptions(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path, car_number="B957MA09")

    repo = FineSubscriptionRepository(db_path)
    try:
        sub = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="alice",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        repo.stop_by_owner_or_creator(sub.id, telegram_user_id=1)

        updated_count = repo.expire_elapsed(today=date(2026, 9, 1))

        assert updated_count == 0
        assert repo.get(sub.id).status == "stopped"  # не переписано на 'expired'
    finally:
        repo.close()


# ---- update_period ----


def test_update_period_changes_dates_without_creating_new_row(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path, car_number="B957MA09")

    repo = FineSubscriptionRepository(db_path)
    try:
        sub = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="alice",
            start_date=date(2026, 9, 1), end_date=date(2026, 10, 1),
        )

        updated = repo.update_period(sub.id, start_date=date(2026, 9, 1), end_date=date(2027, 9, 1))

        assert updated.id == sub.id
        assert updated.end_date == date(2027, 9, 1)
        assert repo.list_by_user(1) == [updated]
    finally:
        repo.close()


# ---- persistence across reopen ----


def test_data_persists_across_repository_reopen(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path, car_number="B957MA09")

    repo1 = FineSubscriptionRepository(db_path)
    sub = repo1.create(
        monitoring_task_id=task_id, car_number="B957MA09",
        telegram_user_id=1, telegram_chat_id=1, telegram_username="alice",
        start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
    )
    repo1.close()

    repo2 = FineSubscriptionRepository(db_path)
    try:
        assert repo2.get(sub.id) == sub
    finally:
        repo2.close()
