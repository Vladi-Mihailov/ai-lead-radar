"""TurkeyUserCarsRepository — turkey_bot_user_cars поверх SQLite.

"📋 Мои авто" (см. design report "Перестроить UX Turkey test bot", решение
п.3): автомобиль добавляется СРАЗУ после валидного ввода номера —
успешная проверка для добавления БОЛЬШЕ НЕ требуется (сознательная смена
направления относительно исходного инварианта этого файла — раньше запись
появлялась только после успешной проверки; текущая задача явно это меняет).

UNIQUE(telegram_user_id, car_number) — дедупликация нормализованных
номеров на пользователя (см. reader/turkey_bot/validation.py::
normalize_plate — car_number сюда передаётся уже нормализованным),
сохранена без изменений.

last_overall_status/last_total_amount — НОВЫЕ колонки (additive ALTER
TABLE, см. _COLUMN_MIGRATIONS — тот же приём, что и
reader/fines/task_repository.py::_COLUMN_MIGRATIONS у Georgian-бота) —
кэш последнего unified-check для списка "Мои авто" без join."""

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from reader.turkey_bot.models import TurkeyUserCar

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

# (column_name, ALTER TABLE ... ADD COLUMN ...) — применяется ТОЛЬКО если
# колонки ещё нет (см. PRAGMA table_info в __init__) — никогда не трогает
# уже существующие строки/данные (см. модуль docstring).
_COLUMN_MIGRATIONS = (
    ("last_overall_status", "ALTER TABLE turkey_bot_user_cars ADD COLUMN last_overall_status TEXT"),
    ("last_total_amount", "ALTER TABLE turkey_bot_user_cars ADD COLUMN last_total_amount TEXT"),
)

_INSERT_NEW_CAR = """
INSERT INTO turkey_bot_user_cars (telegram_user_id, car_number, created_at, last_checked_at)
VALUES (:telegram_user_id, :car_number, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT(telegram_user_id, car_number) DO NOTHING
"""

_UPDATE_LAST_RESULT = """
UPDATE turkey_bot_user_cars SET
    last_checked_at = CURRENT_TIMESTAMP,
    last_overall_status = :overall_status,
    last_total_amount = :total_amount
WHERE telegram_user_id = :telegram_user_id AND car_number = :car_number
"""

_SELECT_OWNED = (
    "SELECT id, telegram_user_id, car_number, created_at, last_checked_at, "
    "last_overall_status, last_total_amount "
    "FROM turkey_bot_user_cars WHERE id = ? AND telegram_user_id = ?"
)

_SELECT_FOR_USER = (
    "SELECT id, telegram_user_id, car_number, created_at, last_checked_at, "
    "last_overall_status, last_total_amount "
    "FROM turkey_bot_user_cars WHERE telegram_user_id = ? "
    "ORDER BY last_checked_at DESC, id DESC"
)

_DELETE_OWNED = "DELETE FROM turkey_bot_user_cars WHERE id = ? AND telegram_user_id = ?"

_SELECT_ANY = (
    "SELECT id, telegram_user_id, car_number, created_at, last_checked_at, "
    "last_overall_status, last_total_amount "
    "FROM turkey_bot_user_cars WHERE id = ?"
)

_SELECT_ALL_PAGE = (
    "SELECT id, telegram_user_id, car_number, created_at, last_checked_at, "
    "last_overall_status, last_total_amount "
    "FROM turkey_bot_user_cars ORDER BY id DESC LIMIT ? OFFSET ?"
)

# Manager/trusted Search по номеру (см. задачу "manager/trusted Search") —
# ВСЕ владельцы этого car_number, БЕЗ dedup (UNIQUE(telegram_user_id,
# car_number) гарантирует, что один и тот же пользователь не может иметь
# два раза один номер, но РАЗНЫЕ пользователи — могут, тот же принцип, что
# и у list_all_page/manager car list). ORDER BY id DESC — та же
# конвенция.
_SELECT_BY_CAR_NUMBER = (
    "SELECT id, telegram_user_id, car_number, created_at, last_checked_at, "
    "last_overall_status, last_total_amount "
    "FROM turkey_bot_user_cars WHERE car_number = ? ORDER BY id DESC"
)


