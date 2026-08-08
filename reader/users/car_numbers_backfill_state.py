import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS car_numbers_backfill_state (
    group_id INTEGER PRIMARY KEY,
    chat_name TEXT,
    last_message_id INTEGER,
    completed INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP
)
"""

_SAVE = """
INSERT INTO car_numbers_backfill_state (
    group_id, chat_name, last_message_id, completed, updated_at
) VALUES (
    :group_id, :chat_name, :last_message_id, :completed, CURRENT_TIMESTAMP
)
ON CONFLICT(group_id) DO UPDATE SET
    chat_name=excluded.chat_name,
    last_message_id=excluded.last_message_id,
    completed=excluded.completed,
    updated_at=CURRENT_TIMESTAMP
"""

_SELECT = """
SELECT group_id, chat_name, last_message_id, completed
FROM car_numbers_backfill_state WHERE group_id = ?
"""


@dataclass(frozen=True)
class CarNumbersBackfillCheckpoint:
    group_id: int
    chat_name: str | None
    last_message_id: int | None
    completed: bool


class CarNumbersBackfillStateRepository:
    """Чекпоинт СТРОГО для reader/users/backfill_car_numbers.py — отдельная
    таблица в том же users.db, полностью независимая от
    HistorySyncStateRepository (checkpoint sync_users.py/history_sync.py, и
    обычный, и --reindex): ни разу не читается и не пишется этим классом,
    ключ таблицы даже не пересекается по имени (group_id, а не chat_id).

    Позволяет backfill с историей произвольного размера (условно
    миллионы/миллиарды сообщений) переживать обрыв процесса (SSH, Ctrl+C,
    OOM, перезагрузка сервера) без повторного чтения уже пройденной части
    истории группы: last_message_id обновляется на каждом flush (см.
    FLUSH_EVERY_MESSAGES в backfill_car_numbers.py), completed=True
    выставляется только когда история группы вычитана целиком — при
    следующем запуске такая группа пропускается мгновенно (см.
    _scan_group), а незавершённая продолжается с last_message_id, а не с
    начала.
    """

    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # FULL (не NORMAL, как в UserRepository): эта таблица — источник
        # истины после аварийного завершения процесса, а пишется она редко
        # (раз в FLUSH_EVERY_MESSAGES сообщений), поэтому стоимость fsync на
        # commit пренебрежима по сравнению с риском потерять позицию чтения.
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def get(self, group_id: int) -> CarNumbersBackfillCheckpoint | None:
        row = self._conn.execute(_SELECT, (group_id,)).fetchone()
        if row is None:
            return None

        group_id, chat_name, last_message_id, completed = row
        return CarNumbersBackfillCheckpoint(
            group_id=group_id,
            chat_name=chat_name,
            last_message_id=last_message_id,
            completed=bool(completed),
        )

    def save_progress(
        self,
        *,
        group_id: int,
        chat_name: str | None,
        last_message_id: int | None,
        completed: bool,
    ) -> None:
        self._conn.execute(
            _SAVE,
            {
                "group_id": group_id,
                "chat_name": chat_name,
                "last_message_id": last_message_id,
                "completed": int(completed),
            },
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
