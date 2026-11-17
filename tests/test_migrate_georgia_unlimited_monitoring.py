"""
Тесты scripts/migrate_georgia_unlimited_monitoring.py — перевод УЖЕ
активных мониторингов Грузии на бессрочный (задача "убрать выбор срока
мониторинга"). Настоящие репозитории (SQLite/tmp_path) создают схему и
строки; скрипт работает с той же БД напрямую.
"""

import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.models import UNLIMITED_END_DATE  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from scripts import migrate_georgia_unlimited_monitoring as migration  # noqa: E402

_TODAY = date(2026, 11, 17)
_SUBS = "fine_monitoring_subscriptions"
_TASKS = "fine_monitoring_tasks"


class _Db:
    def __init__(self, tmp_path):
        self.path = tmp_path / "users.db"
        self.tasks = FineMonitoringTaskRepository(self.path)
        self.subscriptions = FineSubscriptionRepository(self.path)

    def task(self, car_number, *, end_date, status="active", scope="client_bot"):
        task = self.tasks.create(
            car_number=car_number, label=None, start_date=date(2026, 10, 1), end_date=end_date,
            telegram_chat_id=1, created_by_user_id=1, monitoring_scope=scope,
        )
        if status != "active":
            self.tasks.set_status(task.id, status)
        return task.id

    def subscription(self, task_id, car_number, *, user_id, end_date, status="active"):
        subscription = self.subscriptions.create(
            monitoring_task_id=task_id, car_number=car_number, telegram_user_id=user_id,
            telegram_chat_id=user_id, telegram_username=None, start_date=date(2026, 10, 1), end_date=end_date,
        )
        if status != "active":
            self.subscriptions._conn.execute(
                "UPDATE fine_monitoring_subscriptions SET status = ? WHERE id = ?", (status, subscription.id),
            )
            self.subscriptions._conn.commit()
        return subscription.id

    def pending_claim(self, task_id, car_number, *, end_date):
        return self.subscriptions.create_pending_claim(
            monitoring_task_id=task_id, car_number=car_number, owner_username_hint=f"owner_{task_id}",
            created_by_telegram_user_id=99, created_by_telegram_chat_id=99,
            start_date=date(2026, 10, 1), end_date=end_date,
            claim_token=f"token-{task_id}", claim_token_expires_at=datetime(2026, 11, 24, tzinfo=timezone.utc),
        ).id

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, isolation_level=None)

    def connect_read_only(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)

    def end_date(self, table, row_id) -> str:
        with sqlite3.connect(self.path) as conn:
            return conn.execute(f"SELECT end_date FROM {table} WHERE id = ?", (row_id,)).fetchone()[0]

    def backup_rows(self) -> list[tuple]:
        with sqlite3.connect(self.path) as conn:
            return conn.execute(
                f"SELECT table_name, row_id, old_end_date FROM {migration.BACKUP_TABLE} ORDER BY table_name, row_id",
            ).fetchall()

    def close(self):
        self.tasks.close()
        self.subscriptions.close()


@pytest.fixture
def db(tmp_path):
    database = _Db(tmp_path)
    yield database
    database.close()


def _seed(db, *, today=_TODAY) -> dict[str, object]:
    """Все интересные варианты разом: что станет бессрочным, что — нет."""
    future, past = today + timedelta(days=30), today - timedelta(days=16)
    ids = {"future": future, "past": past}
    ids["task_active"] = db.task("AA001AA", end_date=future)
    ids["sub_active"] = db.subscription(ids["task_active"], "AA001AA", user_id=1, end_date=future)
    ids["sub_pending"] = db.pending_claim(ids["task_active"], "AA001AA", end_date=future)
    ids["sub_stopped"] = db.subscription(ids["task_active"], "AA001AA", user_id=2, end_date=future, status="stopped")
    ids["task_operator"] = db.task("BB002BB", end_date=future, scope="operator")
    ids["task_past_active"] = db.task("CC003CC", end_date=past)
    ids["sub_expired"] = db.subscription(ids["task_past_active"], "CC003CC", user_id=3, end_date=past, status="expired")
    ids["sub_archived"] = db.subscription(ids["task_past_active"], "CC003CC", user_id=4, end_date=future, status="archived")
    ids["sub_pending_past"] = db.pending_claim(ids["task_past_active"], "CC003CC", end_date=past)
    ids["task_completed"] = db.task("DD004DD", end_date=past, status="completed")
    ids["task_stopped"] = db.task("EE005EE", end_date=future, status="stopped")
    # Подписка "активна", но её задачу никто не проверяет (например,
    # оператор остановил задачу) — бессрочной её делать нельзя.
    ids["sub_on_stopped_task"] = db.subscription(ids["task_stopped"], "EE005EE", user_id=5, end_date=future)
    # Активная задача с уже прошедшим сроком, но с действующей подпиской —
    # у бессрочной подписки задача тоже должна стать бессрочной.
    ids["task_past_with_sub"] = db.task("GG007GG", end_date=past)
    ids["sub_on_past_task"] = db.subscription(ids["task_past_with_sub"], "GG007GG", user_id=6, end_date=future)
    return ids


