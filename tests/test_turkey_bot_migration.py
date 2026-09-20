"""
Тесты reader/turkey_bot/migration.py — production backfill логика (см.
задачу "Перенос Unified Turkey функционала в production"). Строит
временную SQLite БД с РЕАЛЬНОЙ production-схемой (turkey_bot_known_users/
turkey_bot_user_cars/turkey_fine_checks/turkey_toll_checks) через прямой
SQL (та же schema, что и в reader/turkey_bot/known_users_repository.py и
т.д.) — НЕ hardcode baseline-числа READ-ONLY аудита (45/80/135/110) в
самих проверках, только используются как ориентир при подборе тестовых
сценариев (см. задачу п.11: "45/80 не hardcoded").

Никаких реальных GİB/Avrasya/KGM HTTP-запросов и никакого обращения к
реальной production data/users.db — только временные файлы (tmp_path)."""

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.migration import (
    apply_migration_plan,
    build_migration_plan,
)
from reader.turkey_bot.monitoring.scheduler_job import (
    TURKEY_MONITORING_TZ,
    next_monitoring_slot,
)
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.user_cars_repository import (
    TurkeyUserCarsRepository,
)

_SCHEMA = """
CREATE TABLE turkey_bot_known_users (
    telegram_user_id  INTEGER PRIMARY KEY,
    telegram_chat_id  INTEGER NOT NULL,
    telegram_username TEXT,
    first_seen_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE turkey_bot_user_cars (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id  INTEGER NOT NULL,
    car_number        TEXT NOT NULL,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_checked_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (telegram_user_id, car_number)
);
CREATE TABLE turkey_fine_checks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id  INTEGER NOT NULL,
    telegram_chat_id  INTEGER NOT NULL,
    plate             TEXT NOT NULL,
    requested_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    captcha_attempts  INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,
    gib_message_text  TEXT,
    raw_response      TEXT
);
CREATE TABLE turkey_toll_checks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id  INTEGER NOT NULL,
    telegram_chat_id  INTEGER NOT NULL,
    provider          TEXT NOT NULL DEFAULT 'avrasya',
    plate             TEXT NOT NULL,
    requested_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    captcha_attempts  INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,
    message_text      TEXT,
    raw_response      TEXT
);
"""


def _make_db(tmp_path) -> Path:
    db_path = tmp_path / "prod_like_users.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def _known_user(db_path, *, user_id, chat_id, username=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO turkey_bot_known_users (telegram_user_id, telegram_chat_id, telegram_username) "
        "VALUES (?, ?, ?)",
        (user_id, chat_id, username),
    )
    conn.commit()
    conn.close()


def _garage_car(db_path, *, user_id, plate):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO turkey_bot_user_cars (telegram_user_id, car_number) VALUES (?, ?)",
        (user_id, plate),
    )
    conn.commit()
    conn.close()


def _fine_check(db_path, *, user_id, chat_id, plate, status):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO turkey_fine_checks (telegram_user_id, telegram_chat_id, plate, status) "
        "VALUES (?, ?, ?, ?)",
        (user_id, chat_id, plate, status),
    )
    conn.commit()
    conn.close()


def _toll_check(db_path, *, user_id, chat_id, plate, status, provider="avrasya"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO turkey_toll_checks (telegram_user_id, telegram_chat_id, provider, plate, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, chat_id, provider, plate, status),
    )
    conn.commit()
    conn.close()


def _plan(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return build_migration_plan(conn, db_path=str(db_path))
    finally:
        conn.close()


def _execute(db_path, plan):
    garage = TurkeyUserCarsRepository(db_path)
    subscriptions = TurkeyMonitoringSubscriptionRepository(db_path)
    try:
        return apply_migration_plan(plan, garage=garage, subscriptions=subscriptions)
    finally:
        garage.close()
        subscriptions.close()


# ---- Baseline numbers are NEVER hardcoded — every assertion below reads
# them back off the fixture just created (см. задачу п.11: "45/80 не
# hardcoded"). ----

def test_users_found_reflects_actual_known_users_table(tmp_path):
    db_path = _make_db(tmp_path)
    for i in range(5):
        _known_user(db_path, user_id=100 + i, chat_id=100 + i)

    plan = _plan(db_path)

    assert plan.users_found == 5


def test_existing_garage_car_is_preserved_not_duplicated(tmp_path):
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=1)
    _garage_car(db_path, user_id=1, plate="A123AA123")

    plan = _plan(db_path)
    result = _execute(db_path, plan)

    assert plan.existing_cars == 1
    assert plan.cars_to_insert == 0
    assert result.cars_inserted == 0
    assert result.duplicates_skipped >= 1

    garage = TurkeyUserCarsRepository(db_path)
    assert len(garage.list_cars(1)) == 1
    garage.close()


