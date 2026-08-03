"""
Тесты FineMonitoringTaskRepository — SQLite-репозиторий для мониторинга
штрафов. Только сама таблица/репозиторий — без FineProvider/scheduler/Telegram.
"""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402

_CHAT_ID = -100999
_USER_ID = 111


def _make_repo(tmp_path) -> FineMonitoringTaskRepository:
    return FineMonitoringTaskRepository(tmp_path / "users.db")


def test_create_and_get_roundtrip(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task = repo.create(
            car_number="B957MA09",
            label=None,
            start_date=date(2026, 8, 3),
            end_date=date(2026, 8, 13),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )

        assert task.id is not None
        assert task.car_number == "B957MA09"
        assert task.label is None
        assert task.start_date == date(2026, 8, 3)
        assert task.end_date == date(2026, 8, 13)
        assert task.status == "active"
        assert task.telegram_chat_id == _CHAT_ID
        assert task.created_by_user_id == _USER_ID
        assert task.last_checked_at is None
        assert task.last_check_status is None
        assert task.last_error is None

        fetched = repo.get(task.id)
        assert fetched == task
    finally:
        repo.close()


def test_create_with_label(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task = repo.create(
            car_number="B957MA09",
            label="Toyota Camry",
            start_date=date(2026, 8, 3),
            end_date=date(2026, 8, 13),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )

        assert task.label == "Toyota Camry"
    finally:
        repo.close()


def test_get_returns_none_for_unknown_id(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        assert repo.get(999999) is None
    finally:
        repo.close()


def test_list_active_returns_only_active_tasks(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        active = repo.create(
            car_number="AA001AA",
            label=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )
        stopped = repo.create(
            car_number="BB002BB",
            label=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )
        repo.set_status(stopped.id, "stopped")

        active_tasks = repo.list_active()

        assert [t.id for t in active_tasks] == [active.id]
    finally:
        repo.close()


def test_get_active_by_car_number_filters_correctly(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        same_car_active = repo.create(
            car_number="AA001AA",
            label=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )
        same_car_stopped = repo.create(
            car_number="AA001AA",
            label=None,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )
        repo.set_status(same_car_stopped.id, "stopped")
        repo.create(
            car_number="BB002BB",
            label=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )

        found = repo.get_active_by_car_number("AA001AA")

        assert [t.id for t in found] == [same_car_active.id]
    finally:
        repo.close()


def test_set_status_updates_status_and_updated_at(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task = repo.create(
            car_number="AA001AA",
            label=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )

        repo.set_status(task.id, "completed")

        updated = repo.get(task.id)
        assert updated.status == "completed"
        assert updated.updated_at >= task.updated_at
    finally:
        repo.close()


def test_record_check_result_updates_fields(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task = repo.create(
            car_number="AA001AA",
            label=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )

        repo.record_check_result(task.id, last_check_status="ok", last_error=None)
        after_ok = repo.get(task.id)
        assert after_ok.last_checked_at is not None
        assert after_ok.last_check_status == "ok"
        assert after_ok.last_error is None

        repo.record_check_result(task.id, last_check_status="error", last_error="HTTP 500")
        after_error = repo.get(task.id)
        assert after_error.last_check_status == "error"
        assert after_error.last_error == "HTTP 500"
    finally:
        repo.close()


def test_count_active(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        assert repo.count_active() == 0

        first = repo.create(
            car_number="AA001AA",
            label=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )
        repo.create(
            car_number="BB002BB",
            label=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID,
            created_by_user_id=_USER_ID,
        )
        assert repo.count_active() == 2

        repo.set_status(first.id, "stopped")
        assert repo.count_active() == 1
    finally:
        repo.close()


def test_data_persists_across_repository_reopen(tmp_path):
    db_path = tmp_path / "users.db"

    repo = FineMonitoringTaskRepository(db_path)
    task = repo.create(
        car_number="AA001AA",
        label="Моя машина",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        telegram_chat_id=_CHAT_ID,
        created_by_user_id=_USER_ID,
    )
    repo.close()

    reopened = FineMonitoringTaskRepository(db_path)
    try:
        fetched = reopened.get(task.id)
        assert fetched == task
    finally:
        reopened.close()
