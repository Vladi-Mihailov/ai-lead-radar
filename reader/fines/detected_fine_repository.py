import sqlite3
from datetime import date, datetime
from pathlib import Path

from reader.fines.models import CarFineStats, DetectedFine

_SCHEMA = """
CREATE TABLE IF NOT EXISTS detected_fines (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    monitoring_task_id    INTEGER NOT NULL REFERENCES fine_monitoring_tasks(id),
    car_number            TEXT NOT NULL,
    external_fine_id      TEXT,
    fingerprint           TEXT NOT NULL,
    penalty_date          TEXT,
    due_date              TEXT,
    delivered_status      TEXT,
    raw_data              TEXT NOT NULL,
    first_detected_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notification_sent_at  TIMESTAMP,
    violation_date        TEXT,
    amount                REAL,
    place                 TEXT,
    violation_description TEXT,
    place_ru              TEXT,
    violation_description_ru TEXT
)
"""

# CREATE TABLE IF NOT EXISTS не добавляет колонки в уже существующую
# таблицу — для БД, созданных до появления расширенного fine block (см.
# design report про новый формат уведомлений), добавляем их явно при
# открытии (тот же приём, что и FineMonitoringTaskRepository.
# _migrate_missing_columns), без удаления/пересоздания БД. Все 4 — NULL по
# умолчанию для уже существующих строк: ни fingerprint (см. parser.py —
# считается по сырому protocolAmount, не по этому типизированному полю),
# ни delivery idempotency (detected_fine_id/subscription/recipient_role)
# этой миграцией не затрагиваются — старые штрафы не рассылаются повторно.
_COLUMN_MIGRATIONS = {
    "violation_date": "ALTER TABLE detected_fines ADD COLUMN violation_date TEXT",
    "amount": "ALTER TABLE detected_fines ADD COLUMN amount REAL",
    "place": "ALTER TABLE detected_fines ADD COLUMN place TEXT",
    "violation_description": "ALTER TABLE detected_fines ADD COLUMN violation_description TEXT",
    # Русский перевод place/violation_description (см. reader/fines/
    # translation.py) — отдельные колонки, оригиналы (place/
    # violation_description/raw_data) этой миграцией не затрагиваются и
    # не перезаписываются нигде в коде (source-of-truth, см. задачу).
    "place_ru": "ALTER TABLE detected_fines ADD COLUMN place_ru TEXT",
    "violation_description_ru": "ALTER TABLE detected_fines ADD COLUMN violation_description_ru TEXT",
}

_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_detected_fines_dedup
    ON detected_fines (monitoring_task_id, fingerprint)
"""

# Поддерживает client delivery poller (см. reader/public_bot/delivery_service.py) —
# ему нужно эффективно находить все detected_fines конкретного car_number,
# не сканируя всю таблицу целиком (см. list_by_car_number ниже). Чисто
# аддитивный индекс — не меняет форму данных, миграция не нужна.
_CAR_NUMBER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_detected_fines_car_number
    ON detected_fines (car_number)
"""

_INSERT = """
INSERT INTO detected_fines (
    monitoring_task_id, car_number, external_fine_id, fingerprint,
    penalty_date, due_date, delivered_status, raw_data,
    violation_date, amount, place, violation_description,
    place_ru, violation_description_ru
) VALUES (
    :monitoring_task_id, :car_number, :external_fine_id, :fingerprint,
    :penalty_date, :due_date, :delivered_status, :raw_data,
    :violation_date, :amount, :place, :violation_description,
    :place_ru, :violation_description_ru
)
"""

_SELECT_FIELDS = """
    id, monitoring_task_id, car_number, external_fine_id, fingerprint,
    penalty_date, due_date, delivered_status, raw_data,
    first_detected_at, last_seen_at, notification_sent_at,
    violation_date, amount, place, violation_description,
    place_ru, violation_description_ru
"""

_SELECT_BY_ID = f"SELECT {_SELECT_FIELDS} FROM detected_fines WHERE id = ?"

_SELECT_BY_FINGERPRINT = f"""
    SELECT {_SELECT_FIELDS} FROM detected_fines
    WHERE monitoring_task_id = ? AND fingerprint = ?
"""

_SELECT_BY_CAR_NUMBER = f"""
    SELECT {_SELECT_FIELDS} FROM detected_fines WHERE car_number = ?
"""

_SELECT_PENDING_NOTIFICATIONS = f"""
    SELECT {_SELECT_FIELDS} FROM detected_fines WHERE notification_sent_at IS NULL
"""

_SELECT_STATS_BY_CAR = """
    SELECT car_number, COUNT(*) AS fine_count
    FROM detected_fines
    GROUP BY car_number
    ORDER BY fine_count DESC
"""

