"""Telethon-адаптер reader/inviter_admin_bot/ — извлекает identity/текст/
callback из реальных Telethon-событий и делегирует всю логику
AdminBotController (см. conversation.py). Сам ничего не решает — тот же
принцип разделения, что и у reader/public_bot/handlers.py.

Identity — ВСЕГДА event.sender_id (numeric), никогда не username."""

import logging

from telethon import Button, TelegramClient, events

from reader.inviter_admin_bot.conversation import AdminBotController
from reader.inviter_admin_bot.keyboards import (
    ACCOUNTS_BACK,
    CAMPAIGNS_BACK,
    account_card_keyboard,
    accounts_page_keyboard,
    campaign_card_keyboard,
    campaigns_page_keyboard,
    cancel_keyboard,
    decode_campaign_disable_callback,
    decode_campaign_enable_callback,
    decode_campaign_open_callback,
    decode_campaign_refresh_callback,
    decode_campaign_stats_callback,
    decode_account_limit_callback,
    decode_account_limit_manual_callback,
    decode_account_limit_value_callback,
    decode_account_open_callback,
    decode_account_reauthorize_callback,
    decode_account_sync_callback,
    decode_account_toggle_callback,
    decode_limits_manual_callback,
    decode_limits_open_callback,
    decode_limits_value_callback,
    limit_choice_keyboard,
    limits_value_choice_keyboard,
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

        # "✉️ ЛС-кампании" — собственный namespace dmc_ (см.
        # dm_campaign_callbacks.py); None — не наш callback.
        dm_reply = controller.handle_dm_callback(data, chat_id=event.chat_id, telegram_user_id=event.sender_id)
        if dm_reply is not None:
            await _answer_and_send(event, dm_reply)
            return

        if data == ACCOUNTS_BACK:
            reply = controller.handle_accounts_back(telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply)
            return

        if data == CAMPAIGNS_BACK:
            reply = controller.handle_campaigns_back(telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply)
            return

        campaign_id = decode_campaign_open_callback(data)
        if campaign_id is not None:
            reply = controller.handle_campaign_open(campaign_id, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply)
            return

        campaign_id = decode_campaign_enable_callback(data)
        if campaign_id is not None:
            reply = controller.handle_campaign_set_enabled(campaign_id, True, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply)
            return

        campaign_id = decode_campaign_disable_callback(data)
        if campaign_id is not None:
            reply = controller.handle_campaign_set_enabled(campaign_id, False, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply)
            return

        campaign_id = decode_campaign_stats_callback(data)
        if campaign_id is not None:
            reply = controller.handle_campaign_stats(campaign_id, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply)
            return

        campaign_id = decode_campaign_refresh_callback(data)
        if campaign_id is not None:
            await event.answer("🔄 Обновляем лиды...")
            reply = await controller.handle_campaign_refresh(campaign_id, telegram_user_id=event.sender_id)
            await _send_reply(event, reply, prefer_edit=True)
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

        account_id = decode_limits_open_callback(data)
        if account_id is not None:
            reply = controller.handle_limits_open(account_id, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply)
            return

        limit_value = decode_limits_value_callback(data)
        if limit_value is not None:
            account_id, value = limit_value
            reply = controller.handle_limits_value(account_id, value, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply)
            return

        account_id = decode_limits_manual_callback(data)
        if account_id is not None:
            reply = controller.handle_limits_manual_prompt(
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
    inline_rows = getattr(reply, "inline_rows", None)
    if reply.show_main_menu:
        buttons = main_menu_keyboard()
    elif inline_rows is not None:
        buttons = [[Button.inline(label, data) for label, data in row] for row in inline_rows]
    elif reply.show_cancel_button:
        buttons = cancel_keyboard()
    elif reply.accounts_page_options is not None:
        buttons = accounts_page_keyboard(reply.accounts_page_options)
    elif reply.account_card_id is not None:
        buttons = account_card_keyboard(reply.account_card_id, enabled=bool(reply.account_card_enabled))
    elif reply.limit_choice_account_id is not None:
        buttons = limit_choice_keyboard(reply.limit_choice_account_id)
    elif reply.limits_choice_account_id is not None:
        buttons = limits_value_choice_keyboard(reply.limits_choice_account_id)
    elif getattr(reply, "campaigns_page_options", None) is not None:
        buttons = campaigns_page_keyboard(reply.campaigns_page_options)
    elif getattr(reply, "campaign_card_id", None) is not None:
        buttons = campaign_card_keyboard(
            reply.campaign_card_id, enabled=bool(reply.campaign_card_enabled),
            has_pool=bool(getattr(reply, "campaign_card_has_pool", False)),
        )

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
