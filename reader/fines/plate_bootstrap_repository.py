"""Per-PHYSICAL-plate bootstrap state (см. задачу "Georgia fines
incremental public-feed monitoring") — НЕ per-task/per-subscription:
audit подтвердил (production: car_number=O528HY702 имеет 2 одновременно
активные fine_monitoring_tasks, см. FineMonitoringTaskRepository.
list_debt_car_groups() — группировка по car_number существует именно
потому, что одна физическая машина может иметь несколько задач), поэтому
bootstrap-прогресс хранится по car_number, а НЕ привязан к конкретной
задаче — иначе вторая подписка на уже bootstrapped машину заново
считалась бы "новой" и получила бы ложный per-car-опрос обратно.

car_number — УЖЕ нормализованный (reader/fines/validation.py::
normalize_car_number()) канонический номер, тот же вид, в каком он
хранится в fine_monitoring_tasks.car_number (нормализация происходит ДО
создания задачи везде в проекте, см. reader/commands/fine.py/
reader/public_bot/subscription_service.py) — этот репозиторий сам
повторно не нормализует, доверяет вызывающему коду.

bootstrap_status:
- 'pending'   — ещё не было НИ ОДНОГО успешного full-history searchByAuto
  для этой машины (новая машина, ИЛИ police.ge был недоступен в момент
  добавления — см. задачу: "Do NOT falsely mark completed").
- 'completed' — full-history lookup выполнен хотя бы раз успешно;
  scheduled FineJob больше не должен делать по ней обычный per-car
  поиск (см. reader/jobs/fine_job.py) — дальше она отслеживается
  ИСКЛЮЧИТЕЛЬНО через публичный feed (см.
  reader/jobs/georgia_public_feed_job.py)."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS georgia_plate_bootstrap_state (
    car_number              TEXT PRIMARY KEY,
    bootstrap_status        TEXT NOT NULL DEFAULT 'pending',
    bootstrap_completed_at  TIMESTAMP,
    created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# "ВРЕМЕННЫЙ manager workflow" (см. задачу) — единственная недостающая
# audit-колонка: КТО (Telegram user id менеджера) подтвердил bootstrap
# ('нет штрафов' или 'все штрафы внесены'), а не только когда
# (bootstrap_completed_at уже существовал и для этого годится без правок).
# ALTER TABLE ... ADD COLUMN, guarded по тому же идиому, что и
# FineMonitoringTaskRepository._migrate_missing_columns — существующие БД
# обновляются на лету, без отдельного migration-скрипта. NULL для уже
# существующих строк и для автоматического (не-manual) completion-пути
# (reader/public_bot/subscription_service.py::add_car) — ТАМ действие
# выполнил не человек, а генуинно успешный live searchByAuto.
_COLUMN_MIGRATIONS = {
    "bootstrap_completed_by_user_id": (
        "ALTER TABLE georgia_plate_bootstrap_state ADD COLUMN bootstrap_completed_by_user_id INTEGER"
    ),
}

_SELECT = (
    "SELECT car_number, bootstrap_status, bootstrap_completed_at, created_at, "
    "bootstrap_completed_by_user_id "
    "FROM georgia_plate_bootstrap_state WHERE car_number = ?"
)

# INSERT OR IGNORE — идемпотентно: если строка уже существует (pending ИЛИ
# completed), эта команда ничего не меняет (см. докстрок модуля: вторая
# подписка на уже bootstrapped машину НЕ должна откатить её на pending).
_ENSURE_PENDING = (
    "INSERT OR IGNORE INTO georgia_plate_bootstrap_state (car_number, bootstrap_status) "
    "VALUES (:car_number, 'pending')"
)

_MARK_COMPLETED = (
    "UPDATE georgia_plate_bootstrap_state SET bootstrap_status = 'completed', "
    "bootstrap_completed_at = :completed_at, "
    "bootstrap_completed_by_user_id = :completed_by_user_id "
    "WHERE car_number = :car_number"
)

_LIST_PENDING = (
    "SELECT car_number FROM georgia_plate_bootstrap_state WHERE bootstrap_status = 'pending'"
)


@dataclass(frozen=True)
class PlateBootstrapState:
    car_number: str
    bootstrap_status: str
    bootstrap_completed_at: datetime | None
    created_at: datetime
    # Audit trail (см. задачу "ВРЕМЕННЫЙ manager workflow") — Telegram user
    # id менеджера, подтвердившего bootstrap вручную. None и для ещё не
    # завершённых строк, и для строк, завершённых автоматическим живым
    # searchByAuto (а не менеджером) — см. mark_completed() докстрок.
    bootstrap_completed_by_user_id: int | None = None

    @property
    def is_completed(self) -> bool:
        return self.bootstrap_status == STATUS_COMPLETED


def _row_to_state(row) -> PlateBootstrapState:
    car_number, status, completed_at, created_at, completed_by_user_id = row
    return PlateBootstrapState(
        car_number=car_number,
        bootstrap_status=status,
        bootstrap_completed_at=datetime.fromisoformat(completed_at) if completed_at else None,
        created_at=datetime.fromisoformat(created_at),
        bootstrap_completed_by_user_id=completed_by_user_id,
    )


class GeorgiaPlateBootstrapRepository:
    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._migrate_missing_columns()
        self._conn.commit()

    def _migrate_missing_columns(self) -> None:
        existing_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(georgia_plate_bootstrap_state)")
        }
        for column, statement in _COLUMN_MIGRATIONS.items():
            if column not in existing_columns:
                self._conn.execute(statement)

    def get(self, car_number: str) -> PlateBootstrapState | None:
        row = self._conn.execute(_SELECT, (car_number,)).fetchone()
        return _row_to_state(row) if row else None

    def ensure_pending(self, car_number: str) -> PlateBootstrapState:
        """Идемпотентно создаёт запись со статусом 'pending', если её ещё
        нет вовсе — НИКОГДА не трогает уже существующую строку (см.
        докстрок класса). Вызывается на КАЖДОЕ Add Car (self-service),
        независимо от того, новая это машина или уже известная."""
        self._conn.execute(_ENSURE_PENDING, {"car_number": car_number})
        self._conn.commit()
        return self.get(car_number)

    def mark_completed(
        self, car_number: str, *, completed_at: datetime, completed_by_user_id: int | None = None,
    ) -> None:
        """Вызывается ИСКЛЮЧИТЕЛЬНО после ГЕНУИННО успешного живого
        searchByAuto (см. CheckResult.from_cache — НЕ после emergency-mode
        чтения из БД, см. задачу: "Do NOT falsely mark completed") ИЛИ после
        явного подтверждения менеджера в "ВРЕМЕННЫЙ manager workflow" (см.
        reader/public_bot/manual_bootstrap_service.py) — completed_by_user_id
        различает эти два пути (None = автоматический live-check, задан =
        Telegram user id менеджера, подтвердившего проверку вручную). Нет
        строки для car_number — не создаёт её (ensure_pending должен был
        быть вызван раньше, см. add_car()) — UPDATE на несуществующую
        строку просто ничего не делает, без ошибки."""
        self._conn.execute(
            _MARK_COMPLETED,
            {
                "car_number": car_number,
                "completed_at": completed_at.isoformat(),
                "completed_by_user_id": completed_by_user_id,
            },
        )
        self._conn.commit()

    def is_completed(self, car_number: str) -> bool:
        state = self.get(car_number)
        return state is not None and state.is_completed

    def list_pending(self) -> list[str]:
        rows = self._conn.execute(_LIST_PENDING).fetchall()
        return [row[0] for row in rows]

    def close(self) -> None:
        self._conn.close()
