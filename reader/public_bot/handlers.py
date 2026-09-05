"""Telethon-адаптер @GEShtrafbot — извлекает identity/текст/callback из
реальных Telethon-событий и делегирует всю логику
reader/public_bot/conversation.py::ConversationController. Сам ничего не
решает про мониторинг/подписки/меню — только транспорт, тот же принцип
разделения, что и у reader/commands/dispatcher.py + reader/commands/fine.py
для операторских команд.

Identity — ВСЕГДА event.sender_id (numeric), никогда не username и никогда
не что-либо из тела callback_data (см. reader/public_bot/keyboards.py).
"""

import logging

from telethon import TelegramClient, events

from reader.public_bot.conversation import ConversationController
from reader.public_bot.keyboards import (
    STOP_NO,
    add_client_decision_keyboard,
    check_now_options_keyboard,
    decode_add_client_decision_callback,
    decode_check_now_callback,
    decode_period_callback,
    decode_stop_confirm_callback,
    decode_stop_pick_callback,
    decode_trusted_stop_confirm_callback,
    decode_trusted_stop_pick_callback,
    decode_trusted_tasks_page_callback,
    main_menu_keyboard,
    period_choice_keyboard,
    stop_confirm_keyboard,
    stop_options_keyboard,
    trusted_stop_confirm_keyboard,
    trusted_stop_options_keyboard,
    trusted_tasks_page_keyboard,
)
from reader.public_bot.known_users_repository import BotKnownUsersRepository
from reader.public_bot.texts import CALLBACK_NOT_AUTHORIZED_TEXT

logger = logging.getLogger(__name__)


async def _sender_names(event) -> tuple[str | None, str | None, str | None]:
    """(username, first_name, last_name) отправителя — username здесь
    только для Шага 2 Add Car flow ("Telegram уже отдаёт username —
    использовать автоматически"), а не как identity (та — event.sender_id,
    см. модуль docstring)."""
    sender = await event.get_sender()
    if sender is None:
        return None, None, None
    return (
        getattr(sender, "username", None),
        getattr(sender, "first_name", None),
        getattr(sender, "last_name", None),
    )


def register(
    client: TelegramClient,
    controller: ConversationController,
    known_users_repository: BotKnownUsersRepository | None = None,
) -> None:
    """Регистрирует NewMessage/CallbackQuery handlers на уже
    сконфигурированном bot-mode TelegramClient (см. reader/public_bot/
    main.py). incoming=True + e.is_private — тот же принцип, что и у
    CommandDispatcher (реагировать только на реальные входящие сообщения
    пользователя в приватном чате с ботом, не на служебные апдейты/группы —
    @GEShtrafbot не предназначен для групповых чатов).

    known_users_repository — если передан, ЛЮБОЕ входящее событие (текст
    или callback), независимо от содержимого, обновляет bot_known_users
    (см. design report: единственный способ узнать, что боту можно
    что-либо доставить этому numeric id, — он уже хоть раз ему написал)."""

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

        username, first_name, last_name = await _sender_names(event)
        _record_known_user(event.sender_id, event.chat_id, username)

        reply = await controller.handle_text(
            text,
            chat_id=event.chat_id,
            telegram_user_id=event.sender_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )

        await _send_reply(event, reply)

    @client.on(events.CallbackQuery(func=lambda e: e.is_private))
    async def _on_callback(event: events.CallbackQuery.Event) -> None:
        data = event.data
        username, first_name, last_name = await _sender_names(event)
        _record_known_user(event.sender_id, event.chat_id, username)

        wants_client = decode_add_client_decision_callback(data)
        if wants_client is not None:
            # bool, а не truthy-проверка: False (Отмена) — тоже валидный,
            # обрабатываемый выбор, а не "это не тот callback" (см.
            # keyboards.py::decode_add_client_decision_callback).
            reply = controller.handle_add_client_decision(
                wants_client, chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply)
            return

        days = decode_period_callback(data)
        if days is not None:
            reply = await controller.handle_period_choice(
                days,
                chat_id=event.chat_id,
                telegram_user_id=event.sender_id,
                first_name=first_name,
                last_name=last_name,
            )
            await _answer_and_send(event, reply)
            return

        check_now_id = decode_check_now_callback(data)
        if check_now_id is not None:
            reply = await controller.handle_check_now_choice(
                check_now_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply)
            return

        stop_pick_id = decode_stop_pick_callback(data)
        if stop_pick_id is not None:
            reply = controller.handle_stop_pick(stop_pick_id, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply)
            return

        stop_confirm_id = decode_stop_confirm_callback(data)
        if stop_confirm_id is not None:
            reply = controller.handle_stop_confirm(stop_confirm_id, telegram_user_id=event.sender_id)
            await event.answer()
            await _send_reply(event, reply, prefer_edit=True)
            return

        trusted_tasks_page = decode_trusted_tasks_page_callback(data)
        if trusted_tasks_page is not None:
            reply = controller.handle_trusted_tasks_page(
                trusted_tasks_page, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply)
            return

        trusted_stop_pick_id = decode_trusted_stop_pick_callback(data)
        if trusted_stop_pick_id is not None:
            reply = controller.handle_trusted_stop_pick(
                trusted_stop_pick_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply)
            return

        trusted_stop_confirm_id = decode_trusted_stop_confirm_callback(data)
        if trusted_stop_confirm_id is not None:
            reply = controller.handle_trusted_stop_confirm(
                trusted_stop_confirm_id, telegram_user_id=event.sender_id,
            )
            await event.answer()
            await _send_reply(event, reply, prefer_edit=True)
            return

        if data == STOP_NO:
            reply = controller.handle_stop_cancel()
            await event.answer()
            await _send_reply(event, reply, prefer_edit=True)
            return

        await event.answer("Неизвестная или устаревшая кнопка", alert=True)

    # Bot identity switch (см. audit report) — @ProtocolGEbot.
    logger.info("✔ @ProtocolGEbot handlers зарегистрированы")


