#!/usr/bin/env python
"""Переход Грузии на бессрочный мониторинг штрафов для УЖЕ существующих
записей (см. задачу "убрать выбор срока мониторинга"). Новый код создаёт
задачи/подписки сразу бессрочными (end_date = UNLIMITED_END_DATE, см.
reader/fines/models.py) — этот скрипт делает бессрочными ТОЛЬКО те записи,
которые активны прямо сейчас:

- fine_monitoring_subscriptions: status IN ('active', 'pending_claim') И
  end_date >= today (ровно то, что сейчас доставляется, см.
  FineSubscriptionRepository.list_all_deliverable) И её задача активна
  (подписку на задачу, которую никто не проверяет, бессрочной не делаем —
  клиент навсегда видел бы "активен" без единой проверки);
- fine_monitoring_tasks: status = 'active' И (end_date >= today ИЛИ это
  задача такой подписки — у бессрочной подписки задача тоже бессрочная).

НЕ трогает (без отдельного согласования ничего массово не включается):
stopped/expired/archived подписки, pending_claim с уже прошедшим сроком,
подписки на неактивные задачи, completed/stopped задачи. Активные задачи с
уже прошедшим end_date (их и сейчас проверяет коллектор, но клиентам по ним
ничего не доставляется) по умолчанию тоже не трогаются — только с явным
--include-past-active-tasks.

Исторические сроки не теряются: перед заменой старый end_date каждой
изменённой строки сохраняется в отдельную таблицу ge_monitoring_term_backup
(table_name, row_id, old_end_date, migrated_at); исходные таблицы получают
только новое значение end_date (схема и остальные колонки, включая
updated_at, не меняются). Идемпотентно: повторный --execute ничего не
меняет (уже бессрочные строки не подходят под условие). --rollback
возвращает сохранённые даты строкам, которые всё ещё бессрочные. Ни одного
обращения к police.ge, Telegram или сети — только локальная БД.

По умолчанию — dry-run: ТОЛЬКО читает БД (mode=ro), печатает план, НИЧЕГО
не пишет (и для --rollback тоже — реально пишет только с --execute). Номера
машин и Telegram-идентификаторы не печатаются — только количества.

Использование:
    python -m scripts.migrate_georgia_unlimited_monitoring                        # dry-run (default)
    python -m scripts.migrate_georgia_unlimited_monitoring --execute              # реально пишет
    python -m scripts.migrate_georgia_unlimited_monitoring --rollback             # что вернёт откат
    python -m scripts.migrate_georgia_unlimited_monitoring --rollback --execute   # вернуть старые сроки
    python -m scripts.migrate_georgia_unlimited_monitoring --include-past-active-tasks  # + активные задачи
                                                                                  #   с прошедшим сроком
    python -m scripts.migrate_georgia_unlimited_monitoring --db-path X.db         # другой файл (тесты)

Порядок на production (тот же день по Тбилиси): backup data/users.db →
код нового релиза на диске → dry-run → --execute → перезапуск
@GEShtrafbot → ещё раз --execute (подхватит то, что старый код успел
создать или продлить со сроком между шагами; повтор безопасен) → dry-run
должен показать "итого строк: 0". Пока бот не перезапущен, работает
старый код — бессрочные строки он просто никогда не считает истёкшими.
Запускать от того же пользователя, что и бот (sudo -u leadradar): БД в
WAL-режиме, и -wal/-shm, созданные root, бот потом не сможет открыть.
Backup файла скрипт САМ не делает."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.models import UNLIMITED_END_DATE  # noqa: E402
from reader.settings import FineMonitorSettings, load_settings  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

BACKUP_TABLE = "ge_monitoring_term_backup"
_TASKS = "fine_monitoring_tasks"
_SUBSCRIPTIONS = "fine_monitoring_subscriptions"

_CREATE_BACKUP_TABLE = f"""
CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
    table_name    TEXT NOT NULL,
    row_id        INTEGER NOT NULL,
    old_end_date  TEXT NOT NULL,
    migrated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (table_name, row_id)
)
"""

_ACTIVE_TASK_IDS = f"SELECT id FROM {_TASKS} WHERE status = 'active'"


def _eligible_where(*, include_past_active_tasks: bool) -> dict[str, str]:
    """Условия "активна прямо сейчас" — те же, что у выборок, которые
    решают, проверяется ли машина и доставляются ли уведомления (см.
    модульный докстрок). end_date < :unlimited — уже бессрочные строки не
    трогаем. Задача подписки, которая становится бессрочной, тоже
    становится бессрочной, даже если её собственный срок уже прошёл
    (иначе FineJob, если он снова будет включён, завершил бы задачу, а
    клиент видел бы "активен"). Подзапрос задач смотрит на ещё не
    изменённые подписки — поэтому apply_term_migration() меняет задачи
    первыми."""
    subscriptions = (
        "status IN ('active', 'pending_claim') AND end_date >= :today AND end_date < :unlimited "
        f"AND monitoring_task_id IN ({_ACTIVE_TASK_IDS})"
    )
    task_term = "" if include_past_active_tasks else (
        f" AND (end_date >= :today OR id IN (SELECT monitoring_task_id FROM {_SUBSCRIPTIONS} WHERE {subscriptions}))"
    )
    return {
        _SUBSCRIPTIONS: subscriptions,
        _TASKS: f"status = 'active' AND end_date < :unlimited{task_term}",
    }


@dataclass(frozen=True)
class TermMigrationPlan:
    """Только количества — никаких номеров/идентификаторов (см. модульный
    докстрок)."""

    subscriptions_to_convert: dict[str, int]
    tasks_to_convert: dict[str, int]
    subscriptions_untouched: dict[str, int]
    tasks_untouched: dict[str, int]
    already_unlimited_subscriptions: int
    already_unlimited_tasks: int

    @property
    def total_to_convert(self) -> int:
        return sum(self.subscriptions_to_convert.values()) + sum(self.tasks_to_convert.values())

    def format_report(self) -> str:
        lines = ["Станут бессрочными:"]
        lines += _format_counts("подписки", self.subscriptions_to_convert)
        lines += _format_counts("задачи", self.tasks_to_convert)
        lines.append(f"  итого строк: {self.total_to_convert}")
        lines.append("Не трогаем (статус и даты остаются как есть):")
        lines += _format_counts("подписки", self.subscriptions_untouched)
        lines += _format_counts("задачи", self.tasks_untouched)
        lines.append(
            f"Уже бессрочные: подписки {self.already_unlimited_subscriptions}, "
            f"задачи {self.already_unlimited_tasks}"
        )
        return "\n".join(lines)


def _format_counts(title: str, counts: dict[str, int]) -> list[str]:
    if not counts:
        return [f"  {title}: 0"]
    return [f"  {title} {key}: {value}" for key, value in sorted(counts.items())]


def _params(today: date) -> dict[str, str]:
    return {"today": today.isoformat(), "unlimited": UNLIMITED_END_DATE.isoformat()}


def build_term_migration_plan(
    conn: sqlite3.Connection, *, today: date, include_past_active_tasks: bool = False,
) -> TermMigrationPlan:
    params = _params(today)
    where = _eligible_where(include_past_active_tasks=include_past_active_tasks)
    subscriptions_to_convert = dict(conn.execute(
        f"SELECT status, COUNT(*) FROM {_SUBSCRIPTIONS} WHERE {where[_SUBSCRIPTIONS]} GROUP BY status",
        params,
    ).fetchall())
    tasks_to_convert = dict(conn.execute(
        f"SELECT monitoring_scope || CASE WHEN end_date < :today THEN ' (срок прошёл)' ELSE '' END, COUNT(*) "
        f"FROM {_TASKS} WHERE {where[_TASKS]} GROUP BY 1",
        params,
    ).fetchall())
    subscriptions_untouched = dict(conn.execute(
        "SELECT status || CASE WHEN end_date < :today THEN ' (срок прошёл)' "
        f"WHEN monitoring_task_id NOT IN ({_ACTIVE_TASK_IDS}) THEN ' (задача не проверяется)' ELSE '' END, "
        f"COUNT(*) FROM {_SUBSCRIPTIONS} WHERE end_date < :unlimited AND NOT ({where[_SUBSCRIPTIONS]}) "
        "GROUP BY 1",
        params,
    ).fetchall())
    tasks_untouched = dict(conn.execute(
        f"SELECT monitoring_scope || ' ' || status "
        "|| CASE WHEN end_date < :today THEN ' (срок прошёл)' ELSE '' END, COUNT(*) "
        f"FROM {_TASKS} WHERE end_date < :unlimited AND NOT ({where[_TASKS]}) GROUP BY 1",
        params,
    ).fetchall())
    already_unlimited_subscriptions = conn.execute(
        f"SELECT COUNT(*) FROM {_SUBSCRIPTIONS} WHERE end_date >= :unlimited", params,
    ).fetchone()[0]
    already_unlimited_tasks = conn.execute(
        f"SELECT COUNT(*) FROM {_TASKS} WHERE end_date >= :unlimited", params,
    ).fetchone()[0]
    return TermMigrationPlan(
        subscriptions_to_convert=subscriptions_to_convert,
        tasks_to_convert=tasks_to_convert,
        subscriptions_untouched=subscriptions_untouched,
        tasks_untouched=tasks_untouched,
        already_unlimited_subscriptions=already_unlimited_subscriptions,
        already_unlimited_tasks=already_unlimited_tasks,
    )


def apply_term_migration(
    conn: sqlite3.Connection, *, today: date, include_past_active_tasks: bool = False,
) -> dict[str, int]:
    """Одна транзакция (BEGIN IMMEDIATE — бот пишет в ту же БД
    параллельно): сначала старые end_date в BACKUP_TABLE, затем замена на
    UNLIMITED_END_DATE ровно для тех же строк. Возвращает число изменённых
    строк по таблицам."""
    params = _params(today)
    where = _eligible_where(include_past_active_tasks=include_past_active_tasks)
    changed: dict[str, int] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_CREATE_BACKUP_TABLE)
        for table in (_TASKS, _SUBSCRIPTIONS):
            conn.execute(
                f"INSERT OR IGNORE INTO {BACKUP_TABLE} (table_name, row_id, old_end_date) "
                f"SELECT '{table}', id, end_date FROM {table} WHERE {where[table]}",
                params,
            )
            cursor = conn.execute(f"UPDATE {table} SET end_date = :unlimited WHERE {where[table]}", params)
            changed[table] = cursor.rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return changed


_RESTORABLE_WHERE = (
    "end_date >= :unlimited AND id IN (SELECT row_id FROM {backup} WHERE table_name = '{table}')"
)


def _backup_table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (BACKUP_TABLE,),
    ).fetchone() is not None


def build_rollback_plan(conn: sqlite3.Connection) -> dict[str, int]:
    """Сколько строк вернёт --rollback --execute: сохранённые в
    BACKUP_TABLE и всё ещё бессрочные. Работает и на read-only соединении
    (таблицы нет — откатывать нечего)."""
    if not _backup_table_exists(conn):
        return {_SUBSCRIPTIONS: 0, _TASKS: 0}
    params = {"unlimited": UNLIMITED_END_DATE.isoformat()}
    return {
        table: conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {_RESTORABLE_WHERE.format(backup=BACKUP_TABLE, table=table)}",
            params,
        ).fetchone()[0]
        for table in (_SUBSCRIPTIONS, _TASKS)
    }


def rollback_term_migration(conn: sqlite3.Connection) -> dict[str, int]:
    """Возвращает сохранённый end_date строкам, которые всё ещё бессрочные
    (если строку с тех пор кто-то перевёл на другую дату — её не трогаем),
    и удаляет восстановленные записи из BACKUP_TABLE. Строки, ставшие
    бессрочными уже после перехода (новые добавления), остаются бессрочными.
    Возвращает число восстановленных строк по таблицам."""
    params = {"unlimited": UNLIMITED_END_DATE.isoformat()}
    restored: dict[str, int] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_CREATE_BACKUP_TABLE)
        for table in (_SUBSCRIPTIONS, _TASKS):
            cursor = conn.execute(
                f"UPDATE {table} SET end_date = ("
                f"    SELECT b.old_end_date FROM {BACKUP_TABLE} b "
                f"    WHERE b.table_name = '{table}' AND b.row_id = {table}.id"
                f") WHERE {_RESTORABLE_WHERE.format(backup=BACKUP_TABLE, table=table)}",
                params,
            )
            restored[table] = cursor.rowcount
            conn.execute(
                f"DELETE FROM {BACKUP_TABLE} WHERE table_name = '{table}' AND row_id IN ("
                f"    SELECT id FROM {table} WHERE end_date < :unlimited"
                ")",
                params,
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return restored


def _resolve_db_path_and_today(explicit_db_path: str | None) -> tuple[Path, date]:
    """"Сегодня" — в часовом поясе мониторинга (тот же, что у бота и
    ClientDeliveryService); с явным --db-path config не читается, берётся
    часовой пояс мониторинга по умолчанию."""
    if explicit_db_path is not None:
        db_path = Path(explicit_db_path)
        tz_name = FineMonitorSettings.model_fields["timezone"].default
    else:
        settings = load_settings(CONFIG_PATH)
        db_path = settings.app.users_db_file
        tz_name = settings.fine_monitor.timezone
    return db_path, datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).date()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="Реально записать изменения в БД (default: dry-run, ничего не пишет).",
    )
    parser.add_argument(
        "--rollback", action="store_true",
        help="Вернуть сроки, сохранённые предыдущим --execute (пишет только вместе с --execute).",
    )
    parser.add_argument(
        "--include-past-active-tasks", action="store_true",
        help="Сделать бессрочными и активные задачи с уже прошедшим end_date (по умолчанию не трогаются).",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Путь к БД (default: settings.app.users_db_file из config/config.yaml).",
    )
    args = parser.parse_args(argv)
    if args.rollback and args.include_past_active_tasks:
        parser.error("--include-past-active-tasks не сочетается с --rollback")
    return args


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _connect_for_write(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(db_path, timeout=30, isolation_level=None)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path, today = _resolve_db_path_and_today(args.db_path)

    if not db_path.exists():
        print(f"БД не найдена: {db_path}", file=sys.stderr)
        return 1

    if args.rollback:
        ro_conn = _connect_read_only(db_path)
        try:
            restorable = build_rollback_plan(ro_conn)
        finally:
            ro_conn.close()
        print(f"Откат вернёт старые сроки: подписки {restorable[_SUBSCRIPTIONS]}, задачи {restorable[_TASKS]}.")
        if not args.execute:
            print("\nDry-run — ничего не записано. Запустите с --rollback --execute, чтобы откатить.")
            return 0
        conn = _connect_for_write(db_path)
        try:
            restored = rollback_term_migration(conn)
        finally:
            conn.close()
        print(f"Сроки восстановлены: подписки {restored[_SUBSCRIPTIONS]}, задачи {restored[_TASKS]}.")
        return 0

    ro_conn = _connect_read_only(db_path)
    try:
        plan = build_term_migration_plan(
            ro_conn, today=today, include_past_active_tasks=args.include_past_active_tasks,
        )
    finally:
        ro_conn.close()

    print(f"Сегодня (по часовому поясу мониторинга): {today.isoformat()}")
    print(plan.format_report())

    if not args.execute:
        print("\nDry-run — ничего не записано. Запустите с --execute, чтобы применить.")
        return 0

    print("\n--execute: применяю...")
    conn = _connect_for_write(db_path)
    try:
        changed = apply_term_migration(
            conn, today=today, include_past_active_tasks=args.include_past_active_tasks,
        )
    finally:
        conn.close()
    print(f"Готово: бессрочными стали подписки {changed[_SUBSCRIPTIONS]}, задачи {changed[_TASKS]}.")
    print(f"Старые сроки сохранены в таблице {BACKUP_TABLE}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
