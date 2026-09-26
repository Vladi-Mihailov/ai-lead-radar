import sqlite3
from datetime import date, datetime
from pathlib import Path

from reader.fines.models import (
    FineMonitoringScope,
    FineMonitoringTask,
    FineTaskDebtSnapshot,
    FineTaskStatus,
)

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
    next_archive_check_at TIMESTAMP,
    monitoring_scope    TEXT NOT NULL DEFAULT 'operator',
    last_successful_checked_at TIMESTAMP,
    last_successful_total_amount REAL
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
    # monitoring_scope (см. reader/fines/models.py::FineMonitoringScope) —
    # DEFAULT 'operator' присваивает существующим строкам ровно то
    # поведение, которое они и так имели (единственное фоновое расписание
    # до появления @GEShtrafbot, см. design) — миграция сама по себе никого
    # не переводит в 'client_bot'.
    "monitoring_scope": (
        "ALTER TABLE fine_monitoring_tasks "
        "ADD COLUMN monitoring_scope TEXT NOT NULL DEFAULT 'operator'"
    ),
    # Последний УСПЕШНЫЙ (last_check_status == 'ok') прогон check_task() —
    # ОТДЕЛЬНО от last_checked_at (который перезаписывается и на ошибке
    # тоже, см. record_check_result). Нужен manager "🔄 Обновить
    # задолженности" (см. reader/public_bot/debt_refresh_service.py): при
    # ERROR это поле НЕ трогается, поэтому оно всегда показывает момент
    # последнего ДОСТОВЕРНОГО состояния, а не последней попытки. NULL для
    # существующих строк — ни одна задача не считается "только что
    # проверенной" сама по себе из-за миграции.
    "last_successful_checked_at": (
        "ALTER TABLE fine_monitoring_tasks ADD COLUMN last_successful_checked_at TIMESTAMP"
    ),
    # Сумма последней УСПЕШНОЙ проверки (см. задачу "manager Statistics /
    # refresh для обоих ботов" п.3/п.10) — authoritative источник "🚨
    # Штрафы по последней проверке", пишется ТОЛЬКО FineCheckService.
    # check_task(). NULL для существующих строк ПРИНЦИПИАЛЬНО — миграция
    # намеренно НЕ backfill'ит его из старой detected_fines-суммы (задача
    # явно требует "не выдавать старую historical detected_fines-историю
    # за latest successful snapshot"): такие задачи просто не появятся в
    # debt-списке, пока не пройдут свою следующую обычную (мониторинг/
    # manual/refresh) успешную проверку — без отдельного backfill-скрипта
    # и без массовых principal checks.
    "last_successful_total_amount": (
        "ALTER TABLE fine_monitoring_tasks ADD COLUMN last_successful_total_amount REAL"
    ),
}

_INSERT = """
INSERT INTO fine_monitoring_tasks (
    car_number, label, start_date, end_date, status,
    telegram_chat_id, created_by_user_id, monitoring_scope
) VALUES (
    :car_number, :label, :start_date, :end_date, 'active',
    :telegram_chat_id, :created_by_user_id, :monitoring_scope
)
"""

_SELECT_FIELDS = """
    id, car_number, label, start_date, end_date, status,
    telegram_chat_id, created_by_user_id, created_at, updated_at,
    last_checked_at, last_check_status, last_error,
    archive_check_enabled, next_archive_check_at, monitoring_scope,
    last_successful_checked_at, last_successful_total_amount
"""

_SELECT_BY_ID = f"SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks WHERE id = ?"

_SELECT_ACTIVE = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks WHERE status = 'active'
"""

_SELECT_ACTIVE_BY_CAR = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks
    WHERE car_number = ? AND status = 'active'
"""

# Manager/trusted Search по номеру (см. задачу "manager/trusted Search") —
# БЕЗ фильтра по status (в отличие от _SELECT_ACTIVE_BY_CAR выше) — Search
# должен находить и OFF/completed задачи, тот же принцип, что и у
# _SELECT_ALL_PAGE manager car list. ORDER BY id DESC — та же, уже
# устоявшаяся конвенция "новые сначала" (см. root cause report про
# _SELECT_ALL_PAGE).
_SELECT_BY_CAR_NUMBER = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks
    WHERE car_number = ?
    ORDER BY id DESC