def test_plan_counts_only_currently_active_rows(db):
    _seed(db)
    with db.connect_read_only() as conn:
        plan = migration.build_term_migration_plan(conn, today=_TODAY)

    assert plan.subscriptions_to_convert == {"active": 2, "pending_claim": 1}
    assert plan.tasks_to_convert == {"client_bot": 1, "client_bot (срок прошёл)": 1, "operator": 1}
    assert plan.total_to_convert == 6
    assert plan.subscriptions_untouched == {
        "active (задача не проверяется)": 1, "archived": 1, "expired (срок прошёл)": 1,
        "pending_claim (срок прошёл)": 1, "stopped": 1,
    }
    assert plan.tasks_untouched == {
        "client_bot active (срок прошёл)": 1, "client_bot completed (срок прошёл)": 1, "client_bot stopped": 1,
    }
    report = plan.format_report()
    for plate in ("AA001AA", "BB002BB", "CC003CC"):
        assert plate not in report  # только количества


def test_apply_makes_active_rows_unlimited_and_backs_up_old_dates(db):
    ids = _seed(db)
    conn = db.connect()
    try:
        changed = migration.apply_term_migration(conn, today=_TODAY)
    finally:
        conn.close()

    assert changed == {_SUBS: 3, _TASKS: 3}
    unlimited = UNLIMITED_END_DATE.isoformat()
    for key in ("sub_active", "sub_pending", "sub_on_past_task"):
        assert db.end_date(_SUBS, ids[key]) == unlimited
    for key in ("task_active", "task_operator", "task_past_with_sub"):
        assert db.end_date(_TASKS, ids[key]) == unlimited
    # Ничего, кроме активного прямо сейчас, не трогается.
    future, past = ids["future"].isoformat(), ids["past"].isoformat()
    for key, expected in (("sub_stopped", future), ("sub_expired", past), ("sub_archived", future),
                          ("sub_pending_past", past), ("sub_on_stopped_task", future)):
        assert db.end_date(_SUBS, ids[key]) == expected
    for key, expected in (("task_past_active", past), ("task_completed", past), ("task_stopped", future)):
        assert db.end_date(_TASKS, ids[key]) == expected
    assert db.backup_rows() == sorted([
        (_SUBS, ids["sub_active"], future),
        (_SUBS, ids["sub_pending"], future),
        (_SUBS, ids["sub_on_past_task"], future),
        (_TASKS, ids["task_active"], future),
        (_TASKS, ids["task_operator"], future),
        (_TASKS, ids["task_past_with_sub"], past),
    ])


def test_include_past_active_tasks_converts_them_too(db):
    """Активные задачи с уже прошедшим end_date (их и сейчас проверяет
    коллектор) — только по явному флагу; статус и подписки не меняются."""
    ids = _seed(db)
    with db.connect_read_only() as conn:
        plan = migration.build_term_migration_plan(conn, today=_TODAY, include_past_active_tasks=True)
    assert plan.tasks_to_convert == {"client_bot": 1, "client_bot (срок прошёл)": 2, "operator": 1}

    conn = db.connect()
    try:
        changed = migration.apply_term_migration(conn, today=_TODAY, include_past_active_tasks=True)
    finally:
        conn.close()

    assert changed == {_SUBS: 3, _TASKS: 4}
    assert db.end_date(_TASKS, ids["task_past_active"]) == UNLIMITED_END_DATE.isoformat()
    assert db.end_date(_SUBS, ids["sub_expired"]) == ids["past"].isoformat()
    assert db.subscriptions.get(ids["sub_expired"]).status == "expired"
    assert db.end_date(_TASKS, ids["task_completed"]) == ids["past"].isoformat()


def test_apply_does_not_change_statuses(db):
    ids = _seed(db)
    before = {s.id: s.status for s in db.subscriptions.list_by_car_number("AA001AA")}
    conn = db.connect()
    try:
        migration.apply_term_migration(conn, today=_TODAY)
    finally:
        conn.close()

    assert {s.id: s.status for s in db.subscriptions.list_by_car_number("AA001AA")} == before
    assert db.tasks.get(ids["task_completed"]).status == "completed"
    assert db.tasks.get(ids["task_stopped"]).status == "stopped"


def test_migrated_subscription_stays_deliverable_after_old_end_date(db):
    ids = _seed(db)
    conn = db.connect()
    try:
        migration.apply_term_migration(conn, today=_TODAY)
    finally:
        conn.close()

    long_after = date(2030, 1, 1)
    deliverable = {s.id for s in db.subscriptions.list_all_deliverable(today=long_after)}
    assert ids["sub_active"] in deliverable
    assert ids["sub_expired"] not in deliverable
    assert ids["sub_stopped"] not in deliverable
    # Неперенесённая подписка на остановленную задачу истекает по-старому.
    assert ids["sub_on_stopped_task"] not in deliverable
    assert db.subscriptions.expire_elapsed(today=long_after) == 1
    assert db.subscriptions.get(ids["sub_active"]).status == "active"


