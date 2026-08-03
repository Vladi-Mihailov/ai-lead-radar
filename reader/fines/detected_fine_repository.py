import sqlite3
from datetime import date, datetime
from pathlib import Path

from reader.fines.models import DetectedFine

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
    notification_sent_at  TIMESTAMP
)
"""

_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_detected_fines_dedup
    ON detected_fines (monitoring_task_id, fingerprint)
"""

_INSERT = """
INSERT INTO detected_fines (
    monitoring_task_id, car_number, external_fine_id, fingerprint,
    penalty_date, due_date, delivered_status, raw_data
) VALUES (
    :monitoring_task_id, :car_number, :external_fine_id, :fingerprint,
    :penalty_date, :due_date, :delivered_status, :raw_data
)
"""

_SELECT_FIELDS = """
    id, monitoring_task_id, car_number, external_fine_id, fingerprint,
    penalty_date, due_date, delivered_status, raw_data,
    first_detected_at, last_seen_at, notification_sent_at
"""

_SELECT_BY_ID = f"SELECT {_SELECT_FIELDS} FROM detected_fines WHERE id = ?"

_SELECT_BY_FINGERPRINT = f"""
    SELECT {_SELECT_FIELDS} FROM detected_fines
    WHERE monitoring_task_id = ? AND fingerprint = ?
"""

_SELECT_PENDING_NOTIFICATIONS = f"""
    SELECT {_SELECT_FIELDS} FROM detected_fines WHERE notification_sent_at IS NULL
"""

_MARK_SEEN = "UPDATE detected_fines SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?"

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
        self._conn.execute(_UNIQUE_INDEX)
        self._conn.commit()

    def get_by_fingerprint(self, monitoring_task_id: int, fingerprint: str) -> DetectedFine | None:
        row = self._conn.execute(
            _SELECT_BY_FINGERPRINT, (monitoring_task_id, fingerprint)
        ).fetchone()
        return _row_to_fine(row) if row else None

    def list_pending_notifications(self) -> list[DetectedFine]:
        """Штрафы, о которых оператор ещё не был уведомлён — свежесозданные
        в этом же проходе (notification_sent_at ещё не выставлен) и
        оставшиеся с прошлых неудачных попыток доставки одновременно,
        без разделения на "новые" и "повтор": это один и тот же признак."""
        rows = self._conn.execute(_SELECT_PENDING_NOTIFICATIONS).fetchall()
        return [_row_to_fine(row) for row in rows]

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

    def mark_seen(self, fine_id: int) -> None:
        self._conn.execute(_MARK_SEEN, (fine_id,))
        self._conn.commit()

    def mark_notification_sent(self, fine_id: int) -> None:
        self._conn.execute(_MARK_NOTIFIED, (fine_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
