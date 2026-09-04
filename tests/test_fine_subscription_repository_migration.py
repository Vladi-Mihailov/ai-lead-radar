"""
Тесты безопасной миграции fine_monitoring_subscriptions:
telegram_user_id/telegram_chat_id NOT NULL (Stage 1) -> nullable (trusted-
operator delegated flow, см. design report). SQLite не поддерживает
"ALTER TABLE ... ALTER COLUMN ... DROP NOT NULL" — FineSubscriptionRepository
делает это через официально документированную SQLite "12-step" процедуру
пересоздания таблицы (см. reader/public_bot/subscription_repository.py::
_migrate_nullable_owner_columns_if_needed). Эти тесты — единственная
проверка того, что данные/id/индексы/constraints переживают эту миграцию
без потерь, включая существующие FOREIGN KEY-ссылки из client_fine_deliveries.
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository  # noqa: E402
from reader.public_bot.subscription_repository import (  # noqa: E402
    DuplicateActiveSubscriptionError,
    FineSubscriptionRepository,
)

_CHAT_ID = -100999
_USER_ID = 111

# Точная схема Stage 1 (telegram_user_id/telegram_chat_id NOT NULL, без
# owner_username_hint/created_by_*/claim_token/claim_token_expires_at) —
# то, что реально лежит на сервере (production ни разу не писал в эту
# таблицу, но открывал её этим кодом, см. Stage 3 report).
_LEGACY_SCHEMA = """
CREATE TABLE fine_monitoring_subscriptions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    monitoring_task_id  INTEGER NOT NULL REFERENCES fine_monitoring_tasks(id),
    car_number          TEXT NOT NULL,
    telegram_user_id    INTEGER NOT NULL,
    telegram_chat_id    INTEGER NOT NULL,
    telegram_username   TEXT,
    status              TEXT NOT NULL DEFAULT 'active',
    start_date          TEXT NOT NULL,
    end_date            TEXT NOT NULL,
    source              TEXT NOT NULL DEFAULT 'geshtrafbot',
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    stopped_at          TIMESTAMP
)
"""

_LEGACY_UNIQUE_ACTIVE_INDEX = """
CREATE UNIQUE INDEX idx_fine_subscriptions_active_user_task
    ON fine_monitoring_subscriptions (monitoring_task_id, telegram_user_id)
    WHERE status = 'active'
"""


def _make_task(db_path, car_number="B957MA09") -> int:
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


def _seed_legacy_db(db_path, task_id: int) -> dict:
    """Создаёт legacy-таблицу вручную (в обход FineSubscriptionRepository —
    он уже пишет в новом виде) и заполняет её так, как если бы это была
    реальная production-база: активная подписка, остановленная подписка,
    и (детская проверка FK) обнаруженный штраф + доставка, ссылающаяся на
    активную подписку — обе должны пережить миграцию с тем же id."""
    conn = sqlite3.connect(db_path)
    conn.execute(_LEGACY_SCHEMA)
    conn.execute(_LEGACY_UNIQUE_ACTIVE_INDEX)
    conn.execute(
        "INSERT INTO fine_monitoring_subscriptions "
        "(id, monitoring_task_id, car_number, telegram_user_id, telegram_chat_id, "
        " telegram_username, status, start_date, end_date, source) "
        "VALUES (1, ?, 'B957MA09', 42, 42, 'alice', 'active', '2026-09-01', '2026-12-01', 'geshtrafbot')",
        (task_id,),
    )
    conn.execute(
        "INSERT INTO fine_monitoring_subscriptions "
        "(id, monitoring_task_id, car_number, telegram_user_id, telegram_chat_id, "
        " telegram_username, status, start_date, end_date, source, stopped_at) "
        "VALUES (2, ?, 'B957MA09', 43, 43, 'bob', 'stopped', '2026-08-01', '2026-08-31', "
        " 'geshtrafbot', '2026-08-15 00:00:00')",
        (task_id,),
    )
    conn.commit()
    conn.close()
    return {"active_id": 1, "stopped_id": 2}


def _make_detected_fine(db_path, task_id) -> int:
    from reader.fines.detected_fine_repository import DetectedFineRepository

    fine_repo = DetectedFineRepository(db_path)
    try:
        fine = fine_repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            external_fine_id="AB123456", fingerprint="fp-1",
            penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
            delivered_status="Не вручено", raw_data='{"protocolNo": "AB123456"}',
        )
        return fine.id
    finally:
        fine_repo.close()


# ---- данные переживают миграцию без потерь ----


def test_migration_preserves_existing_rows_and_ids(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    ids = _seed_legacy_db(db_path, task_id)

    repo = FineSubscriptionRepository(db_path)
    try:
        active = repo.get(ids["active_id"])
        stopped = repo.get(ids["stopped_id"])

        assert active is not None
        assert active.id == ids["active_id"]  # id сохранён
        assert active.telegram_user_id == 42
        assert active.telegram_chat_id == 42
        assert active.telegram_username == "alice"
        assert active.status == "active"
        assert active.start_date == date(2026, 9, 1)
        assert active.end_date == date(2026, 12, 1)
        # Новые поля — NULL для легаси-строк, ничего не подставлено задним числом.
        assert active.owner_username_hint is None
        assert active.created_by_telegram_user_id is None
        assert active.claim_token is None

        assert stopped is not None
        assert stopped.id == ids["stopped_id"]
        assert stopped.status == "stopped"
        assert stopped.stopped_at is not None
    finally:
        repo.close()


def test_migration_adds_nullable_owner_columns(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    _seed_legacy_db(db_path, task_id)

    repo = FineSubscriptionRepository(db_path)
    try:
        columns = {
            row[1]: row for row in repo._conn.execute(
                "PRAGMA table_info(fine_monitoring_subscriptions)"
            )
        }
        assert "owner_username_hint" in columns
        assert "created_by_telegram_user_id" in columns
        assert "created_by_telegram_chat_id" in columns
        assert "claim_token" in columns
        assert "claim_token_expires_at" in columns

        # notnull flag (PRAGMA table_info index 3) — 0 означает "может быть NULL".
        assert columns["telegram_user_id"][3] == 0
        assert columns["telegram_chat_id"][3] == 0
    finally:
        repo.close()


def test_migration_preserves_foreign_key_referenced_by_client_fine_deliveries(tmp_path):
    """Существующая (до миграции) ссылка client_fine_deliveries.
    subscription_id -> fine_monitoring_subscriptions.id должна остаться
    валидной ПОСЛЕ пересоздания таблицы — id не должны "поплыть"."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    ids = _seed_legacy_db(db_path, task_id)
    fine_id = _make_detected_fine(db_path, task_id)

    delivery_repo = ClientFineDeliveryRepository(db_path)
    delivery_repo.record_attempt(fine_id, ids["active_id"])
    delivery_repo.close()

    # Открытие FineSubscriptionRepository ПОСЛЕ того, как delivery уже
    # ссылается на старый id — именно так это и произошло бы в реальности
    # (миграция применяется к уже существующей, "живой" базе).
    sub_repo = FineSubscriptionRepository(db_path)
    try:
        assert sub_repo.get(ids["active_id"]) is not None
    finally:
        sub_repo.close()

    delivery_repo2 = ClientFineDeliveryRepository(db_path)
    try:
        delivery = delivery_repo2.get(fine_id, ids["active_id"])
        assert delivery is not None
        assert delivery.attempt_count == 1
    finally:
        delivery_repo2.close()


