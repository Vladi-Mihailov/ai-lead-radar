"""TurkeyUnifiedMigrationNotificationRepository — turkey_unified_migration_
notifications поверх SQLite (см. задачу "Перенос Unified Turkey
функционала в production" п.8 — persistent-отметка о доставке one-off
уведомления существующим пользователям после production deployment).

UNIQUE(telegram_user_id) — КАЖДЫЙ пользователь уведомляется максимум ОДИН
раз, даже при повторном запуске notification-скрипта (см.
scripts/notify_turkey_unified_migration.py). Отметка пишется ТОЛЬКО ПОСЛЕ
успешной отправки (см. reader/turkey_bot/migration_notify.py::notify_all —
mark_notified() вызывается СТРОГО после успешного await
sender.send_message(...), сбой переходит в except и никогда не доходит до
mark_notified())."""

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turkey_unified_migration_notifications (
    telegram_user_id  INTEGER PRIMARY KEY,
    notified_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


class TurkeyUnifiedMigrationNotificationRepository:
    def __init__(self, db_path: Path | str):
        is_memory = db_path == ":memory:"
        self._path = db_path if is_memory else Path(db_path)
        if not is_memory:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        if not is_memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def is_notified(self, telegram_user_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM turkey_unified_migration_notifications WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        return row is not None

    def mark_notified(self, telegram_user_id: int) -> None:
        """INSERT OR IGNORE — гоночный повторный вызов для уже отмеченного
        пользователя НЕ обновляет notified_at (первая успешная отправка —
        источник истины, см. модуль docstring)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO turkey_unified_migration_notifications (telegram_user_id) VALUES (?)",
            (telegram_user_id,),
        )
        self._conn.commit()

    def count_notified(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM turkey_unified_migration_notifications",
        ).fetchone()[0]

    def close(self) -> None:
        self._conn.close()
