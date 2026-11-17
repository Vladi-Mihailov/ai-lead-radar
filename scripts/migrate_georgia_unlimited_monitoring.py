#!/usr/bin/env python
"""Переход Грузии на бессрочный мониторинг штрафов для УЖЕ существующих
записей (см. задачу "убрать выбор срока мониторинга"). Новый код создаёт
задачи/подписки сразу бессрочными (end_date = UNLIMITED_END_DATE, см.
reader/fines/models.py) — этот скрипт делает бессрочными ТОЛЬКО те записи,
которые активны прямо сейчас:

- fine_monitoring_subscriptions: status IN ('active', 'pending_claim') И
  end_date >= today (ровно то, что сейчас доставляется, см.
  FineSubscriptionRepository.list_all_deliverable);
- fine_monitoring_tasks: status = 'active' И end_date >= today.

НЕ трогает (без отдельного согласования ничего массово не включается):
stopped/expired/archived подписки, pending_claim с уже прошедшим сроком,
completed/stopped задачи и активные задачи с уже прошедшим end_date — их
статус и даты остаются как есть.

Исторические сроки не теряются: перед заменой старый end_date каждой
изменённой строки сохраняется в отдельную таблицу ge_monitoring_term_backup
(table_name, row_id, old_end_date, migrated_at); исходные таблицы получают
только новое значение end_date (схема и остальные колонки, включая
updated_at, не меняются). --rollback возвращает сохранённые даты строкам,
которые всё ещё бессрочные. Идемпотентно: повторный --execute ничего не
меняет (уже бессрочные строки не подходят под условие). Ни одного
обращения к police.ge, Telegram или сети — только локальная БД.

По умолчанию — dry-run: ТОЛЬКО читает БД (mode=ro), печатает план, НИЧЕГО
не пишет. Номера машин и Telegram-идентификаторы не печатаются — только
количества.

Использование:
    python -m scripts.migrate_georgia_unlimited_monitoring                # dry-run (default)
    python -m scripts.migrate_georgia_unlimited_monitoring --execute      # реально пишет
    python -m scripts.migrate_georgia_unlimited_monitoring --rollback     # вернуть старые сроки
    python -m scripts.migrate_georgia_unlimited_monitoring --db-path X.db # другой файл (тесты)

Перед --execute на production ОБЯЗАТЕЛЕН backup data/users.db (тот же
паттерн, что и у scripts/migrate_georgia_plate_bootstrap_state.py) — этот
скрипт backup файла САМ не делает."""

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

# Условия "активна прямо сейчас" — те же, что у выборок, которые решают,
# проверяется ли машина и доставляются ли уведомления (см. модульный
# докстрок). end_date < :unlimited — уже бессрочные строки не трогаем.
_ELIGIBLE_WHERE = {
    _SUBSCRIPTIONS: "status IN ('active', 'pending_claim') AND end_date >= :today AND end_date < :unlimited",
    _TASKS: "status = 'active' AND end_date >= :today AND end_date < :unlimited",
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


def build_term_migration_plan(conn: sqlite3.Connection, *, today: date) -> TermMigrationPlan:
    params = _params(today)
    subscriptions_to_convert = dict(conn.execute(
        f"SELECT status, COUNT(*) FROM {_SUBSCRIPTIONS} WHERE {_ELIGIBLE_WHERE[_SUBSCRIPTIONS]} GROUP BY status",
        params,
    ).fetchall())
    tasks_to_convert = dict(conn.execute(
        f"SELECT monitoring_scope, COUNT(*) FROM {_TASKS} WHERE {_ELIGIBLE_WHERE[_TASKS]} "
        "GROUP BY monitoring_scope",
        params,
    ).fetchall())
    subscriptions_untouched = dict(conn.execute(
        f"SELECT status || CASE WHEN end_date < :today THEN ' (срок прошёл)' ELSE '' END, COUNT(*) "
        f"FROM {_SUBSCRIPTIONS} WHERE end_date < :unlimited AND NOT ({_ELIGIBLE_WHERE[_SUBSCRIPTIONS]}) "
        "GROUP BY 1",
        params,
    ).fetchall())
    tasks_untouched = dict(conn.execute(
        f"SELECT monitoring_scope || ' ' || status "
        "|| CASE WHEN end_date < :today THEN ' (срок прошёл)' ELSE '' END, COUNT(*) "
        f"FROM {_TASKS} WHERE end_date < :unlimited AND NOT ({_ELIGIBLE_WHERE[_TASKS]}) GROUP BY 1",
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


def apply_term_migration(conn: sqlite3.Connection, *, today: date) -> dict[str, int]:
    """Одна транзакция (BEGIN IMMEDIATE — бот пишет в ту же БД
    параллельно): сначала старые end_date в BACKUP_TABLE, затем замена на
    UNLIMITED_END_DATE ровно для тех же строк. Возвращает число изменённых
    строк по таблицам."""
    params = _params(today)
    changed: dict[str, int] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_CREATE_BACKUP_TABLE)
        for table in (_SUBSCRIPTIONS, _TASKS):
            conn.execute(
                f"INSERT OR IGNORE INTO {BACKUP_TABLE} (table_name, row_id, old_end_date) "
                f"SELECT '{table}', id, end_date FROM {table} WHERE {_ELIGIBLE_WHERE[table]}",
                params,
            )
            cursor = conn.execute(
                f"UPDATE {table} SET end_date = :unlimited WHERE {_ELIGIBLE_WHERE[table]}", params,
            )
            changed[table] = cursor.rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return changed


def rollback_term_migration(conn: sqlite3.Connection) -> dict[str, int]:
    """Возвращает сохранённый end_date строкам, которые всё ещё бессрочные
    (если строку с тех пор кто-то перевёл на другую дату — её не трогаем),
    и удаляет восстановленные записи из BACKUP_TABLE. Возвращает число
    восстановленных строк по таблицам."""
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
                f") WHERE end_date >= :unlimited AND id IN ("
                f"    SELECT row_id FROM {BACKUP_TABLE} WHERE table_name = '{table}'"
                ")",
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--execute", action="store_true",
        help="Реально записать изменения в БД (default: dry-run, ничего не пишет).",
    )
    mode.add_argument(
        "--rollback", action="store_true",
        help="Вернуть сроки, сохранённые предыдущим --execute.",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Путь к БД (default: settings.app.users_db_file из config/config.yaml).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path, today = _resolve_db_path_and_today(args.db_path)

    if not db_path.exists():
        print(f"БД не найдена: {db_path}", file=sys.stderr)
        return 1

    if args.rollback:
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        try:
            restored = rollback_term_migration(conn)
        finally:
            conn.close()
        print(f"Сроки восстановлены: подписки {restored[_SUBSCRIPTIONS]}, задачи {restored[_TASKS]}.")
        return 0

    ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        plan = build_term_migration_plan(ro_conn, today=today)
    finally:
        ro_conn.close()

    print(f"Сегодня (по часовому поясу мониторинга): {today.isoformat()}")
    print(plan.format_report())

    if not args.execute:
        print("\nDry-run — ничего не записано. Запустите с --execute, чтобы применить.")
        return 0

    print("\n--execute: применяю...")
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    try:
        changed = apply_term_migration(conn, today=today)
    finally:
        conn.close()
    print(f"Готово: бессрочными стали подписки {changed[_SUBSCRIPTIONS]}, задачи {changed[_TASKS]}.")
    print(f"Старые сроки сохранены в таблице {BACKUP_TABLE}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
