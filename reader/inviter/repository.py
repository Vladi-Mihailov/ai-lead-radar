import sqlite3
from datetime import datetime
from pathlib import Path

from reader.inviter.models import (
    InviteCampaign,
    InviteCandidate,
    TelegramAccount,
    UserCampaignInvite,
)
from reader.users.repository import UserRepository

_TELEGRAM_ACCOUNTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    phone           TEXT NOT NULL,
    session_name    TEXT NOT NULL,
    session_path    TEXT NOT NULL,
    daily_limit     INTEGER NOT NULL DEFAULT 30,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at    TIMESTAMP
)
"""

_INVITE_CAMPAIGNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS invite_campaigns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    keyword      TEXT NOT NULL,
    target_chat  TEXT NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_USER_CAMPAIGN_INVITES_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_campaign_invites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    campaign_id  INTEGER NOT NULL,
    account_id   INTEGER,
    status       TEXT NOT NULL DEFAULT 'pending',
    error        TEXT,
    invited_at   TIMESTAMP,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_USER_CAMPAIGN_INVITES_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_user_campaign_invites_user_id "
    "ON user_campaign_invites (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_user_campaign_invites_campaign_id "
    "ON user_campaign_invites (campaign_id)",
    "CREATE INDEX IF NOT EXISTS idx_user_campaign_invites_account_id "
    "ON user_campaign_invites (account_id)",
    "CREATE INDEX IF NOT EXISTS idx_user_campaign_invites_status "
    "ON user_campaign_invites (status)",
)


def _connect(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _parse_keywords_column(raw: str | None) -> list[str]:
    """users.keywords хранится как "kw1, kw2, kw3" (см.
    reader/users/repository.py) — тот же формат разбора, но без импорта
    приватной функции из другого пакета."""
    if not raw:
        return []
    return [kw.strip() for kw in raw.split(",") if kw.strip()]


# Кандидат подходит кампании, если keyword кампании — один из
# запятая-разделённых токенов users.keywords. Оборачиваем обе стороны в
# ", " перед LIKE, чтобы искать ТОЧНЫЙ токен, а не произвольную подстроку
# (иначе keyword "ко" ложно совпал бы внутри "каско"). Общая часть для
# count_candidates()/select_candidates() (с фильтром по username) и
# count_found_candidates() (без него, см. ниже) — единый фрагмент, чтобы
# оба запроса не могли разойтись между собой.
_CANDIDATES_BASE_WHERE = """
    u.access_hash IS NOT NULL
    AND (', ' || u.keywords || ', ') LIKE ('%, ' || c.keyword || ', %')
    AND NOT EXISTS (
        SELECT 1 FROM user_campaign_invites uci
        WHERE uci.user_id = u.user_id
          AND uci.campaign_id = :campaign_id
          AND uci.status = 'invited'
    )