# COALESCE(:new, old) — backfill, никогда не затирает уже сохранённое
# значение NULL'ом (см. задачу про production-инцидент: mark_seen()
# раньше вообще не обновлял violation_date/amount/place/
# violation_description для уже существующих строк, поэтому detected_fines,
# созданные ДО появления этих колонок, так и оставались с ними NULL
# навсегда — даже когда police.ge на каждой следующей проверке продолжал
# отдавать полные данные по тому же fingerprint). Теперь каждая обычная
# проверка (FineJob/ClientFineJob/archive/"Проверить сейчас") сама
# постепенно "самолечит" все такие legacy-строки, без ручной миграции
# данных. violation_date/amount не могут отличаться от уже сохранённых,
# пока fingerprint совпадает (см. compute_fingerprint) — COALESCE здесь
# защищает именно place/violation_description на случай, если ответ
# police.ge когда-либо перестанет их отдавать для уже известного штрафа.
# place_ru/violation_description_ru — тот же COALESCE backfill (см. выше),
# но источник значения другой: не сырой police.ge response, а результат
# FineTranslationService (см. reader/fines/check_service.py) — уже
# посчитанный ДО вызова mark_seen() (перевод — асинхронный вызов, внутри
# SQL UPDATE его не сделать). None здесь означает "нечего обновлять"
# (перевод не нужен — текст не грузинский, уже переведён раньше, или
# временно недоступен — см. FineTranslationError) — COALESCE тогда просто
# сохраняет прежнее значение (для новых строк — NULL, как и раньше).
_MARK_SEEN = """
UPDATE detected_fines
SET last_seen_at = CURRENT_TIMESTAMP,
    violation_date = COALESCE(:violation_date, violation_date),
    amount = COALESCE(:amount, amount),
    place = COALESCE(:place, place),
    violation_description = COALESCE(:violation_description, violation_description),
    place_ru = COALESCE(:place_ru, place_ru),
    violation_description_ru = COALESCE(:violation_description_ru, violation_description_ru)
WHERE id = :id
"""

_MARK_NOTIFIED = """
UPDATE detected_fines SET notification_sent_at = CURRENT_TIMESTAMP WHERE id = ?
"""


def _row_to_fine(row) -> DetectedFine:
    (
        id_,
        monitoring_task_id,
        car_number,
        external_fine_id,
        fingerprint,
        penalty_date,
        due_date,
        delivered_status,
        raw_data,
        first_detected_at,
        last_seen_at,
        notification_sent_at,
        violation_date,
        amount,
        place,
        violation_description,
        place_ru,
        violation_description_ru,
    ) = row

    return DetectedFine(
        id=id_,
        monitoring_task_id=monitoring_task_id,
        car_number=car_number,
        external_fine_id=external_fine_id,
        fingerprint=fingerprint,
        penalty_date=date.fromisoformat(penalty_date) if penalty_date else None,
        due_date=date.fromisoformat(due_date) if due_date else None,
        delivered_status=delivered_status,
        raw_data=raw_data,
        first_detected_at=datetime.fromisoformat(first_detected_at),
        last_seen_at=datetime.fromisoformat(last_seen_at),
        notification_sent_at=(
            datetime.fromisoformat(notification_sent_at) if notification_sent_at else None
        ),
        violation_date=date.fromisoformat(violation_date) if violation_date else None,
        amount=amount,
        place=place,
        violation_description=violation_description,
        place_ru=place_ru,
        violation_description_ru=violation_description_ru,
    )


