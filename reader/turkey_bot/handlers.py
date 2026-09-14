"""Telethon-адаптер Turkey-бота — извлекает identity/текст/callback из
реальных Telethon-событий и делегирует всю логику
reader/turkey_bot/conversation.py::ConversationController (тот же принцип
разделения, что и reader/public_bot/handlers.py).

Identity — ВСЕГДА event.sender_id/event.chat_id (numeric), никогда из тела
callback_data (см. reader/turkey_bot/keyboards.py про фиксированный,
бессодержательный CANCEL_CALLBACK_DATA)."""

import io
import logging

from telethon import TelegramClient, events

from reader.turkey_bot.conversation import BotReply, ConversationController
from reader.turkey_bot.keyboards import CANCEL_CALLBACK_DATA, cancel_keyboard
from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository

logger = logging.getLogger(__name__)

_CAPTCHA_FILENAME = "captcha.png"


def register(
    client: TelegramClient,
    controller: ConversationController,
    known_users_repository: TurkeyBotKnownUsersRepository | None = None,
) -> None:
    """Регистрирует NewMessage/CallbackQuery handlers на уже
    сконфигурированном bot-mode TelegramClient (см. reader/turkey_bot/
    main.py). incoming=True + e.is_private — та же причина, что и у
    reader/public_bot/handlers.py: реагировать только на реальные входящие
    сообщения пользователя в приватном чате с ботом."""

    def _record_known_user(telegram_user_id: int, telegram_chat_id: int, username: str | None) -> None:
        if known_users_repository is not None:
            known_users_repository.record_seen(
                telegram_user_id=telegram_user_id, telegram_chat_id=telegram_chat_id,
                telegram_username=username,
            )

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def _on_message(event: events.NewMessage.Event) -> None:
        text = event.raw_text
        if not text or not text.strip():
            return

        sender = await event.get_sender()
        username = getattr(sender, "username", None) if sender is not None else None
        _record_known_user(event.sender_id, event.chat_id, username)

        reply = await controller.handle_text(
            text, chat_id=event.chat_id, telegram_user_id=event.sender_id,
        )
        await _send_reply(event, reply)

    @client.on(events.CallbackQuery(func=lambda e: e.is_private))
    async def _on_callback(event: events.CallbackQuery.Event) -> None:
        sender = await event.get_sender()
        username = getattr(sender, "username", None) if sender is not None else None
        _record_known_user(event.sender_id, event.chat_id, username)

        if event.data == CANCEL_CALLBACK_DATA:
            reply = await controller.handle_cancel(chat_id=event.chat_id)
            await event.answer()
            await _send_reply(event, reply)
            return

        await event.answer("Неизвестная или устаревшая кнопка", alert=True)

    logger.info("✔ Turkey bot handlers зарегистрированы")


async def _send_reply(event, reply: BotReply) -> None:
    """photo_png не None — единственный случай, отличающий этот бот от
    reader/public_bot/handlers.py: отправляем CAPTCHA как фото с подписью,
    а не текстовым сообщением (см. design report Stage 3: "bot sends the
    CAPTCHA PNG directly in Telegram"). BytesIO с .name — так Telethon
    определяет расширение/mime без временного файла на диске.

    extra_texts — дополнительные сообщения ПОСЛЕ основного (см.
    reader/turkey_bot/conversation.py::BotReply) — только has_debt со
    многими штрафами (см. reader/turkey_bot/texts.py::
    format_has_debt_messages про лимит Telegram и сохранение границ
    штрафов) — отправляются как обычные текстовые сообщения, без
    фото/кнопок (диалог к этому моменту уже завершён)."""
    buttons = cancel_keyboard() if reply.show_cancel_button else None

    if reply.photo_png is not None:
        buffer = io.BytesIO(reply.photo_png)
        buffer.name = _CAPTCHA_FILENAME
        await event.respond(reply.text, file=buffer, buttons=buttons)
    else:
        await event.respond(reply.text, buttons=buttons)

    for extra_text in reply.extra_texts:
        await event.respond(extra_text)
