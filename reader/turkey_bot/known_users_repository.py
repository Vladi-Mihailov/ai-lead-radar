"""TurkeyBotKnownUsersRepository — turkey_bot_known_users поверх SQLite.

Единственный источник истины "написал ли этот numeric Telegram user_id
ЭТОМУ боту хотя бы раз" (см. reader/public_bot/known_users_repository.py —
тот же принцип, но СВОЯ таблица: этот факт per-bot, а не глобальный —
Telegram не позволяет боту первым писать тому, кто не написал ЕМУ, и это
верно независимо для @ProtocolGEbot и для Turkey-бота, даже если это один
и тот же человек с одним и тем же numeric telegram_user_id — см. design
report Stage 1, аудит: "bot_known_users" Георгии НЕ переиспользуется по
этой причине)."""

import sqlite3
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turkey_bot_known_users (
    telegram_user_id  INTEGER PRIMARY KEY,
    telegram_chat_id  INTEGER NOT NULL,
    telegram_username TEXT,
    first_seen_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_UPSERT = """
INSERT INTO turkey_bot_known_users (telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at)
VALUES (:telegram_user_id, :telegram_chat_id, :telegram_username, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT(telegram_user_id) DO UPDATE SET
    telegram_chat_id = excluded.telegram_chat_id,
    telegram_username = COALESCE(excluded.telegram_username, turkey_bot_known_users.telegram_username),
    last_seen_at = CURRENT_TIMESTAMP
"""

_SELECT = (
    "SELECT telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at "
    "FROM turkey_bot_known_users WHERE telegram_user_id = ?"
)

_SELECT_ALL_ORDERED = (
    "SELECT telegram_user_id, telegram_username FROM turkey_bot_known_users "
    "ORDER BY first_seen_at ASC, telegram_user_id ASC"
)


class TurkeyBotKnownUsersRepository:
    def __init__(self, db_path: Path | str):
        # ":memory:" - тот же приём, что и у остальных Turkey-репозиториев
        # (conversation_state_repository.py/check_repository.py/
        # user_cars_repository.py) - используется тестами, в production
        # всегда реальный путь (settings.app.users_db_file).
        is_memory = db_path == ":memory:"
        self._path = db_path if is_memory else Path(db_path)
        if not is_memory:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        if not is_memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def record_seen(
        self, *, telegram_user_id: int, telegram_chat_id: int, telegram_username: str | None,
    ) -> None:
        self._conn.execute(
            _UPSERT,
            {
                "telegram_user_id": telegram_user_id,
                "telegram_chat_id": telegram_chat_id,
                "telegram_username": telegram_username,
            },
        )
        self._conn.commit()

    def is_known(self, telegram_user_id: int) -> bool:
        return self._conn.execute(_SELECT, (telegram_user_id,)).fetchone() is not None

    def get_username(self, telegram_user_id: int) -> str | None:
        """Актуальный auto-captured username ОДНОГО пользователя — см.
        задачу "manager/trusted 'Мои автомобили' для Turkey bot": owner
        каждой строки резолвится по turkey_bot_user_cars.telegram_user_id,
        а не по вызывающему (менеджеру) — этот метод и даёт такой
        per-owner lookup (в отличие от list_all()/list_all_with_chat_id(),
        которые отдают ВЕСЬ список сразу, для статистики). None — либо
        пользователь никогда не писал боту вовсе, либо писал, но у него
        нет публичного Telegram username (тот же смысл, что и у
        record_seen(telegram_username=None) — не путать с "не найден
        вовсе")."""
        row = self._conn.execute(_SELECT, (telegram_user_id,)).fetchone()
        if row is None:
            return None
        return row[2]  # telegram_username, см. _SELECT column order

    def count_total(self) -> int:
        """Всего уникальных Telegram user id, когда-либо написавших ЭТОМУ
        боту — telegram_user_id уже PRIMARY KEY (см. _SCHEMA), тот же
        приём, что и reader/public_bot/known_users_repository.py."""
        return self._conn.execute("SELECT COUNT(*) FROM turkey_bot_known_users").fetchone()[0]

    def count_first_seen_since(self, since: datetime) -> int:
        """Сколько пользователей появились ВПЕРВЫЕ, начиная с since — по
        first_seen_at (выставляется один раз при INSERT и НИКОГДА не
        обновляется, см. _UPSERT выше), не по last_seen_at (тот же приём
        и то же обоснование, что и reader/public_bot/
        known_users_repository.py::count_first_seen_since). since —
        aware datetime (обычно UTC), сравнивается как текстовая
        ISO-строка того же формата, что и SQLite CURRENT_TIMESTAMP."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM turkey_bot_known_users WHERE first_seen_at >= ?",
            (since.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchone()
        return row[0]

    def list_all(self) -> list[tuple[int, str | None]]:
        """(telegram_user_id, telegram_username) для КАЖДОГО известного
        пользователя, отсортировано по first_seen_at ASC, telegram_user_id
        ASC (см. задачу: "deterministic ordering") — НИКОГДА не отдаёт
        telegram_chat_id или что-либо ещё (см. задачу: "never expose
        chat_id or other internal fields") — сама структура возврата
        (двухэлементный tuple) делает это структурно невозможным, не
        только "не показано"."""
        rows = self._conn.execute(_SELECT_ALL_ORDERED).fetchall()
        return [(telegram_user_id, telegram_username) for telegram_user_id, telegram_username in rows]

    def list_all_with_chat_id(self) -> list[tuple[int, int]]:
        """(telegram_user_id, telegram_chat_id) для КАЖДОГО известного
        пользователя — ОТДЕЛЬНЫЙ метод от list_all() выше: тот инвариант
        ("НИКОГДА telegram_chat_id") остаётся в силе именно для list_all()
        (менеджерская статистика/список пользователей, см. его докстрок).
        Этот метод — для случаев, где chat_id реально НУЖЕН для работы, а
        не для показа: отправка сообщений (см. reader/turkey_bot/
        migration_notify.py::notify_all — единственный текущий
        потребитель), НИКОГДА не для рендеринга пользователю/менеджеру."""
        rows = self._conn.execute(
            "SELECT telegram_user_id, telegram_chat_id FROM turkey_bot_known_users "
            "ORDER BY first_seen_at ASC, telegram_user_id ASC",
        ).fetchall()
        return [(telegram_user_id, telegram_chat_id) for telegram_user_id, telegram_chat_id in rows]

    def close(self) -> None:
        self._conn.close()
