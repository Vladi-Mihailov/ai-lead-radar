import sqlite3
from datetime import datetime
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
    access_hash INTEGER,
    peer_type TEXT,
    peer_updated_at TIMESTAMP,
    last_seen_at TIMESTAMP,
    updated_at TIMESTAMP,
    car_numbers TEXT
)
"""

# CREATE TABLE IF NOT EXISTS не добавляет колонки в уже существующую
# таблицу — для баз, созданных до появления каждого из этих полей,
# добавляем их явно при открытии, без удаления/пересоздания БД.
_COLUMN_MIGRATIONS = {
    "keywords": "ALTER TABLE users ADD COLUMN keywords TEXT",
    "access_hash": "ALTER TABLE users ADD COLUMN access_hash INTEGER",
    "peer_type": "ALTER TABLE users ADD COLUMN peer_type TEXT",
    "peer_updated_at": "ALTER TABLE users ADD COLUMN peer_updated_at TIMESTAMP",
    # Госномера, упомянутые пользователем в сообщениях (см.
    # reader/users/car_numbers.py) — nullable, как и keywords, чтобы не
    # терять уже существующие строки users.db на сервере.
    "car_numbers": "ALTER TABLE users ADD COLUMN car_numbers TEXT",
}

_UPSERT = """
INSERT INTO users (
    user_id, username, first_name, last_name, is_bot,
    access_hash, peer_type, peer_updated_at, last_seen_at, updated_at
)
VALUES (
    :user_id, :username, :first_name, :last_name, :is_bot,
    :access_hash, :peer_type,
    CASE WHEN :access_hash IS NOT NULL THEN CURRENT_TIMESTAMP ELSE NULL END,
    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
)
ON CONFLICT(user_id) DO UPDATE SET
    username=COALESCE(excluded.username, users.username),
    first_name=COALESCE(excluded.first_name, users.first_name),
    last_name=COALESCE(excluded.last_name, users.last_name),
    is_bot=excluded.is_bot,
    access_hash=COALESCE(excluded.access_hash, users.access_hash),
    peer_type=COALESCE(excluded.peer_type, users.peer_type),
    peer_updated_at=CASE
        WHEN excluded.access_hash IS NOT NULL THEN CURRENT_TIMESTAMP
        ELSE users.peer_updated_at
    END,
    last_seen_at=CURRENT_TIMESTAMP,
    updated_at=CURRENT_TIMESTAMP