"""

_SELECT_ACTIVE_BY_SCOPE = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks
    WHERE status = 'active' AND monitoring_scope = ?
"""

# Trusted-operator task-level "📋 Мои авто" pagination (см. design report) —
# ORDER BY id ASC даёт стабильную сортировку между запросами (list_active()
# выше её не гарантирует вовсе — не полагаться на неё для пагинации).
_SELECT_ACTIVE_PAGE = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks
    WHERE status = 'active'
    ORDER BY id ASC
    LIMIT :limit OFFSET :offset
"""

# Manager-facing "📋 Мои авто" ON/OFF (см. design report про per-car
# monitoring toggle) — В ОТЛИЧИЕ от _SELECT_ACTIVE_PAGE выше, БЕЗ WHERE
# status = 'active': менеджеру нужно видеть и остановленные/завершённые
# задачи тоже (как ⚪ OFF), не только активные — тот же принцип, что и у
# list_my_cars() для обычного пользователя (список показывает любой
# статус, ON/OFF — это отдельное отображаемое состояние, а не фильтр
# списка). ORDER BY id DESC (а не ASC, как у _SELECT_ACTIVE_PAGE) —
# новые/недавно тронутые задачи (у которых чаще есть активный подписчик
# с известным username) идут ПЕРВЫМИ (см. root cause "manager листал 109
# страниц и не увидел ни одного @username" — при id ASC все такие строки
# были на последних страницах 84 и 117-138 из 138, а не потому что
# owner-resolution была сломана); тот же порядок, что и у Turkey
# TurkeyUserCarsRepository._SELECT_ALL_PAGE (см. задачу "унифицировать оба
# интерфейса").
_SELECT_ALL_PAGE = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_tasks
    ORDER BY id DESC
    LIMIT :limit OFFSET :offset
"""

_COUNT_ALL = "SELECT COUNT(*) FROM fine_monitoring_tasks"

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

_UPDATE_RESET_PERIOD = """
UPDATE fine_monitoring_tasks
SET start_date = :start_date,
    end_date = :end_date,
    updated_at = CURRENT_TIMESTAMP
WHERE id = :id
"""

# monitoring_scope != 'operator' в WHERE — не пишем (и не сдвигаем
# updated_at) там, где задача и так уже 'operator', см.
# ensure_operator_scope().
_ENSURE_OPERATOR_SCOPE = """
UPDATE fine_monitoring_tasks
SET monitoring_scope = 'operator', updated_at = CURRENT_TIMESTAMP
WHERE id = ? AND monitoring_scope != 'operator'
"""

