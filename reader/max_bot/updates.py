"""Входящие апдейты MAX (Update, https://dev.max.ru/docs-api/objects/Update)
-> MaxEvent — единственное, что видят боты Грузии/Турции.

Identity — ВСЕГДА числовой user_id отправителя из апдейта (message.sender /
callback.user / user), никогда не имя/username и никогда не что-либо из
payload кнопки (тот же принцип, что и у Telegram-ботов).

dedup_key — устойчивый ключ апдейта для защиты от повторной доставки
webhook (MAX повторяет POST до 10 раз, если не получил 200 за 30 секунд):
mid сообщения, callback_id нажатия, (user_id, timestamp) для bot_started/
bot_stopped.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

EventKind = Literal["message", "callback", "started", "stopped", "other"]

DIALOG = "dialog"

SUPPORTED_UPDATE_TYPES = ["message_created", "message_callback", "bot_started", "bot_stopped"]


@dataclass(frozen=True)
class MaxUser:
    user_id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    is_bot: bool = False


@dataclass(frozen=True)
class MaxEvent:
    kind: EventKind
    update_type: str
    dedup_key: str
    timestamp: int
    user: MaxUser | None = None
    chat_id: int | None = None
    # dialog / chat / channel — боты отвечают ТОЛЬКО в личных диалогах.
    chat_type: str | None = None
    text: str | None = None
    # payload кнопки (callback) или deep link (?start=..., bot_started).
    payload: str | None = None
    callback_id: str | None = None
    # mid входящего сообщения или сообщения, к которому прикреплена кнопка.
    message_id: str | None = None

    @property
    def is_private(self) -> bool:
        # bot_started/bot_stopped приходят без chat_type — это всегда диалог.
        return self.chat_type in (None, DIALOG)


def _user(raw) -> MaxUser | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("user_id"), int):
        return None
    return MaxUser(
        user_id=raw["user_id"],
        first_name=raw.get("first_name") or None,
        last_name=raw.get("last_name") or None,
        username=raw.get("username") or None,
        is_bot=raw.get("is_bot") is True,
    )


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_update(raw: dict) -> MaxEvent | None:
    """None — не апдейт (мусор в теле запроса). Неподдерживаемые типы
    возвращаются как kind='other' (их тоже нужно дедуплицировать и
    подтверждать, но боты их игнорируют)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("update_type"), str):
        return None
    update_type = raw["update_type"]
    timestamp = _int(raw.get("timestamp")) or 0

    if update_type == "message_created":
        message = raw.get("message") or {}
        body = message.get("body") or {}
        recipient = message.get("recipient") or {}
        mid = body.get("mid")
        user = _user(message.get("sender"))
        return MaxEvent(
            kind="message", update_type=update_type, timestamp=timestamp,
            dedup_key=f"m:{mid}" if mid else _fallback_key(raw),
            user=user, chat_id=_int(recipient.get("chat_id")), chat_type=recipient.get("chat_type"),
            text=body.get("text") if isinstance(body.get("text"), str) else None,
            message_id=mid,
        )

    if update_type == "message_callback":
        callback = raw.get("callback") or {}
        message = raw.get("message") or {}
        recipient = message.get("recipient") or {}
        callback_id = callback.get("callback_id")
        return MaxEvent(
            kind="callback", update_type=update_type, timestamp=timestamp,
            dedup_key=f"c:{callback_id}" if callback_id else _fallback_key(raw),
            user=_user(callback.get("user")), chat_id=_int(recipient.get("chat_id")),
            chat_type=recipient.get("chat_type"),
            payload=callback.get("payload") if isinstance(callback.get("payload"), str) else None,
            callback_id=callback_id, message_id=(message.get("body") or {}).get("mid"),
        )

    if update_type in ("bot_started", "bot_stopped"):
        user = _user(raw.get("user"))
        user_id = user.user_id if user else None
        return MaxEvent(
            kind="started" if update_type == "bot_started" else "stopped",
            update_type=update_type, timestamp=timestamp,
            dedup_key=f"{update_type}:{user_id}:{timestamp}", user=user, chat_id=_int(raw.get("chat_id")),
            payload=raw.get("payload") if isinstance(raw.get("payload"), str) else None,
        )

    return MaxEvent(kind="other", update_type=update_type, timestamp=timestamp, dedup_key=_fallback_key(raw))


def _fallback_key(raw: dict) -> str:
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"h:{digest[:32]}"
