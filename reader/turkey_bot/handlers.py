"""Telethon-адаптер Turkey-бота — извлекает identity/текст/callback из
реальных Telethon-событий и делегирует всю логику
reader/turkey_bot/conversation.py::ConversationController (см. design
report "Перестроить UX Turkey test bot").

Identity — ВСЕГДА event.sender_id/event.chat_id (numeric), никогда из тела
callback_data (car_id/plate в callback сам по себе НЕ доказывает владение —
ConversationController перепроверяет заново на КАЖДОМ вызове)."""

import logging

from telethon import Button, TelegramClient, events

from reader.turkey_bot.conversation import BotReply, ConversationController
from reader.turkey_bot.keyboards import (
    BACK_TO_MY_CARS_CALLBACK_DATA,
    CANCEL_CALLBACK_DATA,
    HELP_BACK_TO_MAIN_CALLBACK_DATA,
    add_car_confirmation_keyboard,
    cancel_keyboard,
    car_card_keyboard,
    car_delete_confirm_keyboard,
    check_now_picker_keyboard,
    decode_car_action_callback,
    decode_car_open_by_plate_callback,
    decode_car_open_callback,
    decode_help_callback,
    encode_car_open_callback,
    georgian_bot_link_keyboard,
    help_menu_keyboard,
    help_section_keyboard,
    main_menu_keyboard,
    my_cars_list_keyboard,
)
from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.texts import BACK_LABEL, UNKNOWN_BUTTON_TEXT

logger = logging.getLogger(__name__)


def register(
    client: TelegramClient,
    controller: ConversationController,
    known_users_repository: TurkeyBotKnownUsersRepository | None = None,
) -> None:
    """Регистрирует NewMessage/CallbackQuery handlers на уже
    сконфигурированном bot-mode TelegramClient (см.
    reader/turkey_bot/main.py)."""

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
        is_trusted = controller.is_trusted(event.sender_id)

        reply = await controller.handle_text(
            text, chat_id=event.chat_id, telegram_user_id=event.sender_id,
        )
        await _send_reply(event, reply, is_trusted=is_trusted)

    @client.on(events.CallbackQuery(func=lambda e: e.is_private))
    async def _on_callback(event: events.CallbackQuery.Event) -> None:
        sender = await event.get_sender()
        username = getattr(sender, "username", None) if sender is not None else None
        _record_known_user(event.sender_id, event.chat_id, username)
        is_trusted = controller.is_trusted(event.sender_id)

        if event.data == CANCEL_CALLBACK_DATA:
            reply = await controller.handle_cancel(chat_id=event.chat_id)
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted)
            return

        if event.data == HELP_BACK_TO_MAIN_CALLBACK_DATA:
            reply = controller.handle_help_back_to_main()
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted)
            return

        help_section = decode_help_callback(event.data)
        if help_section is not None:
            reply = controller.handle_help_callback(help_section)
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted)
            return

        if event.data == BACK_TO_MY_CARS_CALLBACK_DATA:
            # ⬅️ Назад из карточки -> обновлённый список "🚗 Мои
            # автомобили" (см. задачу п.8: НЕ главное меню) — та же
            # ownership-проверка, что и у обычного handle_my_cars (список
            # уже отфильтрован по event.sender_id на уровне SQL).
            reply = controller.handle_my_cars(chat_id=event.chat_id, telegram_user_id=event.sender_id)
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=True)
            return

        car_open_id = decode_car_open_callback(event.data)
        if car_open_id is not None:
            reply = controller.handle_car_open(car_open_id, telegram_user_id=event.sender_id)
            if reply is None:
                await event.answer(UNKNOWN_BUTTON_TEXT, alert=True)
                return
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=True)
            return

        car_open_plate = decode_car_open_by_plate_callback(event.data)
        if car_open_plate is not None:
            reply = controller.handle_car_open_by_plate(car_open_plate, telegram_user_id=event.sender_id)
            if reply is None:
                await event.answer(UNKNOWN_BUTTON_TEXT, alert=True)
                return
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=True)
            return

        car_action = decode_car_action_callback(event.data)
        if car_action is not None:
            action, car_id = car_action
            reply = await controller.handle_car_action(
                action, car_id, chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            if reply is None:
                await event.answer(UNKNOWN_BUTTON_TEXT, alert=True)
                return
            await event.answer()
            # my-cars/card/toggle/delete flow -> редактируем СУЩЕСТВУЮЩЕЕ
            # сообщение (см. задачу п.5: "не создавать новое сообщение без
            # необходимости... повторить Georgian bot pattern") — "check"/
            # "history" дают полноценный отчёт (unified-проверка/история),
            # для них новое сообщение уместнее (тот же принцип, что и в
            # Georgian bot, где 🔎 Проверить сейчас — отдельный, не
            # car-card, flow) — back_to_car_id всё равно даёт путь назад
            # к карточке (см. _first_message_buttons).
            await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=action not in ("check", "history"))
            return

        await event.answer(UNKNOWN_BUTTON_TEXT, alert=True)

    logger.info("✔ Turkey bot handlers зарегистрированы")