def _row_to_car(row) -> TurkeyUserCar:
    car_id, telegram_user_id, car_number, created_at, last_checked_at, last_status, last_total = row
    return TurkeyUserCar(
        id=car_id,
        telegram_user_id=telegram_user_id,
        car_number=car_number,
        created_at=datetime.fromisoformat(created_at),
        last_checked_at=datetime.fromisoformat(last_checked_at),
        last_overall_status=last_status,
        last_total_amount=Decimal(last_total) if last_total is not None else None,
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
        existing_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(turkey_bot_user_cars)")}
        for column_name, migration_sql in _COLUMN_MIGRATIONS:
            if column_name not in existing_columns:
                self._conn.execute(migration_sql)
        self._conn.commit()

    def add_car(self, *, telegram_user_id: int, car_number: str) -> TurkeyUserCar:
        """Добавляет автомобиль СРАЗУ (без предварительной проверки, см.
        модуль docstring) — idempotent (ON CONFLICT DO NOTHING, см.
        _INSERT_NEW_CAR): повторный ввод уже добавленного номера НЕ
        создаёт дубликат и не затирает last_overall_status/
        last_total_amount уже проверенной машины."""
        self._conn.execute(
            _INSERT_NEW_CAR, {"telegram_user_id": telegram_user_id, "car_number": car_number},
        )
        self._conn.commit()
        car = self.get_owned_car_by_number(telegram_user_id, car_number)
        if car is None:
            raise RuntimeError("Не удалось прочитать только что добавленный автомобиль")
        return car

    def get_owned_car_by_number(self, telegram_user_id: int, car_number: str) -> TurkeyUserCar | None:
        """Аналог get_owned_car(), но по нормализованному номеру, а не
        car_id — используется add_car() (проверить, существовал ли
        автомобиль ДО вставки) и handle_car_open_by_plate (см.
        reader/turkey_bot/conversation.py, кнопка "🔎 Подробнее" в
        уведомлениях мониторинга)."""
        row = self._conn.execute(
            "SELECT id, telegram_user_id, car_number, created_at, last_checked_at, "
            "last_overall_status, last_total_amount FROM turkey_bot_user_cars "
            "WHERE telegram_user_id = ? AND car_number = ?",
            (telegram_user_id, car_number),
        ).fetchone()
        return _row_to_car(row) if row else None

    def update_last_result(
        self, *, telegram_user_id: int, car_number: str, overall_status: str, total_amount: Decimal,
    ) -> None:
        """Вызывается ПОСЛЕ unified-check (см. reader/turkey_bot/
        unified/check_service.py) — обновляет кэш для списка "Мои авто".
        Не создаёт строку, если её почему-то ещё нет (add_car — единственный
        путь создания)."""
        self._conn.execute(
            _UPDATE_LAST_RESULT,
            {
                "telegram_user_id": telegram_user_id, "car_number": car_number,
                "overall_status": overall_status, "total_amount": str(total_amount),
            },
        )
        self._conn.commit()

    def list_cars(self, telegram_user_id: int) -> list[TurkeyUserCar]:
        """Только машины ЭТОГО пользователя, отсортированы по
        last_checked_at DESC (недавно проверенные/добавленные — выше)."""
        rows = self._conn.execute(_SELECT_FOR_USER, (telegram_user_id,)).fetchall()
        return [_row_to_car(row) for row in rows]

    def get_owned_car(self, car_id: int, *, telegram_user_id: int) -> TurkeyUserCar | None:
        """None — машина не существует ИЛИ принадлежит другому
        пользователю — единственная проверка авторизации для
        car-card-callback'ов, car_id из callback_data сам по себе ничего
        не доказывает."""
        row = self._conn.execute(_SELECT_OWNED, (car_id, telegram_user_id)).fetchone()
        return _row_to_car(row) if row else None

    def delete_car(self, car_id: int, *, telegram_user_id: int) -> bool:
        """True — машина реально удалена (принадлежала этому
        пользователю); False — не найдена/чужая (тот же ownership-check,
        что и get_owned_car)."""
        cursor = self._conn.execute(_DELETE_OWNED, (car_id, telegram_user_id))
        self._conn.commit()
        return cursor.rowcount > 0

    # ---- manager/trusted-operator "🚗 Мои автомобили" (см. задачу
    # "Реализуем manager/trusted 'Мои автомобили' для Turkey bot") — ВСЕ
    # машины ВСЕХ пользователей, БЕЗ ownership-фильтра. is_trusted()
    # проверяется ИСКЛЮЧИТЕЛЬНО в ConversationController (см.
    # handle_manager_cars_page/handle_manager_car_open/
    # handle_manager_car_toggle) — эти два метода репозитория сами по себе
    # авторизацию не проверяют и не должны использоваться ни в одном
    # self-service пути (get_owned_car/list_cars выше остаются
    # НЕТРОНУТЫМИ и по-прежнему единственный путь для обычных
    # пользователей). ----

    def count_all(self) -> int:
        """Общее число ВСЕХ автомобилей (любого владельца) — для
        пагинации manager-списка."""
        return self._conn.execute("SELECT COUNT(*) FROM turkey_bot_user_cars").fetchone()[0]

    def list_all_page(self, *, offset: int, limit: int) -> list[TurkeyUserCar]:
        """Одна страница ВСЕХ машин, ЛЮБОГО владельца — ORDER BY id DESC
        (стабильная, deterministic — см. задачу), БЕЗ dedup по
        car_number: один и тот же номер, добавленный разными
        пользователями, — отдельные строки (см. задачу п.2 — каждая
        строка это конкретная запись turkey_bot_user_cars, адресуется
        car.id, а не car_number)."""
        rows = self._conn.execute(_SELECT_ALL_PAGE, (limit, offset)).fetchall()
        return [_row_to_car(row) for row in rows]

    def list_by_car_number(self, car_number: str) -> list[TurkeyUserCar]:
        """ВСЕ строки с этим car_number, ЛЮБОГО владельца (см. задачу
        "manager/trusted Search" п.6: "не делать dedup, показать каждую
        связь отдельно") — для manager-only Search-flow, тот же принцип
        авторизации, что и list_all_page/get_any_car (is_trusted()
        проверяется в ConversationController, не здесь). НИКОГДА не
        использовать в self-service путях."""
        rows = self._conn.execute(_SELECT_BY_CAR_NUMBER, (car_number,)).fetchall()
        return [_row_to_car(row) for row in rows]

    def get_any_car(self, car_id: int) -> TurkeyUserCar | None:
        """Как get_owned_car(), но БЕЗ проверки telegram_user_id — ТОЛЬКО
        для manager-flow (is_trusted() уже проверен вызывающим кодом в
        ConversationController), чтобы менеджер мог открыть/переключить
        чужой автомобиль по его car.id. НИКОГДА не использовать в
        self-service путях — там всегда get_owned_car."""
        row = self._conn.execute(_SELECT_ANY, (car_id,)).fetchone()
        return _row_to_car(row) if row else None

    def close(self) -> None:
        self._conn.close()