def test_successful_legacy_fine_check_backfills_missing_car(tmp_path):
    """Явное требование задачи п.6/п.11: "successful legacy check can
    backfill missing car" — машина НЕ в гараже, но была успешно проверена
    через старый turkey_fine_checks."""
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=1)
    _fine_check(db_path, user_id=1, chat_id=1, plate="A123AA123", status="has_debt")

    plan = _plan(db_path)
    assert plan.existing_cars == 0
    assert plan.cars_to_insert == 1

    result = _execute(db_path, plan)
    assert result.cars_inserted == 1

    garage = TurkeyUserCarsRepository(db_path)
    cars = garage.list_cars(1)
    assert len(cars) == 1
    assert cars[0].car_number == "A123AA123"
    garage.close()


def test_successful_legacy_toll_check_backfills_missing_car(tmp_path):
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=1)
    _toll_check(db_path, user_id=1, chat_id=1, plate="A123AA123", status="no_debt", provider="kgm")

    plan = _plan(db_path)
    result = _execute(db_path, plan)

    assert result.cars_inserted == 1
    garage = TurkeyUserCarsRepository(db_path)
    assert garage.list_cars(1)[0].car_number == "A123AA123"
    garage.close()


def test_unexpected_and_error_checks_never_backfill_a_car(tmp_path):
    """Только no_debt/has_debt считаются "успешными" (см. модуль docstring
    reader/turkey_bot/migration.py::_SUCCESS_STATUSES) — unexpected/error
    НИКОГДА не создают машину, тот же принцип, что и у "гаража" всегда."""
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=1)
    _fine_check(db_path, user_id=1, chat_id=1, plate="A123AA123", status="unexpected")
    _toll_check(db_path, user_id=1, chat_id=1, plate="B456BB456", status="error")

    plan = _plan(db_path)

    assert plan.cars_to_insert == 0
    assert plan.unique_user_plate == 0


def test_multiple_checks_of_same_car_backfill_exactly_one_car(tmp_path):
    """Явное требование п.6/п.11: "duplicate checks -> one car"."""
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=1)
    _fine_check(db_path, user_id=1, chat_id=1, plate="A123AA123", status="no_debt")
    _fine_check(db_path, user_id=1, chat_id=1, plate="A123AA123", status="has_debt")
    _toll_check(db_path, user_id=1, chat_id=1, plate="A123AA123", status="no_debt")

    plan = _plan(db_path)
    result = _execute(db_path, plan)

    assert plan.cars_to_insert == 1
    assert result.cars_inserted == 1
    garage = TurkeyUserCarsRepository(db_path)
    assert len(garage.list_cars(1)) == 1
    garage.close()


def test_same_plate_different_users_produce_separate_cars(tmp_path):
    """Явное требование п.6/п.11: "same plate different users -> separate
    cars" — НИКОГДА не сливать в одну запись."""
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=1)
    _known_user(db_path, user_id=2, chat_id=2)
    _fine_check(db_path, user_id=1, chat_id=1, plate="A123AA123", status="no_debt")
    _fine_check(db_path, user_id=2, chat_id=2, plate="A123AA123", status="no_debt")

    plan = _plan(db_path)
    result = _execute(db_path, plan)

    assert plan.cars_to_insert == 2
    assert result.cars_inserted == 2
    garage = TurkeyUserCarsRepository(db_path)
    assert len(garage.list_cars(1)) == 1
    assert len(garage.list_cars(2)) == 1
    garage.close()


def test_cyrillic_lookalike_plate_normalizes_to_same_car_as_latin(tmp_path):
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=1)
    # "А123АА123" (кириллица) и "A123AA123" (латиница) — один и тот же
    # нормализованный номер (см. reader/turkey_bot/validation.py).
    _fine_check(db_path, user_id=1, chat_id=1, plate="А123АА123", status="no_debt")
    _garage_car(db_path, user_id=1, plate="A123AA123")

    plan = _plan(db_path)

    assert plan.unique_user_plate == 1
    assert plan.cars_to_insert == 0  # уже "существует" под нормализованным именем


