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
    last_error          TEXT,
    archive_check_enabled INTEGER NOT NULL DEFAULT 0,
    next_archive_check_at TIMESTAMP
)
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_fine_tasks_car_status
    ON fine_monitoring_tasks (car_number, status)
"""

_ARCHIVE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_fine_tasks_archive_due
    ON fine_monitoring_tasks (archive_check_enabled, next_archive_check_at)
"""

# CREATE TABLE IF NOT EXISTS не добавляет колонки в уже существующую
# таблицу — для баз, созданных до появления архивного режима, добавляем их
# явно при открытии (тот же приём, что и UserRepository._COLUMN_MIGRATIONS/
# _migrate_missing_columns), без удаления/пересоздания БД. DEFAULT 0 у
# archive_check_enabled и NULL у next_archive_check_at для УЖЕ
# существующих строк сохраняют их текущее поведение бит в бит — ни одна
# задача не попадает в архивный режим сама по себе только из-за миграции.
_COLUMN_MIGRATIONS = {
    "archive_check_enabled": (
        "ALTER TABLE fine_monitoring_tasks "
        "ADD COLUMN archive_check_enabled INTEGER NOT NULL DEFAULT 0"
    ),
    "next_archive_check_at": (
        "ALTER TABLE fine_monitoring_tasks ADD COLUMN next_archive_check_at TIMESTAMP"
    ),
}

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
    last_checked_at, last_check_status, last_error,
    archive_check_enabled, next_archive_check_at
"""

_SELECT_BY_ID = f"SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks WHERE id = ?"

_SELECT_ACTIVE = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks WHERE status = 'active'
"""

_SELECT_ACTIVE_BY_CAR = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks
    WHERE car_number = ? AND status = 'active'
"""

_SELECT_DUE_FOR_ARCHIVE_CHECK = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks
    WHERE archive_check_enabled = 1
      AND next_archive_check_at IS NOT NULL
      AND next_archive_check_at <= :now
    ORDER BY next_archive_check_at ASC, id ASC
    LIMIT :limit
"""

_SELECT_COMPLETED_NOT_ARCHIVED = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks
    WHERE status = 'completed' AND archive_check_enabled = 0
    ORDER BY id ASC
"""

_SELECT_ARCHIVE_ENROLLMENT_CANDIDATES = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks
    WHERE archive_check_enabled = 0
      AND (
        status = 'completed'
        OR (status = 'active' AND end_date < :today)
      )
    ORDER BY id ASC
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

_UPDATE_SCHEDULE_ARCHIVE_CHECK = """
UPDATE fine_monitoring_tasks
SET archive_check_enabled = 1,
    next_archive_check_at = :next_archive_check_at,
    updated_at = CURRENT_TIMESTAMP
WHERE id = :id
"""

_UPDATE_RETURN_TO_ACTIVE = """
UPDATE fine_monitoring_tasks
SET status = 'active',
    start_date = :start_date,
    end_date = :end_date,
    archive_check_enabled = 0,
    next_archive_check_at = NULL,
    updated_at = CURRENT_TIMESTAMP
