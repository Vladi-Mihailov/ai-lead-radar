"""TurkeyUserCarsRepository — turkey_bot_user_cars поверх SQLite.

"Гараж" — минимальная, НЕ мониторинговая таблица (см. задачу: "Do not
copy the Georgian monitoring/subscription architecture... Do not create a
monitoring/subscription model"): единственная цель — не заставлять
пользователя вводить номер заново. Запись появляется/обновляется ТОЛЬКО
из reader/turkey_bot_test/conversation.py после РЕАЛЬНО завершённой проверки с
исходом "no_debt" ИЛИ "has_debt" (см. record_successful_check) — ничего
похожего на ON/OFF, период мониторинга, active/stopped здесь нет и не
должно быть.

UNIQUE(telegram_user_id, car_number) — дедупликация нормализованных
номеров на пользователя (см. reader/turkey_bot_test/validation.py::
normalize_plate — car_number сюда передаётся уже нормализованным)."""

import sqlite3
from datetime import datetime
from pathlib import Path

from reader.turkey_bot_test.models import TurkeyUserCar

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turkey_bot_user_cars (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id  INTEGER NOT NULL,
    car_number        TEXT NOT NULL,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_checked_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (telegram_user_id, car_number)
)
"""

_UPSERT = """
INSERT INTO turkey_bot_user_cars (telegram_user_id, car_number, created_at, last_checked_at)
VALUES (:telegram_user_id, :car_number, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT(telegram_user_id, car_number) DO UPDATE SET
    last_checked_at = CURRENT_TIMESTAMP
"""

_SELECT_OWNED = (
    "SELECT id, telegram_user_id, car_number, created_at, last_checked_at "
    "FROM turkey_bot_user_cars WHERE id = ? AND telegram_user_id = ?"
)

_SELECT_FOR_USER = (
    "SELECT id, telegram_user_id, car_number, created_at, last_checked_at "
    "FROM turkey_bot_user_cars WHERE telegram_user_id = ? "
    "ORDER BY last_checked_at DESC, id DESC"
)


def _row_to_car(row) -> TurkeyUserCar:
    car_id, telegram_user_id, car_number, created_at, last_checked_at = row
    return TurkeyUserCar(
        id=car_id,
        telegram_user_id=telegram_user_id,
        car_number=car_number,
        created_at=datetime.fromisoformat(created_at),
        last_checked_at=datetime.fromisoformat(last_checked_at),
    )


class TurkeyUserCarsRepository:
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

    def record_successful_check(self, *, telegram_user_id: int, car_number: str) -> None:
        """Idempotent upsert — второй успешный чек того же номера тем же
        пользователем НЕ создаёт вторую строку (см. UNIQUE выше), только
        обновляет last_checked_at. Вызывать ИСКЛЮЧИТЕЛЬНО для
        завершённых outcome.kind in ("no_debt", "has_debt") — см.
        reader/turkey_bot_test/conversation.py, никогда для rejected/
        unexpected/error/просто-набранного-номера."""
        self._conn.execute(
            _UPSERT, {"telegram_user_id": telegram_user_id, "car_number": car_number},
        )
        self._conn.commit()

    def list_cars(self, telegram_user_id: int) -> list[TurkeyUserCar]:
        """Только машины ЭТОГО пользователя (см. задачу: "each user sees
        only their own cars") — отсортированы по last_checked_at DESC
        (недавно проверенные — выше, привычный порядок для "гаража")."""
        rows = self._conn.execute(_SELECT_FOR_USER, (telegram_user_id,)).fetchall()
        return [_row_to_car(row) for row in rows]

    def get_owned_car(self, car_id: int, *, telegram_user_id: int) -> TurkeyUserCar | None:
        """None — машина не существует ИЛИ принадлежит другому
        пользователю (см. задачу: "Garage callbacks must resolve the
        selected car server-side and verify ownership before starting a
        check") — единственная проверка авторизации для garage-callback'ов,
        car_id из callback_data сам по себе НИЧЕГО не доказывает (тот же
        принцип, что и SubscriptionService.get_actionable_subscription в
        Георгия-боте)."""
        row = self._conn.execute(_SELECT_OWNED, (car_id, telegram_user_id)).fetchone()
        return _row_to_car(row) if row else None

    def close(self) -> None:
        self._conn.close()
