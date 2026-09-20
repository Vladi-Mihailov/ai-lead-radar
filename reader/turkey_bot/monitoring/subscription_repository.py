"""TurkeyMonitoringSubscriptionRepository — turkey_monitoring_subscriptions
поверх SQLite (см. design report п.9 задачи "Перестроить UX Turkey test
bot"). Проще Georgian fine_monitoring_subscriptions намеренно (см. design
report решение п.3) — без периодов/дат окончания: просто ON/OFF toggle на
пару (telegram_user_id, plate), а не time-boxed подписка.

UNIQUE(telegram_user_id, plate) — тот же принцип дедупликации, что и
turkey_bot_user_cars (одна подписка на пару пользователь+номер)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turkey_monitoring_subscriptions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id  INTEGER NOT NULL,
    telegram_chat_id  INTEGER NOT NULL,
    plate             TEXT NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_checked_at   TIMESTAMP,
    next_check_at     TIMESTAMP,
    UNIQUE (telegram_user_id, plate)
);
CREATE INDEX IF NOT EXISTS idx_turkey_monitoring_active
    ON turkey_monitoring_subscriptions(active, next_check_at);
"""


@dataclass(frozen=True)
class TurkeyMonitoringSubscription:
    id: int
    telegram_user_id: int
    telegram_chat_id: int
    plate: str
    active: bool
    created_at: datetime
    updated_at: datetime
    last_checked_at: datetime | None
    next_check_at: datetime | None


def _row_to_subscription(row) -> TurkeyMonitoringSubscription:
    (sub_id, telegram_user_id, telegram_chat_id, plate, active,
     created_at, updated_at, last_checked_at, next_check_at) = row
    return TurkeyMonitoringSubscription(
        id=sub_id, telegram_user_id=telegram_user_id, telegram_chat_id=telegram_chat_id,
        plate=plate, active=bool(active),
        created_at=datetime.fromisoformat(created_at), updated_at=datetime.fromisoformat(updated_at),
        last_checked_at=datetime.fromisoformat(last_checked_at) if last_checked_at else None,
        next_check_at=datetime.fromisoformat(next_check_at) if next_check_at else None,
    )


_COLUMNS = (
    "id, telegram_user_id, telegram_chat_id, plate, active, "
    "created_at, updated_at, last_checked_at, next_check_at"
)


class TurkeyMonitoringSubscriptionRepository:
    def __init__(self, db_path: Path | str):
        is_memory = db_path == ":memory:"
        self._path = db_path if is_memory else Path(db_path)
        if not is_memory:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        if not is_memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def enable(
        self, *, telegram_user_id: int, telegram_chat_id: int, plate: str,
        next_check_at: datetime | None = None,
    ) -> TurkeyMonitoringSubscription:
        self._conn.execute(
            "INSERT INTO turkey_monitoring_subscriptions "
            "(telegram_user_id, telegram_chat_id, plate, active, next_check_at) "
            "VALUES (:user_id, :chat_id, :plate, 1, :next_check_at) "
            "ON CONFLICT(telegram_user_id, plate) DO UPDATE SET "
            "  active = 1, telegram_chat_id = excluded.telegram_chat_id, "
            "  next_check_at = COALESCE(turkey_monitoring_subscriptions.next_check_at, excluded.next_check_at), "
            "  updated_at = CURRENT_TIMESTAMP",
            {
                "user_id": telegram_user_id, "chat_id": telegram_chat_id, "plate": plate,
                "next_check_at": next_check_at.isoformat() if next_check_at else None,
            },
        )
        self._conn.commit()
        return self.get(telegram_user_id=telegram_user_id, plate=plate)

    def disable(self, *, telegram_user_id: int, plate: str) -> None:
        self._conn.execute(
            "UPDATE turkey_monitoring_subscriptions SET active = 0, updated_at = CURRENT_TIMESTAMP "
            "WHERE telegram_user_id = ? AND plate = ?",
            (telegram_user_id, plate),
        )
        self._conn.commit()

    def deactivate_all(self) -> int:
        """⛔ Остановить мониторинг (manager) — ТОЛЬКО Turkey test monitoring
        (своя таблица/БД, физически не может задеть Georgian) — возвращает
        число реально выключенных подписок."""
        cursor = self._conn.execute(
            "UPDATE turkey_monitoring_subscriptions SET active = 0, updated_at = CURRENT_TIMESTAMP "
            "WHERE active = 1",
        )
        self._conn.commit()
        return cursor.rowcount

    def get(self, *, telegram_user_id: int, plate: str) -> TurkeyMonitoringSubscription | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM turkey_monitoring_subscriptions "
            "WHERE telegram_user_id = ? AND plate = ?",
            (telegram_user_id, plate),
        ).fetchone()
        return _row_to_subscription(row) if row else None

    def list_due(self, *, now: datetime) -> list[TurkeyMonitoringSubscription]:
        """Активные подписки, которым пора проверяться (next_check_at <=
        now ИЛИ ещё не назначено) — используется TurkeyMonitoringJob (см.
        reader/turkey_bot/monitoring/scheduler_job.py)."""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM turkey_monitoring_subscriptions "
            "WHERE active = 1 AND (next_check_at IS NULL OR next_check_at <= ?)",
            (now.astimezone(timezone.utc).isoformat(),),
        ).fetchall()
        return [_row_to_subscription(row) for row in rows]

    def mark_checked(self, subscription_id: int, *, checked_at: datetime, next_check_at: datetime) -> None:
        self._conn.execute(
            "UPDATE turkey_monitoring_subscriptions SET "
            "last_checked_at = ?, next_check_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (checked_at.isoformat(), next_check_at.isoformat(), subscription_id),
        )
        self._conn.commit()

    def count_active(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM turkey_monitoring_subscriptions WHERE active = 1",
        ).fetchone()[0]

    def close(self) -> None:
        self._conn.close()
