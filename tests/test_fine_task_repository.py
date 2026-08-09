"""
Тесты FineMonitoringTaskRepository — SQLite-репозиторий для мониторинга
штрафов. Только сама таблица/репозиторий — без FineProvider/scheduler/Telegram.
"""

import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402

_CHAT_ID = -100999
_USER_ID = 111

# Схема ДО появления архивного режима (archive_check_enabled/
# next_archive_check_at) — как на реальном сервере до этой миграции.
_LEGACY_SCHEMA = """
CREATE TABLE fine_monitoring_tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    car_number          TEXT NOT NULL,
    label               TEXT,
    start_date          TEXT NOT NULL,
    end_date            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    telegram_chat_id    INTEGER NOT NULL,
    created_by_user_id  INTEGER NOT NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_checked_at     TIMESTAMP,
    last_check_status   TEXT,
    last_error          TEXT
)
"""


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


# ---- миграция: архивные колонки (archive_check_enabled/next_archive_check_at) ----


def test_migration_adds_archive_columns_to_legacy_database(tmp_path):
    db_path = tmp_path / "users.db"

    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(_LEGACY_SCHEMA)
    legacy_conn.execute(
        "INSERT INTO fine_monitoring_tasks "
        "(car_number, label, start_date, end_date, status, telegram_chat_id, created_by_user_id) "
        "VALUES ('AA001AA', NULL, '2026-01-01', '2026-01-31', 'completed', -100999, 111)"
    )
    legacy_conn.commit()
    legacy_conn.close()

    # FineMonitoringTaskRepository должен открыть эту базу без удаления/
    # пересоздания и добавить недостающие колонки автоматически.
    repo = FineMonitoringTaskRepository(db_path)
    try:
        columns = {
            row[1] for row in repo._conn.execute("PRAGMA table_info(fine_monitoring_tasks)")
        }
        assert "archive_check_enabled" in columns
        assert "next_archive_check_at" in columns

        [task] = repo.get_many([1])
        assert task.car_number == "AA001AA"
        assert task.status == "completed"
        assert task.start_date == date(2026, 1, 1)
        assert task.end_date == date(2026, 1, 31)

        # Существующие записи после миграции сохраняют текущее поведение
        # бит в бит — архивный режим НЕ включается сам по себе миграцией.
        assert task.archive_check_enabled is False
        assert task.next_archive_check_at is None

        # Уже проверенное поведение (list_active/count_active) не должно
        # измениться из-за появления новых колонок.
        assert repo.list_active() == []
        assert repo.count_active() == 0
        assert repo.list_due_for_archive_check(datetime.now(timezone.utc), limit=100) == []
    finally:
        repo.close()


def test_migration_preserves_existing_active_task_behavior(tmp_path):
    """Легаси-задача со status='active' после миграции продолжает
    попадать в list_active()/count_active(), как и раньше."""
    db_path = tmp_path / "users.db"

    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(_LEGACY_SCHEMA)
    legacy_conn.execute(
        "INSERT INTO fine_monitoring_tasks "
        "(car_number, label, start_date, end_date, status, telegram_chat_id, created_by_user_id) "
        "VALUES ('BB002BB', NULL, '2026-08-01', '2026-08-31', 'active', -100999, 111)"
    )
    legacy_conn.commit()
    legacy_conn.close()

    repo = FineMonitoringTaskRepository(db_path)
    try:
        assert repo.count_active() == 1
        [task] = repo.list_active()
        assert task.car_number == "BB002BB"
        assert task.archive_check_enabled is False
    finally:
        repo.close()


def test_migration_is_idempotent_on_already_migrated_database(tmp_path):
    db_path = tmp_path / "users.db"

    first_open = FineMonitoringTaskRepository(db_path)
    first_open.create(
        car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
    )
    first_open.close()

    second_open = FineMonitoringTaskRepository(db_path)
    try:
        assert len(second_open.list_active()) == 1
    finally:
        second_open.close()


