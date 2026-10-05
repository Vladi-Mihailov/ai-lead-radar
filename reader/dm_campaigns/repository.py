"""DmCampaignRepository — dm_campaigns/dm_campaign_accounts поверх того же
data/users.db, тот же стиль, что и у reader/inviter/repository.py: WAL,
additive CREATE TABLE IF NOT EXISTS при открытии, без DROP/пересоздания,
списки — запятая-разделённый TEXT (как invite_campaigns.source_chats).

Seed — "insert if absent" (INSERT OR IGNORE по UNIQUE key): повторное
открытие не создаёт дублей и НЕ трогает уже существующие строки —
enabled/инструкции/ресурсы, заданные менеджером, никогда не сбрасываются
к умолчаниям. Все seed-кампании создаются enabled=0.

dm_campaign_accounts.account_id — логическая ссылка на
telegram_accounts.id (существование проверяется явно при добавлении), без
FOREIGN KEY: так новая таблица никак не может повлиять на операции над
telegram_accounts в inviter (тот же users.db, PRAGMA foreign_keys=ON).

Phase 1: ни одного обращения к Telegram/OpenAI — только конфигурация."""

import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from reader.dm_campaigns.models import (
    CAMPAIGN_MODES,
    DAILY_LIMIT_MAX,
    DAILY_LIMIT_MIN,
    IMPLEMENTED_MODES,
    MAX_GUIDELINE_LENGTH,
    MAX_LIST_ITEM_LENGTH,
    MAX_LIST_ITEMS,
    MODE_MANUAL,
    DmCampaign,
    DmCampaignAccount,
    normalize_text_input,
)