class DetectedFineRepository:
    """Обнаруженные штрафы (detected_fines) — та же БД, что и остальные
    репозитории проекта (settings.app.users_db_file).

    Уникальный индекс (monitoring_task_id, fingerprint) — вторая линия
    защиты от повторной отправки одного и того же штрафа оператору; первая —
    явная проверка get_by_fingerprint() перед вставкой в FineCheckService.
    """

    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # FULL, а не NORMAL (как в UserRepository): notification_sent_at —
        # источник истины о том, показывали ли уже этот штраф оператору.
        # Потеря этого коммита при аварийном завершении процесса означала
        # бы повторную отправку уже показанного штрафа — тот же аргумент,
        # что и у HistorySyncStateRepository для чекпоинтов.
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(_SCHEMA)
        self._migrate_missing_columns()
        self._conn.execute(_UNIQUE_INDEX)
        self._conn.execute(_CAR_NUMBER_INDEX)
        self._conn.commit()

    def _migrate_missing_columns(self) -> None:
        """CREATE TABLE IF NOT EXISTS не добавляет колонки в уже
        существующую таблицу — для БД, созданных до появления расширенного
        fine block, добавляем недостающие явно, без удаления/пересоздания БД
        (тот же приём, что и FineMonitoringTaskRepository)."""
        existing_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(detected_fines)")
        }
        for column, statement in _COLUMN_MIGRATIONS.items():
            if column not in existing_columns:
                self._conn.execute(statement)

    def get_by_fingerprint(self, monitoring_task_id: int, fingerprint: str) -> DetectedFine | None:
        row = self._conn.execute(
            _SELECT_BY_FINGERPRINT, (monitoring_task_id, fingerprint)
        ).fetchone()
        return _row_to_fine(row) if row else None

    def list_by_car_number(self, car_number: str) -> list[DetectedFine]:
        """Все обнаруженные штрафы конкретного номера — используется client
        delivery poller'ом (см. reader/public_bot/delivery_service.py) для
        поиска штрафов, которые нужно доставить подписчикам этой машины.
        Не заменяет и не трогает дедуп (monitoring_task_id, fingerprint) —
        это чтение уже сохранённых, задедупленных записей."""
        rows = self._conn.execute(_SELECT_BY_CAR_NUMBER, (car_number,)).fetchall()
        return [_row_to_fine(row) for row in rows]

    def list_pending_notifications(self) -> list[DetectedFine]:
        """Штрафы, о которых оператор ещё не был уведомлён — свежесозданные
        в этом же проходе (notification_sent_at ещё не выставлен) и
        оставшиеся с прошлых неудачных попыток доставки одновременно,
        без разделения на "новые" и "повтор": это один и тот же признак."""
        rows = self._conn.execute(_SELECT_PENDING_NOTIFICATIONS).fetchall()
        return [_row_to_fine(row) for row in rows]

    def get_stats_by_car(self) -> list[CarFineStats]:
        """Количество опубликованных штрафов по каждому автомобилю (fine
        stats) — сгруппировано и отсортировано по убыванию средствами
        SQLite, а не в Python."""
        rows = self._conn.execute(_SELECT_STATS_BY_CAR).fetchall()
        return [CarFineStats(car_number=row[0], fine_count=row[1]) for row in rows]

    def create(
        self,
        *,
        monitoring_task_id: int,
        car_number: str,
        external_fine_id: str | None,
        fingerprint: str,
        penalty_date: date | None,
        due_date: date | None,
        delivered_status: str | None,
        raw_data: str,
        violation_date: date | None = None,
        amount: float | None = None,
        place: str | None = None,
        violation_description: str | None = None,
        place_ru: str | None = None,
        violation_description_ru: str | None = None,
    ) -> DetectedFine:
        try:
            cursor = self._conn.execute(
                _INSERT,
                {
                    "monitoring_task_id": monitoring_task_id,
                    "car_number": car_number,
                    "external_fine_id": external_fine_id,
                    "fingerprint": fingerprint,
                    "penalty_date": penalty_date.isoformat() if penalty_date else None,
                    "due_date": due_date.isoformat() if due_date else None,
                    "delivered_status": delivered_status,
                    "raw_data": raw_data,
                    "violation_date": violation_date.isoformat() if violation_date else None,
                    "amount": amount,
                    "place": place,
                    "violation_description": violation_description,
                    "place_ru": place_ru,
                    "violation_description_ru": violation_description_ru,
                },
            )
        except sqlite3.IntegrityError:
            # Незакоммиченная транзакция после нарушения UNIQUE/FOREIGN KEY
            # держит блокировку записи на этом соединении (в т.ч. для других
            # соединений к этому же файлу — FineMonitoringTaskRepository и
            # т.п.), пока её явно не откатить. Вызывающий код (например,
            # FineCheckService) ловит это же исключение как сигнал "запись
            # уже существует", соединение должно остаться в чистом состоянии.
            self._conn.rollback()
            raise

        self._conn.commit()

        row = self._conn.execute(_SELECT_BY_ID, (cursor.lastrowid,)).fetchone()
        if row is None:
            raise RuntimeError("Не удалось прочитать только что созданную запись о штрафе")
        return _row_to_fine(row)

    def mark_seen(
        self,
        fine_id: int,
        *,
        violation_date: date | None = None,
        amount: float | None = None,
        place: str | None = None,
        violation_description: str | None = None,
        place_ru: str | None = None,
        violation_description_ru: str | None = None,
    ) -> None:
        self._conn.execute(
            _MARK_SEEN,
            {
                "id": fine_id,
                "violation_date": violation_date.isoformat() if violation_date else None,
                "amount": amount,
                "place": place,
                "violation_description": violation_description,
                "place_ru": place_ru,
                "violation_description_ru": violation_description_ru,
            },
        )
        self._conn.commit()

    def mark_notification_sent(self, fine_id: int) -> None:
        self._conn.execute(_MARK_NOTIFIED, (fine_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
