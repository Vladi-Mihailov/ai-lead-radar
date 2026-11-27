"""Имя клиента, которое менеджер указал для автомобиля (@ProtocolGEbot).

Только подпись для экранов менеджера: менеджер может знать имя клиента и не
знать его Telegram-логин. Имя — не Telegram user_id, не доказательство
личности или владения автомобилем и не доступ: доступ к автомобилю и
личные уведомления по-прежнему даёт только подтверждённая подписка
(fine_monitoring_subscriptions). Хранится по физическому номеру автомобиля
(у одного номера может быть несколько задач мониторинга), поэтому
переживает повторную постановку на мониторинг и последующую привязку
Telegram.
"""

import sqlite3
import unicodedata
from pathlib import Path

CLIENT_NAME_MAX_LENGTH = 64
# Кроме букв любых алфавитов (с диакритикой), цифр и пробела — только
# обычная пунктуация имён: "Александр-2", "O'Brien", "Ivan Jr.".
_ALLOWED_PUNCTUATION = frozenset("-'.")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS georgia_car_client_names (
    car_number   TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by   INTEGER
)
"""


class ClientNameValidationError(Exception):
    """Ожидаемая ошибка ввода — сообщение показывается менеджеру как есть."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


_INVALID_NAME_TEXT = (
    "Имя может содержать буквы любого алфавита, цифры, пробелы и знаки «-», «'», «.» "
    f"— до {CLIENT_NAME_MAX_LENGTH} символов. Например: Иван Иванов, გიორგი, Ahmet 007."
)


def normalize_client_name(raw: str) -> str:
    """Проверенное имя для хранения: NFC, без управляющих и невидимых
    символов, пробелы схлопнуты; буквы (любой алфавит, с диакритикой),
    цифры, пробел и «-'.»; 1..CLIENT_NAME_MAX_LENGTH символов. Иначе
    ClientNameValidationError."""
    text = unicodedata.normalize("NFC", raw or "")
    # Управляющие/невидимые символы (Cc, Cf, ...) — удаляются, а не
    # считаются ошибкой: они приходят из копирования и на экране не видны.
    # Переводы строк и табы — это тоже Cc, они становятся пробелами.
    text = "".join(
        " " if char in "\t\n\r\v\f" else char
        for char in text
        if char in "\t\n\r\v\f" or not unicodedata.category(char).startswith("C")
    )
    text = " ".join(text.split())
    if not text or len(text) > CLIENT_NAME_MAX_LENGTH:
        raise ClientNameValidationError(_INVALID_NAME_TEXT)
    for char in text:
        category = unicodedata.category(char)
        if category[0] in ("L", "M", "N") or char == " " or char in _ALLOWED_PUNCTUATION:
            continue
        raise ClientNameValidationError(_INVALID_NAME_TEXT)
    if not any(unicodedata.category(char)[0] in ("L", "N") for char in text):
        raise ClientNameValidationError(_INVALID_NAME_TEXT)
    return text


class CarClientNameRepository:
    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def get(self, car_number: str) -> str | None:
        row = self._conn.execute(
            "SELECT display_name FROM georgia_car_client_names WHERE car_number = ?", (car_number,),
        ).fetchone()
        return row[0] if row else None

    def get_many(self, car_numbers: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for start in range(0, len(car_numbers), 500):
            chunk = car_numbers[start:start + 500]
            if not chunk:
                continue
            placeholders = ",".join("?" * len(chunk))
            for car_number, name in self._conn.execute(
                f"SELECT car_number, display_name FROM georgia_car_client_names WHERE car_number IN ({placeholders})",
                chunk,
            ):
                result[car_number] = name
        return result

    def set(self, car_number: str, display_name: str, *, updated_by: int | None) -> None:
        """display_name — уже проверенное normalize_client_name значение."""
        self._conn.execute(
            """
            INSERT INTO georgia_car_client_names (car_number, display_name, updated_at, updated_by)
            VALUES (?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(car_number) DO UPDATE SET
                display_name = excluded.display_name,
                updated_at = CURRENT_TIMESTAMP,
                updated_by = excluded.updated_by
            """,
            (car_number, display_name, updated_by),
        )
        self._conn.commit()

    def delete(self, car_number: str) -> bool:
        cursor = self._conn.execute("DELETE FROM georgia_car_client_names WHERE car_number = ?", (car_number,))
        self._conn.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        self._conn.close()
