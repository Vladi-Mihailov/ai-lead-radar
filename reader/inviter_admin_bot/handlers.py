"""Telethon-адаптер reader/inviter_admin_bot/ — извлекает identity/текст/
callback из реальных Telethon-событий и делегирует всю логику
AdminBotController (см. conversation.py). Сам ничего не решает — тот же
принцип разделения, что и у reader/public_bot/handlers.py.

Identity — ВСЕГДА event.sender_id (numeric), никогда не username."""

import logging

from telethon import TelegramClient, events

from reader.inviter_admin_bot.conversation import AdminBotController
from reader.inviter_admin_bot.keyboards import (
    ACCOUNTS_BACK,
    account_card_keyboard,
    accounts_page_keyboard,
    cancel_keyboard,
    decode_account_limit_callback,
    decode_account_limit_manual_callback,
    decode_account_limit_value_callback,
    decode_account_open_callback,
    decode_account_reauthorize_callback,
    decode_account_sync_callback,
    decode_account_toggle_callback,
    limit_choice_keyboard,
    main_menu_keyboard,
)
from reader.inviter_admin_bot.texts import ACCESS_DENIED_TEXT

logger = logging.getLogger(__name__)


def register(client: TelegramClient, controller: AdminBotController) -> None:
    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def _on_message(event: events.NewMessage.Event) -> None:
        text = event.raw_text
        if not text or not text.strip():
            return

        reply = await controller.handle_text(text, chat_id=event.chat_id, telegram_user_id=event.sender_id)
        await _send_reply(event, reply)

    @client.on(events.CallbackQuery(func=lambda e: e.is_private))
    async def _on_callback(event: events.CallbackQuery.Event) -> None:
        data = event.data

        if data == ACCOUNTS_BACK:
            reply = controller.handle_accounts_back(telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply)
            return

        account_id = decode_account_open_callback(data)
        if account_id is not None:
            reply = controller.handle_account_open(account_id, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply)
            return

        account_id = decode_account_toggle_callback(data)
        if account_id is not None:
            reply = controller.handle_account_toggle(account_id, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply)
            return

        account_id = decode_account_limit_callback(data)
        if account_id is not None:
            reply = controller.handle_account_limit_open(account_id, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply)
            return

        limit_value = decode_account_limit_value_callback(data)
        if limit_value is not None:
            account_id, value = limit_value
            reply = controller.handle_account_limit_value(account_id, value, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply)
            return

        account_id = decode_account_limit_manual_callback(data)
        if account_id is not None:
            reply = controller.handle_account_limit_manual_prompt(
                account_id, chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply)
            return

        account_id = decode_account_sync_callback(data)
        if account_id is not None:
            await event.answer("🔄 Проверяем...")
            reply = await controller.handle_account_sync(account_id, telegram_user_id=event.sender_id)
            await _send_reply(event, reply, prefer_edit=True)
            return

        account_id = decode_account_reauthorize_callback(data)
        if account_id is not None:
            reply = await controller.handle_account_reauthorize(
                account_id, chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply)
            return

        await event.answer("Неизвестная или устаревшая кнопка", alert=True)

    logger.info("✔ Inviter admin bot handlers зарегистрированы")


async def _answer_and_send(event, reply) -> None:
    await event.answer()
    await _send_reply(event, reply, prefer_edit=True)


async def _send_reply(event, reply, *, prefer_edit: bool = False) -> None:
    buttons = None
    if reply.show_main_menu:
        buttons = main_menu_keyboard()
    elif reply.show_cancel_button:
        buttons = cancel_keyboard()
    elif reply.accounts_page_options is not None:
        buttons = accounts_page_keyboard(reply.accounts_page_options)
    elif reply.account_card_id is not None:
        buttons = account_card_keyboard(reply.account_card_id, enabled=bool(reply.account_card_enabled))
    elif reply.limit_choice_account_id is not None:
        buttons = limit_choice_keyboard(reply.limit_choice_account_id)

    if reply.text == ACCESS_DENIED_TEXT:
        buttons = None

    if prefer_edit:
        try:
            await event.edit(reply.text, buttons=buttons)
            return
        except Exception:
            logger.warning(
                "Не удалось отредактировать сообщение inviter_admin_bot, отправляю новое", exc_info=True,
            )

    await event.respond(reply.text, buttons=buttons)