def test_ambiguous_user_with_no_known_chat_id_is_skipped(tmp_path):
    """Явное требование п.3/п.6/п.11: НЕ назначать наугад — пользователь
    есть в garage/checks, но ни разу не встречался в known_users (нет
    источника chat_id)."""
    db_path = _make_db(tmp_path)
    _garage_car(db_path, user_id=999, plate="A123AA123")  # НЕТ known_user для 999

    plan = _plan(db_path)

    assert plan.ambiguous_skipped == 1
    assert plan.ambiguous[0].reason == "no_known_chat_id"
    assert plan.subscriptions_to_create == 0

    result = _execute(db_path, plan)
    assert result.subscriptions_inserted == 0


def test_chat_id_mismatch_between_known_users_and_checks_is_ambiguous(tmp_path):
    """Явное требование READ-ONLY аудита п.9/п.13 — "не считать
    user_id==chat_id по умолчанию": если запись в чеках говорит ДРУГОЙ
    chat_id, чем known_users, это подозрительно — SKIP, не угадывать."""
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=111)
    _fine_check(db_path, user_id=1, chat_id=999, plate="A123AA123", status="no_debt")  # chat_id расходится

    plan = _plan(db_path)

    assert plan.ambiguous_skipped == 1
    assert plan.ambiguous[0].reason == "chat_id_mismatch"


def test_every_migrated_car_gets_an_active_monitoring_subscription(tmp_path):
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=1)
    _garage_car(db_path, user_id=1, plate="A123AA123")

    plan = _plan(db_path)
    result = _execute(db_path, plan)

    assert plan.subscriptions_to_create == 1
    assert result.subscriptions_inserted == 1

    subscriptions = TurkeyMonitoringSubscriptionRepository(db_path)
    sub = subscriptions.get(telegram_user_id=1, plate="A123AA123")
    assert sub is not None
    assert sub.active is True
    assert sub.telegram_chat_id == 1
    subscriptions.close()


def test_existing_active_subscription_is_left_unchanged(tmp_path):
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=1)
    _garage_car(db_path, user_id=1, plate="A123AA123")
    subscriptions = TurkeyMonitoringSubscriptionRepository(db_path)
    subscriptions.enable(telegram_user_id=1, telegram_chat_id=1, plate="A123AA123")
    subscriptions.close()

    plan = _plan(db_path)
    assert plan.subscriptions_to_create == 0
    result = _execute(db_path, plan)
    assert result.subscriptions_inserted == 0

    subscriptions = TurkeyMonitoringSubscriptionRepository(db_path)
    assert subscriptions.count_active() == 1
    subscriptions.close()


def test_existing_inactive_subscription_is_not_silently_reactivated(tmp_path):
    """Явное требование п.7/п.11: "inactive existing subscription not
    reactivated" — критично, чтобы migration никогда не отменял
    сознательное решение пользователя выключить мониторинг."""
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=1)
    _garage_car(db_path, user_id=1, plate="A123AA123")
    subscriptions = TurkeyMonitoringSubscriptionRepository(db_path)
    subscriptions.enable(telegram_user_id=1, telegram_chat_id=1, plate="A123AA123")
    subscriptions.disable(telegram_user_id=1, plate="A123AA123")
    subscriptions.close()

    plan = _plan(db_path)
    assert plan.subscriptions_to_create == 0  # запись уже существует (inactive)
    result = _execute(db_path, plan)
    assert result.subscriptions_inserted == 0

    subscriptions = TurkeyMonitoringSubscriptionRepository(db_path)
    sub = subscriptions.get(telegram_user_id=1, plate="A123AA123")
    assert sub.active is False  # НЕ реактивирована
    subscriptions.close()


