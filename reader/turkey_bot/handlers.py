"""Telethon-адаптер Turkey-бота — извлекает identity/текст/callback из
реальных Telethon-событий и делегирует всю логику
reader/turkey_bot/conversation.py::ConversationController (см. design
report "Перестроить UX Turkey test bot").

Identity — ВСЕГДА event.sender_id/event.chat_id (numeric), никогда из тела
callback_data (car_id/plate в callback сам по себе НЕ доказывает владение —
ConversationController перепроверяет заново на КАЖДОМ вызове)."""

import logging

from telethon import Button, TelegramClient, events
from telethon.errors.rpcerrorlist import QueryIdInvalidError

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
    debt_refresh_button_keyboard,
    debt_refresh_confirm_keyboard,
    decode_car_action_callback,
    decode_car_open_by_plate_callback,
    decode_car_open_callback,
    decode_debt_refresh_cancel_callback,
    decode_debt_refresh_confirm_callback,
    decode_debt_refresh_pick_callback,
    decode_help_callback,
    decode_manager_car_open_callback,
    decode_manager_car_toggle_callback,
    decode_manager_cars_page_callback,
    decode_search_back_callback,
    decode_search_new_callback,
    decode_search_page_callback,
    encode_car_open_callback,
    georgian_bot_link_keyboard,
    help_menu_keyboard,
    help_section_keyboard,
    main_menu_keyboard,
    manager_car_detail_keyboard,
    manager_cars_page_keyboard,
    my_cars_list_keyboard,
    search_entry_keyboard,
    search_result_keyboard,
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

    def _record_known_user(
        telegram_user_id: int,
        telegram_chat_id: int,
        username: str | None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> None:
        if known_users_repository is not None:
            known_users_repository.record_seen(
                telegram_user_id=telegram_user_id, telegram_chat_id=telegram_chat_id,
                telegram_username=username, first_name=first_name, last_name=last_name,
            )

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def _on_message(event: events.NewMessage.Event) -> None:
        text = event.raw_text
        if not text or not text.strip():
            return

        sender = await event.get_sender()
        username = getattr(sender, "username", None) if sender is not None else None
        first_name = getattr(sender, "first_name", None) if sender is not None else None
        last_name = getattr(sender, "last_name", None) if sender is not None else None
        _record_known_user(event.sender_id, event.chat_id, username, first_name, last_name)
        is_trusted = controller.is_trusted(event.sender_id)

        reply = await controller.handle_text(
            text, chat_id=event.chat_id, telegram_user_id=event.sender_id,
        )
        await _send_reply(event, reply, is_trusted=is_trusted)

    @client.on(events.CallbackQuery(func=lambda e: e.is_private))
    async def _on_callback(event: events.CallbackQuery.Event) -> None:
        sender = await event.get_sender()
        username = getattr(sender, "username", None) if sender is not None else None
        first_name = getattr(sender, "first_name", None) if sender is not None else None
        last_name = getattr(sender, "last_name", None) if sender is not None else None
        _record_known_user(event.sender_id, event.chat_id, username, first_name, last_name)
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
            if action == "check":
                # "check" запускает UnifiedTurkeyCheckService целиком
                # (GİB+Avrasya+KGM, до 35 CAPTCHA-попыток на провайдера) —
                # это 9-34+ секунд реального времени (см. диагностику
                # "QueryIdInvalidError после долгой manual check"). К
                # моменту завершения Telegram callback-query токен уже
                # истекает, и event.answer() ПОСЛЕ проверки бросает
                # QueryIdInvalidError, которая (будучи непойманной) рушила
                # весь handler ДО _send_reply — итоговый ответ терялся,
                # хотя проверка успешно завершалась. Поэтому ack делаем
                # СРАЗУ, до начала проверки (тот же приём, что и у
                # debt_refresh_confirm выше), и ловим ИМЕННО ожидаемый
                # QueryIdInvalidError — остальные Telegram-ошибки не
                # маскируем.
                try:
                    await event.answer()
                except QueryIdInvalidError:
                    pass
                reply = await controller.handle_car_action(
                    action, car_id, chat_id=event.chat_id, telegram_user_id=event.sender_id,
                )
                if reply is None:
                    # Ответ уже отправлен выше (event.answer() нельзя
                    # вызвать дважды) — та же "неизвестная кнопка" ситуация
                    # (car_id удалён/чужой), но обычным сообщением вместо
                    # alert-попапа.
                    await event.respond(UNKNOWN_BUTTON_TEXT)
                    return
                await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=False)
                return
            reply = await controller.handle_car_action(
                action, car_id, chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            if reply is None:
                await event.answer(UNKNOWN_BUTTON_TEXT, alert=True)
                return
            await event.answer()
            # my-cars/card/toggle/delete flow -> редактируем СУЩЕСТВУЮЩЕЕ
            # сообщение (см. задачу п.5: "не создавать новое сообщение без
            # необходимости... повторить Georgian bot pattern") — "history"
            # даёт полноценный отчёт (та же логика, что раньше объединяла
            # его с "check": новое сообщение уместнее, чем правка карточки)
            # — back_to_car_id всё равно даёт путь назад к карточке (см.
            # _first_message_buttons).
            await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=action != "history")
            return

        # manager/trusted-operator "🚗 Мои автомобили" (см. задачу
        # "Реализуем manager/trusted 'Мои автомобили' для Turkey bot") —
        # is_trusted перепроверяется ВНУТРИ controller-методов на каждый
        # вызов (см. задачу п.8.H), не только здесь через is_trusted
        # переменную выше (которая только решает layout main_menu_keyboard) —
        # None от controller означает либо НЕ trusted, либо car_id не
        # существует, в обоих случаях один и тот же безопасный отказ.
        manager_page = decode_manager_cars_page_callback(event.data)
        if manager_page is not None:
            reply = controller.handle_manager_cars_page(manager_page, telegram_user_id=event.sender_id)
            if reply is None:
                await event.answer(UNKNOWN_BUTTON_TEXT, alert=True)
                return
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=True)
            return

        manager_car_open = decode_manager_car_open_callback(event.data)
        if manager_car_open is not None:
            car_id, page = manager_car_open
            reply = controller.handle_manager_car_open(car_id, page, telegram_user_id=event.sender_id)
            if reply is None:
                await event.answer(UNKNOWN_BUTTON_TEXT, alert=True)
                return
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=True)
            return

        manager_car_toggle = decode_manager_car_toggle_callback(event.data)
        if manager_car_toggle is not None:
            car_id, page = manager_car_toggle
            reply = controller.handle_manager_car_toggle(car_id, page, telegram_user_id=event.sender_id)
            if reply is None:
                await event.answer(UNKNOWN_BUTTON_TEXT, alert=True)
                return
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=True)
            return

        search_page = decode_search_page_callback(event.data)
        if search_page is not None:
            query_type, query, page = search_page
            reply = controller.handle_search_page(query_type, query, page, telegram_user_id=event.sender_id)
            if reply is None:
                await event.answer(UNKNOWN_BUTTON_TEXT, alert=True)
                return
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=True)
            return

        if decode_search_new_callback(event.data):
            reply = controller.handle_search_new(chat_id=event.chat_id, telegram_user_id=event.sender_id)
            if reply is None:
                await event.answer(UNKNOWN_BUTTON_TEXT, alert=True)
                return
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=True)
            return

        if decode_search_back_callback(event.data):
            reply = controller.handle_search_back(chat_id=event.chat_id, telegram_user_id=event.sender_id)
            if reply is None:
                await event.answer(UNKNOWN_BUTTON_TEXT, alert=True)
                return
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=True)
            return

        if decode_debt_refresh_pick_callback(event.data):
            reply = controller.handle_debt_refresh_pick(telegram_user_id=event.sender_id)
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=True)
            return

        if decode_debt_refresh_confirm_callback(event.data):
            # Немедленный event.answer() ДО (а не после) долгой live-
            # проверки (см. задачу "TELEGRAM CALLBACK UX") — в отличие от
            # остальных callback'ов этого файла: refresh может проверять
            # много машин подряд (до 35 captcha-попыток на провайдера
            # каждая, см. TurkeyDebtRefreshService._REFRESH_MAX_ATTEMPTS) —
            # ждать этого перед снятием спиннера кнопки означало бы риск
            # "зависшего" индикатора загрузки у менеджера.
            await event.answer()
            reply = await controller.handle_debt_refresh_confirm(telegram_user_id=event.sender_id)
            await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=False)
            return

        if decode_debt_refresh_cancel_callback(event.data):
            reply = controller.handle_debt_refresh_cancel(telegram_user_id=event.sender_id)
            await event.answer()
            await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=True)
            return

        await event.answer(UNKNOWN_BUTTON_TEXT, alert=True)

    logger.info("✔ Turkey bot handlers зарегистрированы")


