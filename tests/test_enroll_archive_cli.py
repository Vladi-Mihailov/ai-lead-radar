"""
Тесты reader/fines/enroll_archive.py — разовый CLI для перевода задач
мониторинга в архивный режим. run_enrollment() — тестируемое ядро (без
load_settings()/CONFIG_PATH); parse_ids() — чистый парсер аргумента --ids.
"""

import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.enroll_archive import parse_ids, run_enrollment  # noqa: E402
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


# ---- parse_ids ----


def test_parse_ids_simple_range():
    assert parse_ids("144-1141") == list(range(144, 1142))


def test_parse_ids_comma_list():
    assert parse_ids("1,2,5-7") == [1, 2, 5, 6, 7]


def test_parse_ids_deduplicates_and_sorts():
    assert parse_ids("5,3,3,1-3") == [1, 2, 3, 5]


def test_parse_ids_ignores_whitespace():
    assert parse_ids(" 1 , 2 , 5 - 7 ") == [1, 2, 5, 6, 7]


def test_parse_ids_rejects_backwards_range():
    with pytest.raises(ValueError):
        parse_ids("10-5")


# ---- run_enrollment: dry-run vs --apply ----


def test_run_enrollment_dry_run_by_default_does_not_write(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    try:
        task_ids = [_create_completed(repo, f"AA{i:03d}AA") for i in range(3)]

        exit_code = run_enrollment(
            repo, task_ids=task_ids, all_completed=False, apply=False,
            today=_TODAY, days=3, hour=4, tz=_TBILISI,
        )

        assert exit_code == 0
        output = capsys.readouterr().out
        assert "Найдено задач: 3" in output
        assert "dry-run" in output.lower()

        for task_id in task_ids:
            assert repo.get(task_id).archive_check_enabled is False
    finally:
        repo.close()


def test_run_enrollment_with_apply_writes_to_database(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    try:
        task_ids = [_create_completed(repo, f"AA{i:03d}AA") for i in range(3)]

        exit_code = run_enrollment(
            repo, task_ids=task_ids, all_completed=False, apply=True,
            today=_TODAY, days=3, hour=4, tz=_TBILISI,
        )

        assert exit_code == 0
        output = capsys.readouterr().out
        assert "Найдено задач: 3" in output
        assert "dry-run" not in output.lower()

        for task_id in task_ids:
            task = repo.get(task_id)
            assert task.archive_check_enabled is True
            assert task.next_archive_check_at is not None
    finally:
        repo.close()


def test_run_enrollment_prints_found_count_before_any_change(tmp_path, capsys):
    """Перед изменением команда должна показать "найдено задач: N" —
    печатается до вызова enroll_tasks_in_archive_mode()."""
    repo = _make_repo(tmp_path)
    try:
        task_ids = [_create_completed(repo, f"AA{i:03d}AA") for i in range(998 % 50)]  # маленький, быстрый прогон
        run_enrollment(
            repo, task_ids=task_ids, all_completed=False, apply=True,
            today=_TODAY, days=30, hour=4, tz=_TBILISI,
        )

        output = capsys.readouterr().out
        first_line = output.splitlines()[0]
        assert first_line == f"Найдено задач: {len(task_ids)}"
    finally:
        repo.close()


def test_run_enrollment_prints_distribution_by_day_after_apply(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    try:
        task_ids = [_create_completed(repo, f"AA{i:03d}AA") for i in range(10)]

        run_enrollment(
            repo, task_ids=task_ids, all_completed=False, apply=True,
            today=_TODAY, days=5, hour=4, tz=_TBILISI,
        )

        output = capsys.readouterr().out
        assert "Распределение по дням" in output
        # 10 задач на 5 дней -> ровно по 2 в день.
        for offset in range(5):
            day = date(2026, 9, 1 + offset).isoformat()
            assert f"{day}: 2" in output
        assert "Итого: 10" in output
    finally:
        repo.close()


def test_run_enrollment_with_no_matching_ids_stops_early(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    try:
        exit_code = run_enrollment(
            repo, task_ids=[999999], all_completed=False, apply=True,
            today=_TODAY, days=30, hour=4, tz=_TBILISI,
        )

        assert exit_code == 0
        output = capsys.readouterr().out
        assert "Найдено задач: 1" in output
    finally:
        repo.close()


def test_run_enrollment_all_completed_flag_uses_repository_query(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    try:
        completed_ids = [_create_completed(repo, f"AA{i:03d}AA") for i in range(4)]
        # end_date ПОСЛЕ _TODAY — период ещё не истёк, должна остаться нетронутой.
        still_active_task = repo.create(
            car_number="ZZ999ZZ", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 10, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        run_enrollment(
            repo, task_ids=None, all_completed=True, apply=True,
            today=_TODAY, days=4, hour=4, tz=_TBILISI,
        )

        for task_id in completed_ids:
            assert repo.get(task_id).archive_check_enabled is True
        assert repo.get(still_active_task.id).archive_check_enabled is False
        assert repo.get(still_active_task.id).status == "active"
    finally:
        repo.close()


def test_run_enrollment_all_completed_flag_also_picks_up_active_overdue_tasks(tmp_path, capsys):
    """Production-сценарий: партия задач массово добавлена как
    status='active' с уже прошедшим end_date (id 144..1141) —
    --all-completed обязан их найти и перевести, а не только
    status='completed' задачи."""
    repo = _make_repo(tmp_path)
    try:
        active_overdue = repo.create(
            car_number="AA001AA", label=None,
            start_date=date(2026, 7, 1), end_date=date(2026, 8, 8),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

        exit_code = run_enrollment(
            repo, task_ids=None, all_completed=True, apply=True,
            today=_TODAY, days=30, hour=4, tz=_TBILISI,
        )

        assert exit_code == 0
        updated = repo.get(active_overdue.id)
        assert updated.status == "completed"
        assert updated.archive_check_enabled is True
        assert repo.list_active() == []
    finally:
        repo.close()


def test_run_enrollment_998_ids_end_to_end_matches_expected_distribution(tmp_path, capsys):
    """Полный прогон в масштабе production-партии (998 задач/30 дней) через
    CLI-ядро, а не напрямую через enroll_tasks_in_archive_mode()."""
    repo = _make_repo(tmp_path)
    try:
        task_ids = [_create_completed(repo, f"AA{i:04d}AA") for i in range(998)]

        exit_code = run_enrollment(
            repo, task_ids=task_ids, all_completed=False, apply=True,
            today=_TODAY, days=30, hour=4, tz=_TBILISI,
        )

        assert exit_code == 0

        counts_by_day: dict = {}
        for task_id in task_ids:
            due_at = repo.get(task_id).next_archive_check_at
            day = due_at.astimezone(_TBILISI).date()
            counts_by_day[day] = counts_by_day.get(day, 0) + 1

        assert sum(counts_by_day.values()) == 998
        assert len(counts_by_day) == 30
        sizes = sorted(counts_by_day.values(), reverse=True)
        assert sizes[:8] == [34] * 8
        assert sizes[8:] == [33] * 22
    finally:
        repo.close()


def test_run_enrollment_998_active_overdue_tasks_matches_real_production_shape(tmp_path, capsys):
    """Ровно форма реального production-инцидента: 998 задач массово
    вставлены со status='active' (не 'completed'!) и end_date вручную
    проставлен в прошлом (2026-08-08) — как батч id 144..1141. CLI должен
    и включить архивный режим, и сам довершить переход в 'completed', так
    что ни одна из них не останется в list_active()."""
    repo = _make_repo(tmp_path)
    try:
        task_ids = []
        for i in range(998):
            task = repo.create(
                car_number=f"BB{i:04d}BB", label=None,
                start_date=date(2026, 7, 1), end_date=date(2026, 8, 8),
                telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
            )
            assert task.status == "active"
            task_ids.append(task.id)

        exit_code = run_enrollment(
            repo, task_ids=task_ids, all_completed=False, apply=True,
            today=_TODAY, days=30, hour=4, tz=_TBILISI,
        )

        assert exit_code == 0
        output = capsys.readouterr().out
        assert "Переведено в архивный режим: 998" in output
        assert "Дополнительно переведено" in output and "998" in output

        assert repo.list_active() == []
        for task_id in task_ids:
            task = repo.get(task_id)
            assert task.status == "completed"
            assert task.archive_check_enabled is True
            assert task.next_archive_check_at is not None
    finally:
        repo.close()
