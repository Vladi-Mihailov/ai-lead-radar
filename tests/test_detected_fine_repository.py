"""
Тесты DetectedFineRepository — SQLite-репозиторий для мониторинга
штрафов. Только сама таблица/репозиторий — без FineProvider/scheduler/Telegram.
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import CarFineStats  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402

_CHAT_ID = -100999
_USER_ID = 111


def _make_task(tmp_path, db_path) -> int:
    """Реальная задача мониторинга — нужна из-за FOREIGN KEY на detected_fines."""
    task_repo = FineMonitoringTaskRepository(db_path)
    try:
        task = task_repo.create(
            car_number="B957MA09",
            label=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )
        return task.id
    finally:
        task_repo.close()


def test_create_and_get_by_fingerprint_roundtrip(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(tmp_path, db_path)

    repo = DetectedFineRepository(db_path)
    try:
        fine = repo.create(
            monitoring_task_id=task_id,
            car_number="B957MA09",
            external_fine_id="AB123456",
            fingerprint="deadbeef",
            penalty_date=date(2026, 8, 6),
            due_date=date(2026, 8, 20),
            delivered_status=None,
            raw_data='{"protocolNo": "AB123456"}',
        )

        assert fine.id is not None
        assert fine.monitoring_task_id == task_id
        assert fine.external_fine_id == "AB123456"
        assert fine.fingerprint == "deadbeef"
        assert fine.penalty_date == date(2026, 8, 6)
        assert fine.due_date == date(2026, 8, 20)
        assert fine.notification_sent_at is None

        found = repo.get_by_fingerprint(task_id, "deadbeef")
        assert found == fine
    finally:
        repo.close()


def test_get_by_fingerprint_returns_none_when_not_found(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(tmp_path, db_path)

    repo = DetectedFineRepository(db_path)
    try:
        assert repo.get_by_fingerprint(task_id, "does-not-exist") is None
    finally:
        repo.close()


def test_optional_fields_can_be_none(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(tmp_path, db_path)

    repo = DetectedFineRepository(db_path)
    try:
        fine = repo.create(
            monitoring_task_id=task_id,
            car_number="B957MA09",
            external_fine_id=None,
            fingerprint="fp-no-external-id",
            penalty_date=None,
            due_date=None,
            delivered_status=None,
            raw_data="{}",
        )

        assert fine.external_fine_id is None
        assert fine.penalty_date is None
        assert fine.due_date is None
        assert fine.delivered_status is None
    finally:
        repo.close()


def test_unique_constraint_prevents_duplicate_fingerprint_for_same_task(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(tmp_path, db_path)

    repo = DetectedFineRepository(db_path)
    try:
        repo.create(
            monitoring_task_id=task_id,
            car_number="B957MA09",
            external_fine_id="AB123456",
            fingerprint="dup-fp",
            penalty_date=date(2026, 8, 6),
            due_date=date(2026, 8, 20),
            delivered_status=None,
            raw_data="{}",
        )

        with pytest.raises(sqlite3.IntegrityError):
            repo.create(
                monitoring_task_id=task_id,
                car_number="B957MA09",
                external_fine_id="AB123456",
                fingerprint="dup-fp",
                penalty_date=date(2026, 8, 6),
                due_date=date(2026, 8, 20),
                delivered_status=None,
                raw_data="{}",
            )
    finally:
        repo.close()


def test_same_fingerprint_allowed_for_different_tasks(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    try:
        task_a = task_repo.create(
            car_number="AA001AA",
            label=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )
        task_b = task_repo.create(
            car_number="BB002BB",
            label=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )
    finally:
        task_repo.close()

    repo = DetectedFineRepository(db_path)
    try:
        first = repo.create(
            monitoring_task_id=task_a.id,
            car_number="AA001AA",
            external_fine_id="X1",
            fingerprint="shared-fp",
            penalty_date=None,
            due_date=None,
            delivered_status=None,
            raw_data="{}",
        )
        second = repo.create(
            monitoring_task_id=task_b.id,
            car_number="BB002BB",
            external_fine_id="X1",
            fingerprint="shared-fp",
            penalty_date=None,
            due_date=None,
            delivered_status=None,
            raw_data="{}",
        )

        assert first.id != second.id
    finally:
        repo.close()


def test_foreign_key_violation_rejected_for_unknown_task(tmp_path):
    db_path = tmp_path / "users.db"
    # Таблица fine_monitoring_tasks должна существовать для проверки FK —
    # достаточно создать/закрыть репозиторий задач, ни одной записи не нужно.
    FineMonitoringTaskRepository(db_path).close()

    repo = DetectedFineRepository(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            repo.create(
                monitoring_task_id=999999,
                car_number="AA001AA",
                external_fine_id=None,
                fingerprint="fp",
                penalty_date=None,
                due_date=None,
                delivered_status=None,
                raw_data="{}",
            )
    finally:
        repo.close()


def test_mark_seen_backfills_extended_fields_when_previously_null(tmp_path):
    """Production-инцидент: legacy detected_fines (созданные до появления
    violation_date/amount/place/violation_description) оставались с этими
    полями NULL навсегда, потому что mark_seen() их никогда не обновлял —
    см. check_service.py."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(tmp_path, db_path)

    repo = DetectedFineRepository(db_path)
    try:
        fine = repo.create(
            monitoring_task_id=task_id,
            car_number="B957MA09",
            external_fine_id="AB123456",
            fingerprint="fp-backfill",
            penalty_date=date(2026, 8, 6),
            due_date=date(2026, 8, 20),
            delivered_status="Не вручено",
            raw_data="{}",
        )
        assert fine.violation_date is None

        repo.mark_seen(
            fine.id,
            violation_date=date(2026, 8, 5),
            amount=100.0,
            place="Test place",
            violation_description="Test violation",
        )

        updated = repo.get_by_fingerprint(task_id, "fp-backfill")
        assert updated.violation_date == date(2026, 8, 5)
        assert updated.amount == 100.0
        assert updated.place == "Test place"
        assert updated.violation_description == "Test violation"
    finally:
        repo.close()


