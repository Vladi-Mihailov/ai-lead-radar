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