def test_new_task_created_after_migration_defaults_archive_fields(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task = repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        assert task.archive_check_enabled is False
        assert task.next_archive_check_at is None
    finally:
        repo.close()


# ---- get_many / list_completed_not_archived ----


def test_get_many_returns_only_matching_ids_in_any_order_input(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        first = repo.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        second = repo.create(
            car_number="BB002BB", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.create(
            car_number="CC003CC", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        found = repo.get_many([second.id, first.id, 999999])

        assert {t.id for t in found} == {first.id, second.id}
    finally:
        repo.close()


def test_get_many_with_empty_list_returns_empty(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        assert repo.get_many([]) == []
    finally:
        repo.close()


def test_list_completed_not_archived_excludes_active_stopped_and_already_archived(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        active = repo.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        stopped = repo.create(
            car_number="BB002BB", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.set_status(stopped.id, "stopped")
        completed_candidate = repo.create(
            car_number="CC003CC", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.set_status(completed_candidate.id, "completed")
        already_archived = repo.create(
            car_number="DD004DD", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.set_status(already_archived.id, "completed")
        repo.enroll_in_archive_mode({already_archived.id: datetime.now(timezone.utc)})

        candidates = repo.list_completed_not_archived()

        assert [t.id for t in candidates] == [completed_candidate.id]
        assert active.id not in [t.id for t in candidates]
    finally:
        repo.close()


def test_list_archive_enrollment_candidates_includes_active_with_past_end_date(tmp_path):
    """Production-сценарий: задача массово создана как status='active' с
    уже прошедшим end_date (партия id 144..1141) — list_completed_not_archived()
    её не увидела бы вовсе, list_archive_enrollment_candidates() обязана."""
    repo = _make_repo(tmp_path)
    try:
        today = date(2026, 9, 1)

        active_overdue = repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 7, 1), end_date=date(2026, 8, 8),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        still_active = repo.create(
            car_number="BB002BB", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 9, 30),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        active_ending_today = repo.create(
            car_number="EE005EE", label=None,
            start_date=date(2026, 8, 1), end_date=today,
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        stopped = repo.create(
            car_number="CC003CC", label=None,
            start_date=date(2026, 7, 1), end_date=date(2026, 8, 8),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.set_status(stopped.id, "stopped")
        completed = repo.create(
            car_number="DD004DD", label=None,
            start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.set_status(completed.id, "completed")
        already_archived = repo.create(
            car_number="FF006FF", label=None,
            start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.set_status(already_archived.id, "completed")
        repo.enroll_in_archive_mode({already_archived.id: datetime.now(timezone.utc)})

        candidates = {t.id for t in repo.list_archive_enrollment_candidates(today)}

        assert candidates == {active_overdue.id, completed.id}
        assert still_active.id not in candidates  # end_date ещё не прошёл
        assert active_ending_today.id not in candidates  # end_date == today, ещё не "< today"
        assert stopped.id not in candidates  # явно остановлена оператором
        assert already_archived.id not in candidates  # уже в архивном режиме
    finally:
        repo.close()


# ---- list_due_for_archive_check ----


def test_list_due_for_archive_check_filters_by_due_time_and_flag(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)

        due = repo.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.set_status(due.id, "completed")
        repo.enroll_in_archive_mode({due.id: now})

        not_yet_due = repo.create(
            car_number="BB002BB", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.set_status(not_yet_due.id, "completed")
        repo.enroll_in_archive_mode({not_yet_due.id: now + timedelta(days=1)})

        not_archived = repo.create(
            car_number="CC003CC", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.set_status(not_archived.id, "completed")

        result = repo.list_due_for_archive_check(now, limit=100)

        assert [t.id for t in result] == [due.id]
    finally:
        repo.close()


def test_list_due_for_archive_check_orders_by_due_time_then_id(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)
        earlier = now - timedelta(days=2)
        same_time = now - timedelta(days=1)

        # Создаём в порядке, который НЕ совпадает с ожидаемым порядком
        # выборки — иначе тест не отличил бы правильную сортировку от
        # случайного порядка вставки/rowid.
        task_c = repo.create(
            car_number="CC003CC", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        task_a = repo.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        task_b = repo.create(
            car_number="BB002BB", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        for task in (task_c, task_a, task_b):
            repo.set_status(task.id, "completed")

        # task_b — самый ранний due time. task_c и task_a делят одно и то же
        # due time (same_time) — при равенстве сортировка идёт по id ASC;
        # task_c создан первым (меньший id), значит должен идти раньше task_a.
        repo.enroll_in_archive_mode(
            {task_b.id: earlier, task_c.id: same_time, task_a.id: same_time}
        )
        assert task_c.id < task_a.id  # проверка допущения теста, а не поведения репозитория

        result = repo.list_due_for_archive_check(now, limit=100)

        assert [t.id for t in result] == [task_b.id, task_c.id, task_a.id]
    finally:
        repo.close()


def test_list_due_for_archive_check_respects_limit(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        now = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)
        ids = []
        for i in range(5):
            task = repo.create(
                car_number=f"AA00{i}AA", label=None,
                start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
                telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
            )
            repo.set_status(task.id, "completed")
            ids.append(task.id)
        repo.enroll_in_archive_mode({task_id: now for task_id in ids})

        result = repo.list_due_for_archive_check(now, limit=2)

        assert len(result) == 2
        # limit не выбирает случайные 2 — это первые 2 по (due, id) ASC.
        assert [t.id for t in result] == sorted(ids)[:2]
    finally:
        repo.close()


# ---- enroll_in_archive_mode / schedule_first_archive_check ----


def test_enroll_in_archive_mode_sets_flag_and_due_time(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task = repo.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.set_status(task.id, "completed")

        due_at = datetime(2026, 9, 5, 4, 0, tzinfo=timezone.utc)
        repo.enroll_in_archive_mode({task.id: due_at})

        updated = repo.get(task.id)
        assert updated.archive_check_enabled is True
        assert updated.next_archive_check_at == due_at
        # status не трогается этим методом — остаётся completed.
        assert updated.status == "completed"
    finally:
        repo.close()


def test_enroll_in_archive_mode_with_empty_schedule_does_nothing(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        repo.enroll_in_archive_mode({})  # не должно бросать исключение
    finally:
        repo.close()


def test_schedule_first_archive_check_sets_single_task(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task = repo.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.set_status(task.id, "completed")

        due_at = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
        repo.schedule_first_archive_check(task.id, next_check_at=due_at)

        updated = repo.get(task.id)
        assert updated.archive_check_enabled is True
        assert updated.next_archive_check_at == due_at
    finally:
        repo.close()


# ---- reschedule_next_archive_check / return_to_active_monitoring ----


def test_reschedule_next_archive_check_updates_due_time_only(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task = repo.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.set_status(task.id, "completed")
        first_due = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)
        repo.enroll_in_archive_mode({task.id: first_due})

        second_due = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)
        repo.reschedule_next_archive_check(task.id, next_check_at=second_due)

        updated = repo.get(task.id)
        assert updated.next_archive_check_at == second_due
        assert updated.archive_check_enabled is True
        assert updated.status == "completed"
    finally:
        repo.close()


def test_return_to_active_monitoring_resets_task_to_frequent_mode(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task = repo.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.set_status(task.id, "completed")
        repo.enroll_in_archive_mode({task.id: datetime.now(timezone.utc)})

        new_start = date(2026, 9, 10)
        new_end = date(2026, 10, 10)
        repo.return_to_active_monitoring(task.id, start_date=new_start, end_date=new_end)

        updated = repo.get(task.id)
        assert updated.status == "active"
        assert updated.start_date == new_start
        assert updated.end_date == new_end
        assert updated.archive_check_enabled is False
        assert updated.next_archive_check_at is None

        # Снова попадает в обычный частый мониторинг.
        assert [t.id for t in repo.list_active()] == [task.id]
    finally:
        repo.close()
