"""MAX-клиенты Турции: «Мои авто», мониторинг и снимки «что подписчик уже
видел» — отдельные таблицы max_tr_*. Telegram-таблицы (turkey_bot_user_cars,
turkey_monitoring_subscriptions) не читаются и не пишутся; общие только
результаты проверок turkey_check_runs/turkey_provider_results (номер — одна
физическая машина, долг GİB/Avrasya/KGM — свойство номера).

max_tr_seen — по одной строке на (подписка, провайдер): последний успешный
результат провайдера, о котором подписчик знает (базовая точка при
включении, показанная ручная проверка, доставленное уведомление). Это
previous для reader/turkey_bot/monitoring/change_detector.py::detect_changes;
ERROR туда не попадает никогда (как и в Telegram-мониторинге).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from reader.max_bot.storage import connect, fmt_ts, parse_ts
from reader.turkey_bot.unified.models import ProviderCheckResult, ProviderStatus

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS max_tr_cars (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        max_user_id         INTEGER NOT NULL,
        plate               TEXT NOT NULL,
        created_at          TIMESTAMP NOT NULL,
        last_checked_at     TIMESTAMP,
        last_overall_status TEXT,
        last_total_amount   TEXT,
        UNIQUE (max_user_id, plate)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS max_tr_monitoring (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        max_user_id  INTEGER NOT NULL,
        plate        TEXT NOT NULL,
        active       INTEGER NOT NULL DEFAULT 1,
        created_at   TIMESTAMP NOT NULL,
        updated_at   TIMESTAMP NOT NULL,
        UNIQUE (max_user_id, plate)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_max_tr_monitoring_plate ON max_tr_monitoring (plate, active)",
    """
    CREATE TABLE IF NOT EXISTS max_tr_seen (
        subscription_id   INTEGER NOT NULL,
        provider          TEXT NOT NULL,
        status            TEXT NOT NULL,
        debt_count        INTEGER NOT NULL DEFAULT 0,
        total_amount      TEXT NOT NULL,
        principal_amount  TEXT,
        penalty_amount    TEXT,
        checked_at        TIMESTAMP NOT NULL,
        seen_at           TIMESTAMP NOT NULL,
        PRIMARY KEY (subscription_id, provider)
    )
    """,
)

_CAR_FIELDS = "id, max_user_id, plate, created_at, last_checked_at, last_overall_status, last_total_amount"
_MONITORING_FIELDS = "id, max_user_id, plate, active, created_at, updated_at"


@dataclass(frozen=True)
class MaxTrCar:
    id: int
    max_user_id: int
    plate: str
    created_at: datetime
    last_checked_at: datetime | None
    last_overall_status: str | None
    last_total_amount: Decimal | None


@dataclass(frozen=True)
class MaxTrMonitoring:
    id: int
    max_user_id: int
    plate: str
    active: bool
    created_at: datetime
    updated_at: datetime


def _car(row) -> MaxTrCar:
    return MaxTrCar(
        id=row[0], max_user_id=row[1], plate=row[2], created_at=parse_ts(row[3]), last_checked_at=parse_ts(row[4]),
        last_overall_status=row[5], last_total_amount=Decimal(row[6]) if row[6] is not None else None,
    )


def _monitoring(row) -> MaxTrMonitoring:
    return MaxTrMonitoring(
        id=row[0], max_user_id=row[1], plate=row[2], active=bool(row[3]),
        created_at=parse_ts(row[4]), updated_at=parse_ts(row[5]),
    )


def _decimal_or_none(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _str_or_none(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _now() -> str:
    return fmt_ts(datetime.now(timezone.utc))


class MaxTurkeyRepository:
    def __init__(self, db_path: Path | str):
        self._conn = connect(db_path)
        for statement in _SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def last_telegram_monitoring_finished_at(self) -> datetime | None:
        """Когда закончилась последняя проверка планового Telegram-мониторинга
        (initiator monitoring_*) — по ней видно, что прогон 21:00 ещё идёт.
        Только чтение общей turkey_check_runs."""
        try:
            row = self._conn.execute(
                "SELECT MAX(finished_at) FROM turkey_check_runs WHERE initiator LIKE ? ESCAPE '!'",
                ("monitoring!_%",),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None or row[0] is None:
            return None
        value = datetime.fromisoformat(row[0])
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    # ---- «Мои авто» ----

    def add_car(self, *, max_user_id: int, plate: str) -> tuple[MaxTrCar, bool]:
        """(машина, создана ли сейчас) — повторное добавление дубля не создаёт."""
        try:
            self._conn.execute(
                "INSERT INTO max_tr_cars (max_user_id, plate, created_at) VALUES (?, ?, ?)",
                (max_user_id, plate, _now()),
            )
            created = True
        except sqlite3.IntegrityError:
            created = False
        self._conn.commit()
        return self.get_car_by_plate(max_user_id, plate), created

    def get_car_by_plate(self, max_user_id: int, plate: str) -> MaxTrCar | None:
        row = self._conn.execute(
            f"SELECT {_CAR_FIELDS} FROM max_tr_cars WHERE max_user_id = ? AND plate = ?", (max_user_id, plate),
        ).fetchone()
        return _car(row) if row else None

    def get_owned_car(self, car_id: int, *, max_user_id: int) -> MaxTrCar | None:
        """Права на действие по кнопке: id из payload публичен, владелец —
        только тот, чей user_id записан в строке."""
        row = self._conn.execute(
            f"SELECT {_CAR_FIELDS} FROM max_tr_cars WHERE id = ? AND max_user_id = ?", (car_id, max_user_id),
        ).fetchone()
        return _car(row) if row else None

    def list_cars(self, max_user_id: int) -> list[MaxTrCar]:
        rows = self._conn.execute(
            f"SELECT {_CAR_FIELDS} FROM max_tr_cars WHERE max_user_id = ? ORDER BY id", (max_user_id,),
        ).fetchall()
        return [_car(row) for row in rows]

    def update_last_result(self, *, max_user_id: int, plate: str, overall_status: str, total_amount: Decimal) -> None:
        self._conn.execute(
            "UPDATE max_tr_cars SET last_checked_at = ?, last_overall_status = ?, last_total_amount = ? "
            "WHERE max_user_id = ? AND plate = ?",
            (_now(), overall_status, str(total_amount), max_user_id, plate),
        )
        self._conn.commit()

    def delete_car(self, car_id: int, *, max_user_id: int) -> bool:
        """Удаляет машину ЭТОГО пользователя вместе с его мониторингом и
        снимками; чужие машины и Telegram-данные не затрагиваются."""
        car = self.get_owned_car(car_id, max_user_id=max_user_id)
        if car is None:
            return False
        monitoring = self.get_monitoring(max_user_id, car.plate)
        if monitoring is not None:
            self._conn.execute("DELETE FROM max_tr_seen WHERE subscription_id = ?", (monitoring.id,))
            self._conn.execute("DELETE FROM max_tr_monitoring WHERE id = ?", (monitoring.id,))
        self._conn.execute("DELETE FROM max_tr_cars WHERE id = ? AND max_user_id = ?", (car.id, max_user_id))
        self._conn.commit()
        return True

    def count_cars(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM max_tr_cars").fetchone()[0]

    def count_cars_for_plate(self, plate: str) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM max_tr_cars WHERE plate = ?", (plate,)).fetchone()[0]

    # ---- мониторинг ----

    def get_monitoring(self, max_user_id: int, plate: str) -> MaxTrMonitoring | None:
        row = self._conn.execute(
            f"SELECT {_MONITORING_FIELDS} FROM max_tr_monitoring WHERE max_user_id = ? AND plate = ?",
            (max_user_id, plate),
        ).fetchone()
        return _monitoring(row) if row else None

    def enable(self, *, max_user_id: int, plate: str) -> MaxTrMonitoring:
        now = _now()
        self._conn.execute(
            """
            INSERT INTO max_tr_monitoring (max_user_id, plate, active, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT (max_user_id, plate) DO UPDATE SET active = 1, updated_at = excluded.updated_at
            """,
            (max_user_id, plate, now, now),
        )
        self._conn.commit()
        return self.get_monitoring(max_user_id, plate)

    def disable(self, *, max_user_id: int, plate: str) -> None:
        self._conn.execute(
            "UPDATE max_tr_monitoring SET active = 0, updated_at = ? WHERE max_user_id = ? AND plate = ?",
            (_now(), max_user_id, plate),
        )
        self._conn.commit()

    def active_plates(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT plate FROM max_tr_monitoring WHERE active = 1 ORDER BY plate",
        ).fetchall()
        return [row[0] for row in rows]

    def active_for_plate(self, plate: str) -> list[MaxTrMonitoring]:
        rows = self._conn.execute(
            f"SELECT {_MONITORING_FIELDS} FROM max_tr_monitoring WHERE plate = ? AND active = 1 ORDER BY id", (plate,),
        ).fetchall()
        return [_monitoring(row) for row in rows]

    def count_monitoring(self) -> dict[str, int]:
        row = self._conn.execute("SELECT SUM(active = 1), SUM(active = 0) FROM max_tr_monitoring").fetchone()
        return {"active": row[0] or 0, "inactive": row[1] or 0}

    # ---- снимки «уже видел» ----

    def get_seen(self, subscription_id: int) -> dict[str, ProviderCheckResult]:
        rows = self._conn.execute(
            "SELECT provider, status, debt_count, total_amount, principal_amount, penalty_amount, checked_at "
            "FROM max_tr_seen WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchall()
        return {
            row[0]: ProviderCheckResult(
                provider=row[0], status=ProviderStatus(row[1]), debt_count=row[2], total_amount=Decimal(row[3]),
                principal_amount=_decimal_or_none(row[4]), penalty_amount=_decimal_or_none(row[5]),
                items=(), error_type=None, checked_at=parse_ts(row[6]),
            )
            for row in rows
        }

    def set_seen(self, subscription_id: int, result: ProviderCheckResult) -> None:
        if result.status == ProviderStatus.ERROR:
            raise ValueError("ERROR-результат не может быть базовой точкой сравнения")
        self._conn.execute(
            """
            INSERT INTO max_tr_seen
                (subscription_id, provider, status, debt_count, total_amount, principal_amount, penalty_amount,
                 checked_at, seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (subscription_id, provider) DO UPDATE SET
                status = excluded.status, debt_count = excluded.debt_count, total_amount = excluded.total_amount,
                principal_amount = excluded.principal_amount, penalty_amount = excluded.penalty_amount,
                checked_at = excluded.checked_at, seen_at = excluded.seen_at
            """,
            (
                subscription_id, result.provider, result.status.value, result.debt_count, str(result.total_amount),
                _str_or_none(result.principal_amount), _str_or_none(result.penalty_amount),
                fmt_ts(result.checked_at), _now(),
            ),
        )
        self._conn.commit()

    def clear_seen(self, subscription_id: int) -> None:
        self._conn.execute("DELETE FROM max_tr_seen WHERE subscription_id = ?", (subscription_id,))
        self._conn.commit()
