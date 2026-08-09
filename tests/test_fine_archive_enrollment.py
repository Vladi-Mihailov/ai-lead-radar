"""
Тесты reader/fines/archive_enrollment.py — разовый (не автоматический)
перевод задач мониторинга в архивный режим. FineMonitoringTaskRepository —
настоящий (SQLite, tmp_path), без FineProvider/Telegram — это чистая
работа с репозиторием.
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.archive_enrollment import enroll_tasks_in_archive_mode  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402

_CHAT_ID = -100999
_USER_ID = 111
_TBILISI = ZoneInfo("Asia/Tbilisi")
_TODAY = date(2026, 9, 1)


def _make_repo(tmp_path) -> FineMonitoringTaskRepository:
    return FineMonitoringTaskRepository(tmp_path / "users.db")


def _create_completed(repo, car_number: str) -> int:
    task = repo.create(
        car_number=car_number, label=None,
        start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
    )
    repo.set_status(task.id, "completed")
    return task.id


def test_enroll_marks_completed_tasks_and_distributes_schedule(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task_ids = [_create_completed(repo, f"AA{i:03d}AA") for i in range(5)]

        result = enroll_tasks_in_archive_mode(
            repo, task_ids, today=_TODAY, days=5, hour=4, tz=_TBILISI,
        )

        assert sorted(result.enrolled_task_ids) == sorted(task_ids)
        assert result.skipped_not_completed == []
        assert result.skipped_already_archived == []
        assert result.not_found_task_ids == []
        assert set(result.schedule) == set(task_ids)

        for task_id in task_ids:
            task = repo.get(task_id)
            assert task.archive_check_enabled is True
            assert task.next_archive_check_at is not None
    finally:
        repo.close()


def test_enroll_skips_still_active_and_stopped_tasks(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        # Всё ещё активная задача — end_date ПОСЛЕ _TODAY, период ещё не
        # истёк (в отличие от теста про active+просроченный end_date ниже).
        still_active_task = repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 9, 30),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        stopped_task = repo.create(
            car_number="BB002BB", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        repo.set_status(stopped_task.id, "stopped")

        result = enroll_tasks_in_archive_mode(
            repo, [still_active_task.id, stopped_task.id], today=_TODAY, days=30, hour=4, tz=_TBILISI,
        )

        assert result.enrolled_task_ids == []
        assert result.transitioned_to_completed == []
        assert sorted(result.skipped_not_completed) == sorted([still_active_task.id, stopped_task.id])

        assert repo.get(still_active_task.id).status == "active"
        assert repo.get(still_active_task.id).archive_check_enabled is False
        assert repo.get(stopped_task.id).status == "stopped"
        assert repo.get(stopped_task.id).archive_check_enabled is False
    finally:
        repo.close()


def test_enroll_transitions_active_task_with_past_end_date_to_completed(tmp_path):
    """Production-инцидент: партия задач массово добавлена как
    status='active', end_date проставлен вручную в прошлом (см. батч id
    144..1141). Enrollment обязан не только включить archive_check_enabled,
    но и сам довершить переход active -> completed — иначе задача осталась
    бы одновременно и в list_active() (обычные 3 проверки в день), и в
    архивной выборке."""
    repo = _make_repo(tmp_path)
    try:
        task = repo.create(
            car_number="AA001AA", label=None,
            # end_date строго раньше _TODAY (2026-09-01) — период истёк.
            start_date=date(2026, 7, 1), end_date=date(2026, 8, 8),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        assert task.status == "active"  # как при массовом создании — по умолчанию

        result = enroll_tasks_in_archive_mode(
            repo, [task.id], today=_TODAY, days=30, hour=4, tz=_TBILISI,
        )

        assert result.enrolled_task_ids == [task.id]
        assert result.transitioned_to_completed == [task.id]
        assert result.skipped_not_completed == []

        updated = repo.get(task.id)
        assert updated.status == "completed"
        assert updated.end_date == date(2026, 8, 8)  # end_date сам по себе не меняется
        assert updated.archive_check_enabled is True
        assert updated.next_archive_check_at is not None

        # Гарантированно больше не попадает в обычный FineJob.
        assert repo.list_active() == []

        # И присутствует в архивной выборке в назначенную дату.
        due = repo.list_due_for_archive_check(updated.next_archive_check_at, limit=100)
        assert [t.id for t in due] == [task.id]
    finally:
        repo.close()


def test_enroll_active_with_past_end_date_dry_run_does_not_transition(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task = repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 7, 1), end_date=date(2026, 8, 8),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        result = enroll_tasks_in_archive_mode(
            repo, [task.id], today=_TODAY, days=30, hour=4, tz=_TBILISI, dry_run=True,
        )

        assert result.enrolled_task_ids == [task.id]
        assert result.transitioned_to_completed == [task.id]

        # dry_run=True — ничего не записано, включая переход в completed.
        unchanged = repo.get(task.id)
        assert unchanged.status == "active"
        assert unchanged.archive_check_enabled is False
        assert unchanged.next_archive_check_at is None
    finally:
        repo.close()


def test_enroll_active_with_past_end_date_alongside_completed_tasks(tmp_path):
    """Смешанный список — не влияет на обычные уже-completed задачи."""
    repo = _make_repo(tmp_path)
    try:
        already_completed = _create_completed(repo, "AA001AA")
        active_overdue = repo.create(
            car_number="BB002BB", label=None,
            start_date=date(2026, 7, 1), end_date=date(2026, 8, 8),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        ).id

        result = enroll_tasks_in_archive_mode(
            repo, [already_completed, active_overdue], today=_TODAY, days=30, hour=4, tz=_TBILISI,
        )

        assert sorted(result.enrolled_task_ids) == sorted([already_completed, active_overdue])
        assert result.transitioned_to_completed == [active_overdue]

        assert repo.get(already_completed).status == "completed"
        assert repo.get(active_overdue).status == "completed"
        assert repo.get(already_completed).archive_check_enabled is True
        assert repo.get(active_overdue).archive_check_enabled is True
    finally:
        repo.close()


def test_enroll_skips_unknown_ids_and_reports_them(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        result = enroll_tasks_in_archive_mode(
            repo, [999999], today=_TODAY, days=30, hour=4, tz=_TBILISI,
        )

        assert result.enrolled_task_ids == []
        assert result.not_found_task_ids == [999999]
    finally:
        repo.close()


def test_repeated_enrollment_is_idempotent_and_does_not_move_schedule(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task_ids = [_create_completed(repo, f"AA{i:03d}AA") for i in range(3)]

        first = enroll_tasks_in_archive_mode(
            repo, task_ids, today=_TODAY, days=3, hour=4, tz=_TBILISI,
        )
        assert sorted(first.enrolled_task_ids) == sorted(task_ids)
        due_times_after_first = {task_id: repo.get(task_id).next_archive_check_at for task_id in task_ids}

        # Повторный запуск позже, с тем же (пересекающимся) списком id —
        # уже enrolled задачи не должны переехать на новое расписание.
        second = enroll_tasks_in_archive_mode(
            repo, task_ids, today=_TODAY + timedelta(days=10), days=3, hour=4, tz=_TBILISI,
        )

        assert second.enrolled_task_ids == []
        assert sorted(second.skipped_already_archived) == sorted(task_ids)

        due_times_after_second = {task_id: repo.get(task_id).next_archive_check_at for task_id in task_ids}
        assert due_times_after_second == due_times_after_first
    finally:
        repo.close()


def test_dry_run_does_not_write_to_database(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task_ids = [_create_completed(repo, f"AA{i:03d}AA") for i in range(3)]

        result = enroll_tasks_in_archive_mode(
            repo, task_ids, today=_TODAY, days=3, hour=4, tz=_TBILISI, dry_run=True,
        )

        # Превью показывает то же самое, что и реальный прогон, но ничего
        # не пишет.
        assert sorted(result.enrolled_task_ids) == sorted(task_ids)
        assert set(result.schedule) == set(task_ids)

        for task_id in task_ids:
            task = repo.get(task_id)
            assert task.archive_check_enabled is False
            assert task.next_archive_check_at is None
    finally:
        repo.close()


def test_dry_run_preview_matches_subsequent_real_apply(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task_ids = [_create_completed(repo, f"AA{i:03d}AA") for i in range(10)]

        preview = enroll_tasks_in_archive_mode(
            repo, task_ids, today=_TODAY, days=10, hour=4, tz=_TBILISI, dry_run=True,
        )
        applied = enroll_tasks_in_archive_mode(
            repo, task_ids, today=_TODAY, days=10, hour=4, tz=_TBILISI, dry_run=False,
        )

        assert preview.schedule == applied.schedule
    finally:
        repo.close()


def test_enroll_only_touches_requested_ids_not_other_completed_tasks(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        in_range = _create_completed(repo, "AA001AA")
        out_of_range = _create_completed(repo, "BB002BB")

        result = enroll_tasks_in_archive_mode(
            repo, [in_range], today=_TODAY, days=30, hour=4, tz=_TBILISI,
        )

        assert result.enrolled_task_ids == [in_range]
        assert repo.get(in_range).archive_check_enabled is True
        assert repo.get(out_of_range).archive_check_enabled is False
    finally:
        repo.close()


def test_enroll_998_tasks_matches_production_scale_example(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        task_ids = [_create_completed(repo, f"AA{i:04d}AA") for i in range(998)]

        result = enroll_tasks_in_archive_mode(
            repo, task_ids, today=_TODAY, days=30, hour=4, tz=_TBILISI,
        )

        assert len(result.enrolled_task_ids) == 998

        counts_by_day: dict = {}
        for task_id in result.enrolled_task_ids:
            due_at = repo.get(task_id).next_archive_check_at
            day = due_at.astimezone(_TBILISI).date()
            counts_by_day[day] = counts_by_day.get(day, 0) + 1

        assert len(counts_by_day) == 30
        assert sum(counts_by_day.values()) == 998
        assert max(counts_by_day.values()) - min(counts_by_day.values()) <= 1
        sizes = sorted(counts_by_day.values(), reverse=True)
        assert sizes[:8] == [34] * 8
        assert sizes[8:] == [33] * 22
    finally:
        repo.close()
