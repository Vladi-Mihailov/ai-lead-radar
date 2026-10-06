"""ProtocolSupportService — живой диалог пользователя бота с оператором через
внутреннюю группу менеджеров. Без Telethon: Telegram-операции делает
SupportGateway (см. telethon_gateway.py), здесь только правила.

Пользователь -> группа: каждое сообщение открытого диалога уходит в группу
карточкой с источником (флаг + @бот, имя, username; телефон — никогда) и
кнопкой «✅ Закрыть диалог»; id сообщения группы сохраняется в БД.

Группа -> пользователь: ответ доставляется ТОЛЬКО если одновременно:
сообщение из настроенной группы, автор — trusted-оператор (тот же список
public_bot.trusted_operator_user_ids), это не бот и не наше исходящее,
это Reply на известное сообщение диалога, диалог этого же бота (bot_key) и
он открыт. Иначе — молча игнорируется.

Один процесс = один бот (bot_key): GE-процесс не обрабатывает ответы на
сообщения TR-диалогов и наоборот, даже в общей группе."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from reader.protocol_support import texts
from reader.protocol_support.repository import (
    CLOSED_BY_MANAGER,
    CLOSED_BY_USER,
    DIRECTION_MANAGER_TO_USER,
    DIRECTION_USER_TO_MANAGER,
    ProtocolSupportRepository,
    SupportDialog,
)

logger = logging.getLogger(__name__)

KIND_TEXT = "text"
KIND_PHOTO = "photo"
KIND_DOCUMENT = "document"
KIND_UNSUPPORTED = "unsupported"

# Лимиты Telegram: текст 4096, подпись к медиа 1024 (с запасом под шапку).
_MAX_TEXT = 3500
_MAX_CAPTION = 1000


@dataclass(frozen=True)
class BotProfile:
    key: str            # 'ge' / 'tr'
    username: str       # без @
    flag: str           # 🇬🇪 / 🇹🇷


@dataclass(frozen=True)
class SupportMessage:
    kind: str
    text: str = ""
    media: object | None = None     # Telegram media (переотправляется как есть, без скачивания)
    message_id: int | None = None


class SupportGateway(Protocol):
    async def post_to_group(self, chat_id: int, text: str, *, media: object | None, close_dialog_id: int,
                            reply_to: int | None) -> int: ...

    async def send_to_user(self, user_id: int, text: str, *, media: object | None = None,
                           main_menu: bool = False) -> None: ...

    async def reply_in_group(self, chat_id: int, text: str, *, reply_to: int) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


class ProtocolSupportService:
    def __init__(self, repository: ProtocolSupportRepository, profile: BotProfile, gateway: SupportGateway, *,
                 support_chat_id: int | None, is_trusted: Callable[[int], bool],
                 clock: Callable[[], datetime] = _utcnow):
        self._repo = repository
        self.profile = profile
        self._gateway = gateway
        self.support_chat_id = support_chat_id if isinstance(support_chat_id, int) else None
        self._is_trusted = is_trusted
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self.support_chat_id is not None

    # ---------------- пользователь ----------------

    def open_dialog(self, user_id: int) -> SupportDialog | None:
        return self._repo.get_open(self.profile.key, user_id) if self.enabled else None

    def enter(self, user_id: int, *, username: str | None, display_name: str | None) -> tuple[SupportDialog, bool]:
        """«👨‍💼 Оператор»: уже открытый диалог — возвращается, новый не создаётся."""
        return self._repo.open_or_get(
            bot_key=self.profile.key, telegram_user_id=user_id, username=username, display_name=display_name,
            support_chat_id=self.support_chat_id, now=self._clock(),
        )

    async def forward_from_user(self, user_id: int, *, username: str | None, display_name: str | None,
                                message: SupportMessage) -> bool:
        """Сообщение пользователя -> группа. False — доставить не удалось
        (пользователю показывается «Сейчас не удалось связаться…»)."""
        dialog = self.open_dialog(user_id)
        if dialog is None:
            return False
        first = dialog.root_manager_message_id is None
        limit = _MAX_CAPTION if message.media is not None else _MAX_TEXT
        card = texts.format_user_card(
            flag=self.profile.flag, bot_username=self.profile.username, display_name=display_name,
            username=username, body=_clip(message.text, limit), first=first,
        )
        try:
            group_message_id = await self._gateway.post_to_group(
                self.support_chat_id, card, media=message.media, close_dialog_id=dialog.id,
                reply_to=None if first else dialog.root_manager_message_id,
            )
        except Exception as exc:  # noqa: BLE001 — сервис не падает; причина — в лог без данных пользователя
            logger.warning("support[%s]: не удалось отправить обращение диалога %s в группу (%s)",
                           self.profile.key, dialog.id, type(exc).__name__)
            return False
        now = self._clock()
        if first:
            self._repo.set_root_message(dialog.id, group_message_id, now=now)
        self._repo.touch(dialog.id, username=username, display_name=display_name, now=now)
        self._repo.add_message(dialog_id=dialog.id, support_chat_id=self.support_chat_id,
                               manager_group_message_id=group_message_id, user_message_id=message.message_id,
                               direction=DIRECTION_USER_TO_MANAGER, now=now)
        return True

    async def close_by_user(self, user_id: int) -> bool:
        dialog = self.open_dialog(user_id)
        if dialog is None:
            return False
        return self._repo.close_dialog(dialog.id, closed_by=CLOSED_BY_USER, now=self._clock())

    # ---------------- группа менеджеров ----------------

    async def handle_manager_message(self, *, chat_id: int, sender_id: int | None, sender_is_bot: bool,
                                     outgoing: bool, reply_to_msg_id: int | None,
                                     message: SupportMessage) -> str:
        """Итог: 'delivered' / 'failed' / 'ignored:<причина>'."""
        if not self.enabled or chat_id != self.support_chat_id:
            return "ignored:other_chat"
        if outgoing or sender_is_bot:
            return "ignored:bot"  # защита от петли bot -> group -> handler
        if reply_to_msg_id is None:
            return "ignored:not_reply"
        if sender_id is None or not self._is_trusted(sender_id):
            return "ignored:untrusted"
        dialog = self._repo.find_by_group_message(chat_id, reply_to_msg_id)
        if dialog is None:
            return "ignored:unknown_message"
        if dialog.bot_key != self.profile.key:
            return "ignored:other_bot"
        if not dialog.is_open:
            return "ignored:closed"
        if message.kind == KIND_UNSUPPORTED:
            await self._safe_group_reply(chat_id, texts.UNSUPPORTED_MANAGER_TEXT, message.message_id)
            return "ignored:unsupported"
        limit = _MAX_CAPTION if message.media is not None else _MAX_TEXT
        try:
            await self._gateway.send_to_user(dialog.telegram_user_id,
                                             texts.format_operator_reply(_clip(message.text, limit)),
                                             media=message.media)
        except Exception as exc:  # noqa: BLE001 — пользователь заблокировал бота и т.п.
            logger.warning("support[%s]: ответ оператора в диалог %s не доставлен (%s)",
                           self.profile.key, dialog.id, type(exc).__name__)
            await self._safe_group_reply(chat_id, texts.DELIVERY_FAILED_TEXT, message.message_id)
            return "failed"
        if message.message_id is not None:
            self._repo.add_message(dialog_id=dialog.id, support_chat_id=chat_id,
                                   manager_group_message_id=message.message_id, user_message_id=None,
                                   direction=DIRECTION_MANAGER_TO_USER, now=self._clock())
        return "delivered"

    async def close_by_manager(self, dialog_id: int, *, clicker_id: int | None) -> str:
        """Кнопка «✅ Закрыть диалог» на карточке. Идемпотентно."""
        if clicker_id is None or not self._is_trusted(clicker_id):
            return "denied"
        dialog = self._repo.get(dialog_id)
        if dialog is None or dialog.bot_key != self.profile.key:
            return "unknown"
        if not self._repo.close_dialog(dialog.id, closed_by=CLOSED_BY_MANAGER, now=self._clock()):
            return "already_closed"
        try:
            await self._gateway.send_to_user(dialog.telegram_user_id, texts.CLOSED_BY_MANAGER_TEXT, main_menu=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("support[%s]: уведомление о закрытии диалога %s не доставлено (%s)",
                           self.profile.key, dialog.id, type(exc).__name__)
        return "closed"

    async def _safe_group_reply(self, chat_id: int, text: str, reply_to: int | None) -> None:
        if reply_to is None:
            return
        try:
            await self._gateway.reply_in_group(chat_id, text, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001
            logger.warning("support[%s]: служебный ответ в группу не отправлен (%s)", self.profile.key,
                           type(exc).__name__)