async def _answer_and_send(event, reply) -> None:
    """Общий хвост для callback'ов, которые могут вернуть None (=
    подписка не найдена/не принадлежит этому пользователю, см.
    reader/public_bot/keyboards.py про то, почему сам факт валидного
    subscription_id в callback_data ничего не доказывает) — в этом случае
    показываем короткий alert и ничего не создаём/не меняем."""
    if reply is None:
        await event.answer(CALLBACK_NOT_AUTHORIZED_TEXT, alert=True)
        return
    await event.answer()
    await _send_reply(event, reply, prefer_edit=True)


async def _send_reply(event, reply, *, prefer_edit: bool = False) -> None:
    buttons = None
    if reply.show_main_menu:
        buttons = main_menu_keyboard()
    elif reply.show_period_buttons:
        buttons = period_choice_keyboard()
    elif reply.show_add_client_decision_buttons:
        buttons = add_client_decision_keyboard()
    elif reply.check_now_options:
        buttons = check_now_options_keyboard(reply.check_now_options)
    elif reply.stop_options:
        buttons = stop_options_keyboard(reply.stop_options)
    elif reply.stop_confirm_subscription_id is not None:
        buttons = stop_confirm_keyboard(reply.stop_confirm_subscription_id)
    elif reply.trusted_stop_options:
        buttons = trusted_stop_options_keyboard(reply.trusted_stop_options)
    elif reply.trusted_stop_confirm_task_id is not None:
        buttons = trusted_stop_confirm_keyboard(
            reply.trusted_stop_confirm_task_id, label=reply.trusted_stop_confirm_button_label,
        )
    elif reply.trusted_tasks_page is not None:
        buttons = trusted_tasks_page_keyboard(
            page=reply.trusted_tasks_page, total_pages=reply.trusted_tasks_total_pages,
        )

    if prefer_edit:
        try:
            await event.edit(reply.text, buttons=buttons)
            return
        except Exception:
            # Сообщение с кнопками могло стать недоступным для редактирования
            # (например, Telegram ограничивает срок редактирования) —
            # результат всё равно должен дойти до пользователя.
            logger.warning(
                "Не удалось отредактировать сообщение @ProtocolGEbot, отправляю новое",
                exc_info=True,
            )

    await event.respond(reply.text, buttons=buttons)
