"""Production migration/backfill логика — перенос данных существующих
пользователей @ProtocolTRbot в новую unified-архитектуру (см. задачу
"Перенос Unified Turkey функционала в production").

READ-ONLY аудит (см. отчёт в диалоге) дал baseline на реальной production
БД: 45 known users, 80 garage user+plate, 135 historical fine checks, 110
historical toll checks, 0 cars требуют backfill (garage уже полный), 80
monitoring subscriptions будет создано, 0 ambiguous mappings. Эти числа
НИГДЕ не захардкожены в логике ниже — только результат вычислений над
реальным содержимым БД (см. тесты: baseline сверяется, не предполагается).

Инварианты (см. задачу п.5-п.7):
  - build_migration_plan() — ЧИСТО READ-ONLY, ни одного INSERT/UPDATE/
    ALTER; безопасно вызывать на боевой БД без --execute;
  - apply_migration_plan() — ТОЛЬКО INSERT через уже существующие
    idempotent repository-методы (TurkeyUserCarsRepository.add_car —
    "ON CONFLICT DO NOTHING", TurkeyMonitoringSubscriptionRepository —
    вызывается ТОЛЬКО когда subscription для этой пары ЕЩЁ НЕ существует
    вовсе, см. _apply_one — поэтому существующая active ИЛИ inactive
    подписка никогда не трогается повторным запуском);
  - Источник пар (telegram_user_id, plate) — объединение turkey_bot_user_cars
    ∪ успешных turkey_fine_checks ∪ успешных turkey_toll_checks (см. задачу
    п.6), НЕ только garage;
  - chat_id — ТОЛЬКО из turkey_bot_known_users, с перекрёстной сверкой
    против chat_id, независимо записанного в каждой строке
    turkey_fine_checks/turkey_toll_checks для того же user_id (см. задачу
    READ-ONLY аудита п.9/п.13: "не считать user_id==chat_id по
    умолчанию") — расхождение или отсутствие -> ambiguous, SKIP, никогда
    не назначается наугад."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from reader.turkey_bot.monitoring.scheduler_job import next_monitoring_slot
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository
from reader.turkey_bot.validation import normalize_plate

logger = logging.getLogger(__name__)

_SUCCESS_STATUSES = ("no_debt", "has_debt")


@dataclass(frozen=True)
class ResolvedCarPair:
    telegram_user_id: int
    telegram_chat_id: int
    plate: str


@dataclass(frozen=True)
class AmbiguousRecord:
    telegram_user_id: int
    plate: str
    reason: str


@dataclass(frozen=True)
class MigrationPlan:
    db_path: str
    users_found: int
    fine_checks_found: int
    toll_checks_found: int
    unique_user_plate: int
    existing_cars: int
    cars_to_insert: int
    subscriptions_to_create: int
    ambiguous: tuple[AmbiguousRecord, ...]
    next_monitoring_slot_utc: datetime
    resolved_pairs: tuple[ResolvedCarPair, ...] = field(repr=False)

    @property
    def ambiguous_skipped(self) -> int:
        return len(self.ambiguous)

    def format_report(self) -> str:
        lines = [
            f"DB path: {self.db_path}",
            f"users found: {self.users_found}",
            f"historical checks found: fine={self.fine_checks_found} toll={self.toll_checks_found}",
            f"unique user+plate: {self.unique_user_plate}",
            f"existing cars: {self.existing_cars}",
            f"cars to insert: {self.cars_to_insert}",
            f"subscriptions to create: {self.subscriptions_to_create}",
            f"ambiguous/skipped: {self.ambiguous_skipped}",
            f"next monitoring slot (UTC): {self.next_monitoring_slot_utc.isoformat()}",
        ]
        return "\n".join(lines)


@dataclass(frozen=True)
class MigrationExecutionResult:
    cars_inserted: int
    subscriptions_inserted: int
    duplicates_skipped: int
    errors: int

    def format_report(self) -> str:
        return "\n".join([
            f"cars inserted: {self.cars_inserted}",
            f"subscriptions inserted: {self.subscriptions_inserted}",
            f"duplicates skipped: {self.duplicates_skipped}",
            f"errors: {self.errors}",
        ])


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,),
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _known_chat_ids(conn: sqlite3.Connection) -> dict[int, int]:
    if not _table_exists(conn, "turkey_bot_known_users"):
        return {}
    return dict(
        conn.execute("SELECT telegram_user_id, telegram_chat_id FROM turkey_bot_known_users").fetchall(),
    )


def _checks_chat_ids(conn: sqlite3.Connection, table: str) -> dict[tuple[int, str], int]:
    """(user_id, RAW plate ровно как в этой таблице) -> chat_id — ТОЛЬКО
    для перекрёстной сверки, не источник пар (см. модуль docstring)."""
    if not _table_exists(conn, table):
        return {}
    placeholders = ",".join("?" for _ in _SUCCESS_STATUSES)
    rows = conn.execute(
        f"SELECT telegram_user_id, plate, telegram_chat_id FROM {table} WHERE status IN ({placeholders})",
        _SUCCESS_STATUSES,
    ).fetchall()
    return {(user_id, plate): chat_id for user_id, plate, chat_id in rows}


def _garage_pairs(conn: sqlite3.Connection) -> set[tuple[int, str]]:
    if not _table_exists(conn, "turkey_bot_user_cars"):
        return set()
    return set(conn.execute("SELECT telegram_user_id, car_number FROM turkey_bot_user_cars").fetchall())


def _check_pairs(conn: sqlite3.Connection, table: str) -> set[tuple[int, str]]:
    if not _table_exists(conn, table):
        return set()
    placeholders = ",".join("?" for _ in _SUCCESS_STATUSES)
    rows = conn.execute(
        f"SELECT telegram_user_id, plate FROM {table} WHERE status IN ({placeholders})",
        _SUCCESS_STATUSES,
    ).fetchall()
    return {(user_id, plate) for user_id, plate in rows}


def _existing_subscription_pairs(conn: sqlite3.Connection) -> set[tuple[int, str]]:
    if not _table_exists(conn, "turkey_monitoring_subscriptions"):
        return set()
    return set(
        conn.execute("SELECT telegram_user_id, plate FROM turkey_monitoring_subscriptions").fetchall(),
    )


def build_migration_plan(
    conn: sqlite3.Connection, *, db_path: str, now: datetime | None = None,
) -> MigrationPlan:
    """ЧИСТО READ-ONLY (см. модуль docstring) — вызывающий код (см.
    scripts/migrate_turkey_unified.py) обязан открывать `conn` в
    режиме `mode=ro` для dry-run, чтобы это было гарантировано
    структурно, а не только по соглашению."""
    now = now or datetime.now(timezone.utc)

    users_found = _count(conn, "turkey_bot_known_users")
    fine_checks_found = _count(conn, "turkey_fine_checks")
    toll_checks_found = _count(conn, "turkey_toll_checks")

    garage_pairs = _garage_pairs(conn)
    fine_pairs = _check_pairs(conn, "turkey_fine_checks")
    toll_pairs = _check_pairs(conn, "turkey_toll_checks")
    all_raw_pairs = garage_pairs | fine_pairs | toll_pairs

    known_chat_ids = _known_chat_ids(conn)
    fine_chat_ids = _checks_chat_ids(conn, "turkey_fine_checks")
    toll_chat_ids = _checks_chat_ids(conn, "turkey_toll_checks")

    resolved: list[ResolvedCarPair] = []
    ambiguous: list[AmbiguousRecord] = []
    seen_resolved: set[tuple[int, str]] = set()
    seen_normalized_for_count: set[tuple[int, str]] = set()

    for user_id, raw_plate in sorted(all_raw_pairs):
        normalized = normalize_plate(raw_plate)
        display_plate = normalized or raw_plate
        seen_normalized_for_count.add((user_id, display_plate))

        if normalized is None:
            ambiguous.append(AmbiguousRecord(user_id, raw_plate, "plate_not_normalizable"))
            continue

        chat_id = known_chat_ids.get(user_id)
        if chat_id is None:
            ambiguous.append(AmbiguousRecord(user_id, normalized, "no_known_chat_id"))
            continue

        mismatch = False
        for source_chat_ids in (fine_chat_ids, toll_chat_ids):
            recorded = source_chat_ids.get((user_id, raw_plate))
            if recorded is not None and recorded != chat_id:
                mismatch = True
                break
        if mismatch:
            ambiguous.append(AmbiguousRecord(user_id, normalized, "chat_id_mismatch"))
            continue

        key = (user_id, normalized)
        if key not in seen_resolved:
            seen_resolved.add(key)
            resolved.append(ResolvedCarPair(telegram_user_id=user_id, telegram_chat_id=chat_id, plate=normalized))

    existing_car_keys = {
        (user_id, normalize_plate(plate) or plate) for user_id, plate in garage_pairs
    }
    cars_to_insert = sum(
        1 for pair in resolved if (pair.telegram_user_id, pair.plate) not in existing_car_keys
    )

    existing_sub_pairs = _existing_subscription_pairs(conn)
    subscriptions_to_create = sum(
        1 for pair in resolved if (pair.telegram_user_id, pair.plate) not in existing_sub_pairs
    )

    return MigrationPlan(
        db_path=db_path,
        users_found=users_found,
        fine_checks_found=fine_checks_found,
        toll_checks_found=toll_checks_found,
        unique_user_plate=len(seen_normalized_for_count),
        existing_cars=len(garage_pairs),
        cars_to_insert=cars_to_insert,
        subscriptions_to_create=subscriptions_to_create,
        ambiguous=tuple(ambiguous),
        next_monitoring_slot_utc=next_monitoring_slot(now),
        resolved_pairs=tuple(resolved),
    )


def apply_migration_plan(
    plan: MigrationPlan,
    *,
    garage: TurkeyUserCarsRepository,
    subscriptions: TurkeyMonitoringSubscriptionRepository,
    now: datetime | None = None,
) -> MigrationExecutionResult:
    """Пишет ТОЛЬКО через уже существующие idempotent repository-методы —
    никакого сырого SQL здесь (см. модуль docstring). Requires the caller
    to have already constructed `garage`/`subscriptions` against the
    target db_path (их конструкторы сами создают недостающие
    таблицы/колонки, см. reader/turkey_bot/user_cars_repository.py::
    _COLUMN_MIGRATIONS и reader/turkey_bot/monitoring/
    subscription_repository.py::_SCHEMA — additive, см. задачу п.5)."""
    now = now or datetime.now(timezone.utc)
    cars_inserted = 0
    subscriptions_inserted = 0
    duplicates_skipped = 0
    errors = 0

    for pair in plan.resolved_pairs:
        try:
            already_had_car = garage.get_owned_car_by_number(pair.telegram_user_id, pair.plate) is not None
            garage.add_car(telegram_user_id=pair.telegram_user_id, car_number=pair.plate)
            if already_had_car:
                duplicates_skipped += 1
            else:
                cars_inserted += 1
        except Exception:
            logger.exception(
                "Turkey migration: не удалось создать car user=%s plate=%s",
                pair.telegram_user_id, pair.plate,
            )
            errors += 1
            continue

        try:
            # НИКОГДА не вызывать subscriptions.enable(), если подписка уже
            # существует (в ЛЮБОМ состоянии active/inactive) — enable() сам
            # по себе upsert'ит active=1 (см. задачу п.7: "existing
            # inactive -> НЕ реактивировать"), поэтому проверка "уже есть?"
            # обязана идти СНАЧАЛА, а не полагаться на ON CONFLICT внутри
            # enable().
            existing_subscription = subscriptions.get(
                telegram_user_id=pair.telegram_user_id, plate=pair.plate,
            )
            if existing_subscription is not None:
                duplicates_skipped += 1
                continue

            subscriptions.enable(
                telegram_user_id=pair.telegram_user_id, telegram_chat_id=pair.telegram_chat_id,
                plate=pair.plate, next_check_at=next_monitoring_slot(now),
            )
            subscriptions_inserted += 1
        except Exception:
            logger.exception(
                "Turkey migration: не удалось создать subscription user=%s plate=%s",
                pair.telegram_user_id, pair.plate,
            )
            errors += 1

    return MigrationExecutionResult(
        cars_inserted=cars_inserted, subscriptions_inserted=subscriptions_inserted,
        duplicates_skipped=duplicates_skipped, errors=errors,
    )