def test_rerun_fixes_task_moved_back_to_a_term_under_an_unlimited_subscription(db):
    """Процесс операторского чата со старым кодом ("fine add" до своего
    перезапуска) может перевести бессрочную задачу клиента на срок; когда
    этот срок пройдёт, повторный запуск всё равно вернёт задаче бессрочность
    — у неё есть действующая (уже бессрочная) подписка."""
    ids = _seed(db)
    conn = db.connect()
    try:
        migration.apply_term_migration(conn, today=_TODAY)
        db.tasks.reset_period(ids["task_active"], start_date=date(2026, 10, 1), end_date=date(2026, 11, 1))

        changed = migration.apply_term_migration(conn, today=_TODAY)
    finally:
        conn.close()

    assert changed == {_SUBS: 0, _TASKS: 1}
    assert db.end_date(_TASKS, ids["task_active"]) == UNLIMITED_END_DATE.isoformat()


def test_apply_is_idempotent(db):
    _seed(db)
    conn = db.connect()
    try:
        migration.apply_term_migration(conn, today=_TODAY)
        backup_after_first = db.backup_rows()
        second = migration.apply_term_migration(conn, today=_TODAY)
    finally:
        conn.close()

    assert second == {_SUBS: 0, _TASKS: 0}
    assert db.backup_rows() == backup_after_first


def test_rollback_restores_old_dates_only_for_migrated_rows(db):
    ids = _seed(db)
    conn = db.connect()
    try:
        migration.apply_term_migration(conn, today=_TODAY)
        # Новая бессрочная задача после миграции (новый код) — без backup.
        new_task = db.task("FF006FF", end_date=UNLIMITED_END_DATE)
        # Задачу, ставшую бессрочной, с тех пор перевели на другую дату
        # (например, операторский "fine add") — её откат не трогает.
        db.tasks.reset_period(ids["task_operator"], start_date=_TODAY, end_date=date(2026, 12, 31))

        assert migration.build_rollback_plan(conn) == {_SUBS: 3, _TASKS: 2}
        restored = migration.rollback_term_migration(conn)
    finally:
        conn.close()

    future = ids["future"].isoformat()
    assert restored == {_SUBS: 3, _TASKS: 2}
    assert db.end_date(_TASKS, ids["task_past_with_sub"]) == ids["past"].isoformat()
    assert db.end_date(_SUBS, ids["sub_active"]) == future
    assert db.end_date(_SUBS, ids["sub_pending"]) == future
    assert db.end_date(_TASKS, ids["task_active"]) == future
    assert db.end_date(_TASKS, ids["task_operator"]) == "2026-12-31"
    assert db.end_date(_TASKS, new_task) == UNLIMITED_END_DATE.isoformat()
    # Восстановленные строки ушли из backup.
    assert db.backup_rows() == []


def test_rollback_plan_before_any_migration_is_empty_and_read_only(db):
    _seed(db)
    with db.connect_read_only() as conn:
        assert migration.build_rollback_plan(conn) == {_SUBS: 0, _TASKS: 0}


def _real_today() -> date:
    """main() берёт "сегодня" из часов (часовой пояс мониторинга)."""
    return datetime.now(ZoneInfo("Asia/Tbilisi")).date()


def test_main_dry_run_writes_nothing(db, capsys):
    ids = _seed(db, today=_real_today())

    assert migration.main(["--db-path", str(db.path)]) == 0

    output = capsys.readouterr().out
    assert "Dry-run" in output
    assert db.end_date(_SUBS, ids["sub_active"]) == ids["future"].isoformat()
    with sqlite3.connect(db.path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert migration.BACKUP_TABLE not in tables


def test_main_execute_then_rollback_dry_run_then_rollback(db, capsys):
    ids = _seed(db, today=_real_today())

    assert migration.main(["--db-path", str(db.path), "--execute"]) == 0
    assert db.end_date(_SUBS, ids["sub_active"]) == UNLIMITED_END_DATE.isoformat()

    # --rollback без --execute ничего не пишет.
    assert migration.main(["--db-path", str(db.path), "--rollback"]) == 0
    assert db.end_date(_SUBS, ids["sub_active"]) == UNLIMITED_END_DATE.isoformat()

    assert migration.main(["--db-path", str(db.path), "--rollback", "--execute"]) == 0
    assert db.end_date(_SUBS, ids["sub_active"]) == ids["future"].isoformat()
    output = capsys.readouterr().out
    assert "Откат вернёт старые сроки: подписки 3, задачи 3." in output
    assert "AA001AA" not in output


def test_main_rejects_rollback_with_include_flag(db):
    with pytest.raises(SystemExit):
        migration.main(["--db-path", str(db.path), "--rollback", "--include-past-active-tasks"])


def test_main_reports_missing_database(tmp_path, capsys):
    assert migration.main(["--db-path", str(tmp_path / "missing.db")]) == 1
