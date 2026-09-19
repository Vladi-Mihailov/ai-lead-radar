"""Telethon-адаптер Turkey-бота — извлекает identity/текст/callback из
реальных Telethon-событий и делегирует всю логику
reader/turkey_bot/conversation.py::ConversationController (тот же принцип
разделения, что и reader/public_bot/handlers.py).

Identity — ВСЕГДА event.sender_id/event.chat_id (numeric), никогда из тела
callback_data (см. reader/turkey_bot/keyboards.py про фиксированный,
бессодержательный CANCEL_CALLBACK_DATA и про то, что garage car_id сам по
себе НЕ является доказательством владения — ConversationController.
handle_garage_check перепроверяет владение заново на КАЖДОМ вызове)."""

import io
import logging

from telethon import Button, TelegramClient, events

from reader.turkey_bot.conversation import BotReply, ConversationController
from reader.turkey_bot.keyboards import (
    CANCEL_CALLBACK_DATA,
    HELP_BACK_TO_MAIN_CALLBACK_DATA,
    cancel_keyboard,
    decode_garage_check_callback,
    decode_help_callback,
    decode_toll_provider_callback,
    garage_keyboard,
    georgian_bot_link_keyboard,
    help_menu_keyboard,
    help_section_keyboard,
    main_menu_keyboard,
    toll_provider_keyboard,
)
from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.texts import (
    GEORGIAN_BOT_LINK_TEXT,
    UNKNOWN_BUTTON_TEXT,
    WELCOME_TEXT,
)

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
            reply = await controller.handle_help_back_to_main()
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted)
            return

        help_section = decode_help_callback(event.data)
        if help_section is not None:
            reply = await controller.handle_help_callback(help_section)
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted)
            return

        toll_provider = decode_toll_provider_callback(event.data)
        if toll_provider is not None:
            reply = await controller.handle_toll_provider_callback(
                toll_provider, chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted)
            return

        decoded_garage_callback = decode_garage_check_callback(event.data)
        if decoded_garage_callback is not None:
            provider, garage_car_id = decoded_garage_callback
            reply = await controller.handle_garage_check(
                garage_car_id, chat_id=event.chat_id, telegram_user_id=event.sender_id,
                provider=provider,
            )
            if reply is None:
                # Машина не найдена ИЛИ принадлежит другому пользователю
                # (см. ConversationController.handle_garage_check) - тот
                # же общий, неинформативный alert, что и для неизвестной
                # кнопки ниже (см. reader/turkey_bot/texts.py::
                # UNKNOWN_BUTTON_TEXT про то, почему одинаковый ответ в
                # обоих случаях безопаснее).
                await event.answer(UNKNOWN_BUTTON_TEXT, alert=True)
                return
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted)
            return

        await event.answer(UNKNOWN_BUTTON_TEXT, alert=True)

    logger.info("✔ Turkey bot handlers зарегистрированы")


async def _send_reply(event, reply: BotReply, *, is_trusted: bool = False) -> None:
    """photo_png не None — отправляем CAPTCHA как фото с подписью (см.
    design report Stage 3: "bot sends the CAPTCHA PNG directly in
    Telegram"). BytesIO с .name — так Telethon определяет расширение/mime
    без временного файла на диске.

    Приоритет клавиатур на ОДНОМ сообщении (Telethon не может совместить
    несколько видов сразу): show_cancel_button (пока идёт диалог) >
    garage_cars (список гаража) > cta_buttons (коммерческие CTA после
    подтверждённого has_debt — GIB, Avrasya ИЛИ KGM, см.
    reader/turkey_bot/conversation.py::ConversationController._debt_cta_buttons,
    ОДИНАКОВО для trusted и не-trusted пользователей) > toll_provider_keyboard
    (выбор Avrasya/KGM после CHECK_TOLLS_LABEL, см.
    reader/turkey_bot/keyboards.py::toll_provider_keyboard) > help_keyboard
    (ℹ️ Справка и её разделы) > show_main_menu (персистентное reply-меню,
    см. reader/turkey_bot/conversation.py::BotReply про то, почему это
    именно в таком порядке).

    extra_texts — дополнительные сообщения ПОСЛЕ основного (см.
    reader/turkey_bot/conversation.py::BotReply) — has_debt со многими
    штрафами и список пользователей для 📊 Статистика — отправляются как
    обычные текстовые сообщения; cta_buttons/show_main_menu (если
    установлены) прикрепляются к ПОСЛЕДНЕМУ из них, а не к первому -
    кнопки должны появиться там, где разговор действительно завершился
    (тот же принцип для обоих полей, cta_buttons приоритетнее, см. выше)."""
    has_extra = bool(reply.extra_texts)

    if reply.show_cancel_button:
        first_buttons = cancel_keyboard()
    elif reply.garage_cars is not None:
        first_buttons = garage_keyboard(list(reply.garage_cars))
    elif reply.cta_buttons and not has_extra:
        # has_debt CTA (см. reader/turkey_bot/conversation.py::
        # ConversationController._debt_cta_buttons) — те же (label, url)
        # пары, что и у reader/public_bot/handlers.py для Георгии;
        # conversation.py намеренно передаёт их как строки, не Telethon
        # Button — реальные кнопки строятся только здесь. При наличии
        # extra_texts кнопки уходят на ПОСЛЕДНЕЕ сообщение (см. ниже), не
        # на первое.
        first_buttons = [[Button.url(label, url) for label, url in reply.cta_buttons]]
    elif reply.toll_provider_keyboard:
        first_buttons = toll_provider_keyboard()
    elif reply.help_keyboard == "menu":
        first_buttons = help_menu_keyboard()
    elif reply.help_keyboard == "section":
        first_buttons = help_section_keyboard()
    elif reply.show_main_menu and not has_extra:
        first_buttons = main_menu_keyboard(is_trusted=is_trusted)
    else:
        first_buttons = None

    if reply.photo_png is not None:
        buffer = io.BytesIO(reply.photo_png)
        buffer.name = _CAPTCHA_FILENAME
        await event.respond(reply.text, file=buffer, buttons=first_buttons)
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

    # См. design report "связать Georgian bot и Turkey bot взаимными
    # кнопками перехода" — ОТДЕЛЬНОЕ сообщение с inline URL-кнопкой в
    # Georgian-бот, ТОЛЬКО когда реально показан текст главного меню (см.
    # reader/turkey_bot/keyboards.py::main_menu_keyboard докстрок про
    # ValueError при смешивании reply/inline кнопок) — НЕ на каждом экране
    # с show_main_menu=True (результаты проверок/действий тоже прикрепляют
    # персистентную клавиатуру для удобства, но это не "открытие главного
    # меню", см. задачу: "не добавлять переход в каждый экран").
    if reply.show_main_menu and reply.text == WELCOME_TEXT:
        await event.respond(GEORGIAN_BOT_LINK_TEXT, buttons=georgian_bot_link_keyboard())