"""

_SELECT = (
    "SELECT user_id, username, first_name, last_name, is_bot, access_hash, peer_type "
    "FROM users WHERE user_id = ?"
)
_SELECT_KEYWORDS = "SELECT keywords FROM users WHERE user_id = ?"
_SELECT_PEER_UPDATED_AT = "SELECT peer_updated_at FROM users WHERE user_id = ?"

_UPSERT_KEYWORDS = """
INSERT INTO users (user_id, keywords, last_seen_at, updated_at)
VALUES (:user_id, :keywords, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT(user_id) DO UPDATE SET
    keywords=:keywords,
    updated_at=CURRENT_TIMESTAMP
"""

_SELECT_CAR_NUMBERS = "SELECT car_numbers FROM users WHERE user_id = ?"

_SELECT_USERS_WITH_CAR_NUMBERS = (
    "SELECT user_id, username, first_name, last_name, is_bot, access_hash, peer_type, car_numbers "
    "FROM users WHERE car_numbers IS NOT NULL AND car_numbers != ''"
)

_UPSERT_CAR_NUMBERS = """
INSERT INTO users (user_id, car_numbers, last_seen_at, updated_at)
VALUES (:user_id, :car_numbers, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT(user_id) DO UPDATE SET
    car_numbers=:car_numbers,
    updated_at=CURRENT_TIMESTAMP
"""

_SELECT_ACCESS_HASH_AND_USERNAME = "SELECT access_hash, username, is_bot FROM users WHERE user_id = ?"

_UPDATE_ACCESS_HASH = """
UPDATE users
SET access_hash = :access_hash,
    username = COALESCE(:username, username),
    is_bot = COALESCE(:is_bot, is_bot),
    updated_at = CURRENT_TIMESTAMP,
    peer_updated_at = CURRENT_TIMESTAMP
WHERE user_id = :user_id
"""

_MARK_AS_BOT = """
UPDATE users
SET is_bot = 1,
    updated_at = CURRENT_TIMESTAMP
WHERE user_id = :user_id
"""


def _parse_keywords(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [kw.strip() for kw in raw.split(",") if kw.strip()]


def _format_keywords(keywords: list[str]) -> str:
    return ", ".join(keywords)


def _parse_car_numbers(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [number.strip() for number in raw.split(",") if number.strip()]


def _format_car_numbers(car_numbers: list[str]) -> str:
    return ", ".join(car_numbers)


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
        self._migrate_missing_columns()
        self._conn.commit()

    def _migrate_missing_columns(self) -> None:
        """CREATE TABLE IF NOT EXISTS не добавляет колонки в уже
        существующую таблицу — для баз, созданных до появления любого из
        этих полей, добавляем недостающие явно, без удаления/пересоздания БД."""
        existing_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(users)")}
        for column, statement in _COLUMN_MIGRATIONS.items():
            if column not in existing_columns:
                self._conn.execute(statement)

    def upsert(self, user: TelegramUserInfo) -> None:
        self._conn.execute(
            _UPSERT,
            {
                "user_id": user.user_id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "is_bot": int(user.is_bot),
                "access_hash": user.access_hash,
                "peer_type": user.peer_type,
            },
        )
        self._conn.commit()

    def get(self, user_id: int) -> TelegramUserInfo | None:
        row = self._conn.execute(_SELECT, (user_id,)).fetchone()
        if row is None:
            return None

        user_id, username, first_name, last_name, is_bot, access_hash, peer_type = row
        return TelegramUserInfo(
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            is_bot=bool(is_bot),
            access_hash=access_hash,
            peer_type=peer_type,
        )

    def get_peer_updated_at(self, user_id: int) -> datetime | None:
        row = self._conn.execute(_SELECT_PEER_UPDATED_AT, (user_id,)).fetchone()
        if row is None or row[0] is None:
            return None
        return datetime.fromisoformat(row[0])

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

    def add_car_numbers(self, user_id: int, car_numbers: list[str]) -> None:
        """Объединяет новые car_numbers (уже нормализованные — см.
        reader/users/car_numbers.py.extract_car_numbers) с уже
        сохранёнными для user_id, без дублей. В отличие от add_keywords()
        (порядок первого появления) хранит их СОРТИРОВАННЫМИ — для
        car_numbers важнее детерминированный порядок хранения, а не
        порядок появления в сообщениях (см. задачу). Создаёт запись
        пользователя, если её ещё нет — как и add_keywords().

        Общий метод для reader/core/pipeline.py (Pipeline, по новым
        сообщениям) и reader/users/history_sync.py (sync_users.py, по
        истории/backfill) — тот же архитектурный путь, что и add_keywords()."""
        if not car_numbers:
            return

        row = self._conn.execute(_SELECT_CAR_NUMBERS, (user_id,)).fetchone()
        existing = _parse_car_numbers(row[0] if row else None)

        merged = sorted(set(existing) | set(car_numbers))

        self._conn.execute(
            _UPSERT_CAR_NUMBERS,
            {"user_id": user_id, "car_numbers": _format_car_numbers(merged)},
        )
        self._conn.commit()

    def get_car_numbers(self, user_id: int) -> list[str]:
        row = self._conn.execute(_SELECT_CAR_NUMBERS, (user_id,)).fetchone()
        return _parse_car_numbers(row[0] if row else None)

    def find_by_car_number(self, car_number: str) -> list[TelegramUserInfo]:
        """Пользователи, у которых car_numbers содержит РОВНО этот номер —
        car_number должен быть уже нормализован вызывающим кодом (как
        fine_monitoring_tasks.car_number, см.
        reader/fines/validation.py.normalize_car_number(): один и тот же
        алфавит/регистр [A-Z0-9], без пробелов/дефисов, что и у значений,
        которые сюда кладёт reader/users/car_numbers.py.extract_car_numbers()).

        Намеренно НЕ использует SQL LIKE по car_numbers (строка вида
        "A111AA77, X777XX197") — подстрока могла бы случайно совпасть с
        частью другого номера (например, "A111AA77" внутри "XA111AA779").
        Вместо этого читает только строки с непустым car_numbers (обычно
        меньшинство таблицы) и сверяет каждую как уже разобранный список
        через _parse_car_numbers() — дёшево и однозначно корректно.

        Список, а не Optional[TelegramUserInfo]: add_car_numbers() не
        гарантирует уникальность car_number между разными user_id (два
        разных Telegram-пользователя вполне могут упомянуть один и тот же
        номер в истории чата) — вызывающий код обязан сам решить, что
        делать при 0 или >1 совпадениях, а не молча брать первое."""
        rows = self._conn.execute(_SELECT_USERS_WITH_CAR_NUMBERS).fetchall()

        matches = []
        for user_id, username, first_name, last_name, is_bot, access_hash, peer_type, car_numbers_raw in rows:
            if car_number in _parse_car_numbers(car_numbers_raw):
                matches.append(
                    TelegramUserInfo(
                        user_id=user_id,
                        username=username,
                        first_name=first_name,
                        last_name=last_name,
                        is_bot=bool(is_bot),
                        access_hash=access_hash,
                        peer_type=peer_type,
                    )
                )
        return matches

    def update_access_hash(
        self, user_id: int, access_hash: int, username: str | None = None,
        is_bot: bool | None = None,
    ) -> bool:
        """Обновляет ТОЛЬКО access_hash (и, если они реально отличаются,
        username/is_bot), заодно продвигая peer_updated_at — время получения
        Telegram peer-данных (access_hash/username), то же поле, что и
        upsert() ставит при появлении access_hash — уже существующего
        пользователя. Например, когда reader/inviter/service.py резолвит
        candidate лично СВОИМ аккаунтом через client.get_entity(...),
        потому что access_hash, сохранённый читающим аккаунтом
        (sync_users.py/main.py), не годится для другого (инвайтящего)
        аккаунта — тем же вызовом сохраняется и свежий User.bot, если его
        статус ранее был неизвестен (is_bot=NULL в БД), см. задачу про
        безопасность инвайтера. В отличие от upsert() ничего не создаёт —
        если user_id ещё не в таблице, ничего не делает и возвращает False.
        Никакие другие поля (first_name/last_name/keywords/peer_type/...) не
        трогает.

        is_bot=None (по умолчанию) означает "не передано/неизвестно" — как и
        у access_hash/peer_type в TelegramUserInfo, а НЕ "не бот": уже
        сохранённое значение не затирается. Передайте is_bot=True/False
        явно, только когда статус реально подтверждён Telethon в этот момент
        (см. InviterService._resolve_input_peer).

        Возвращает False без единого UPDATE (в т.ч. без сдвига
        peer_updated_at), если реально ничего не меняется — access_hash
        совпадает, username пуст либо совпадает с уже сохранённым, а is_bot
        не передан либо совпадает с уже сохранённым — чтобы не писать в БД
        на каждое приглашение."""
        row = self._conn.execute(_SELECT_ACCESS_HASH_AND_USERNAME, (user_id,)).fetchone()
        if row is None:
            return False

        current_access_hash, current_username, current_is_bot = row
        new_username = username if (username and username != current_username) else None
        is_bot_changed = is_bot is not None and int(is_bot) != current_is_bot

        if access_hash == current_access_hash and new_username is None and not is_bot_changed:
            return False

        self._conn.execute(
            _UPDATE_ACCESS_HASH,
            {
                "access_hash": access_hash,
                "username": new_username,
                "is_bot": int(is_bot) if is_bot_changed else None,
                "user_id": user_id,
            },
        )
        self._conn.commit()
        return True

    def mark_as_bot(self, user_id: int) -> bool:
        """Помечает пользователя ботом (is_bot=1) — используется
        InviterService, когда Telegram сам подтверждает это через RPC-ошибку
        при попытке приглашения (см. _classify_invite_error), в момент,
        когда полноценного entity (и его access_hash) уже нет под рукой —
        в отличие от update_access_hash(). Чтобы такой бот больше никогда не
        попадал в кандидаты ни для одной кампании (см. задачу об инциденте
        с приглашением Telegram-бота и 3-дневным ограничением аккаунта).

        Как и update_access_hash() — ничего не создаёт: если user_id ещё не
        в таблице, ничего не делает и возвращает False."""
        cursor = self._conn.execute(_MARK_AS_BOT, {"user_id": user_id})
        self._conn.commit()
        return cursor.rowcount > 0

    def count(self) -> int:
        """Общее количество пользователей в локальном кэше (SELECT COUNT(*))."""
        return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def close(self) -> None:
        self._conn.close()
