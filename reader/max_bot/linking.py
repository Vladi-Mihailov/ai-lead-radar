"""Привязка MAX-аккаунта к Telegram-аккаунту — ТОЛЬКО по одноразовому коду:
код выдаёт MAX-бот владельцу MAX-аккаунта, ввести его нужно в Telegram-боте
той же страны (/max_link КОД). Так человек доказывает, что управляет обоими
аккаунтами. Совпадение телефона, имени или госномера доказательством не
считается и нигде не используется.

Привязка ничего не переносит сама: подписки Telegram не меняются, MAX-клиент
может явно скопировать список автомобилей в MAX (с новой точкой отсчёта —
без исторических штрафов). Отключение уведомлений/удаление машины в одном
мессенджере на другой не влияет.

Код хранится только как SHA-256, живёт 15 минут, одноразовый; новый код
гасит прежние неиспользованные; попытки ввода ограничены по Telegram-
пользователю (подбор кода бессмысленен: 32^8 вариантов, 5 попыток в час).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from reader.max_bot.storage import MaxBotStorage, fmt_ts, parse_ts

CODE_TTL = timedelta(minutes=15)
MAX_ATTEMPTS_PER_HOUR = 5
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8

LinkStatus = Literal["linked", "valid", "invalid", "expired", "used", "rate_limited", "wrong_bot"]


@dataclass(frozen=True)
class LinkResult:
    status: LinkStatus
    max_user_id: int | None = None
    # Имя MAX-аккаунта для подтверждения в Telegram (preview).
    max_display_name: str | None = None


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def normalize_code(raw: str) -> str:
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


class MaxTelegramLinkService:
    def __init__(self, storage: MaxBotStorage):
        self._storage = storage
        self._conn = storage.connection

    def issue_code(self, max_user_id: int, *, now: datetime | None = None) -> str:
        """Новый код (без дефиса); прежние неиспользованные коды пользователя гасятся."""
        now = now or datetime.now(timezone.utc)
        code = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))
        self._conn.execute(
            "DELETE FROM max_link_codes WHERE bot = ? AND max_user_id = ? AND used_at IS NULL",
            (self._storage.bot, max_user_id),
        )
        self._conn.execute(
            "INSERT INTO max_link_codes (code_hash, bot, max_user_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (_hash(code), self._storage.bot, max_user_id, fmt_ts(now), fmt_ts(now + CODE_TTL)),
        )
        self._conn.commit()
        return code

    def _check(self, raw_code: str, *, telegram_user_id: int, now: datetime) -> LinkResult:
        """Попытка (учитывается в лимите) + проверка кода без его погашения."""
        hour_ago = fmt_ts(now - timedelta(hours=1))
        attempts = self._conn.execute(
            "SELECT COUNT(*) FROM max_link_attempts WHERE telegram_user_id = ? AND attempted_at >= ?",
            (telegram_user_id, hour_ago),
        ).fetchone()[0]
        if attempts >= MAX_ATTEMPTS_PER_HOUR:
            return LinkResult("rate_limited")
        self._conn.execute(
            "INSERT INTO max_link_attempts (telegram_user_id, attempted_at) VALUES (?, ?)",
            (telegram_user_id, fmt_ts(now)),
        )
        self._conn.execute("DELETE FROM max_link_attempts WHERE attempted_at < ?", (fmt_ts(now - timedelta(days=1)),))
        self._conn.commit()

        code = normalize_code(raw_code)
        row = self._conn.execute(
            "SELECT bot, max_user_id, expires_at, used_at FROM max_link_codes WHERE code_hash = ?", (_hash(code),),
        ).fetchone() if len(code) == _CODE_LENGTH else None
        if row is None:
            return LinkResult("invalid")
        bot, max_user_id, expires_at, used_at = row
        if bot != self._storage.bot:
            return LinkResult("wrong_bot")
        if used_at is not None:
            return LinkResult("used")
        if parse_ts(expires_at) < now.astimezone(timezone.utc).replace(tzinfo=None):
            return LinkResult("expired")
        return LinkResult("valid", max_user_id=max_user_id)

    def preview(self, raw_code: str, *, telegram_user_id: int, now: datetime | None = None) -> LinkResult:
        """Шаг 1 в Telegram: к какому MAX-аккаунту будет привязка (код не гасится)."""
        result = self._check(raw_code, telegram_user_id=telegram_user_id, now=now or datetime.now(timezone.utc))
        if result.status != "valid":
            return result
        user = self._storage.get_user(result.max_user_id)
        name = " ".join(part for part in (user.first_name, user.last_name) if part) if user else ""
        return LinkResult("valid", max_user_id=result.max_user_id, max_display_name=name or None)

    def confirm(self, raw_code: str, *, telegram_user_id: int, now: datetime | None = None) -> LinkResult:
        """Шаг 2 в Telegram (кнопка «✅ Привязать»): привязка и погашение кода."""
        now = now or datetime.now(timezone.utc)
        checked = self._check(raw_code, telegram_user_id=telegram_user_id, now=now)
        if checked.status != "valid":
            return checked
        code = normalize_code(raw_code)
        max_user_id = checked.max_user_id
        cursor = self._conn.execute(
            "UPDATE max_link_codes SET used_at = ?, used_by_telegram_user_id = ? "
            "WHERE code_hash = ? AND used_at IS NULL",
            (fmt_ts(now), telegram_user_id, _hash(code)),
        )
        if cursor.rowcount != 1:
            self._conn.commit()
            return LinkResult("used")
        self._conn.execute(
            """
            INSERT INTO max_account_links (bot, max_user_id, telegram_user_id, linked_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (bot, max_user_id) DO UPDATE SET
                telegram_user_id = excluded.telegram_user_id, linked_at = excluded.linked_at
            """,
            (self._storage.bot, max_user_id, telegram_user_id, fmt_ts(now)),
        )
        self._conn.commit()
        return LinkResult("linked", max_user_id=max_user_id)

    def linked_telegram_user(self, max_user_id: int) -> int | None:
        row = self._conn.execute(
            "SELECT telegram_user_id FROM max_account_links WHERE bot = ? AND max_user_id = ?",
            (self._storage.bot, max_user_id),
        ).fetchone()
        return row[0] if row else None

    def unlink(self, max_user_id: int) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM max_account_links WHERE bot = ? AND max_user_id = ?", (self._storage.bot, max_user_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1


LINK_RESULT_TEXTS = {
    "valid": "Код действителен.",
    "linked": "✅ Аккаунт MAX привязан. Перенести автомобили можно в MAX-боте: «🔗 Telegram» → «📥 Перенести».",
    "invalid": "⚠️ Код не найден. Получите новый код в MAX-боте: «🔗 Telegram».",
    "expired": "⚠️ Код устарел (действует 15 минут). Получите новый код в MAX-боте.",
    "used": "⚠️ Этот код уже использован. Получите новый код в MAX-боте.",
    "rate_limited": "⚠️ Слишком много попыток. Попробуйте через час.",
    "wrong_bot": "⚠️ Это код другого бота: введите его в Telegram-боте той же страны.",
}