# ---- индексы/constraints работают как прежде после миграции ----


def test_unique_active_index_still_enforced_after_migration(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    ids = _seed_legacy_db(db_path, task_id)

    repo = FineSubscriptionRepository(db_path)
    try:
        # user_id=42 уже активен на этой задаче (ids["active_id"]) — новая
        # попытка создать ЕЩЁ ОДНУ активную подписку того же пользователя
        # на ту же задачу должна по-прежнему быть отклонена.
        with pytest.raises(DuplicateActiveSubscriptionError):
            repo.create(
                monitoring_task_id=task_id, car_number="B957MA09",
                telegram_user_id=42, telegram_chat_id=42, telegram_username="alice",
                start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            )
    finally:
        repo.close()


def test_autoincrement_continues_without_collision_after_migration(tmp_path):
    """Явный перенос id при копировании строк НЕ продвигает
    sqlite_sequence сам по себе — без явного восстановления следующий
    автоинкрементный INSERT рискует получить id, который уже занят
    легаси-строкой. Проверяем, что новый id строго больше максимального
    существовавшего."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    ids = _seed_legacy_db(db_path, task_id)
    max_legacy_id = max(ids.values())

    repo = FineSubscriptionRepository(db_path)
    try:
        new_sub = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=999, telegram_chat_id=999, telegram_username="newcomer",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        assert new_sub.id > max_legacy_id
        # Никакая другая строка не была случайно перезаписана/задета.
        assert repo.get(ids["active_id"]).telegram_username == "alice"
        assert repo.get(ids["stopped_id"]).telegram_username == "bob"
    finally:
        repo.close()


# ---- идемпотентность ----


def test_migration_is_idempotent_on_reopen(tmp_path):
    """Повторное открытие УЖЕ мигрированной базы не должно ни падать, ни
    повторно пересоздавать таблицу/терять данные."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    ids = _seed_legacy_db(db_path, task_id)

    first_open = FineSubscriptionRepository(db_path)
    first_open.close()

    second_open = FineSubscriptionRepository(db_path)
    try:
        assert second_open.get(ids["active_id"]).telegram_username == "alice"
        assert second_open.get(ids["stopped_id"]).telegram_username == "bob"

        columns = {
            row[1] for row in second_open._conn.execute(
                "PRAGMA table_info(fine_monitoring_subscriptions)"
            )
        }
        assert "owner_username_hint" in columns
    finally:
        second_open.close()


def test_fresh_database_does_not_trigger_migration_and_has_new_schema_directly(tmp_path):
    """База, которая никогда не видела legacy-схему (обычный случай для
    локальной разработки/тестов) — CREATE TABLE IF NOT EXISTS сразу создаёт
    её в новом (nullable) виде, миграция — no-op."""
    db_path = tmp_path / "users.db"

    repo = FineSubscriptionRepository(db_path)
    try:
        columns = {
            row[1]: row for row in repo._conn.execute(
                "PRAGMA table_info(fine_monitoring_subscriptions)"
            )
        }
        assert columns["telegram_user_id"][3] == 0
        assert "claim_token" in columns
    finally:
        repo.close()