def _first_message_buttons(reply: BotReply, *, is_trusted: bool, has_extra: bool):
    """Приоритет клавiатур на ПЕРВОМ отправленном сообщении (Telethon не
    может совместить несколько видов сразу) — show_cancel_button >
    my_cars/check-picker > car_delete_confirm > car_card >
    check_now_confirmation > cta_buttons (+ back_to_car_id, если задан) >
    back_to_car_id (один) > show_georgian_bot_link > help_keyboard >
    show_main_menu (если нет extra_texts)."""
    if reply.show_cancel_button:
        return cancel_keyboard()
    if reply.my_cars is not None:
        cars = list(reply.my_cars)
        if reply.is_check_picker:
            return check_now_picker_keyboard(cars)
        return my_cars_list_keyboard(cars, reply.my_cars_monitoring_active or {})
    if reply.car_delete_confirm_car_id is not None:
        return car_delete_confirm_keyboard(reply.car_delete_confirm_car_id)
    if reply.car_card is not None:
        return car_card_keyboard(reply.car_card, monitoring_active=reply.car_card_monitoring_active)
    if reply.check_now_confirmation_car_id is not None:
        return add_car_confirmation_keyboard(reply.check_now_confirmation_car_id)
    if reply.cta_buttons and not has_extra:
        rows = [[Button.url(label, url) for label, url in reply.cta_buttons]]
        if reply.back_to_car_id is not None:
            rows.append([Button.inline(BACK_LABEL, encode_car_open_callback(reply.back_to_car_id))])
        return rows
    if reply.back_to_car_id is not None:
        return [[Button.inline(BACK_LABEL, encode_car_open_callback(reply.back_to_car_id))]]
    if reply.show_georgian_bot_link:
        return georgian_bot_link_keyboard()
    if reply.help_keyboard == "menu":
        return help_menu_keyboard()
    if reply.help_keyboard == "section":
        return help_section_keyboard()
    if reply.show_main_menu and not has_extra:
        return main_menu_keyboard(is_trusted=is_trusted)
    return None


async def _send_reply(event, reply: BotReply, *, is_trusted: bool = False, prefer_edit: bool = False) -> None:
    """extra_texts — дополнительные сообщения ПОСЛЕ основного; cta_buttons/
    show_main_menu (если установлены) прикрепляются к ПОСЛЕДНЕМУ из них, а
    не к первому — кнопки должны появиться там, где разговор реально
    завершился.

    prefer_edit=True — редактирует СУЩЕСТВУЮЩЕЕ сообщение (event.edit)
    вместо отправки нового (см. reader/public_bot/handlers.py::_send_reply
    — тот же Georgian pattern "не создавать новое сообщение без
    необходимости"), с safe fallback на event.respond, если редактирование
    невозможно (например, сообщение слишком старое для Telegram edit-
    window)."""
    has_extra = bool(reply.extra_texts)

    first_buttons = _first_message_buttons(reply, is_trusted=is_trusted, has_extra=has_extra)
    if prefer_edit:
        try:
            await event.edit(reply.text, buttons=first_buttons)
        except Exception:
            logger.warning("Не удалось отредактировать сообщение Turkey test bot, отправляю новое", exc_info=True)
            await event.respond(reply.text, buttons=first_buttons)
    else:
        await event.respond(reply.text, buttons=first_buttons)

    for index, extra_text in enumerate(reply.extra_texts):
        is_last = index == len(reply.extra_texts) - 1
        if is_last and reply.cta_buttons:
            extra_buttons = [[Button.url(label, url) for label, url in reply.cta_buttons]]
        elif is_last and reply.show_main_menu:
            extra_buttons = main_menu_keyboard(is_trusted=is_trusted)
        else:
            extra_buttons = None
        await event.respond(extra_text, buttons=extra_buttons)
