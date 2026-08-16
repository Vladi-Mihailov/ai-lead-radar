"""Единственный кусок checkout, который переживает перезапуск процесса —
не полное состояние (см. задачу: "не притворяйся, что browser session можно
восстановить"), а минимальная запись "по этому Telegram-сообщению уже что-то
начато, дальше <статус>" — ровно то, что нужно, чтобы после restart второй
"pay" не создал вторую policy/платёж (см. задачу, раздел 7).

sqlite, тот же стиль, что и reader/fines/task_repository.py/
reader/users/repository.py (WAL, синхронный sqlite3 внутри async-методов —
тот же приём, что и в остальном проекте, объём данных исчезающе мал)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

# ":memory:" — специальное значение sqlite3 (не файл на диске) — используется
# тестами (см. tests/test_checkout_service.py), в production всегда реальный
# путь (см. reader/main.py — тот же users_db_file, что и у остальных
# repository проекта).
_MEMORY_DB = ":memory:"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkout_locks (
    chat_id         INTEGER NOT NULL,
    ocr_message_id  INTEGER NOT NULL,
    checkout_id     TEXT NOT NULL,
    status          TEXT NOT NULL,
    failure_reason  TEXT,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, ocr_message_id)
)
"""

_UPSERT = """
INSERT INTO checkout_locks (chat_id, ocr_message_id, checkout_id, status, failure_reason, updated_at)
VALUES (:chat_id, :ocr_message_id, :checkout_id, :status, :failure_reason, CURRENT_TIMESTAMP)
ON CONFLICT(chat_id, ocr_message_id) DO UPDATE SET
    checkout_id = excluded.checkout_id,
    status = excluded.status,
    failure_reason = excluded.failure_reason,
    updated_at = CURRENT_TIMESTAMP
"""

_SELECT = """
SELECT checkout_id, status, failure_reason FROM checkout_locks
WHERE chat_id = ? AND ocr_message_id = ?
"""


@dataclass(frozen=True)
class CheckoutLock:
    checkout_id: str
    status: str
    failure_reason: str | None


class CheckoutLockRepository:
    def __init__(self, db_path: Path | str):
        is_memory = db_path == _MEMORY_DB
        self._path = db_path if is_memory else Path(db_path)
        self._conn = sqlite3.connect(str(self._path))
        if not is_memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def get(self, chat_id: int, ocr_message_id: int) -> CheckoutLock | None:
        row = self._conn.execute(_SELECT, (chat_id, ocr_message_id)).fetchone()
        if row is None:
            return None
        return CheckoutLock(checkout_id=row[0], status=row[1], failure_reason=row[2])

    def upsert(
        self, *, chat_id: int, ocr_message_id: int, checkout_id: str, status: str, failure_reason: str | None,
    ) -> None:
        self._conn.execute(
            _UPSERT,
            {
                "chat_id": chat_id,
                "ocr_message_id": ocr_message_id,
                "checkout_id": checkout_id,
                "status": status,
                "failure_reason": failure_reason,
            },
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
