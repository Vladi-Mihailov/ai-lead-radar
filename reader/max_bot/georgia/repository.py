"""MAX-подписки Грузии и их доставки — отдельные таблицы, Telegram-таблицы
(fine_monitoring_subscriptions, client_fine_deliveries,
georgia_fine_amount_deliveries) не читаются и не пишутся.

Подписка ссылается на ОБЩУЮ задачу fine_monitoring_tasks (monitoring_task_id):
машина, подключённая и в Telegram, и в MAX, проверяется одной задачей.

baseline_at — точка отсчёта «новых» штрафов этой подписки: штрафы, впервые
увиденные системой не позже неё, — история, клиенту в MAX новыми не
приходят. Пока идёт первичная проверка при добавлении, baseline_at =
PENDING_BASELINE (никогда не наступает): поллер доставки не пришлёт штрафы,
которые клиент и так увидит в ответе на добавление.

history_cutoff_at — окончательная граница истории, фиксируется ОДИН раз:
при добавлении, если первичная проверка номера (bootstrap) уже завершена, —
равна baseline_at; иначе — когда поллер впервые увидит её завершённой
(max(baseline_at, момент завершения)). Повторные отметки завершения при
добавлении того же номера другими клиентами (Telegram или MAX) её не
сдвигают — иначе настоящие новые штрафы ушли бы в «историю».
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from reader.max_bot.storage import connect, fmt_ts, parse_ts

MaxGeSubscriptionStatus = Literal["active", "stopped", "archived"]

PENDING_BASELINE = datetime(9999, 12, 31)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS max_ge_subscriptions (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        max_user_id         INTEGER NOT NULL,
        car_number          TEXT NOT NULL,
        monitoring_task_id  INTEGER NOT NULL,
        status              TEXT NOT NULL DEFAULT 'active',
        baseline_at         TIMESTAMP NOT NULL,
        history_cutoff_at   TIMESTAMP,
        created_at          TIMESTAMP NOT NULL,
        updated_at          TIMESTAMP NOT NULL,
        stopped_at          TIMESTAMP
    )
    """,
    # Одна неудалённая подписка на (пользователь, номер): повторное
    # «Добавить» возвращает её же, после удаления создаётся новая.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_max_ge_subscriptions_user_car
        ON max_ge_subscriptions (max_user_id, car_number) WHERE status != 'archived'
    """,
    "CREATE INDEX IF NOT EXISTS idx_max_ge_subscriptions_car ON max_ge_subscriptions (car_number, status)",
    """
    CREATE TABLE IF NOT EXISTS max_ge_fine_deliveries (
        detected_fine_id  INTEGER NOT NULL,
        subscription_id   INTEGER NOT NULL,
        protocol_no       TEXT,
        attempt_count     INTEGER NOT NULL DEFAULT 0,
        last_attempt_at   TIMESTAMP,
        delivered_at      TIMESTAMP,
        excluded_at       TIMESTAMP,
        exclusion_reason  TEXT,
        PRIMARY KEY (detected_fine_id, subscription_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_max_ge_fine_deliveries_protocol
        ON max_ge_fine_deliveries (subscription_id, protocol_no)
    """,
    """
    CREATE TABLE IF NOT EXISTS max_ge_amount_deliveries (
        event_id          INTEGER NOT NULL,
        subscription_id   INTEGER NOT NULL,
        attempt_count     INTEGER NOT NULL DEFAULT 0,
        last_attempt_at   TIMESTAMP,
        delivered_at      TIMESTAMP,
        excluded_at       TIMESTAMP,
        exclusion_reason  TEXT,
        PRIMARY KEY (event_id, subscription_id)
    )
    """,
)

_FIELDS = (
    "id, max_user_id, car_number, monitoring_task_id, status, baseline_at, created_at, updated_at, stopped_at, "
    "history_cutoff_at"
)


@dataclass(frozen=True)
class MaxGeSubscription:
    id: int
    max_user_id: int
    car_number: str
    monitoring_task_id: int
    status: MaxGeSubscriptionStatus
    baseline_at: datetime
    created_at: datetime
    updated_at: datetime
    stopped_at: datetime | None
    history_cutoff_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def baseline_pending(self) -> bool:
        return self.baseline_at >= PENDING_BASELINE


@dataclass(frozen=True)
class MaxDelivery:
    """Попытка/факт доставки одного штрафа или одного события увеличения
    одной MAX-подписке (тот же смысл полей, что у Telegram-доставок)."""

    attempt_count: int
    last_attempt_at: datetime | None
    delivered_at: datetime | None
    excluded_at: datetime | None
    exclusion_reason: str | None


def _row(row) -> MaxGeSubscription:
    return MaxGeSubscription(
        id=row[0], max_user_id=row[1], car_number=row[2], monitoring_task_id=row[3], status=row[4],
        baseline_at=parse_ts(row[5]), created_at=parse_ts(row[6]), updated_at=parse_ts(row[7]),
        stopped_at=parse_ts(row[8]), history_cutoff_at=parse_ts(row[9]),
    )


def _now() -> str:
    return fmt_ts(datetime.now(timezone.utc))


class MaxGeorgiaRepository:
    def __init__(self, db_path: Path | str):
        self._conn = connect(db_path)
        for statement in _SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- подписки ----

    def get(self, subscription_id: int) -> MaxGeSubscription | None:
        row = self._conn.execute(
            f"SELECT {_FIELDS} FROM max_ge_subscriptions WHERE id = ?", (subscription_id,),
        ).fetchone()
        return _row(row) if row else None

    def get_owned(self, subscription_id: int, *, max_user_id: int) -> MaxGeSubscription | None:
        """Единственная проверка прав на действия по кнопке: id из payload
        публичен, владелец — только тот, чей user_id записан в подписке."""
        subscription = self.get(subscription_id)
        if subscription is None or subscription.max_user_id != max_user_id or subscription.status == "archived":
            return None
        return subscription

    def get_for_user_and_car(self, max_user_id: int, car_number: str) -> MaxGeSubscription | None:
        row = self._conn.execute(
            f"SELECT {_FIELDS} FROM max_ge_subscriptions "
            "WHERE max_user_id = ? AND car_number = ? AND status != 'archived'",
            (max_user_id, car_number),
        ).fetchone()
        return _row(row) if row else None

    def list_by_user(self, max_user_id: int) -> list[MaxGeSubscription]:
        rows = self._conn.execute(
            f"SELECT {_FIELDS} FROM max_ge_subscriptions WHERE max_user_id = ? AND status != 'archived' ORDER BY id",
            (max_user_id,),
        ).fetchall()
        return [_row(row) for row in rows]

    def list_active(self) -> list[MaxGeSubscription]:
        rows = self._conn.execute(
            f"SELECT {_FIELDS} FROM max_ge_subscriptions WHERE status = 'active' ORDER BY id",
        ).fetchall()
        return [_row(row) for row in rows]

    def list_for_car(self, car_number: str) -> list[MaxGeSubscription]:
        rows = self._conn.execute(
            f"SELECT {_FIELDS} FROM max_ge_subscriptions WHERE car_number = ? AND status != 'archived' ORDER BY id",
            (car_number,),
        ).fetchall()
        return [_row(row) for row in rows]

    def create_pending(self, *, max_user_id: int, car_number: str, monitoring_task_id: int) -> MaxGeSubscription:
        """Новая подписка с отложенной точкой отсчёта (см. модуль). Если
        неудалённая уже есть — она же (sqlite3.IntegrityError не наружу)."""
        now = _now()
        try:
            cursor = self._conn.execute(
                "INSERT INTO max_ge_subscriptions "
                "(max_user_id, car_number, monitoring_task_id, status, baseline_at, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', ?, ?, ?)",
                (max_user_id, car_number, monitoring_task_id, fmt_ts(PENDING_BASELINE), now, now),
            )
        except sqlite3.IntegrityError:
            self._conn.rollback()
            existing = self.get_for_user_and_car(max_user_id, car_number)
            if existing is None:
                raise
            return existing
        self._conn.commit()
        return self.get(cursor.lastrowid)

    def set_baseline(self, subscription_id: int, *, at: datetime, history_final: bool) -> None:
        """history_final — первичная проверка номера уже завершена: граница
        истории фиксируется сразу (= точке отсчёта)."""
        self._conn.execute(
            "UPDATE max_ge_subscriptions SET baseline_at = ?, history_cutoff_at = ?, updated_at = ? WHERE id = ?",
            (fmt_ts(at), fmt_ts(at) if history_final else None, _now(), subscription_id),
        )
        self._conn.commit()

    def set_history_cutoff(self, subscription_id: int, *, at: datetime) -> None:
        self._conn.execute(
            "UPDATE max_ge_subscriptions SET history_cutoff_at = ? WHERE id = ? AND history_cutoff_at IS NULL",
            (fmt_ts(at), subscription_id),
        )
        self._conn.commit()

    def resolve_stale_pending(self, *, older_than: datetime, at: datetime) -> int:
        """Подписки, оставшиеся с отложенной точкой отсчёта (процесс упал
        посреди первичной проверки): всё известное к этому моменту — история."""
        cursor = self._conn.execute(
            "UPDATE max_ge_subscriptions SET baseline_at = ? WHERE baseline_at = ? AND created_at < ?",
            (fmt_ts(at), fmt_ts(PENDING_BASELINE), fmt_ts(older_than)),
        )
        self._conn.commit()
        return cursor.rowcount

    def set_status(self, subscription_id: int, status: MaxGeSubscriptionStatus) -> None:
        stopped_at = _now() if status != "active" else None
        self._conn.execute(
            "UPDATE max_ge_subscriptions SET status = ?, updated_at = ?, stopped_at = ? WHERE id = ?",
            (status, _now(), stopped_at, subscription_id),
        )
        self._conn.commit()

    def move_to_task(self, subscription_id: int, task_id: int) -> None:
        self._conn.execute(
            "UPDATE max_ge_subscriptions SET monitoring_task_id = ?, updated_at = ? WHERE id = ?",
            (task_id, _now(), subscription_id),
        )
        self._conn.commit()

    def count_by_status(self) -> dict[str, int]:
        return dict(self._conn.execute("SELECT status, COUNT(*) FROM max_ge_subscriptions GROUP BY status").fetchall())

    # ---- доставки штрафов ----

    def get_fine_delivery(self, detected_fine_id: int, subscription_id: int) -> MaxDelivery | None:
        return self._get_delivery("max_ge_fine_deliveries", "detected_fine_id", detected_fine_id, subscription_id)

    def handled_protocols(self, subscription_id: int) -> set[str]:
        """Квитанции, уже доставленные или исключённые для подписки (по одной
        доставке на квитанцию, как у Telegram-доставок)."""
        rows = self._conn.execute(
            "SELECT protocol_no FROM max_ge_fine_deliveries WHERE subscription_id = ? AND protocol_no IS NOT NULL "
            "AND (delivered_at IS NOT NULL OR excluded_at IS NOT NULL)",
            (subscription_id,),
        ).fetchall()
        return {row[0] for row in rows}

    def record_fine_attempt(self, detected_fine_id: int, subscription_id: int, protocol_no: str | None) -> None:
        self._conn.execute(
            """
            INSERT INTO max_ge_fine_deliveries
                (detected_fine_id, subscription_id, protocol_no, attempt_count, last_attempt_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT (detected_fine_id, subscription_id) DO UPDATE SET
                attempt_count = attempt_count + 1, last_attempt_at = excluded.last_attempt_at
            """,
            (detected_fine_id, subscription_id, protocol_no, _now()),
        )
        self._conn.commit()

    def mark_fine_delivered(self, detected_fine_id: int, subscription_id: int) -> None:
        self._conn.execute(
            "UPDATE max_ge_fine_deliveries SET delivered_at = ? WHERE detected_fine_id = ? AND subscription_id = ?",
            (_now(), detected_fine_id, subscription_id),
        )
        self._conn.commit()

    def exclude_fine(
        self, detected_fine_id: int, subscription_id: int, protocol_no: str | None, *, reason: str,
    ) -> None:
        """Явное исключение (история, граница, пауза): не отправляется
        никогда; уже доставленное не меняется."""
        self._conn.execute(
            """
            INSERT INTO max_ge_fine_deliveries
                (detected_fine_id, subscription_id, protocol_no, excluded_at, exclusion_reason)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (detected_fine_id, subscription_id) DO UPDATE SET
                excluded_at = excluded.excluded_at, exclusion_reason = excluded.exclusion_reason
            WHERE delivered_at IS NULL AND excluded_at IS NULL
            """,
            (detected_fine_id, subscription_id, protocol_no, _now(), reason),
        )
        self._conn.commit()

    def fine_delivery_counts(self) -> dict[str, int]:
        return self._delivery_counts("max_ge_fine_deliveries")

    # ---- доставки увеличений суммы ----

    def get_amount_delivery(self, event_id: int, subscription_id: int) -> MaxDelivery | None:
        return self._get_delivery("max_ge_amount_deliveries", "event_id", event_id, subscription_id)

    def record_amount_attempt(self, event_id: int, subscription_id: int) -> None:
        self._conn.execute(
            """
            INSERT INTO max_ge_amount_deliveries (event_id, subscription_id, attempt_count, last_attempt_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT (event_id, subscription_id) DO UPDATE SET
                attempt_count = attempt_count + 1, last_attempt_at = excluded.last_attempt_at
            """,
            (event_id, subscription_id, _now()),
        )
        self._conn.commit()

    def mark_amount_delivered(self, event_id: int, subscription_id: int) -> None:
        self._conn.execute(
            "UPDATE max_ge_amount_deliveries SET delivered_at = ? WHERE event_id = ? AND subscription_id = ?",
            (_now(), event_id, subscription_id),
        )
        self._conn.commit()

    def exclude_amount(self, event_id: int, subscription_id: int, *, reason: str) -> None:
        self._conn.execute(
            """
            INSERT INTO max_ge_amount_deliveries (event_id, subscription_id, excluded_at, exclusion_reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (event_id, subscription_id) DO UPDATE SET
                excluded_at = excluded.excluded_at, exclusion_reason = excluded.exclusion_reason
            WHERE delivered_at IS NULL AND excluded_at IS NULL
            """,
            (event_id, subscription_id, _now(), reason),
        )
        self._conn.commit()

    def amount_delivery_counts(self) -> dict[str, int]:
        return self._delivery_counts("max_ge_amount_deliveries")

    # ---- общее ----

    def _get_delivery(self, table: str, key: str, key_value: int, subscription_id: int) -> MaxDelivery | None:
        row = self._conn.execute(
            f"SELECT attempt_count, last_attempt_at, delivered_at, excluded_at, exclusion_reason FROM {table} "
            f"WHERE {key} = ? AND subscription_id = ?",
            (key_value, subscription_id),
        ).fetchone()
        if row is None:
            return None
        return MaxDelivery(
            attempt_count=row[0], last_attempt_at=parse_ts(row[1]), delivered_at=parse_ts(row[2]),
            excluded_at=parse_ts(row[3]), exclusion_reason=row[4],
        )

    def _delivery_counts(self, table: str) -> dict[str, int]:
        row = self._conn.execute(
            f"SELECT SUM(delivered_at IS NOT NULL), SUM(excluded_at IS NOT NULL), "
            f"SUM(delivered_at IS NULL AND excluded_at IS NULL) FROM {table}",
        ).fetchone()
        return {"delivered": row[0] or 0, "excluded": row[1] or 0, "pending": row[2] or 0}
