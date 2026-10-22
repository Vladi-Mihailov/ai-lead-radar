#!/usr/bin/env python
"""Production backfill: заполняет georgia_plate_bootstrap_state (см.
reader/fines/plate_bootstrap_repository.py) для УЖЕ существующих активных
fine_monitoring_tasks — без этого шага GeorgiaPublicFineFeedJob не стал бы
единственным источником проверки ни для одного physical plate, и FineJob/
ArchiveFineJob продолжали бы делать per-car опрос для всех как раньше
(безопасно само по себе, но не даёт эффекта от задачи "Georgia fines
incremental public-feed monitoring" — см. audit report).

Правило (см. task report "EXISTING PLATE MIGRATION", обоснование —
аудит production БД, 2026-10-22): физический car_number (среди активных
задач) -> 'completed', если ХОТЯ БЫ ОДНА его активная задача имеет
last_successful_checked_at IS NOT NULL (т.е. уже прошла реальный живой
searchByAuto хотя бы раз) — ЭТО УЖЕ full-history lookup, повторять его не
нужно. Группировка ИМЕННО по car_number (не по task_id) — одна физическая
машина может иметь НЕСКОЛЬКО активных задач одновременно (см. production:
car_number=O528HY702, 2 задачи) — наивный подсчёт по задачам даёт
несогласованные числа (см. task report "MIGRATION COUNT ROOT CAUSE":
521 "задач с успехом" + 3 "задач без успеха" = 524 != 523 физических
плейтов именно из-за этого).

car_number БЕЗ ни одной задачи с last_successful_checked_at -> 'pending' —
следующий нормальный живой поиск (per-car ИЛИ ближайший bootstrap-триггер)
либо GeorgiaPublicFineFeedJob's first-run catch-up доберёт её честно.

Do NOT launch a full-history lookup storm (см. задачу) — этот скрипт
НИКОГДА не делает ни одного HTTP-запроса к police.ge, только читает/пишет
локальную БД.

По умолчанию — dry-run: ТОЛЬКО читает БД (mode=ro), печатает план, НИЧЕГО
не пишет. Идемпотентно: ensure_pending()/mark_completed() безопасно
повторно запускать сколько угодно раз (см. GeorgiaPlateBootstrapRepository
докстрок) — уже 'completed' строка не откатывается на 'pending' повторным
запуском.

Использование:
    python -m scripts.migrate_georgia_plate_bootstrap_state                # dry-run (default)
    python -m scripts.migrate_georgia_plate_bootstrap_state --execute      # реально пишет
    python -m scripts.migrate_georgia_plate_bootstrap_state --db-path X.db # другой файл (тесты)

Перед --execute на production ОБЯЗАТЕЛЕН backup data/users.db (тот же
паттерн, что и у scripts/migrate_turkey_unified.py) — этот скрипт backup
САМ не делает."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.plate_bootstrap_repository import (
    GeorgiaPlateBootstrapRepository,
)
from reader.logging_setup import setup_logging
from reader.settings import load_settings

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

_SELECT_ACTIVE_PLATE_SUCCESS_FLAGS = """
    SELECT car_number, MAX(CASE WHEN last_successful_checked_at IS NOT NULL THEN 1 ELSE 0 END)
    FROM fine_monitoring_tasks
    WHERE status = 'active'
    GROUP BY car_number
"""


@dataclass(frozen=True)
class MigrationPlan:
    completed_plates: list[str] = field(default_factory=list)
    pending_plates: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.completed_plates) + len(self.pending_plates)

    def format_report(self) -> str:
        lines = [
            f"TOTAL_ACTIVE_PHYSICAL_PLATES = {self.total}",
            f"BOOTSTRAP_COMPLETED_FROM_EXISTING_STATE = {len(self.completed_plates)}",
            f"BOOTSTRAP_PENDING = {len(self.pending_plates)}",
        ]
        # Инвариант задачи — явная проверка, а не молчаливое доверие числам.
        assert set(self.completed_plates).isdisjoint(self.pending_plates)
        assert len(self.completed_plates) + len(self.pending_plates) == self.total
        return "\n".join(lines)


def build_migration_plan(ro_conn: sqlite3.Connection) -> MigrationPlan:
    rows = ro_conn.execute(_SELECT_ACTIVE_PLATE_SUCCESS_FLAGS).fetchall()
    completed = sorted(car_number for car_number, has_success in rows if has_success)
    pending = sorted(car_number for car_number, has_success in rows if not has_success)
    return MigrationPlan(completed_plates=completed, pending_plates=pending)


def apply_migration_plan(plan: MigrationPlan, repo: GeorgiaPlateBootstrapRepository) -> None:
    now = datetime.now(timezone.utc)
    for car_number in plan.pending_plates:
        repo.ensure_pending(car_number)
    for car_number in plan.completed_plates:
        repo.ensure_pending(car_number)  # идемпотентно создаёт строку, если её ещё нет
        repo.mark_completed(car_number, completed_at=now)


def _resolve_db_path(explicit: str | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    settings = load_settings(CONFIG_PATH)
    return settings.app.users_db_file


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="Реально записать изменения в БД (default: dry-run, ничего не пишет).",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Путь к БД (default: settings.app.users_db_file из config/config.yaml).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging("INFO")
    args = _parse_args(argv)
    db_path = _resolve_db_path(args.db_path)

    if not db_path.exists():
        print(f"БД не найдена: {db_path}", file=sys.stderr)
        return 1

    ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        plan = build_migration_plan(ro_conn)
    finally:
        ro_conn.close()

    print(plan.format_report())

    if not args.execute:
        print("\nDry-run — ничего не записано. Запустите с --execute, чтобы применить.")
        return 0

    print("\n--execute: применяю migration...")
    repo = GeorgiaPlateBootstrapRepository(db_path)
    try:
        apply_migration_plan(plan, repo)
    finally:
        repo.close()

    print(f"Готово: {len(plan.completed_plates)} plate(s) -> completed, "
          f"{len(plan.pending_plates)} plate(s) -> pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