def test_mark_seen_without_extended_fields_keeps_old_behavior(tmp_path):
    """Существующие вызовы mark_seen(fine_id) без новых kwargs (например,
    старый код/тесты) не должны ничего затирать — просто обновляют
    last_seen_at, как и раньше."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(tmp_path, db_path)

    repo = DetectedFineRepository(db_path)
    try:
        fine = repo.create(
            monitoring_task_id=task_id,
            car_number="B957MA09",
            external_fine_id="AB123456",
            fingerprint="fp-no-backfill",
            penalty_date=date(2026, 8, 6),
            due_date=date(2026, 8, 20),
            delivered_status="Не вручено",
            raw_data="{}",
            violation_date=date(2026, 8, 5),
            amount=100.0,
            place="Test place",
            violation_description="Test violation",
        )

        repo.mark_seen(fine.id)

        unchanged = repo.get_by_fingerprint(task_id, "fp-no-backfill")
        assert unchanged.violation_date == date(2026, 8, 5)
        assert unchanged.amount == 100.0
        assert unchanged.place == "Test place"
        assert unchanged.violation_description == "Test violation"
    finally:
        repo.close()


def test_mark_seen_updates_last_seen_at(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(tmp_path, db_path)

    repo = DetectedFineRepository(db_path)
    try:
        fine = repo.create(
            monitoring_task_id=task_id,
            car_number="B957MA09",
            external_fine_id="AB123456",
            fingerprint="fp",
            penalty_date=None,
            due_date=None,
            delivered_status=None,
            raw_data="{}",
        )

        repo.mark_seen(fine.id)

        updated = repo.get_by_fingerprint(task_id, "fp")
        assert updated.last_seen_at >= fine.last_seen_at
    finally:
        repo.close()


def test_mark_notification_sent_updates_notification_sent_at(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(tmp_path, db_path)

    repo = DetectedFineRepository(db_path)
    try:
        fine = repo.create(
            monitoring_task_id=task_id,
            car_number="B957MA09",
            external_fine_id="AB123456",
            fingerprint="fp",
            penalty_date=None,
            due_date=None,
            delivered_status=None,
            raw_data="{}",
        )
        assert fine.notification_sent_at is None

        repo.mark_notification_sent(fine.id)

        updated = repo.get_by_fingerprint(task_id, "fp")
        assert updated.notification_sent_at is not None
    finally:
        repo.close()


def test_get_stats_by_car_groups_and_sorts_by_count_desc(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    try:
        task_a = task_repo.create(
            car_number="B957MA09",
            label=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )
        task_b = task_repo.create(
            car_number="AA001AA",
            label=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )
    finally:
        task_repo.close()

    repo = DetectedFineRepository(db_path)
    try:
        for i in range(2):
            repo.create(
                monitoring_task_id=task_a.id,
                car_number="B957MA09",
                external_fine_id=f"A{i}",
                fingerprint=f"fp-a-{i}",
                penalty_date=None,
                due_date=None,
                delivered_status=None,
                raw_data="{}",
            )
        repo.create(
            monitoring_task_id=task_b.id,
            car_number="AA001AA",
            external_fine_id="X1",
            fingerprint="fp-b-1",
            penalty_date=None,
            due_date=None,
            delivered_status=None,
            raw_data="{}",
        )

        assert repo.get_stats_by_car() == [
            CarFineStats(car_number="B957MA09", fine_count=2),
            CarFineStats(car_number="AA001AA", fine_count=1),
        ]
    finally:
        repo.close()


def test_get_stats_by_car_returns_empty_list_when_no_fines(tmp_path):
    db_path = tmp_path / "users.db"
    FineMonitoringTaskRepository(db_path).close()

    repo = DetectedFineRepository(db_path)
    try:
        assert repo.get_stats_by_car() == []
    finally:
        repo.close()


def test_data_persists_across_repository_reopen(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(tmp_path, db_path)

    repo = DetectedFineRepository(db_path)
    fine = repo.create(
        monitoring_task_id=task_id,
        car_number="B957MA09",
        external_fine_id="AB123456",
        fingerprint="fp",
        penalty_date=date(2026, 8, 6),
        due_date=date(2026, 8, 20),
        delivered_status="Не вручено",
        raw_data='{"protocolNo": "AB123456"}',
    )
    repo.close()

    reopened = DetectedFineRepository(db_path)
    try:
        fetched = reopened.get_by_fingerprint(task_id, "fp")
        assert fetched == fine
    finally:
        reopened.close()


# ---- list_by_car_number (client delivery poller, см. design report Stage 4) ----


# ---- расширенные поля (violation_date/amount/place/violation_description)
# и безопасная additive-миграция — см. задачу про новый формат уведомлений ----


def test_create_persists_extended_fields(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(tmp_path, db_path)

    repo = DetectedFineRepository(db_path)
    try:
        fine = repo.create(
            monitoring_task_id=task_id,
            car_number="B957MA09",
            external_fine_id="AB123456",
            fingerprint="fp-ext",
            penalty_date=date(2026, 8, 6),
            due_date=date(2026, 8, 20),
            delivered_status="Вручено",
            raw_data="{}",
            violation_date=date(2026, 8, 5),
            amount=100.0,
            place="Test place",
            violation_description="Test violation",
        )

        assert fine.violation_date == date(2026, 8, 5)
        assert fine.amount == 100.0
        assert fine.place == "Test place"
        assert fine.violation_description == "Test violation"

        found = repo.get_by_fingerprint(task_id, "fp-ext")
        assert found == fine
    finally:
        repo.close()


def test_extended_fields_default_to_none_when_not_provided(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(tmp_path, db_path)

    repo = DetectedFineRepository(db_path)
    try:
        fine = repo.create(
            monitoring_task_id=task_id,
            car_number="B957MA09",
            external_fine_id="AB123456",
            fingerprint="fp-no-ext",
            penalty_date=None,
            due_date=None,
            delivered_status=None,
            raw_data="{}",
        )

        assert fine.violation_date is None
        assert fine.amount is None
        assert fine.place is None
        assert fine.violation_description is None
    finally:
        repo.close()


def test_migration_adds_extended_columns_to_legacy_table_preserving_rows(tmp_path):
    """Legacy (до появления расширенного fine block) detected_fines — без
    violation_date/amount/place/violation_description вовсе. Открытие
    репозитория должно добавить эти колонки (ALTER TABLE ADD COLUMN,
    NULL по умолчанию) БЕЗ потери уже существующих строк — см. задачу
    "безопасная additive DB migration без потери существующих
    detected_fines"."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(tmp_path, db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE detected_fines (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                monitoring_task_id    INTEGER NOT NULL REFERENCES fine_monitoring_tasks(id),
                car_number            TEXT NOT NULL,
                external_fine_id      TEXT,
                fingerprint           TEXT NOT NULL,
                penalty_date          TEXT,
                due_date              TEXT,
                delivered_status      TEXT,
                raw_data              TEXT NOT NULL,
                first_detected_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                notification_sent_at  TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO detected_fines "
            "(monitoring_task_id, car_number, external_fine_id, fingerprint, "
            " penalty_date, due_date, delivered_status, raw_data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "B957MA09", "AB123456", "legacy-fp",
             "2026-08-06", "2026-08-20", "Вручено", '{"protocolNo": "AB123456"}'),
        )
        conn.commit()
    finally:
        conn.close()

    repo = DetectedFineRepository(db_path)
    try:
        legacy = repo.get_by_fingerprint(task_id, "legacy-fp")

        assert legacy is not None  # старая строка не потеряна
        assert legacy.external_fine_id == "AB123456"
        assert legacy.penalty_date == date(2026, 8, 6)
        assert legacy.delivered_status == "Вручено"
        # Новые колонки — NULL для старой строки, а не выдуманное значение.
        assert legacy.violation_date is None
        assert legacy.amount is None
        assert legacy.place is None
        assert legacy.violation_description is None

        # Репозиторий по-прежнему может создавать НОВЫЕ строки с новыми
        # полями после миграции существующей таблицы.
        fresh = repo.create(
            monitoring_task_id=task_id,
            car_number="B957MA09",
            external_fine_id="AB999999",
            fingerprint="fresh-fp",
            penalty_date=date(2026, 9, 1),
            due_date=date(2026, 9, 20),
            delivered_status="Не вручено",
            raw_data="{}",
            violation_date=date(2026, 8, 31),
            amount=50.0,
            place="Another place",
            violation_description="Another violation",
        )
        assert fresh.violation_date == date(2026, 8, 31)
        assert fresh.amount == 50.0
    finally:
        repo.close()


def test_list_by_car_number_returns_all_fines_for_that_car(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(tmp_path, db_path)

    repo = DetectedFineRepository(db_path)
    try:
        first = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            external_fine_id="AB1", fingerprint="fp-1",
            penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
            delivered_status=None, raw_data="{}",
        )
        second = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            external_fine_id="AB2", fingerprint="fp-2",
            penalty_date=date(2026, 8, 7), due_date=date(2026, 8, 21),
            delivered_status=None, raw_data="{}",
        )

        found = repo.list_by_car_number("B957MA09")

        assert {f.id for f in found} == {first.id, second.id}
        assert repo.list_by_car_number("ZZ999ZZ") == []
    finally:
        repo.close()
