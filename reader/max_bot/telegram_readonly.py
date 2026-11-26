"""Чтение Telegram-данных из MAX-процессов — ТОЛЬКО через read-only
соединение SQLite (mode=ro): перенос машин после привязки аккаунтов и счётчик
Telegram-подписчиков на экране менеджера. Записать в Telegram-таблицы этим
соединением невозможно, их DDL/миграции здесь тоже не запускаются.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _readonly(db_path: Path | str) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=30)


class TelegramReadOnly:
    def __init__(self, db_path: Path | str):
        self._conn = _readonly(db_path)

    def close(self) -> None:
        self._conn.close()

    def _rows(self, sql: str, params: tuple) -> list[tuple]:
        try:
            return self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Таблицы ещё нет (Telegram-бот на этой БД не запускался).
            return []

    # ---- Грузия ----

    def georgia_active_cars(self, telegram_user_id: int) -> list[str]:
        rows = self._rows(
            "SELECT DISTINCT car_number FROM fine_monitoring_subscriptions "
            "WHERE telegram_user_id = ? AND status = 'active' ORDER BY car_number",
            (telegram_user_id,),
        )
        return [row[0] for row in rows]

    def georgia_subscriber_count(self, car_number: str) -> int:
        rows = self._rows(
            "SELECT COUNT(*) FROM fine_monitoring_subscriptions "
            "WHERE car_number = ? AND status IN ('active', 'pending_claim')",
            (car_number,),
        )
        return rows[0][0] if rows else 0

    # ---- Турция ----

    def turkey_cars(self, telegram_user_id: int) -> list[tuple[str, bool]]:
        """(номер, мониторинг включён) машин Telegram-пользователя."""
        rows = self._rows(
            "SELECT c.car_number, COALESCE(s.active, 0) FROM turkey_bot_user_cars c "
            "LEFT JOIN turkey_monitoring_subscriptions s "
            "  ON s.telegram_user_id = c.telegram_user_id AND s.plate = c.car_number "
            "WHERE c.telegram_user_id = ? ORDER BY c.car_number",
            (telegram_user_id,),
        )
        return [(row[0], bool(row[1])) for row in rows]

    def turkey_subscriber_count(self, plate: str) -> int:
        rows = self._rows(
            "SELECT COUNT(*) FROM turkey_monitoring_subscriptions WHERE plate = ? AND active = 1", (plate,),
        )
        return rows[0][0] if rows else 0
