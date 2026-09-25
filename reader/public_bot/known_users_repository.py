"""BotKnownUsersRepository — bot_known_users поверх SQLite.

Единственный источник истины "написал ли этот numeric Telegram user_id
боту хотя бы раз" (см. design report: Telegram не позволяет боту первым
писать пользователю, который никогда не начинал с ним диалог — резолв
@username в numeric id сам по себе НЕ гарантирует возможность что-либо
ему доставить). Обновляется на КАЖДОЕ входящее событие (текстовое
сообщение или нажатие inline-кнопки), независимо от его содержимого — см.
reader/public_bot/handlers.py.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from reader.public_bot.models import BotKnownUser

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_known_users (
    telegram_user_id  INTEGER PRIMARY KEY,
    telegram_chat_id  INTEGER NOT NULL,
    telegram_username TEXT,
    first_seen_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# first_name/last_name (см. задачу "add name search to trusted bot
# search") — additive ALTER TABLE, тот же приём, что и
# reader/fines/task_repository.py::_COLUMN_MIGRATIONS — для БД, созданных
# до появления этих колонок, добавляем их явно при открытии, без
# удаления/пересоздания. NULL по умолчанию для уже существующих строк —
# самолечится следующим же сообщением этого пользователя боту (см.
# record_seen ниже).
_COLUMN_MIGRATIONS = {
    "first_name": "ALTER TABLE bot_known_users ADD COLUMN first_name TEXT",
    "last_name": "ALTER TABLE bot_known_users ADD COLUMN last_name TEXT",
}

_SELECT_FIELDS = (
    "telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at, "
    "first_name, last_name"
)

_UPSERT = """
INSERT INTO bot_known_users (
    telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at,
    first_name, last_name
)
VALUES (
    :telegram_user_id, :telegram_chat_id, :telegram_username, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
    :first_name, :last_name
)
ON CONFLICT(telegram_user_id) DO UPDATE SET
    telegram_chat_id = excluded.telegram_chat_id,
    telegram_username = COALESCE(excluded.telegram_username, bot_known_users.telegram_username),
    first_name = COALESCE(excluded.first_name, bot_known_users.first_name),
    last_name = COALESCE(excluded.last_name, bot_known_users.last_name),
    last_seen_at = CURRENT_TIMESTAMP
"""

_SELECT = f"SELECT {_SELECT_FIELDS} FROM bot_known_users WHERE telegram_user_id = ?"

# Manager/trusted Search по @username (см. задачу) — COLLATE NOCASE даёт
# case-insensitive сравнение (задача явно требует: "case-insensitive
# search"), без отдельной нормализации регистра в Python. telegram_username
# не PRIMARY KEY — теоретически возможна гонка (два разных telegram_user_id
# сохранили один и тот же username в разное время, старое значение ещё не
# обновлено до нового владельца, см. _UPSERT COALESCE) — берём самый
# СВЕЖИЙ по last_seen_at, а не произвольную строку.
_SELECT_BY_USERNAME = (
    f"SELECT {_SELECT_FIELDS} FROM bot_known_users WHERE telegram_username = ? COLLATE NOCASE "
    "ORDER BY last_seen_at DESC LIMIT 1"
)

# Manager/trusted Search по имени (см. задачу "add name search to trusted
# bot search" п.4) — EXACT normalized match (TRIM + LOWER), НЕ
# prefix/substring (задача явно предпочитает точное совпадение, чтобы не
# давать случайных результатов) — по first_name ИЛИ last_name ИЛИ полному
# "first_name last_name". COALESCE(..., '') на обеих частях full_name —
# без него SQLite-конкатенация с NULL (last_name отсутствует) дала бы
# NULL целиком, и полное имя никогда бы не совпало. Имена НЕ уникальны
# (см. задачу п.6) — возвращает ВСЕХ совпавших пользователей, не первого.
_SELECT_BY_NAME = f"""
    SELECT {_SELECT_FIELDS} FROM bot_known_users
    WHERE LOWER(TRIM(first_name)) = LOWER(TRIM(:query))
       OR LOWER(TRIM(last_name)) = LOWER(TRIM(:query))
       OR LOWER(TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))) = LOWER(TRIM(:query))
    ORDER BY last_seen_at DESC