_DM_CAMPAIGNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS dm_campaigns (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    key                  TEXT NOT NULL UNIQUE,
    title                TEXT NOT NULL,
    enabled              INTEGER NOT NULL DEFAULT 0,
    scenario_name        TEXT,
    ai_guideline         TEXT,
    resources            TEXT,
    source_chats         TEXT,
    fresh_context_chats  TEXT,
    follow_up_enabled    INTEGER NOT NULL DEFAULT 0,
    follow_up_guideline  TEXT,
    created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_DM_CAMPAIGN_ACCOUNTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS dm_campaign_accounts (
    campaign_id  INTEGER NOT NULL REFERENCES dm_campaigns(id),
    account_id   INTEGER NOT NULL,
    daily_limit  INTEGER,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (campaign_id, account_id)
)
"""

# (key, title, scenario_name). scenario_name — только хранится (связь с
# config/scenarios.yaml появится в Phase 2): "insurance" и
# "car_border_crossing" — существующие сценарии, "fuel" — будущий.
SEED_CAMPAIGNS = (
    ("fuel", "⛽ Бензин", "fuel"),
    ("border_queue", "🚧 Очереди на границе", "car_border_crossing"),
    ("insurance", "🛡 Страховка", "insurance"),
)

# Аддитивные колонки dm_campaigns. mode: существующие и новые кампании
# получают 'manual' (DEFAULT) — ни одна не начинает отправлять сама.
_DM_CAMPAIGNS_COLUMN_MIGRATIONS = (
    ("mode", f"TEXT NOT NULL DEFAULT '{MODE_MANUAL}'"),
)

_SEED = (
    "INSERT OR IGNORE INTO dm_campaigns (key, title, scenario_name, enabled) "
    "VALUES (?, ?, ?, 0)"
)

_CAMPAIGN_COLUMNS = (
    "id, key, title, enabled, scenario_name, ai_guideline, resources, source_chats, "
    "fresh_context_chats, follow_up_enabled, follow_up_guideline, created_at, updated_at, mode"
)


def _connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _parse_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _format_list(items: Iterable[str]) -> str | None:
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    return ", ".join(cleaned) if cleaned else None


def _validate_list(items: Iterable[str]) -> tuple[str, ...]:
    cleaned = tuple(str(item).strip() for item in items if str(item).strip())
    if len(cleaned) > MAX_LIST_ITEMS:
        raise ValueError(f"Слишком много элементов (максимум {MAX_LIST_ITEMS}).")
    for item in cleaned:
        # Запятая — разделитель хранения, внутри элемента её быть не может.
        if "," in item:
            raise ValueError("Элемент списка не может содержать запятую.")
        if len(item) > MAX_LIST_ITEM_LENGTH:
            raise ValueError(f"Элемент длиннее {MAX_LIST_ITEM_LENGTH} символов.")
    return cleaned


def _validate_text(text: str | None) -> str | None:
    normalized = normalize_text_input(text)
    if normalized is not None and len(normalized) > MAX_GUIDELINE_LENGTH:
        raise ValueError(f"Текст длиннее {MAX_GUIDELINE_LENGTH} символов.")
    return normalized


def _row_to_campaign(row) -> DmCampaign:
    (
        id_, key, title, enabled, scenario_name, ai_guideline, resources, source_chats,
        fresh_context_chats, follow_up_enabled, follow_up_guideline, created_at, updated_at, mode,
    ) = row
    return DmCampaign(
        id=id_,
        key=key,
        title=title,
        enabled=bool(enabled),
        scenario_name=scenario_name,
        ai_guideline=ai_guideline,
        resources=_parse_list(resources),
        source_chats=_parse_list(source_chats),
        fresh_context_chats=_parse_list(fresh_context_chats),
        follow_up_enabled=bool(follow_up_enabled),
        follow_up_guideline=follow_up_guideline,
        created_at=_parse_datetime(created_at),
        updated_at=_parse_datetime(updated_at),
        mode=mode or MODE_MANUAL,
    )


class DmCampaignRepository:
    def __init__(self, db_path: Path | str):
        self._conn = _connect(db_path)
        self._conn.execute(_DM_CAMPAIGNS_SCHEMA)
        self._conn.execute(_DM_CAMPAIGN_ACCOUNTS_SCHEMA)
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(dm_campaigns)")}
        for column, definition in _DM_CAMPAIGNS_COLUMN_MIGRATIONS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE dm_campaigns ADD COLUMN {column} {definition}")
        self._conn.executemany(_SEED, SEED_CAMPAIGNS)
        self._conn.commit()

    # ---- кампании ----

    def list_campaigns(self) -> list[DmCampaign]:
        rows = self._conn.execute(f"SELECT {_CAMPAIGN_COLUMNS} FROM dm_campaigns ORDER BY id").fetchall()
        return [_row_to_campaign(row) for row in rows]

    def get_campaign(self, campaign_id: int) -> DmCampaign | None:
        row = self._conn.execute(
            f"SELECT {_CAMPAIGN_COLUMNS} FROM dm_campaigns WHERE id = ?", (campaign_id,),
        ).fetchone()
        return _row_to_campaign(row) if row else None

    def get_campaign_by_key(self, key: str) -> DmCampaign | None:
        row = self._conn.execute(
            f"SELECT {_CAMPAIGN_COLUMNS} FROM dm_campaigns WHERE key = ?", (key,),
        ).fetchone()
        return _row_to_campaign(row) if row else None

    def _update(self, campaign_id: int, **fields) -> DmCampaign | None:
        """Единственное место UPDATE dm_campaigns — имена колонок приходят
        только из методов ниже, не извне. None — кампании нет."""
        assignments = ", ".join(f"{column} = :{column}" for column in fields)
        cursor = self._conn.execute(
            f"UPDATE dm_campaigns SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = :id",
            {**fields, "id": campaign_id},
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_campaign(campaign_id)

    def set_enabled(self, campaign_id: int, enabled: bool) -> DmCampaign | None:
        return self._update(campaign_id, enabled=int(bool(enabled)))

    def set_mode(self, campaign_id: int, mode: str) -> DmCampaign | None:
        """auto — допустимое по схеме, но НЕ реализованное значение:
        установить его нельзя, пока нет автоматической отправки."""
        if mode not in CAMPAIGN_MODES:
            raise ValueError(f"Неизвестный режим кампании: {mode!r}")
        if mode not in IMPLEMENTED_MODES:
            raise ValueError(f"Режим {mode!r} ещё не реализован — доступен только ручной режим.")
        return self._update(campaign_id, mode=mode)

    def update_guideline(self, campaign_id: int, text: str | None) -> DmCampaign | None:
        return self._update(campaign_id, ai_guideline=_validate_text(text))

    def update_resources(self, campaign_id: int, items: Iterable[str]) -> DmCampaign | None:
        return self._update(campaign_id, resources=_format_list(_validate_list(items)))

    def update_source_chats(self, campaign_id: int, items: Iterable[str]) -> DmCampaign | None:
        """Пустой список — "все отслеживаемые группы" (см. DmCampaign)."""
        return self._update(campaign_id, source_chats=_format_list(_validate_list(items)))

    def update_fresh_context_chats(self, campaign_id: int, items: Iterable[str]) -> DmCampaign | None:
        return self._update(campaign_id, fresh_context_chats=_format_list(_validate_list(items)))

    def set_follow_up_enabled(self, campaign_id: int, enabled: bool) -> DmCampaign | None:
        return self._update(campaign_id, follow_up_enabled=int(bool(enabled)))

    def update_follow_up_guideline(self, campaign_id: int, text: str | None) -> DmCampaign | None:
        return self._update(campaign_id, follow_up_guideline=_validate_text(text))

    # ---- allowlist аккаунтов ----

    def list_campaign_accounts(self, campaign_id: int) -> list[DmCampaignAccount]:
        rows = self._conn.execute(
            "SELECT campaign_id, account_id, daily_limit FROM dm_campaign_accounts "
            "WHERE campaign_id = ? ORDER BY account_id",
            (campaign_id,),
        ).fetchall()
        return [DmCampaignAccount(campaign_id=c, account_id=a, daily_limit=d) for c, a, d in rows]

    def get_campaign_account(self, campaign_id: int, account_id: int) -> DmCampaignAccount | None:
        row = self._conn.execute(
            "SELECT campaign_id, account_id, daily_limit FROM dm_campaign_accounts "
            "WHERE campaign_id = ? AND account_id = ?",
            (campaign_id, account_id),
        ).fetchone()
        return DmCampaignAccount(campaign_id=row[0], account_id=row[1], daily_limit=row[2]) if row else None

    def _account_exists(self, account_id: int) -> bool:
        try:
            row = self._conn.execute(
                "SELECT 1 FROM telegram_accounts WHERE id = ?", (account_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            # telegram_accounts ещё не создана (ни один репозиторий инвайтера
            # не открывал эту БД) — значит, и такого аккаунта нет.
            return False
        return row is not None

    def set_campaign_account_enabled(self, campaign_id: int, account_id: int, enabled: bool) -> bool:
        """True — состояние применено. False — нет такой кампании, или (при
        добавлении) нет такого telegram_accounts.id. Добавление
        идемпотентно (INSERT OR IGNORE — уже заданный daily_limit не
        сбрасывается), удаление — просто DELETE строки allowlist (сам
        telegram_accounts не затрагивается)."""
        if self.get_campaign(campaign_id) is None:
            return False
        if enabled:
            if not self._account_exists(account_id):
                return False
            self._conn.execute(
                "INSERT OR IGNORE INTO dm_campaign_accounts (campaign_id, account_id) VALUES (?, ?)",
                (campaign_id, account_id),
            )
        else:
            self._conn.execute(
                "DELETE FROM dm_campaign_accounts WHERE campaign_id = ? AND account_id = ?",
                (campaign_id, account_id),
            )
        self._conn.commit()
        return True

    def set_campaign_account_daily_limit(
        self, campaign_id: int, account_id: int, daily_limit: int | None,
    ) -> DmCampaignAccount | None:
        """None в daily_limit — "не настроен". Только для аккаунта, уже
        разрешённого кампании; None в результате — такой пары нет."""
        if daily_limit is not None and not (DAILY_LIMIT_MIN <= daily_limit <= DAILY_LIMIT_MAX):
            raise ValueError(f"Лимит должен быть от {DAILY_LIMIT_MIN} до {DAILY_LIMIT_MAX}.")
        cursor = self._conn.execute(
            "UPDATE dm_campaign_accounts SET daily_limit = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE campaign_id = ? AND account_id = ?",
            (daily_limit, campaign_id, account_id),
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_campaign_account(campaign_id, account_id)

    def close(self) -> None:
        self._conn.close()
