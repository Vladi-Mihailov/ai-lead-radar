"""Telethon-слой живого диалога с оператором — общий для @ProtocolGEbot и
@ProtocolTRbot (каждый процесс передаёт свой клиент, профиль и главное меню).

- TelethonSupportGateway — отправка в группу менеджеров и пользователю.
  Фото/документ переотправляются тем же Telegram-медиа (send_file с media
  исходного сообщения) — без скачивания и повторной загрузки.
- register_support_group_handlers — сообщения и кнопка «✅ Закрыть диалог»
  в группе менеджеров (только настроенный chat_id).
- handle_private_support — режим OPERATOR_CHAT в личном чате с ботом;
  вызывается в начале обычного обработчика: True — сообщение обработано
  здесь и в обычную логику проверки штрафов/дорог не идёт."""

import logging
from collections.abc import Callable

from telethon import Button, events

from reader.protocol_support import texts
from reader.protocol_support.service import (
    KIND_DOCUMENT,
    KIND_PHOTO,
    KIND_TEXT,
    KIND_UNSUPPORTED,
    ProtocolSupportService,
    SupportMessage,
)

logger = logging.getLogger(__name__)

_CLOSE_PREFIX = b"psclose:"


def encode_close(dialog_id: int) -> bytes:
    return _CLOSE_PREFIX + str(int(dialog_id)).encode("ascii")


def decode_close(data: bytes | None) -> int | None:
    if not data or not data.startswith(_CLOSE_PREFIX):
        return None
    try:
        return int(data[len(_CLOSE_PREFIX):].decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None


def operator_chat_keyboard() -> list[list[Button]]:
    return [[Button.text(texts.END_DIALOG_LABEL, resize=True)],
            [Button.text(texts.SUPPORT_MAIN_MENU_LABEL, resize=True)]]


def message_from_event(event) -> SupportMessage:
    message = getattr(event, "message", None)
    text = (getattr(event, "raw_text", None) or "").strip()
    message_id = getattr(message, "id", None)
    if message is not None and getattr(message, "photo", None):
        return SupportMessage(KIND_PHOTO, text, media=message.media, message_id=message_id)
    if message is not None and getattr(message, "document", None) and not getattr(message, "sticker", None):
        return SupportMessage(KIND_DOCUMENT, text, media=message.media, message_id=message_id)
    if text:
        return SupportMessage(KIND_TEXT, text, message_id=message_id)
    return SupportMessage(KIND_UNSUPPORTED, "", message_id=message_id)


def _display_name(sender) -> str | None:
    parts = [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
    name = " ".join(p for p in parts if p)
    return name or None


class TelethonSupportGateway:
    def __init__(self, client, *, user_main_menu: Callable[[], list]):
        self._client = client
        self._user_main_menu = user_main_menu

    async def post_to_group(self, chat_id: int, text: str, *, media: object | None, close_dialog_id: int,
                            reply_to: int | None) -> int:
        buttons = [[Button.inline(texts.CLOSE_DIALOG_BUTTON, encode_close(close_dialog_id))]]
        if media is not None:
            sent = await self._client.send_file(chat_id, media, caption=text, buttons=buttons, reply_to=reply_to)
        else:
            sent = await self._client.send_message(chat_id, text, buttons=buttons, reply_to=reply_to,
                                                   link_preview=False)
        return sent.id

    async def send_to_user(self, user_id: int, text: str, *, media: object | None = None,
                           main_menu: bool = False) -> None:
        buttons = self._user_main_menu() if main_menu else None
        if media is not None:
            await self._client.send_file(user_id, media, caption=text, buttons=buttons)
        else:
            await self._client.send_message(user_id, text, buttons=buttons, link_preview=False)

    async def reply_in_group(self, chat_id: int, text: str, *, reply_to: int) -> None:
        await self._client.send_message(chat_id, text, reply_to=reply_to)


def register_support_group_handlers(client, service: ProtocolSupportService) -> None:
    """Только если в настройках задан группа (service.enabled)."""
    if not service.enabled:
        return
    chat_id = service.support_chat_id

    @client.on(events.NewMessage(chats=[chat_id], incoming=True))
    async def _on_group_message(event) -> None:
        if getattr(event, "is_private", False):
            return
        try:
            sender = await event.get_sender()
            outcome = await service.handle_manager_message(
                chat_id=event.chat_id, sender_id=event.sender_id, sender_is_bot=bool(getattr(sender, "bot", False)),
                outgoing=bool(getattr(event, "out", False)), reply_to_msg_id=getattr(event, "reply_to_msg_id", None),
                message=message_from_event(event),
            )
        except Exception:  # noqa: BLE001 — никогда не роняем бота из-за группы
            logger.exception("support[%s]: ошибка обработки сообщения группы", service.profile.key)
            return
        if outcome in ("delivered", "failed"):
            logger.info("support[%s]: ответ оператора -> %s", service.profile.key, outcome)

    @client.on(events.CallbackQuery(chats=[chat_id], pattern=_CLOSE_PREFIX))
    async def _on_close(event) -> None:
        dialog_id = decode_close(event.data)
        if dialog_id is None:
            await event.answer()
            return
        outcome = await service.close_by_manager(dialog_id, clicker_id=event.sender_id)
        answers = {"closed": texts.DIALOG_CLOSED_NOTE, "already_closed": "Диалог уже закрыт",
                   "denied": "Нет доступа", "unknown": "Диалог не найден"}
        await event.answer(answers.get(outcome, ""), alert=outcome == "denied")
        if outcome in ("closed", "already_closed"):
            try:
                await event.edit(buttons=None)
            except Exception:  # noqa: BLE001 — кнопка останется, закрытие уже идемпотентно
                pass


async def handle_private_support(event, support: ProtocolSupportService | None, *, is_trusted: bool,
                                 reset_state: Callable[[], None], main_menu: list, main_menu_text: str) -> bool:
    """True — сообщение обработано режимом оператора (в обычную логику бота
    не передавать). Менеджеры (trusted) кнопки «Оператор» не имеют."""
    text = (getattr(event, "raw_text", None) or "").strip()
    user_id = event.sender_id

    if text == texts.OPERATOR_LABEL and not is_trusted:
        if support is None or not support.enabled:
            await event.respond(texts.UNAVAILABLE_TEXT, buttons=main_menu)
            return True
        sender = await event.get_sender()
        reset_state()  # не оставлять пользователя одновременно в обычном шаге и в OPERATOR_CHAT
        support.enter(user_id, username=getattr(sender, "username", None), display_name=_display_name(sender))
        await event.respond(texts.CONNECTED_TEXT, buttons=operator_chat_keyboard())
        return True

    if support is None or support.open_dialog(user_id) is None:
        return False

    if text == texts.END_DIALOG_LABEL:
        await support.close_by_user(user_id)
        await event.respond(texts.CLOSED_BY_USER_TEXT, buttons=main_menu)
        return True
    if text in (texts.SUPPORT_MAIN_MENU_LABEL, "/start"):
        await support.close_by_user(user_id)
        await event.respond(main_menu_text, buttons=main_menu)
        return True

    message = message_from_event(event)
    if message.kind == KIND_UNSUPPORTED:
        await event.respond(texts.UNSUPPORTED_USER_TEXT, buttons=operator_chat_keyboard())
        return True
    sender = await event.get_sender()
    delivered = await support.forward_from_user(
        user_id, username=getattr(sender, "username", None), display_name=_display_name(sender), message=message,
    )
    if not delivered:
        await event.respond(texts.UNAVAILABLE_TEXT, buttons=operator_chat_keyboard())
    return True