"""


def _unicode_lower(value: str | None) -> str | None:
    """Заменяет встроенный SQLite LOWER() (см. __init__::create_function) —
    он по умолчанию регистронезависим ТОЛЬКО для ASCII (см. задачу "add
    name search to trusted bot search" п.4: "case-insensitive" — реальные
    имена пользователей часто кириллические, а SQLite LOWER('ИВАН')
    без этого возвращает 'ИВАН' без изменений, из-за чего _SELECT_BY_NAME
    молча не находил бы ни одного совпадения при разном регистре
    кириллицы). Python str.lower() корректно обрабатывает Unicode —
    ASCII-сравнения (username, см. _SELECT_BY_USERNAME) это никак не
    меняет: COLLATE NOCASE для них не использует LOWER() вовсе."""
    return value.lower() if isinstance(value, str) else value


def _row_to_known_user(row) -> BotKnownUser:
    telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at, first_name, last_name = row
    return BotKnownUser(
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        telegram_username=telegram_username,
        first_seen_at=datetime.fromisoformat(first_seen_at),
        last_seen_at=datetime.fromisoformat(last_seen_at),
        first_name=first_name,
        last_name=last_name,
    )


class BotKnownUsersRepository:
    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.create_function("LOWER", 1, _unicode_lower)
        self._conn.execute(_SCHEMA)
        existing_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(bot_known_users)")}
        for column, statement in _COLUMN_MIGRATIONS.items():
            if column not in existing_columns:
                self._conn.execute(statement)
        self._conn.commit()

    def record_seen(
        self,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        telegram_username: str | None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> None:
        """Идемпотентный upsert — безопасно вызывать на КАЖДОЕ входящее
        событие без проверки "а был ли он уже здесь" заранее.
        first_name/last_name — Default None сохраняет существующие
        вызовы record_seen(...) без этих параметров (см. задачу: "не
        сломать существующий поиск")."""
        self._conn.execute(
            _UPSERT,
            {
                "telegram_user_id": telegram_user_id,
                "telegram_chat_id": telegram_chat_id,
                "telegram_username": telegram_username,
                "first_name": first_name,
                "last_name": last_name,
            },
        )
        self._conn.commit()

    def is_known(self, telegram_user_id: int) -> bool:
        return self._conn.execute(_SELECT, (telegram_user_id,)).fetchone() is not None

    def get(self, telegram_user_id: int) -> BotKnownUser | None:
        row = self._conn.execute(_SELECT, (telegram_user_id,)).fetchone()
        return _row_to_known_user(row) if row else None

    def find_by_username(self, username: str) -> BotKnownUser | None:
        """Case-insensitive lookup по telegram_username (см. задачу
        "manager/trusted Search"), НЕ по numeric telegram_user_id (тот —
        get() выше) — вызывающий код (ConversationController) передаёт
        уже trim'ленный, без ведущего "@" username. None — ни один
        известный пользователь не писал боту под этим username."""
        row = self._conn.execute(_SELECT_BY_USERNAME, (username,)).fetchone()
        return _row_to_known_user(row) if row else None

    def find_by_name(self, query: str) -> list[BotKnownUser]:
        """EXACT (после TRIM+LOWER) совпадение по first_name/last_name/
        полному имени (см. _SELECT_BY_NAME) — см. задачу "add name search
        to trusted bot search" п.4/п.6: имена НЕ уникальны, возвращает
        ВСЕХ совпавших пользователей (может быть пустой список), не
        первого попавшегося."""
        rows = self._conn.execute(_SELECT_BY_NAME, {"query": query}).fetchall()
        return [_row_to_known_user(row) for row in rows]

    def count_total(self) -> int:
        """Всего уникальных Telegram user id, когда-либо написавших боту —
        telegram_user_id уже PRIMARY KEY (см. _SCHEMA), COUNT(*) сам по
        себе не может посчитать дубликаты (см. "📊 Статистика", задача:
        "считать по уникальным Telegram user IDs")."""
        return self._conn.execute("SELECT COUNT(*) FROM bot_known_users").fetchone()[0]

    def count_first_seen_since(self, since: datetime) -> int:
        """Сколько пользователей появились ВПЕРВЫЕ, начиная с since —
        по first_seen_at (выставляется один раз при INSERT и НИКОГДА не
        обновляется, см. _UPSERT выше), а не last_seen_at/updated_at (см.
        "📊 Статистика", задача: "новые" по реальной дате первого
        появления, не по последней активности — иначе давно известный
        пользователь, просто снова написавший боту, ложно считался бы
        "новым"). since — aware datetime (обычно UTC), сравнивается как
        текстовая ISO-строка того же формата, что и SQLite
        CURRENT_TIMESTAMP (naive "YYYY-MM-DD HH:MM:SS", см.
        reader/time_display.py про этот же приём)."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM bot_known_users WHERE first_seen_at >= ?",
            (since.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchone()
        return row[0]

    def close(self) -> None:
        self._conn.close()
