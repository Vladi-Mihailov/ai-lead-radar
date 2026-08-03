import sqlite3
from datetime import date, datetime
from pathlib import Path

from reader.fines.models import FineMonitoringTask, FineTaskStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fine_monitoring_tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    car_number          TEXT NOT NULL,
    label               TEXT,
    start_date          TEXT NOT NULL,
    end_date            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    telegram_chat_id    INTEGER NOT NULL,
    created_by_user_id  INTEGER NOT NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_checked_at     TIMESTAMP,
    last_check_status   TEXT,
    last_error          TEXT
)
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_fine_tasks_car_status
    ON fine_monitoring_tasks (car_number, status)
"""

_INSERT = """
INSERT INTO fine_monitoring_tasks (
    car_number, label, start_date, end_date, status,
    telegram_chat_id, created_by_user_id
) VALUES (
    :car_number, :label, :start_date, :end_date, 'active',
    :telegram_chat_id, :created_by_user_id
)
"""

_SELECT_FIELDS = """
    id, car_number, label, start_date, end_date, status,
    telegram_chat_id, created_by_user_id, created_at, updated_at,
    last_checked_at, last_check_status, last_error
"""

_SELECT_BY_ID = f"SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks WHERE id = ?"

_SELECT_ACTIVE = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks WHERE status = 'active'
"""

_SELECT_ACTIVE_BY_CAR = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks
    WHERE car_number = ? AND status = 'active'
"""

_UPDATE_STATUS = """
UPDATE fine_monitoring_tasks
SET status = ?, updated_at = CURRENT_TIMESTAMP
WHERE id = ?
"""

_UPDATE_CHECK_RESULT = """
UPDATE fine_monitoring_tasks
SET last_checked_at = CURRENT_TIMESTAMP,
    last_check_status = :last_check_status,
    last_error = :last_error,
    updated_at = CURRENT_TIMESTAMP
WHERE id = :task_id
"""

_COUNT_ACTIVE = "SELECT COUNT(*) FROM fine_monitoring_tasks WHERE status = 'active'"


def _row_to_task(row) -> FineMonitoringTask:
    (
        id_,
        car_number,
        label,
        start_date,
        end_date,
        status,
        telegram_chat_id,
        created_by_user_id,
        created_at,
        updated_at,
        last_checked_at,
        last_check_status,
        last_error,
    ) = row

    return FineMonitoringTask(
        id=id_,
        car_number=car_number,
        label=label,
        start_date=date.fromisoformat(start_date),
        end_date=date.fromisoformat(end_date),
        status=status,
        telegram_chat_id=telegram_chat_id,
        created_by_user_id=created_by_user_id,
        created_at=datetime.fromisoformat(created_at),
        updated_at=datetime.fromisoformat(updated_at),
        last_checked_at=datetime.fromisoformat(last_checked_at) if last_checked_at else None,
        last_check_status=last_check_status,
        last_error=last_error,
    )


class FineMonitoringTaskRepository:
    """Задачи мониторинга штрафов (fine_monitoring_tasks) поверх SQLite.

    Та же БД, что и у UserRepository/HistorySyncStateRepository
    (settings.app.users_db_file) — отдельная таблица, отдельное соединение,
    по тому же образцу, что и остальные репозитории проекта. Нормализация
    номера/дат — забота вызывающего кода (validation.py), не репозитория.
    """

    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(_SCHEMA)
        self._conn.execute(_INDEX)
        self._conn.commit()

    def create(
        self,
        *,
        car_number: str,
        label: str | None,
        start_date: date,
        end_date: date,
        telegram_chat_id: int,
        created_by_user_id: int,
    ) -> FineMonitoringTask:
        cursor = self._conn.execute(
            _INSERT,
            {
                "car_number": car_number,
                "label": label,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "telegram_chat_id": telegram_chat_id,
                "created_by_user_id": created_by_user_id,
            },
        )
        self._conn.commit()

        task = self.get(cursor.lastrowid)
        if task is None:
            raise RuntimeError("Не удалось прочитать только что созданную задачу мониторинга")
        return task

    def get(self, task_id: int) -> FineMonitoringTask | None:
        row = self._conn.execute(_SELECT_BY_ID, (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def list_active(self) -> list[FineMonitoringTask]:
        rows = self._conn.execute(_SELECT_ACTIVE).fetchall()
        return [_row_to_task(row) for row in rows]

    def get_active_by_car_number(self, car_number: str) -> list[FineMonitoringTask]:
        rows = self._conn.execute(_SELECT_ACTIVE_BY_CAR, (car_number,)).fetchall()
        return [_row_to_task(row) for row in rows]

    def set_status(self, task_id: int, status: FineTaskStatus) -> None:
        self._conn.execute(_UPDATE_STATUS, (status, task_id))
        self._conn.commit()

    def record_check_result(
        self, task_id: int, *, last_check_status: str, last_error: str | None
    ) -> None:
        self._conn.execute(
            _UPDATE_CHECK_RESULT,
            {
                "task_id": task_id,
                "last_check_status": last_check_status,
                "last_error": last_error,
            },
        )
        self._conn.commit()

    def count_active(self) -> int:
        return self._conn.execute(_COUNT_ACTIVE).fetchone()[0]

    def close(self) -> None:
        self._conn.close()
