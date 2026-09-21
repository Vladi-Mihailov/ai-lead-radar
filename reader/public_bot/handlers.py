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

from telethon import Button, TelegramClient, events

from reader.public_bot.conversation import ConversationController
from reader.public_bot.keyboards import (
    STOP_NO,
    add_client_decision_keyboard,
    car_delete_confirm_keyboard,
    car_detail_keyboard,
    check_now_options_keyboard,
    decode_add_client_decision_callback,
    decode_check_now_callback,
    decode_my_car_delete_callback,
    decode_my_car_delete_cancel_callback,
    decode_my_car_delete_confirm_callback,
    decode_my_car_open_callback,
    decode_my_car_turn_off_callback,
    decode_my_car_turn_on_callback,
    decode_my_cars_page_callback,
    decode_period_callback,
    decode_trusted_stop_confirm_callback,
    decode_trusted_stop_pick_callback,
    decode_trusted_task_continue_callback,
    decode_trusted_task_open_callback,
    decode_trusted_task_period_callback,
    decode_trusted_task_toggle_callback,
    decode_trusted_tasks_page_callback,
    main_menu_keyboard,
    my_cars_page_keyboard,
    period_choice_keyboard,
    trusted_stop_confirm_keyboard,
    trusted_stop_options_keyboard,
    trusted_task_detail_keyboard,
    trusted_task_off_keyboard,
    trusted_task_period_choice_keyboard,
    trusted_tasks_page_keyboard,
    turkey_bot_link_keyboard,
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
        is_trusted = controller.is_trusted(event.sender_id)

        reply = await controller.handle_text(
            text,
            chat_id=event.chat_id,
            telegram_user_id=event.sender_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )

        await _send_reply(event, reply, is_trusted=is_trusted)

    @client.on(events.CallbackQuery(func=lambda e: e.is_private))
    async def _on_callback(event: events.CallbackQuery.Event) -> None:
        data = event.data
        username, first_name, last_name = await _sender_names(event)
        _record_known_user(event.sender_id, event.chat_id, username)
        is_trusted = controller.is_trusted(event.sender_id)

        wants_client = decode_add_client_decision_callback(data)
        if wants_client is not None:
            # bool, а не truthy-проверка: False (Отмена) — тоже валидный,
            # обрабатываемый выбор, а не "это не тот callback" (см.
            # keyboards.py::decode_add_client_decision_callback).
            reply = controller.handle_add_client_decision(
                wants_client, chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
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
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        check_now_id = decode_check_now_callback(data)
        if check_now_id is not None:
            reply = await controller.handle_check_now_choice(
                check_now_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        my_cars_page = decode_my_cars_page_callback(data)
        if my_cars_page is not None:
            reply = controller.handle_my_cars_page(my_cars_page, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        my_car_open = decode_my_car_open_callback(data)
        if my_car_open is not None:
            subscription_id, page = my_car_open
            reply = controller.handle_my_car_open(subscription_id, page, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        my_car_turn_off = decode_my_car_turn_off_callback(data)
        if my_car_turn_off is not None:
            subscription_id, page = my_car_turn_off
            reply = controller.handle_my_car_turn_off(subscription_id, page, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        my_car_turn_on = decode_my_car_turn_on_callback(data)
        if my_car_turn_on is not None:
            subscription_id, page = my_car_turn_on
            reply = controller.handle_my_car_turn_on(subscription_id, page, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        my_car_delete = decode_my_car_delete_callback(data)
        if my_car_delete is not None:
            subscription_id, page = my_car_delete
            reply = controller.handle_my_car_delete_prompt(subscription_id, page, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        my_car_delete_confirm = decode_my_car_delete_confirm_callback(data)
        if my_car_delete_confirm is not None:
            subscription_id, page = my_car_delete_confirm
            reply = controller.handle_my_car_delete_confirm(
                subscription_id, page, telegram_user_id=event.sender_id,
            )
            await event.answer()
            await _send_reply(event, reply, prefer_edit=True, is_trusted=is_trusted)
            return

        my_car_delete_cancel = decode_my_car_delete_cancel_callback(data)
        if my_car_delete_cancel is not None:
            subscription_id, page = my_car_delete_cancel
            reply = controller.handle_my_car_delete_cancel(
                subscription_id, page, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        trusted_tasks_page = decode_trusted_tasks_page_callback(data)
        if trusted_tasks_page is not None:
            reply = controller.handle_trusted_tasks_page(
                trusted_tasks_page, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        trusted_task_open = decode_trusted_task_open_callback(data)
        if trusted_task_open is not None:
            task_id, page = trusted_task_open
            reply = controller.handle_trusted_task_open(task_id, page, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        trusted_task_toggle = decode_trusted_task_toggle_callback(data)
        if trusted_task_toggle is not None:
            task_id, page = trusted_task_toggle
            reply = controller.handle_trusted_task_toggle(task_id, page, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        trusted_task_continue = decode_trusted_task_continue_callback(data)
        if trusted_task_continue is not None:
            task_id, page = trusted_task_continue
            reply = controller.handle_trusted_task_continue(task_id, page, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        trusted_task_period = decode_trusted_task_period_callback(data)
        if trusted_task_period is not None:
            task_id, days, page = trusted_task_period
            reply = controller.handle_trusted_task_period_choice(
                task_id, days, page, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        trusted_stop_pick_id = decode_trusted_stop_pick_callback(data)
        if trusted_stop_pick_id is not None:
            reply = controller.handle_trusted_stop_pick(
                trusted_stop_pick_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        trusted_stop_confirm_id = decode_trusted_stop_confirm_callback(data)
        if trusted_stop_confirm_id is not None:
            reply = controller.handle_trusted_stop_confirm(
                trusted_stop_confirm_id, telegram_user_id=event.sender_id,
            )
            await event.answer()
            await _send_reply(event, reply, prefer_edit=True, is_trusted=is_trusted)
            return

        if data == STOP_NO:
            reply = controller.handle_stop_cancel()
            await event.answer()
            await _send_reply(event, reply, prefer_edit=True, is_trusted=is_trusted)
            return

        await event.answer("Неизвестная или устаревшая кнопка", alert=True)

    # Bot identity switch (см. audit report) — @ProtocolGEbot.
    logger.info("✔ @ProtocolGEbot handlers зарегистрированы")


async def _answer_and_send(event, reply, *, is_trusted: bool = False) -> None:
    """Общий хвост для callback'ов, которые могут вернуть None (=
    подписка не найдена/не принадлежит этому пользователю, см.
    reader/public_bot/keyboards.py про то, почему сам факт валидного
    subscription_id в callback_data ничего не доказывает) — в этом случае
    показываем короткий alert и ничего не создаём/не меняем."""
    if reply is None:
        await event.answer(CALLBACK_NOT_AUTHORIZED_TEXT, alert=True)
        return
    await event.answer()
    await _send_reply(event, reply, prefer_edit=True, is_trusted=is_trusted)


async def _send_reply(event, reply, *, prefer_edit: bool = False, is_trusted: bool = False) -> None:
    """is_trusted — ТОЛЬКО для main_menu_keyboard(is_trusted=...) (см.
    "⛔ Остановить мониторинг"/"📊 Статистика"): единственное место,
    решающее, показывать ли эти две кнопки в персистентной reply-
    клавиатуре — вычисляется caller'ом (_on_message/_on_callback) через
    controller.is_trusted(event.sender_id) один раз на событие."""
    buttons = None
    if reply.show_main_menu:
        buttons = main_menu_keyboard(is_trusted=is_trusted)
    elif reply.show_period_buttons:
        buttons = period_choice_keyboard()
    elif reply.show_add_client_decision_buttons:
        buttons = add_client_decision_keyboard()
    elif reply.check_now_options:
        buttons = check_now_options_keyboard(reply.check_now_options)
    elif reply.trusted_stop_options:
        buttons = trusted_stop_options_keyboard(reply.trusted_stop_options)
    elif reply.trusted_stop_confirm_task_id is not None:
        buttons = trusted_stop_confirm_keyboard(
            reply.trusted_stop_confirm_task_id, label=reply.trusted_stop_confirm_button_label,
        )
    elif reply.trusted_tasks_page is not None:
        buttons = trusted_tasks_page_keyboard(
            reply.trusted_tasks_page_options or [],
            page=reply.trusted_tasks_page, total_pages=reply.trusted_tasks_total_pages,
        )
    elif reply.trusted_task_detail_id is not None:
        buttons = trusted_task_detail_keyboard(page=reply.trusted_task_detail_page)
    elif reply.trusted_task_off_id is not None:
        buttons = trusted_task_off_keyboard(reply.trusted_task_off_id, page=reply.trusted_task_off_page)
    elif reply.trusted_task_period_id is not None:
        buttons = trusted_task_period_choice_keyboard(
            reply.trusted_task_period_id, page=reply.trusted_task_period_page,
        )
    elif reply.my_cars_page_options is not None:
        # car-centric "📋 Мои авто" (см. design report про переработку
        # UX) — пустой список ([]) — валидный (см. design: "показать 0 из
        # N" в принципе не бывает; пустая страница отфильтрована в
        # ConversationController), но is not None различает "список
        # автомобилей" от "его вовсе не было в этом ответе".
        buttons = my_cars_page_keyboard(
            reply.my_cars_page_options, page=reply.my_cars_page, total_pages=reply.my_cars_total_pages,
        )
    elif reply.car_delete_confirm_subscription_id is not None:
        buttons = car_delete_confirm_keyboard(
            reply.car_delete_confirm_subscription_id, page=reply.car_delete_confirm_page,
        )
    elif reply.car_detail_subscription_id is not None:
        buttons = car_detail_keyboard(
            reply.car_detail_subscription_id,
            monitoring_state=reply.car_detail_monitoring_state,
            page=reply.car_detail_page,
        )
    elif reply.cta_buttons:
        # Manual "🔎 Проверить сейчас" — коммерческие CTA (см.
        # ConversationController._owner_cta_buttons); conversation.py
        # намеренно передаёт их как (label, url), не Telethon Button —
        # реальные кнопки строятся только здесь.
        buttons = [
            [Button.url(label, url) for label, url in row] for row in reply.cta_buttons
        ]
    elif reply.show_turkey_bot_link:
        # См. design report "унификация UI" — переход в Turkey-бот теперь
        # ОТВЕТ на нажатие "🇹🇷 Штрафы Турции" (обычной reply-кнопки, см.
        # reader/public_bot/keyboards.py::main_menu_keyboard), а НЕ
        # автоматическое companion-сообщение при показе главного меню.
        buttons = turkey_bot_link_keyboard()

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
