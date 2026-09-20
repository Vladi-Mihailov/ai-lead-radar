#!/usr/bin/env python
"""Production backfill: переносит существующих пользователей/автомобили
@ProtocolTRbot (reader/turkey_bot/) в новую unified-архитектуру (см. задачу
"Перенос Unified Turkey функционала в production", READ-ONLY аудит).

По умолчанию — dry-run: ТОЛЬКО читает БД (см. reader/turkey_bot/migration.py::
build_migration_plan — открывает соединение в режиме SQLite `mode=ro`,
поэтому даже случайная попытка записи структурно невозможна, а не просто
"мы не вызываем write-функции"), печатает план, НИЧЕГО не пишет.

Только --execute реально пишет в БД — и делает это ТОЛЬКО через уже
существующие, additive/idempotent repository-классы (см.
reader/turkey_bot/user_cars_repository.py, reader/turkey_bot/monitoring/
subscription_repository.py, reader/turkey_bot/unified/run_repository.py) —
никакого сырого DDL здесь, никаких DROP/DELETE где бы то ни было.

Использование:
    python -m scripts.migrate_turkey_unified                # dry-run (default)
    python -m scripts.migrate_turkey_unified --execute       # реально пишет
    python -m scripts.migrate_turkey_unified --db-path X.db  # другой файл (тесты)

Перед --execute на production ОБЯЗАТЕЛЕН backup data/users.db (см. задачу
п.11 READ-ONLY аудита — timestamped copy, тот же паттерн именования, что
уже используется на сервере: data/users.db.bak-pre-<описание>-<timestamp>).
Этот скрипт backup САМ не делает — это отдельный, явный шаг deployment
sequence, не автоматизируется здесь намеренно (см. отчёт: "предупредить
перед выполнением")."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.logging_setup import setup_logging
from reader.settings import load_settings
from reader.turkey_bot.migration import (
    apply_migration_plan,
    build_migration_plan,
)
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.unified.run_repository import (
    TurkeyCheckRunRepository,
)
from reader.turkey_bot.user_cars_repository import (
    TurkeyUserCarsRepository,
)

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

logger = logging.getLogger(__name__)


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
        help="Путь к БД (default: settings.app.users_db_file из config/config.yaml). "
             "НИКОГДА не указывайте здесь data/turkey_bot_test.db.",
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
        plan = build_migration_plan(ro_conn, db_path=str(db_path))
    finally:
        ro_conn.close()

    print(plan.format_report())
    if plan.ambiguous:
        print("\nAmbiguous records (skipped, NOT migrated automatically):")
        for record in plan.ambiguous:
            print(f"  user={record.telegram_user_id} plate={record.plate} reason={record.reason}")

    if not args.execute:
        print("\nDry-run — ничего не записано. Запустите с --execute, чтобы применить.")
        return 0

    print("\n--execute: применяю migration...")
    garage = TurkeyUserCarsRepository(db_path)
    subscriptions = TurkeyMonitoringSubscriptionRepository(db_path)
    # Конструктор сам создаёт turkey_check_runs/turkey_provider_results
    # (CREATE TABLE IF NOT EXISTS, см. reader/turkey_bot/unified/
    # run_repository.py) — новая unified-история начинается с этого
    # момента (см. задачу п.10), сам migration-скрипт в эти таблицы
    # ничего не пишет.
    runs = TurkeyCheckRunRepository(db_path)
    try:
        result = apply_migration_plan(plan, garage=garage, subscriptions=subscriptions)
    finally:
        garage.close()
        subscriptions.close()
        runs.close()

    print(result.format_report())
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
