"""
Тесты scripts/migrate_georgia_unlimited_monitoring.py — перевод УЖЕ
активных мониторингов Грузии на бессрочный (задача "убрать выбор срока
мониторинга"). Настоящие репозитории (SQLite/tmp_path) создают схему и
строки; скрипт работает с той же БД напрямую.
"""

import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.models import UNLIMITED_END_DATE  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from scripts import migrate_georgia_unlimited_monitoring as migration  # noqa: E402

_TODAY = date(2026, 11, 17)
_FUTURE = date(2026, 12, 17)
_PAST = date(2026, 11, 1)


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


def _seed(db) -> dict[str, int]:
    """Все интересные варианты разом: что станет бессрочным, что — нет."""
    ids = {}
    ids["task_active"] = db.task("AA001AA", end_date=_FUTURE)
    ids["sub_active"] = db.subscription(ids["task_active"], "AA001AA", user_id=1, end_date=_FUTURE)
    ids["sub_pending"] = db.pending_claim(ids["task_active"], "AA001AA", end_date=_FUTURE)
    ids["sub_stopped"] = db.subscription(ids["task_active"], "AA001AA", user_id=2, end_date=_FUTURE, status="stopped")
    ids["task_operator"] = db.task("BB002BB", end_date=_FUTURE, scope="operator")
    ids["task_past_active"] = db.task("CC003CC", end_date=_PAST)
    ids["sub_expired"] = db.subscription(ids["task_past_active"], "CC003CC", user_id=3, end_date=_PAST, status="expired")
    ids["sub_archived"] = db.subscription(ids["task_past_active"], "CC003CC", user_id=4, end_date=_FUTURE, status="archived")
    ids["sub_pending_past"] = db.pending_claim(ids["task_past_active"], "CC003CC", end_date=_PAST)
    ids["task_completed"] = db.task("DD004DD", end_date=_PAST, status="completed")
    ids["task_stopped"] = db.task("EE005EE", end_date=_FUTURE, status="stopped")
    return ids


def test_plan_counts_only_currently_active_rows(db):
    _seed(db)
    with sqlite3.connect(f"file:{db.path}?mode=ro", uri=True) as conn:
        plan = migration.build_term_migration_plan(conn, today=_TODAY)

    assert plan.subscriptions_to_convert == {"active": 1, "pending_claim": 1}
    assert plan.tasks_to_convert == {"client_bot": 1, "operator": 1}
    assert plan.total_to_convert == 4
    assert plan.subscriptions_untouched == {
        "archived": 1, "expired (срок прошёл)": 1, "pending_claim (срок прошёл)": 1, "stopped": 1,
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

    assert changed == {"fine_monitoring_subscriptions": 2, "fine_monitoring_tasks": 2}
    unlimited = UNLIMITED_END_DATE.isoformat()
    for key in ("sub_active", "sub_pending"):
        assert db.end_date("fine_monitoring_subscriptions", ids[key]) == unlimited
    for key in ("task_active", "task_operator"):
        assert db.end_date("fine_monitoring_tasks", ids[key]) == unlimited
    # Ничего, кроме активного прямо сейчас, не трогается.
    for key, expected in (("sub_stopped", _FUTURE), ("sub_expired", _PAST), ("sub_archived", _FUTURE),
                          ("sub_pending_past", _PAST)):
        assert db.end_date("fine_monitoring_subscriptions", ids[key]) == expected.isoformat()
    for key, expected in (("task_past_active", _PAST), ("task_completed", _PAST), ("task_stopped", _FUTURE)):
        assert db.end_date("fine_monitoring_tasks", ids[key]) == expected.isoformat()
    assert db.backup_rows() == sorted([
        ("fine_monitoring_subscriptions", ids["sub_active"], _FUTURE.isoformat()),
        ("fine_monitoring_subscriptions", ids["sub_pending"], _FUTURE.isoformat()),
        ("fine_monitoring_tasks", ids["task_active"], _FUTURE.isoformat()),
        ("fine_monitoring_tasks", ids["task_operator"], _FUTURE.isoformat()),
    ])


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
    assert db.subscriptions.expire_elapsed(today=long_after) == 0
    deliverable = {s.id for s in db.subscriptions.list_all_deliverable(today=long_after)}
    assert ids["sub_active"] in deliverable
    assert ids["sub_expired"] not in deliverable
    assert ids["sub_stopped"] not in deliverable


def test_apply_is_idempotent(db):
    _seed(db)
    conn = db.connect()
    try:
        migration.apply_term_migration(conn, today=_TODAY)
        backup_after_first = db.backup_rows()
        second = migration.apply_term_migration(conn, today=_TODAY)
    finally:
        conn.close()

    assert second == {"fine_monitoring_subscriptions": 0, "fine_monitoring_tasks": 0}
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

        restored = migration.rollback_term_migration(conn)
    finally:
        conn.close()

    assert restored == {"fine_monitoring_subscriptions": 2, "fine_monitoring_tasks": 1}
    assert db.end_date("fine_monitoring_subscriptions", ids["sub_active"]) == _FUTURE.isoformat()
    assert db.end_date("fine_monitoring_subscriptions", ids["sub_pending"]) == _FUTURE.isoformat()
    assert db.end_date("fine_monitoring_tasks", ids["task_active"]) == _FUTURE.isoformat()
    assert db.end_date("fine_monitoring_tasks", ids["task_operator"]) == "2026-12-31"
    assert db.end_date("fine_monitoring_tasks", new_task) == UNLIMITED_END_DATE.isoformat()
    # Восстановленные строки ушли из backup.
    assert db.backup_rows() == []


def test_main_dry_run_writes_nothing(db, capsys):
    ids = _seed(db)

    assert migration.main(["--db-path", str(db.path)]) == 0

    output = capsys.readouterr().out
    assert "Dry-run" in output
    assert db.end_date("fine_monitoring_subscriptions", ids["sub_active"]) == _FUTURE.isoformat()
    with sqlite3.connect(db.path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert migration.BACKUP_TABLE not in tables


def test_main_execute_then_rollback(db, capsys):
    ids = _seed(db)

    assert migration.main(["--db-path", str(db.path), "--execute"]) == 0
    assert db.end_date("fine_monitoring_subscriptions", ids["sub_active"]) == UNLIMITED_END_DATE.isoformat()

    assert migration.main(["--db-path", str(db.path), "--rollback"]) == 0
    assert db.end_date("fine_monitoring_subscriptions", ids["sub_active"]) == _FUTURE.isoformat()
    output = capsys.readouterr().out
    assert "AA001AA" not in output


def test_main_reports_missing_database(tmp_path, capsys):
    assert migration.main(["--db-path", str(tmp_path / "missing.db")]) == 1