def test_migration_run_twice_is_idempotent(tmp_path):
    """Явное требование п.4/п.7/п.11: "migration run twice -> same
    result" — второй прогон не создаёт дубликаты ни машин, ни подписок."""
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=1)
    _known_user(db_path, user_id=2, chat_id=2)
    _fine_check(db_path, user_id=1, chat_id=1, plate="A123AA123", status="has_debt")
    _garage_car(db_path, user_id=2, plate="B456BB456")

    first_plan = _plan(db_path)
    first_result = _execute(db_path, first_plan)
    assert first_result.cars_inserted == 1  # user 1's car из fine_checks
    assert first_result.subscriptions_inserted == 2  # оба пользователя

    second_plan = _plan(db_path)
    assert second_plan.cars_to_insert == 0
    assert second_plan.subscriptions_to_create == 0

    second_result = _execute(db_path, second_plan)
    assert second_result.cars_inserted == 0
    assert second_result.subscriptions_inserted == 0

    garage = TurkeyUserCarsRepository(db_path)
    assert len(garage.list_cars(1)) == 1
    assert len(garage.list_cars(2)) == 1
    garage.close()
    subscriptions = TurkeyMonitoringSubscriptionRepository(db_path)
    assert subscriptions.count_active() == 2
    subscriptions.close()


def test_legacy_history_tables_are_never_modified_by_migration(tmp_path):
    """Явное требование п.10/п.11: turkey_fine_checks/turkey_toll_checks
    остаются legacy history — migration их только ЧИТАЕТ."""
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=1)
    _fine_check(db_path, user_id=1, chat_id=1, plate="A123AA123", status="has_debt")
    _toll_check(db_path, user_id=1, chat_id=1, plate="A123AA123", status="no_debt")

    conn = sqlite3.connect(db_path)
    before_fine = conn.execute("SELECT * FROM turkey_fine_checks").fetchall()
    before_toll = conn.execute("SELECT * FROM turkey_toll_checks").fetchall()
    conn.close()

    plan = _plan(db_path)
    _execute(db_path, plan)

    conn = sqlite3.connect(db_path)
    after_fine = conn.execute("SELECT * FROM turkey_fine_checks").fetchall()
    after_toll = conn.execute("SELECT * FROM turkey_toll_checks").fetchall()
    conn.close()

    assert after_fine == before_fine
    assert after_toll == before_toll


def test_migration_creates_no_fake_unified_history(tmp_path):
    """Явное требование п.10: "не создавать fake historical unified
    runs" — migration НИКОГДА не пишет в turkey_check_runs (та таблица
    создаётся ТОЛЬКО через TurkeyCheckRunRepository, отдельно от
    migration-скрипта, см. scripts/migrate_turkey_unified.py — и остаётся
    ПУСТОЙ до первой реальной unified-проверки)."""
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=1)
    _fine_check(db_path, user_id=1, chat_id=1, plate="A123AA123", status="has_debt")

    plan = _plan(db_path)
    _execute(db_path, plan)

    conn = sqlite3.connect(db_path)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'turkey_check_runs'",
    ).fetchone()
    conn.close()
    # apply_migration_plan сам НЕ создаёт эту таблицу (её создаёт
    # TurkeyCheckRunRepository, конструируемый ОТДЕЛЬНО в CLI-скрипте) —
    # здесь она ещё не должна существовать вовсе.
    assert exists is None


def test_next_monitoring_slot_uses_strict_europe_istanbul_schedule(tmp_path):
    """Явное требование п.7/п.11: 13:00/21:00 Europe/Istanbul — тот же,
    неизменённый next_monitoring_slot(), что и у остального мониторинга
    (см. reader/turkey_bot/monitoring/scheduler_job.py)."""
    db_path = _make_db(tmp_path)
    _known_user(db_path, user_id=1, chat_id=1)
    _garage_car(db_path, user_id=1, plate="A123AA123")

    now = datetime(2026, 9, 20, 8, 0, tzinfo=TURKEY_MONITORING_TZ)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    plan = build_migration_plan(conn, db_path=str(db_path), now=now.astimezone(timezone.utc))
    conn.close()

    expected = next_monitoring_slot(now.astimezone(timezone.utc))
    assert plan.next_monitoring_slot_utc == expected
    local = plan.next_monitoring_slot_utc.astimezone(TURKEY_MONITORING_TZ)
    assert (local.hour, local.minute) == (13, 0)


def test_production_db_path_reported_matches_what_was_passed(tmp_path):
    """См. задачу п.11: "production DB path only" — build_migration_plan
    отражает ТОЧНО тот путь, который ему передали, никогда не подменяет
    его на data/turkey_bot_test.db или что-либо ещё."""
    db_path = _make_db(tmp_path)
    plan = _plan(db_path)

    assert plan.db_path == str(db_path)
    assert "turkey_bot_test" not in plan.db_path