def _first_message_buttons(reply: BotReply, *, is_trusted: bool, has_extra: bool):
    """Приоритет клавiатур на ПЕРВОМ отправленном сообщении (Telethon не
    может совместить несколько видов сразу) — show_cancel_button >
    my_cars/check-picker > manager_cars_page_options > manager_car_detail_page >
    car_delete_confirm > car_card > check_now_confirmation > cta_buttons
    (+ back_to_car_id, если задан) > back_to_car_id (один) >
    show_georgian_bot_link > help_keyboard > show_main_menu (если нет
    extra_texts). manager_cars_page_options/manager_car_detail_page — см.
    задачу "Реализуем manager/trusted 'Мои автомобили' для Turkey bot" —
    ОТДЕЛЬНЫЕ поля от my_cars выше, никогда не заданы одновременно с ним
    (см. ConversationController: self-service и manager — разные ветки
    handle_text)."""
    if reply.show_cancel_button:
        return cancel_keyboard()
    if reply.my_cars is not None:
        cars = list(reply.my_cars)
        if reply.is_check_picker:
            return check_now_picker_keyboard(cars)
        return my_cars_list_keyboard(cars, reply.my_cars_monitoring_active or {})
    if reply.manager_cars_page_options is not None:
        return manager_cars_page_keyboard(
            reply.manager_cars_page_options,
            page=reply.manager_cars_page or 0,
            total_pages=reply.manager_cars_total_pages or 1,
        )
    if reply.manager_car_detail_page is not None:
        return manager_car_detail_keyboard(reply.manager_car_detail_page)
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
    if reply.search_prompt:
        # Manager/trusted Search (см. задачу) — экран ввода запроса.
        return search_entry_keyboard()
    if reply.search_result_shown:
        return search_result_keyboard(
            query_type=reply.search_query_type, query=reply.search_query,
            page=reply.search_page, total_pages=reply.search_total_pages,
        )
    if reply.help_keyboard == "menu":
        return help_menu_keyboard()
    if reply.help_keyboard == "section":
        return help_section_keyboard()
    if reply.debt_refresh_confirm_car_count is not None:
        return debt_refresh_confirm_keyboard(reply.debt_refresh_confirm_car_count)
    if reply.debt_refresh_available and not has_extra:
        return debt_refresh_button_keyboard()
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
        elif is_last and reply.debt_refresh_available:
            extra_buttons = debt_refresh_button_keyboard()
        elif is_last and reply.show_main_menu:
            extra_buttons = main_menu_keyboard(is_trusted=is_trusted)
        else:
            extra_buttons = None
        await event.respond(extra_text, buttons=extra_buttons)