WHERE id = :id
"""


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
        archive_check_enabled,
        next_archive_check_at,
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
        archive_check_enabled=bool(archive_check_enabled),
        next_archive_check_at=(
            datetime.fromisoformat(next_archive_check_at) if next_archive_check_at else None
        ),
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
        self._migrate_missing_columns()
        self._conn.execute(_INDEX)
        self._conn.execute(_ARCHIVE_INDEX)
        self._conn.commit()

    def _migrate_missing_columns(self) -> None:
        """CREATE TABLE IF NOT EXISTS не добавляет колонки в уже
        существующую таблицу — для баз, созданных до появления архивного
        режима, добавляем недостающие явно, без удаления/пересоздания БД."""
        existing_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(fine_monitoring_tasks)")
        }
        for column, statement in _COLUMN_MIGRATIONS.items():
            if column not in existing_columns:
                self._conn.execute(statement)

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

    def get_many(self, task_ids: list[int]) -> list[FineMonitoringTask]:
        """Пакетная выборка по произвольному списку id — используется
        enrollment-сценарием (см. reader/fines/archive_enrollment.py),
        которому на вход подаётся диапазон/список id, а не хардкод в коде
        приложения (сам список формирует вызывающий CLI/оператор)."""
        if not task_ids:
            return []

        placeholders = ",".join("?" for _ in task_ids)
        rows = self._conn.execute(
            f"SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks WHERE id IN ({placeholders})",
            list(task_ids),
        ).fetchall()
        return [_row_to_task(row) for row in rows]

    def list_completed_not_archived(self) -> list[FineMonitoringTask]:
        """Задачи с истёкшим обычным периодом (status='completed'),
        которые ещё ни разу не были поставлены в архивный режим —
        "безопасный" кандидатный пул для явного enrollment (см. докстрок
        reader/fines/archive_enrollment.py про то, почему это НЕ делается
        автоматически для уже существующих записей)."""
        rows = self._conn.execute(_SELECT_COMPLETED_NOT_ARCHIVED).fetchall()
        return [_row_to_task(row) for row in rows]

    def list_archive_enrollment_candidates(self, today: date) -> list[FineMonitoringTask]:
        """Как list_completed_not_archived(), но дополнительно включает
        задачи со status='active', чей end_date уже строго меньше today —
        период фактически истёк, но ни один прогон FineJob ещё не перевёл
        их в 'completed' (например, партия задач, вставленная напрямую в
        БД со status='active' и уже прошедшим end_date — см. инцидент с
        production-партией id 144..1141). Используется --all-completed в
        reader/fines/enroll_archive.py; сам факт перевода active ->
        completed для таких задач делает enroll_tasks_in_archive_mode(),
        не этот метод (репозиторий остаётся только чтением)."""
        rows = self._conn.execute(
            _SELECT_ARCHIVE_ENROLLMENT_CANDIDATES, {"today": today.isoformat()}
        ).fetchall()
        return [_row_to_task(row) for row in rows]

    def list_due_for_archive_check(self, now: datetime, limit: int) -> list[FineMonitoringTask]:
        """Задачи в архивном режиме, чей next_archive_check_at уже наступил
        (<= now), в порядке next_archive_check_at ASC, id ASC — сначала
        самые просроченные, при равенстве — детерминированно по id.
        limit — safety limit на один проход ArchiveFineJob (см.
        settings.fine_monitor.archive_daily_limit): backlog сверх лимита
        остаётся due и просто попадёт в следующий запуск, ничего не теряя
        и не выбираясь повторно внутри одного вызова."""
        rows = self._conn.execute(
            _SELECT_DUE_FOR_ARCHIVE_CHECK, {"now": now.isoformat(), "limit": limit}
        ).fetchall()
        return [_row_to_task(row) for row in rows]

    def enroll_in_archive_mode(self, schedule: dict[int, datetime]) -> None:
        """Массово выставляет archive_check_enabled=1 и next_archive_check_at
        по уже готовому расписанию {task_id: next_archive_check_at} (см.
        reader/fines/archive_scheduling.py — вычисление расписания находится
        там, а не здесь: репозиторий только пишет). Ничего не проверяет
        (status/уже включённый архивный режим и т.п.) — это ответственность
        вызывающего кода (см. enroll_tasks_in_archive_mode)."""
        if not schedule:
            return

        self._conn.executemany(
            _UPDATE_SCHEDULE_ARCHIVE_CHECK,
            [
                {"id": task_id, "next_archive_check_at": next_check_at.isoformat()}
                for task_id, next_check_at in schedule.items()
            ],
        )
        self._conn.commit()

    def schedule_first_archive_check(self, task_id: int, *, next_check_at: datetime) -> None:
        """Как enroll_in_archive_mode(), но для ОДНОЙ задачи — используется
        FineJob сразу после того, как обычный период только что завершился
        (см. reader/jobs/fine_job.py), чтобы новые задачи автоматически
        получали первую архивную проверку без отдельного enrollment-шага."""
        self._conn.execute(
            _UPDATE_SCHEDULE_ARCHIVE_CHECK,
            {"id": task_id, "next_archive_check_at": next_check_at.isoformat()},
        )
        self._conn.commit()

    def reschedule_next_archive_check(self, task_id: int, *, next_check_at: datetime) -> None:
        """Сдвигает next_archive_check_at на следующий срок после успешной
        архивной проверки без новых штрафов — archive_check_enabled
        остаётся 1 (задача остаётся в архивном режиме)."""
        self._conn.execute(
            "UPDATE fine_monitoring_tasks SET next_archive_check_at = :next_archive_check_at, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = :id",
            {"id": task_id, "next_archive_check_at": next_check_at.isoformat()},
        )
        self._conn.commit()

    def return_to_active_monitoring(
        self, task_id: int, *, start_date: date, end_date: date
    ) -> None:
        """Архивная проверка нашла новый штраф — задача возвращается в
        обычный частый мониторинг: status='active' (снова попадает в
        list_active()/3 проверки в день), новый период [start_date,
        end_date], архивный режим выключен (archive_check_enabled=0,
        next_archive_check_at=NULL) до тех пор, пока этот новый период сам
        не завершится и задача снова не станет кандидатом на архив."""
        self._conn.execute(
            _UPDATE_RETURN_TO_ACTIVE,
            {
                "id": task_id,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
