import sqlite3
from pathlib import Path

from reader.users.models import TelegramUserInfo

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    is_bot INTEGER,
    keywords TEXT,
    last_seen_at TIMESTAMP,
    updated_at TIMESTAMP
)
"""

_ADD_KEYWORDS_COLUMN = "ALTER TABLE users ADD COLUMN keywords TEXT"

_UPSERT = """
INSERT INTO users (user_id, username, first_name, last_name, is_bot, last_seen_at, updated_at)
VALUES (:user_id, :username, :first_name, :last_name, :is_bot, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT(user_id) DO UPDATE SET
    username=COALESCE(excluded.username, users.username),
    first_name=COALESCE(excluded.first_name, users.first_name),
    last_name=COALESCE(excluded.last_name, users.last_name),
    is_bot=excluded.is_bot,
    last_seen_at=CURRENT_TIMESTAMP,
    updated_at=CURRENT_TIMESTAMP
"""

_SELECT = "SELECT user_id, username, first_name, last_name, is_bot FROM users WHERE user_id = ?"
_SELECT_KEYWORDS = "SELECT keywords FROM users WHERE user_id = ?"

_UPSERT_KEYWORDS = """
INSERT INTO users (user_id, keywords, last_seen_at, updated_at)
VALUES (:user_id, :keywords, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT(user_id) DO UPDATE SET
    keywords=:keywords,
    updated_at=CURRENT_TIMESTAMP
"""


def _parse_keywords(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [kw.strip() for kw in raw.split(",") if kw.strip()]


def _format_keywords(keywords: list[str]) -> str:
    return ", ".join(keywords)


class UserRepository:
    """Локальный кэш пользователей Telegram (username/имя) поверх SQLite."""

    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(_SCHEMA)
        self._migrate_add_keywords_column()
        self._conn.commit()

    def _migrate_add_keywords_column(self) -> None:
        """CREATE TABLE IF NOT EXISTS не добавляет колонку в уже
        существующую таблицу — для баз, созданных до появления keywords,
        добавляем её явно, без необходимости удалять/пересоздавать БД."""
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(users)")}
        if "keywords" not in columns:
            self._conn.execute(_ADD_KEYWORDS_COLUMN)

    def upsert(self, user: TelegramUserInfo) -> None:
        self._conn.execute(
            _UPSERT,
            {
                "user_id": user.user_id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "is_bot": int(user.is_bot),
            },
        )
        self._conn.commit()

    def get(self, user_id: int) -> TelegramUserInfo | None:
        row = self._conn.execute(_SELECT, (user_id,)).fetchone()
        if row is None:
            return None

        user_id, username, first_name, last_name, is_bot = row
        return TelegramUserInfo(
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            is_bot=bool(is_bot),
        )

    def add_keywords(self, user_id: int, keywords: list[str]) -> None:
        """Объединяет новые keywords с уже сохранёнными для user_id, без
        дублей, с сохранением порядка первого появления. Создаёт запись
        пользователя, если её ещё нет — остальные поля (username и т.п.)
        заполнит отдельный upsert(), если/когда появится эта информация;
        здесь они не трогаются и не перезаписываются.

        Общий метод для reader/main.py (Pipeline, по новым сообщениям) и
        reader/users/history_sync.py (sync_users.py, по истории) — чтобы не
        дублировать логику объединения списков.
        """
        if not keywords:
            return

        row = self._conn.execute(_SELECT_KEYWORDS, (user_id,)).fetchone()
        existing = _parse_keywords(row[0] if row else None)

        seen = set(existing)
        merged = list(existing)
        for keyword in keywords:
            if keyword not in seen:
                seen.add(keyword)
                merged.append(keyword)

        self._conn.execute(
            _UPSERT_KEYWORDS,
            {"user_id": user_id, "keywords": _format_keywords(merged)},
        )
        self._conn.commit()

    def get_keywords(self, user_id: int) -> list[str]:
        row = self._conn.execute(_SELECT_KEYWORDS, (user_id,)).fetchone()
        return _parse_keywords(row[0] if row else None)

    def count(self) -> int:
        """Общее количество пользователей в локальном кэше (SELECT COUNT(*))."""
        return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def close(self) -> None:
        self._conn.close()