# end_date сравнивается с ТЕМ ЖЕ самым :end_date, что и записывается —
# однострочный compare-and-set: пишем, только если кандидат СТРОГО позже
# уже сохранённого значения (см. extend_period_if_shorter()).
_EXTEND_PERIOD_IF_SHORTER = """
UPDATE fine_monitoring_tasks
SET end_date = :end_date, updated_at = CURRENT_TIMESTAMP
WHERE id = :id AND end_date < :end_date
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
        monitoring_scope,
        last_successful_checked_at,
        last_successful_total_amount,
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
        monitoring_scope=monitoring_scope,
        last_successful_checked_at=(
            datetime.fromisoformat(last_successful_checked_at) if last_successful_checked_at else None
        ),
        last_successful_total_amount=last_successful_total_amount,
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
        monitoring_scope: FineMonitoringScope = "operator",
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
                "monitoring_scope": monitoring_scope,
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

    def list_active_by_scope(self, scope: FineMonitoringScope) -> list[FineMonitoringTask]:
        """Как list_active(), но только задачи конкретного monitoring_scope.
        Взаимоисключающее партиционирование для будущих FineJob('operator')/
        ClientFineJob('client_bot') — см. design про "минимальную и чистую
        scheduling-модель": ОДНА задача проверяется РОВНО одним фоновым
        job'ом (никогда обоими сразу), поэтому общая для оператора и
        клиента машина не проверяется дважды. Ни один существующий
        вызывающий код (FineJob/fine list/fine update-all) не переключён на
        этот метод в этой задаче — используется только напрямую там, где
        уже вызывается явно."""
        rows = self._conn.execute(_SELECT_ACTIVE_BY_SCOPE, (scope,)).fetchall()
        return [_row_to_task(row) for row in rows]

    def get_active_by_car_number(self, car_number: str) -> list[FineMonitoringTask]:
        rows = self._conn.execute(_SELECT_ACTIVE_BY_CAR, (car_number,)).fetchall()
        return [_row_to_task(row) for row in rows]

    def list_by_car_number(self, car_number: str) -> list[FineMonitoringTask]:
        """Как get_active_by_car_number(), но ЛЮБОГО status (см.
        _SELECT_BY_CAR_NUMBER выше) — manager/trusted Search должен
        находить и OFF/completed задачи, не только active (тот же принцип,
        что и list_all_page для manager car list)."""
        rows = self._conn.execute(_SELECT_BY_CAR_NUMBER, (car_number,)).fetchall()
        return [_row_to_task(row) for row in rows]

    def list_active_page(self, *, offset: int, limit: int) -> list[FineMonitoringTask]:
        """Одна страница активных задач, ORDER BY id ASC (стабильная
        сортировка между запросами — см. design report про trusted-operator
        task-level "📋 Мои авто" pagination). count_active() (см. ниже)
        даёт общее число для вычисления числа страниц вызывающим кодом —
        сам этот метод ничего не знает про размер страницы/её номер."""
        rows = self._conn.execute(
            _SELECT_ACTIVE_PAGE, {"offset": offset, "limit": limit},
        ).fetchall()
        return [_row_to_task(row) for row in rows]

    def list_all_page(self, *, offset: int, limit: int) -> list[FineMonitoringTask]:
        """Как list_active_page(), но БЕЗ фильтра по status (см.
        _SELECT_ALL_PAGE выше) — manager-facing "📋 Мои авто" ON/OFF
        (см. design report): менеджер должен видеть и OFF-машины
        (status='stopped'/'completed'), не только активные. count_all()
        (см. ниже) даёт общее число для той же пагинации."""
        rows = self._conn.execute(
            _SELECT_ALL_PAGE, {"offset": offset, "limit": limit},
        ).fetchall()
        return [_row_to_task(row) for row in rows]

    def count_all(self) -> int:
        return self._conn.execute(_COUNT_ALL).fetchone()[0]

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

    def record_successful_check(self, task_id: int, *, total_amount: float) -> None:
        """Отдельно от record_check_result() выше — вызывается ИСКЛЮЧИТЕЛЬНО
        из FineCheckService.check_task(), ЕДИНСТВЕННОГО authoritative
        layer'а (см. задачу "manager Statistics / refresh для обоих ботов"
        п.11), КОГДА проверка реально завершилась status=='ok' — это
        покрывает ЛЮБОЙ путь проверки (мониторинг/manual "Проверить
        сейчас"/Add Car/manager refresh) без отдельной реализации
        persistence в каждом из них. При ERROR этот метод НЕ вызывается
        вовсе, поэтому last_successful_checked_at/
        last_successful_total_amount переживают последующие неудачные
        попытки без каких-либо условных UPDATE (тот же приём "просто не
        трогать поле", что и у FineCheckService._resolve_translation/
        detected_fines при ERROR). total_amount — "что показала ЭТА
        успешная проверка" (сумма current_fines, см. check_task()), а НЕ
        сумма истории detected_fines — может быть меньше, больше или
        0 по сравнению с прошлым значением (задача явно требует "не
        монотонно неубывающая история")."""
        self._conn.execute(
            "UPDATE fine_monitoring_tasks SET last_successful_checked_at = CURRENT_TIMESTAMP, "
            "last_successful_total_amount = :total_amount WHERE id = :task_id",
            {"task_id": task_id, "total_amount": total_amount},
        )
        self._conn.commit()

    def list_tasks_with_known_debt(self) -> list[FineTaskDebtSnapshot]:
        """"🚨 Штрафы по последней проверке" (см. задачу) — ТОЛЬКО задачи,
        у которых последняя УСПЕШНАЯ проверка показала total_amount > 0
        (см. last_successful_total_amount) — НЕ сумма истории
        detected_fines. NULL (ни одной успешной проверки ПОСЛЕ появления
        этого поля ещё не было, см. задачу п.10 про миграцию) и <= 0 —
        не кандидаты, тот же принцип, что и у Turkey get_debt_rows.
        Отсортировано по убыванию суммы средствами SQLite."""
        rows = self._conn.execute(
            "SELECT id, car_number, last_successful_total_amount, last_successful_checked_at "
            "FROM fine_monitoring_tasks "
            "WHERE last_successful_total_amount IS NOT NULL AND last_successful_total_amount > 0 "
            "ORDER BY last_successful_total_amount DESC, last_successful_checked_at DESC"
        ).fetchall()
        return [
            FineTaskDebtSnapshot(
                task_id=row[0], car_number=row[1], total_amount=row[2],
                checked_at=datetime.fromisoformat(row[3]),
            )
            for row in rows
        ]

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

    def reset_period(self, task_id: int, *, start_date: date, end_date: date) -> FineMonitoringTask:
        """Перезаписывает start_date/end_date уже АКТИВНОЙ задачи (см.
        reader/commands/fine.py _handle_add — повторное добавление
        автомобиля, у которого уже есть active-задача, сбрасывает её
        период относительно сегодняшней даты, а не продлевает от старого
        end_date). В отличие от return_to_active_monitoring() (для
        completed/архивных задач, возвращаемых в активный режим) — не
        трогает status/archive_check_enabled/next_archive_check_at:
        задача и так уже активна, остальные настройки менять не нужно."""
        self._conn.execute(
            _UPDATE_RESET_PERIOD,
            {
                "id": task_id,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )
        self._conn.commit()

        task = self.get(task_id)
        if task is None:
            raise RuntimeError(f"Задача мониторинга {task_id} не найдена")
        return task

    def ensure_operator_scope(self, task_id: int) -> FineMonitoringTask:
        """Повышает monitoring_scope задачи до 'operator', если она была
        'client_bot' — используется reader/commands/fine.py::_handle_add,
        когда оператор явно выполняет "fine add" для номера, задача
        которого уже существует и была изначально заведена клиентским
        ботом (см. design). Апгрейд НЕОБРАТИМ — обратного метода
        "понизить до client_bot" не существует: однажды взятая оператором
        под контроль задача остаётся 'operator' навсегда.

        No-op без единой записи (в т.ч. без сдвига updated_at), если
        задача и так уже 'operator' — тот же принцип, что и у
        UserRepository.update_access_hash()."""
        self._conn.execute(_ENSURE_OPERATOR_SCOPE, (task_id,))
        self._conn.commit()

        task = self.get(task_id)
        if task is None:
            raise RuntimeError(f"Задача мониторинга {task_id} не найдена")
        return task

    def extend_period_if_shorter(self, task_id: int, candidate_end_date: date) -> FineMonitoringTask:
        """Продлевает end_date задачи, ТОЛЬКО если candidate_end_date
        строго позже уже сохранённого — никогда не сокращает период. В
        отличие от reset_period() (которым пользуется оператор через
        "fine add" — там период безусловно перезаписывается) — нужен для
        клиентских подписок: у одной задачи может быть несколько
        подписчиков с разными периодами одновременно, и период самой
        задачи должен покрывать самого "долгого" из них, а не последнего
        добавленного/продлившего подписку (см. design)."""
        self._conn.execute(
            _EXTEND_PERIOD_IF_SHORTER,
            {"id": task_id, "end_date": candidate_end_date.isoformat()},
        )
        self._conn.commit()

        task = self.get(task_id)
        if task is None:
            raise RuntimeError(f"Задача мониторинга {task_id} не найдена")
        return task

    def close(self) -> None:
        self._conn.close()