"""

# username IS NOT NULL/не пуст — обязателен: если candidate не известен
# приглашающему аккаунту (InviterService._resolve_input_peer:
# client.get_input_entity() не нашёл его в кэше ЭТОГО аккаунта — а
# access_hash в users.db получен ДРУГИМ, читающим, аккаунтом и для
# приглашающего часто невалиден), единственный способ его всё же
# резолвить — client.get_entity("@username"). Без username такой
# candidate физически не подготовить ни для одного нового аккаунта
# ("... не известен этому аккаунту и не имеет username для резолва") —
# поэтому не выбираем его вовсе, а не проваливаем каждую попытку.
_USERNAME_FILTER = "u.username IS NOT NULL AND TRIM(u.username) <> ''"

_CANDIDATES_WHERE = f"{_CANDIDATES_BASE_WHERE} AND {_USERNAME_FILTER}"

_COUNT_CANDIDATES = f"""
SELECT COUNT(*)
FROM users u
JOIN invite_campaigns c ON c.id = :campaign_id
WHERE {_CANDIDATES_WHERE}
"""

# "Всего найдено" для операторского отчёта (см.
# InviterService._notify_campaign_result) — те же условия, что и
# count_candidates(), но БЕЗ фильтра по username, чтобы можно было
# показать, сколько кандидатов отсеялось именно из-за его отсутствия
# (found_total - count_candidates()).
_COUNT_FOUND_CANDIDATES = f"""
SELECT COUNT(*)
FROM users u
JOIN invite_campaigns c ON c.id = :campaign_id
WHERE {_CANDIDATES_BASE_WHERE}
"""

_SELECT_CANDIDATES = f"""
SELECT u.user_id, u.username, u.keywords, u.access_hash, u.last_seen_at
FROM users u
JOIN invite_campaigns c ON c.id = :campaign_id
WHERE {_CANDIDATES_WHERE}
ORDER BY u.last_seen_at DESC
LIMIT :limit
"""


def _row_to_candidate(row) -> InviteCandidate:
    user_id, username, keywords, access_hash, last_seen_at = row
    return InviteCandidate(
        user_id=user_id,
        username=username,
        keywords=_parse_keywords_column(keywords),
        access_hash=access_hash,
        last_seen_at=_parse_datetime(last_seen_at),
    )


class TelegramAccountRepository:
    """CRUD поверх telegram_accounts (SQLite) — без бизнес-логики: выбор
    аккаунта, проверка daily_limit и т.п. реализуются в service.py, когда
    появится сама логика приглашений."""

    _UPDATABLE_COLUMNS = (
        "name", "phone", "session_name", "session_path",
        "daily_limit", "enabled", "last_used_at",
    )

    def __init__(self, db_path: Path):
        self._conn = _connect(db_path)
        self._conn.execute(_TELEGRAM_ACCOUNTS_SCHEMA)
        self._conn.commit()

    def create(
        self,
        *,
        name: str,
        phone: str,
        session_name: str,
        session_path: str,
        daily_limit: int = 30,
        enabled: bool = True,
    ) -> TelegramAccount:
        cursor = self._conn.execute(
            """
            INSERT INTO telegram_accounts (
                name, phone, session_name, session_path, daily_limit, enabled
            ) VALUES (:name, :phone, :session_name, :session_path, :daily_limit, :enabled)
            """,
            {
                "name": name,
                "phone": phone,
                "session_name": session_name,
                "session_path": session_path,
                "daily_limit": daily_limit,
                "enabled": enabled,
            },
        )
        self._conn.commit()

        account = self.get(cursor.lastrowid)
        if account is None:
            raise RuntimeError("Не удалось прочитать только что созданный аккаунт")
        return account

    def update(self, account_id: int, **fields) -> TelegramAccount:
        unknown = set(fields) - set(self._UPDATABLE_COLUMNS)
        if unknown:
            raise ValueError(f"Неизвестные поля для обновления: {sorted(unknown)}")

        if fields:
            if "last_used_at" in fields:
                fields["last_used_at"] = _isoformat(fields["last_used_at"])
            assignments = ", ".join(f"{column} = :{column}" for column in fields)
            self._conn.execute(
                f"UPDATE telegram_accounts SET {assignments} WHERE id = :id",
                {**fields, "id": account_id},
            )
            self._conn.commit()

        account = self.get(account_id)
        if account is None:
            raise RuntimeError(f"Аккаунт {account_id} не найден")
        return account

    def get(self, account_id: int) -> TelegramAccount | None:
        row = self._conn.execute(
            """
            SELECT id, name, phone, session_name, session_path, daily_limit,
                   enabled, created_at, last_used_at
            FROM telegram_accounts WHERE id = ?
            """,
            (account_id,),
        ).fetchone()
        return _row_to_account(row) if row else None

    def list(self) -> list[TelegramAccount]:
        rows = self._conn.execute(
            """
            SELECT id, name, phone, session_name, session_path, daily_limit,
                   enabled, created_at, last_used_at
            FROM telegram_accounts ORDER BY id
            """
        ).fetchall()
        return [_row_to_account(row) for row in rows]

    def close(self) -> None:
        self._conn.close()


def _row_to_account(row) -> TelegramAccount:
    (
        id_, name, phone, session_name, session_path, daily_limit,
        enabled, created_at, last_used_at,
    ) = row
    return TelegramAccount(
        id=id_,
        name=name,
        phone=phone,
        session_name=session_name,
        session_path=session_path,
        daily_limit=daily_limit,
        enabled=bool(enabled),
        created_at=_parse_datetime(created_at),
        last_used_at=_parse_datetime(last_used_at),
    )


class InviteCampaignRepository:
    """CRUD поверх invite_campaigns (SQLite) — без бизнес-логики: подбор
    пользователей по keyword и сама отправка приглашений реализуются в
    service.py, когда появится сама логика приглашений."""

    _UPDATABLE_COLUMNS = ("name", "keyword", "target_chat", "enabled")

    def __init__(self, db_path: Path):
        self._conn = _connect(db_path)
        self._conn.execute(_INVITE_CAMPAIGNS_SCHEMA)
        self._conn.commit()

    def create(
        self, *, name: str, keyword: str, target_chat: str, enabled: bool = True,
    ) -> InviteCampaign:
        cursor = self._conn.execute(
            """
            INSERT INTO invite_campaigns (name, keyword, target_chat, enabled)
            VALUES (:name, :keyword, :target_chat, :enabled)
            """,
            {"name": name, "keyword": keyword, "target_chat": target_chat, "enabled": enabled},
        )
        self._conn.commit()

        campaign = self.get(cursor.lastrowid)
        if campaign is None:
            raise RuntimeError("Не удалось прочитать только что созданную кампанию")
        return campaign

    def update(self, campaign_id: int, **fields) -> InviteCampaign:
        unknown = set(fields) - set(self._UPDATABLE_COLUMNS)
        if unknown:
            raise ValueError(f"Неизвестные поля для обновления: {sorted(unknown)}")

        if fields:
            assignments = ", ".join(f"{column} = :{column}" for column in fields)
            self._conn.execute(
                f"UPDATE invite_campaigns SET {assignments} WHERE id = :id",
                {**fields, "id": campaign_id},
            )
            self._conn.commit()

        campaign = self.get(campaign_id)
        if campaign is None:
            raise RuntimeError(f"Кампания {campaign_id} не найдена")
        return campaign

    def get(self, campaign_id: int) -> InviteCampaign | None:
        row = self._conn.execute(
            """
            SELECT id, name, keyword, target_chat, enabled, created_at
            FROM invite_campaigns WHERE id = ?
            """,
            (campaign_id,),
        ).fetchone()
        return _row_to_campaign(row) if row else None

    def list(self) -> list[InviteCampaign]:
        rows = self._conn.execute(
            """
            SELECT id, name, keyword, target_chat, enabled, created_at
            FROM invite_campaigns ORDER BY id
            """
        ).fetchall()
        return [_row_to_campaign(row) for row in rows]

    def close(self) -> None:
        self._conn.close()


def _row_to_campaign(row) -> InviteCampaign:
    id_, name, keyword, target_chat, enabled, created_at = row
    return InviteCampaign(
        id=id_,
        name=name,
        keyword=keyword,
        target_chat=target_chat,
        enabled=bool(enabled),
        created_at=_parse_datetime(created_at),
    )


class UserCampaignInviteRepository:
    """CRUD поверх user_campaign_invites (SQLite) — без бизнес-логики: набор
    допустимых значений status и переходы между ними здесь не определяются
    (см. service.py, когда появится сама логика приглашений).

    select_candidates()/count_candidates() читают таблицу users, которой
    владеет и мигрирует UserRepository (reader/users/repository.py) — эта
    БД могла быть создана до появления keywords/access_hash там, а
    sync_users.py/main.py (единственные, кто раньше открывал UserRepository
    и тем самым мигрировал users) на момент первого запуска инвайтера могли
    ещё не запускаться в этом окружении. Поэтому здесь эта же миграция
    выполняется явно (создание временного UserRepository, без дублирования
    её схемы/ALTER-логики) — как и остальные таблицы проекта, users
    подхватывает актуальную схему автоматически, без ручных правок SQLite."""

    _UPDATABLE_COLUMNS = ("account_id", "status", "error", "invited_at")

    def __init__(self, db_path: Path):
        UserRepository(db_path).close()
        self._conn = _connect(db_path)
        self._conn.execute(_USER_CAMPAIGN_INVITES_SCHEMA)
        for statement in _USER_CAMPAIGN_INVITES_INDEXES:
            self._conn.execute(statement)
        self._conn.commit()

    def create(
        self,
        *,
        user_id: int,
        campaign_id: int,
        account_id: int | None = None,
        status: str = "pending",
        error: str | None = None,
        invited_at: datetime | None = None,
    ) -> UserCampaignInvite:
        cursor = self._conn.execute(
            """
            INSERT INTO user_campaign_invites (
                user_id, campaign_id, account_id, status, error, invited_at
            ) VALUES (:user_id, :campaign_id, :account_id, :status, :error, :invited_at)
            """,
            {
                "user_id": user_id,
                "campaign_id": campaign_id,
                "account_id": account_id,
                "status": status,
                "error": error,
                "invited_at": _isoformat(invited_at),
            },
        )
        self._conn.commit()

        invite = self.get(cursor.lastrowid)
        if invite is None:
            raise RuntimeError("Не удалось прочитать только что созданное приглашение")
        return invite

    def update(self, invite_id: int, **fields) -> UserCampaignInvite:
        unknown = set(fields) - set(self._UPDATABLE_COLUMNS)
        if unknown:
            raise ValueError(f"Неизвестные поля для обновления: {sorted(unknown)}")

        if fields:
            if "invited_at" in fields:
                fields["invited_at"] = _isoformat(fields["invited_at"])
            assignments = ", ".join(f"{column} = :{column}" for column in fields)
            self._conn.execute(
                f"UPDATE user_campaign_invites "
                f"SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = :id",
                {**fields, "id": invite_id},
            )
            self._conn.commit()

        invite = self.get(invite_id)
        if invite is None:
            raise RuntimeError(f"Приглашение {invite_id} не найдено")
        return invite

    def get(self, invite_id: int) -> UserCampaignInvite | None:
        row = self._conn.execute(
            """
            SELECT id, user_id, campaign_id, account_id, status, error,
                   invited_at, created_at, updated_at
            FROM user_campaign_invites WHERE id = ?
            """,
            (invite_id,),
        ).fetchone()
        return _row_to_invite(row) if row else None

    def list(self) -> list[UserCampaignInvite]:
        rows = self._conn.execute(
            """
            SELECT id, user_id, campaign_id, account_id, status, error,
                   invited_at, created_at, updated_at
            FROM user_campaign_invites ORDER BY id
            """
        ).fetchall()
        return [_row_to_invite(row) for row in rows]

    def count_candidates(self, campaign_id: int) -> int:
        """Сколько пользователей подходят кампании campaign_id прямо сейчас
        (access_hash есть, username есть и не пуст, keyword кампании — среди
        их keywords, ещё не приглашены для этой кампании) — без учёта limit,
        см. select_candidates()."""
        row = self._conn.execute(_COUNT_CANDIDATES, {"campaign_id": campaign_id}).fetchone()
        return row[0]

    def count_found_candidates(self, campaign_id: int) -> int:
        """То же самое, что count_candidates(), но БЕЗ фильтра по
        username — "всего найдено" для операторского отчёта (см.
        InviterService._notify_campaign_result). Разница
        count_found_candidates() - count_candidates() — сколько
        подходящих по остальным условиям пользователей физически нельзя
        подготовить к приглашению из-за отсутствия username."""
        row = self._conn.execute(_COUNT_FOUND_CANDIDATES, {"campaign_id": campaign_id}).fetchone()
        return row[0]

    def select_candidates(self, campaign_id: int, *, limit: int) -> list[InviteCandidate]:
        """Кандидаты на приглашение в кампанию campaign_id: keywords
        содержит keyword кампании, access_hash задан и username задан и не
        пуст (иначе приглашающий аккаунт, которому candidate не известен,
        не сможет его резолвить вовсе — см. InviterService._resolve_input_peer
        и требование задачи о фильтрации по username), ещё нет записи со
        status='invited' для ЭТОЙ кампании — отсортированные по
        last_seen_at DESC, не более limit штук. Только выборка — никаких
        записей в user_campaign_invites не создаёт и не изменяет (см.
        service.py)."""
        rows = self._conn.execute(
            _SELECT_CANDIDATES, {"campaign_id": campaign_id, "limit": limit}
        ).fetchall()
        return [_row_to_candidate(row) for row in rows]

    def close(self) -> None:
        self._conn.close()


def _row_to_invite(row) -> UserCampaignInvite:
    (
        id_, user_id, campaign_id, account_id, status, error,
        invited_at, created_at, updated_at,
    ) = row
    return UserCampaignInvite(
        id=id_,
        user_id=user_id,
        campaign_id=campaign_id,
        account_id=account_id,
        status=status,
        error=error,
        invited_at=_parse_datetime(invited_at),
        created_at=_parse_datetime(created_at),
        updated_at=_parse_datetime(updated_at),
    )
