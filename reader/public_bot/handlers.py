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
from telethon.errors.rpcerrorlist import QueryIdInvalidError

from reader.protocol_support.service import ProtocolSupportService
from reader.protocol_support.telethon_gateway import handle_private_support
from reader.public_bot import manager_alerts
from reader.public_bot.conversation import ConversationController
from reader.public_bot.keyboards import (
    STOP_NO,
    add_client_decision_keyboard,
    car_delete_confirm_keyboard,
    car_detail_keyboard,
    check_now_options_keyboard,
    debt_refresh_confirm_keyboard,
    debt_statistics_keyboard,
    decode_add_client_decision_callback,
    decode_check_now_callback,
    decode_debt_list_page_callback,
    decode_debt_refresh_cancel_callback,
    decode_debt_refresh_confirm_callback,
    decode_debt_refresh_pick_callback,
    decode_manual_bootstrap_add_another_callback,
    decode_manual_bootstrap_addfine_cancel_callback,
    decode_manual_bootstrap_addfine_edit_callback,
    decode_manual_bootstrap_addfine_save_callback,
    decode_manual_bootstrap_addfine_start_callback,
    decode_manual_bootstrap_finish_back_callback,
    decode_manual_bootstrap_finish_confirm_callback,
    decode_manual_bootstrap_finish_prompt_callback,
    decode_manual_bootstrap_nofine_callback,
    decode_manual_bootstrap_nofine_cancel_callback,
    decode_manual_bootstrap_nofine_confirm_callback,
    decode_manual_bootstrap_pending_open_callback,
    decode_my_car_check_now_once_callback,
    decode_my_car_delete_callback,
    decode_my_car_delete_cancel_callback,
    decode_my_car_delete_confirm_callback,
    decode_my_car_open_callback,
    decode_my_car_resume_callback,
    decode_my_car_turn_off_callback,
    decode_my_car_turn_on_callback,
    decode_my_cars_page_callback,
    decode_period_callback,
    decode_protocol_check_back_to_menu_callback,
    decode_protocol_check_back_to_method_callback,
    decode_protocol_check_back_to_protocol_step1_callback,
    decode_protocol_check_back_to_vehicle_step1_callback,
    decode_protocol_check_method_callback,
    decode_search_back_callback,
    decode_search_new_callback,
    decode_search_page_callback,
    decode_trusted_check_now_once_callback,
    decode_trusted_stop_confirm_callback,
    decode_trusted_stop_pick_callback,
    decode_trusted_task_continue_callback,
    decode_trusted_task_open_callback,
    decode_trusted_task_period_callback,
    decode_trusted_task_toggle_callback,
    decode_trusted_tasks_page_callback,
    main_menu_keyboard,
    my_car_inactive_keyboard,
    my_cars_page_keyboard,
    period_choice_keyboard,
    protocol_check_back_to_method_keyboard,
    protocol_check_back_to_protocol_step1_keyboard,
    protocol_check_back_to_vehicle_step1_keyboard,
    protocol_check_method_keyboard,
    search_entry_keyboard,
    search_result_keyboard,
    trusted_check_now_inactive_keyboard,
    trusted_stop_confirm_keyboard,
    trusted_stop_options_keyboard,
    trusted_task_detail_keyboard,
    trusted_task_off_keyboard,
    trusted_task_period_choice_keyboard,
    trusted_tasks_page_keyboard,
    turkey_bot_link_keyboard,
)
from reader.public_bot.known_users_repository import BotKnownUsersRepository
from reader.public_bot.subscription_service import CHECK_ALREADY_IN_PROGRESS
from reader.public_bot.texts import CALLBACK_NOT_AUTHORIZED_TEXT, MAIN_MENU_TEXT

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
    support: ProtocolSupportService | None = None,
    manager_chat_id: int | None = None,
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
    что-либо доставить этому numeric id, — он уже хоть раз ему написал).

    manager_chat_id — "NEW USER EMERGENCY WORKFLOW" (см. задачу "back off
    when police.ge is unavailable"): куда слать проактивные алерты о новых
    пользователях/первом добавленном автомобиле (см. reader/public_bot/
    manager_alerts.py) — settings.public_bot.support_chat_id (та же
    "внутренняя группа менеджеров", см. reader/public_bot/main.py). None —
    фича выключена (напр. support_chat_id не настроен)."""

    def _record_known_user(
        telegram_user_id: int,
        telegram_chat_id: int,
        username: str | None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> bool:
        """True — telegram_user_id НЕ был известен ДО этого вызова (см.
        задачу "NEW USER EMERGENCY WORKFLOW") — проверяется ДО upsert, по
        персистентной bot_known_users, поэтому restart процесса/повторный
        /start того же пользователя всегда дают False (exactly-once по
        самой природе таблицы, без отдельного флага)."""
        if known_users_repository is None:
            return False
        is_new = not known_users_repository.is_known(telegram_user_id)
        known_users_repository.record_seen(
            telegram_user_id=telegram_user_id, telegram_chat_id=telegram_chat_id,
            telegram_username=username, first_name=first_name, last_name=last_name,
        )
        return is_new

    async def _notify_manager_new_user(
        event, username: str | None, first_name: str | None, last_name: str | None,
    ) -> None:
        if manager_chat_id is None:
            return
        text = manager_alerts.format_new_user_alert(
            telegram_user_id=event.sender_id, username=username,
            first_name=first_name, last_name=last_name,
        )
        try:
            await event.client.send_message(manager_chat_id, text, parse_mode="md")
        except Exception:
            # Fail-open (тот же принцип, что и у translation/notification
            # везде в проекте) — сбой алерта менеджеру не должен ломать
            # обычное взаимодействие пользователя с ботом.
            logger.exception("manager alert (новый пользователь): не удалось отправить")

    async def _notify_manager_alert_if_any(event, reply) -> None:
        text = getattr(reply, "manager_alert_text", None)
        if manager_chat_id is None or not text:
            return
        raw_buttons = getattr(reply, "manager_alert_buttons", None)
        buttons = (
            [[Button.inline(label, data) for label, data in row] for row in raw_buttons]
            if raw_buttons
            else None
        )
        try:
            await event.client.send_message(manager_chat_id, text, parse_mode="md", buttons=buttons)
        except Exception:
            logger.exception("manager alert: не удалось отправить")

    async def _notify_customers_if_any(event, reply) -> None:
        """"ВРЕМЕННЫЙ manager workflow для Georgia fines bot" (см. задачу,
        "✅ Штрафов нет" → клиент получает canonical "штрафов не найдено")
        — см. BotReply.customer_notify_messages докстрок: fail-open, тот
        же принцип, что и у _notify_manager_alert_if_any выше (сбой
        доставки одному клиенту не должен мешать остальным/менеджеру)."""
        messages = getattr(reply, "customer_notify_messages", None)
        if not messages:
            return
        for chat_id, text in messages:
            try:
                await event.client.send_message(chat_id, text, parse_mode="md")
            except Exception:
                logger.exception("manual bootstrap: не удалось доставить сообщение клиенту")

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def _on_message(event: events.NewMessage.Event) -> None:
        # «👨‍💼 Оператор» / открытый диалог с оператором — раньше обычной
        # логики (и раньше отсечения пустого текста: фото/документ без подписи).
        trusted = controller.is_trusted(event.sender_id)
        if await handle_private_support(
            event, support, is_trusted=trusted, reset_state=lambda: controller.reset_conversation(event.chat_id),
            main_menu=main_menu_keyboard(is_trusted=trusted), main_menu_text=MAIN_MENU_TEXT,
        ):
            return

        text = event.raw_text
        if not text or not text.strip():
            return

        username, first_name, last_name = await _sender_names(event)
        is_new_user = _record_known_user(event.sender_id, event.chat_id, username, first_name, last_name)
        if is_new_user:
            await _notify_manager_new_user(event, username, first_name, last_name)
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
        is_new_user = _record_known_user(event.sender_id, event.chat_id, username, first_name, last_name)
        if is_new_user:
            await _notify_manager_new_user(event, username, first_name, last_name)
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
            # add_car()/add_delegated_car*() внутри делают немедленную
            # onboarding-проверку (live police.ge) — см.
            # _ack_then_await_slow_check про QueryIdInvalidError.
            await _ack_then_await_slow_check(
                event,
                controller.handle_period_choice(
                    days,
                    chat_id=event.chat_id,
                    telegram_user_id=event.sender_id,
                    first_name=first_name,
                    last_name=last_name,
                ),
                is_trusted=is_trusted,
                on_reply=_notify_manager_alert_if_any,
            )
            return

        check_now_id = decode_check_now_callback(data)
        if check_now_id is not None:
            # Production incident O127PX62 (2026-10-21) — см.
            # _ack_then_await_slow_check. "⏳" + repeated-click guard (см.
            # "Fix the Georgia public bot Check Now flow") — см.
            # _ack_hourglass_then_await_slow_check.
            await _ack_hourglass_then_await_slow_check(
                event,
                lambda on_check_started: controller.handle_check_now_choice(
                    check_now_id, telegram_user_id=event.sender_id, on_check_started=on_check_started,
                ),
                is_trusted=is_trusted,
            )
            return

        my_car_check_now_once = decode_my_car_check_now_once_callback(data)
        if my_car_check_now_once is not None:
            await _ack_hourglass_then_await_slow_check(
                event,
                lambda on_check_started: controller.handle_my_car_check_now_once(
                    my_car_check_now_once, telegram_user_id=event.sender_id,
                    on_check_started=on_check_started,
                ),
                is_trusted=is_trusted,
            )
            return

        my_car_resume = decode_my_car_resume_callback(data)
        if my_car_resume is not None:
            reply = controller.handle_my_car_resume_prompt(
                my_car_resume, chat_id=event.chat_id, telegram_user_id=event.sender_id, username=username,
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

        trusted_check_now_once_id = decode_trusted_check_now_once_callback(data)
        if trusted_check_now_once_id is not None:
            await _ack_hourglass_then_await_slow_check(
                event,
                lambda on_check_started: controller.handle_trusted_check_now_once(
                    trusted_check_now_once_id, telegram_user_id=event.sender_id,
                    on_check_started=on_check_started,
                ),
                is_trusted=is_trusted,
            )
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

        if decode_debt_refresh_pick_callback(data):
            reply = controller.handle_debt_refresh_pick(telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        if decode_debt_refresh_confirm_callback(data):
            # Батч-проверка нескольких машин подряд — ещё медленнее
            # одиночного check_now (см. _ack_then_await_slow_check про
            # QueryIdInvalidError; прежнее "ack сознательно после" здесь
            # решение пересмотрено по production-инциденту O127PX62).
            await _ack_then_await_slow_check(
                event,
                controller.handle_debt_refresh_confirm(telegram_user_id=event.sender_id),
                is_trusted=is_trusted,
            )
            return

        if decode_debt_refresh_cancel_callback(data):
            reply = controller.handle_debt_refresh_cancel(telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        debt_list_page = decode_debt_list_page_callback(data)
        if debt_list_page is not None:
            reply = controller.handle_debt_list_page(debt_list_page, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        search_page = decode_search_page_callback(data)
        if search_page is not None:
            query_type, query, page = search_page
            reply = await controller.handle_search_page(
                query_type, query, page, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        if decode_search_new_callback(data):
            reply = controller.handle_search_new(chat_id=event.chat_id, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        if decode_search_back_callback(data):
            reply = controller.handle_search_back(chat_id=event.chat_id, telegram_user_id=event.sender_id)
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        protocol_check_method = decode_protocol_check_method_callback(data)
        if protocol_check_method is not None:
            reply = controller.handle_protocol_check_method(
                protocol_check_method, chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        if decode_protocol_check_back_to_menu_callback(data):
            reply = controller.handle_protocol_check_back_to_menu(
                chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        if decode_protocol_check_back_to_method_callback(data):
            reply = controller.handle_protocol_check_back_to_method(
                chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        if decode_protocol_check_back_to_vehicle_step1_callback(data):
            reply = controller.handle_protocol_check_back_to_vehicle_step1(
                chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        if decode_protocol_check_back_to_protocol_step1_callback(data):
            reply = controller.handle_protocol_check_back_to_protocol_step1(
                chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        # "ВРЕМЕННЫЙ manager workflow для Georgia fines bot" (см. задачу) —
        # fine_admin-only, см. ConversationController._is_fine_admin
        # (перепроверяется внутри каждого handle_manual_bootstrap_* заново,
        # сам факт валидного car_number в callback_data ничего не доказывает,
        # см. keyboards.py докстрок).
        pending_open_plate = decode_manual_bootstrap_pending_open_callback(data)
        if pending_open_plate is not None:
            reply = controller.handle_manual_bootstrap_pending_open(
                pending_open_plate, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        nofine_plate = decode_manual_bootstrap_nofine_callback(data)
        if nofine_plate is not None:
            reply = controller.handle_manual_bootstrap_nofine(
                nofine_plate, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        nofine_cancel_plate = decode_manual_bootstrap_nofine_cancel_callback(data)
        if nofine_cancel_plate is not None:
            reply = controller.handle_manual_bootstrap_nofine_cancel(
                nofine_cancel_plate, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        nofine_confirm_plate = decode_manual_bootstrap_nofine_confirm_callback(data)
        if nofine_confirm_plate is not None:
            # customer_notify_messages (см. BotReply докстрок) — отдельная
            # рассылка клиентам, тот же on_reply-приём, что и у
            # _notify_manager_alert_if_any в period-callback выше.
            await _answer_and_send(
                event,
                controller.handle_manual_bootstrap_nofine_confirm(
                    nofine_confirm_plate, telegram_user_id=event.sender_id,
                ),
                is_trusted=is_trusted,
                on_reply=_notify_customers_if_any,
            )
            return

        addfine_start_plate = decode_manual_bootstrap_addfine_start_callback(data)
        if addfine_start_plate is not None:
            reply = controller.handle_manual_bootstrap_addfine_start(
                addfine_start_plate, chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        addfine_edit_plate = decode_manual_bootstrap_addfine_edit_callback(data)
        if addfine_edit_plate is not None:
            # "✏️ Исправить" (см. задачу п.7) — тот же старт с нуля, что и
            # "➕ Добавить штраф"/"➕ Добавить ещё штраф" (см.
            # handle_manual_bootstrap_addfine_start докстрок).
            reply = controller.handle_manual_bootstrap_addfine_start(
                addfine_edit_plate, chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        add_another_plate = decode_manual_bootstrap_add_another_callback(data)
        if add_another_plate is not None:
            reply = controller.handle_manual_bootstrap_addfine_start(
                add_another_plate, chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        addfine_cancel_plate = decode_manual_bootstrap_addfine_cancel_callback(data)
        if addfine_cancel_plate is not None:
            reply = controller.handle_manual_bootstrap_addfine_cancel(
                addfine_cancel_plate, chat_id=event.chat_id, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        addfine_save_plate = decode_manual_bootstrap_addfine_save_callback(data)
        if addfine_save_plate is not None:
            await _ack_then_await_slow_check(
                event,
                controller.handle_manual_bootstrap_addfine_save(
                    addfine_save_plate, chat_id=event.chat_id, telegram_user_id=event.sender_id,
                ),
                is_trusted=is_trusted,
            )
            return

        finish_prompt_plate = decode_manual_bootstrap_finish_prompt_callback(data)
        if finish_prompt_plate is not None:
            reply = controller.handle_manual_bootstrap_finish_prompt(
                finish_prompt_plate, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        finish_back_plate = decode_manual_bootstrap_finish_back_callback(data)
        if finish_back_plate is not None:
            reply = controller.handle_manual_bootstrap_finish_back(
                finish_back_plate, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        finish_confirm_plate = decode_manual_bootstrap_finish_confirm_callback(data)
        if finish_confirm_plate is not None:
            reply = controller.handle_manual_bootstrap_finish_confirm(
                finish_confirm_plate, telegram_user_id=event.sender_id,
            )
            await _answer_and_send(event, reply, is_trusted=is_trusted)
            return

        await event.answer("Неизвестная или устаревшая кнопка", alert=True)

    # Bot identity switch (см. audit report) — @ProtocolGEbot.
    logger.info("✔ @ProtocolGEbot handlers зарегистрированы")


# Production bug (см. журнал): event.respond(reply.text, ...) падал с
# telethon.errors.rpcerrorlist.MessageTooLongError — police.ge-запрос
# успевал завершиться (HTTP 200), но СГЕНЕРИРОВАННЫЙ текст ответа
# превышал предел Telegram (реальный лимит — 4096 символов) — консервативный
# запас ниже реального лимита (не 4096 ровно), т.к. Telethon/Telegram
# считают длину уже ПОСЛЕ собственной обработки entities/markdown, где
# точный запас неочевиден. Генерик-хелпер — общий для ЛЮБОГО BotReply
# (см. задачу: "not specific to one plate or 'Проверить сейчас'"), не
# знает ничего про police.ge/FineCheckService/бизнес-логику.
_TELEGRAM_SAFE_TEXT_LIMIT = 3900


def _split_telegram_text(text: str, *, limit: int = _TELEGRAM_SAFE_TEXT_LIMIT) -> list[str]:
    """Режет text на куски <= limit символов, НИКОГДА не теряя и не
    дублируя ни одного символа ("".join(_split_telegram_text(text)) ==
    text всегда, см. тесты). Предпочитает границы строк (см. задачу:
    "Prefer splitting at newline boundaries first") — splitlines(keepends=
    True) сохраняет исходные символы конца строки ровно как они были, что
    и даёт лёгкое "join без потерь". Если ОДНА строка сама по себе длиннее
    limit (см. задачу п.2: "hard-split it safely") — режется жёстко, по
    limit символов, без попытки угадать "красивую" границу внутри неё."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            for start in range(0, len(line), limit):
                chunks.append(line[start:start + limit])
            continue
        if current and len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


async def _answer_and_send(event, reply, *, is_trusted: bool = False, on_reply=None) -> None:
    """Общий хвост для callback'ов, которые могут вернуть None (=
    подписка не найдена/не принадлежит этому пользователю, см.
    reader/public_bot/keyboards.py про то, почему сам факт валидного
    subscription_id в callback_data ничего не доказывает) — в этом случае
    показываем короткий alert и ничего не создаём/не меняем.

    on_reply — тот же необязательный side-effect хук, что и у
    _ack_then_await_slow_check (см. его докстрок) — None для всех call
    site'ов, кроме manual-bootstrap "✅ Штрафов нет" (см.
    BotReply.customer_notify_messages)."""
    if reply is None:
        await event.answer(CALLBACK_NOT_AUTHORIZED_TEXT, alert=True)
        return
    await event.answer()
    await _send_reply(event, reply, prefer_edit=True, is_trusted=is_trusted)
    if on_reply is not None:
        await on_reply(event, reply)


async def _ack_then_await_slow_check(event, coro, *, is_trusted: bool = False, on_reply=None) -> None:
    """Для callback'ов, чей handler делает реальный live-check (police.ge,
    Add Car onboarding-проверка, debt refresh) — может занимать секунды (см.
    production incident O127PX62, 2026-10-21: request_timeout=30s + false-
    zero confirmation retry легко превышают срок жизни callback-query
    token — event.answer() ПОСЛЕ проверки бросал QueryIdInvalidError,
    непойманную, рушившую handler ДО _send_reply — итоговый ответ
    терялся, хотя сама проверка успешно завершалась, пусть даже с
    graceful ошибкой). ТОТ ЖЕ проверенный приём, что уже чинил идентичный
    баг на Turkey (см. reader/turkey_bot/handlers.py, commit f062cb7 "fix:
    preserve Turkey replies after long checks") — ack СРАЗУ, до начала
    проверки, ловим ИМЕННО ожидаемый QueryIdInvalidError (остальные
    Telegram-ошибки не маскируем).

    None от coro — тот же смысл, что и в _answer_and_send (подписка/
    задача не найдена/не принадлежит/состояние устарело), но alert-попап
    после answer() уже недоступен (Telegram не позволяет answer() дважды
    на один callback) — обычным сообщением, не alert, тот же приём, что и
    у Turkey (UNKNOWN_BUTTON_TEXT через event.respond()).

    prefer_edit=False (не True, как у _answer_and_send) — та же причина,
    что и у Turkey: после возможной многосекундной паузы оригинальное
    сообщение может быть уже неактуальным/устаревшим для пользователя,
    новое сообщение — более надёжная гарантия доставки.

    on_reply — опциональный побочный side-effect хук (см. задачу "NEW USER
    EMERGENCY WORKFLOW": BotReply.manager_alert_text для только что
    добавленного первого автомобиля) — вызывается ПОСЛЕ отправки ответа
    пользователю (не должен задерживать/блокировать его), None для всех
    остальных call site'ов этой функции."""
    try:
        await event.answer()
    except QueryIdInvalidError:
        pass
    reply = await coro
    if reply is None:
        await event.respond(CALLBACK_NOT_AUTHORIZED_TEXT)
        return
    await _send_reply(event, reply, is_trusted=is_trusted, prefer_edit=False)
    if on_reply is not None:
        await on_reply(event, reply)


async def _ack_hourglass_then_await_slow_check(event, coro_factory, *, is_trusted: bool = False) -> None:
    """Для ровно ТРЁХ callback'ов — check_now_id/my_car_check_now_once/
    trusted_check_now_once (см. "Fix the Georgia public bot Check Now
    flow") — "🔎 Проверить сейчас" семейство, и ТОЛЬКО для них: period
    (onboarding-проверка) и debt_refresh_confirm (батч) намеренно НЕ
    переведены на этот хелпер, остаются на _ack_then_await_slow_check как
    раньше — разные UX-контексты, голый "⏳" там не запрашивался.

    Два отличия от _ack_then_await_slow_check выше:
    1) "⏳" отправляется СРАЗУ после ack, но НЕ раньше гарантированного
       захвата repeated-click guard'а (см. SubscriptionService.
       _check_in_progress) — coro_factory принимает on_check_started,
       вызываемый check_now()/check_now_task_for_search() РОВНО ОДИН раз,
       сразу после захвата guard'а, ДО медленного police.ge/REDNET-запроса
       (см. их докстроки). Это гарантирует: дубль-клик, отклонённый guard'ом
       (CHECK_ALREADY_IN_PROGRESS, см. ниже), никогда не порождает второй
       "⏳" — on_check_started для него просто не вызывается вовсе.
    2) Финальный результат РЕДАКТИРУЕТ именно это "⏳"-сообщение
       (_send_reply(..., edit_message=...)), а не отправляет третье — если
       "⏳" так и не было отправлено (completed/истёкшая карточка выше в
       handle_check_now_choice — guard вообще не затрагивается, живой check
       не запускается), edit_message остаётся None и поведение совпадает с
       обычным _send_reply(..., edit_message=None): одно новое сообщение,
       как и раньше.

    coro_factory — callable(on_check_started) -> coroutine (НЕ готовая
    корутина, как у _ack_then_await_slow_check выше — on_check_started
    должен попасть внутрь controller.handle_*(...) ДО того, как эта
    корутина начнёт выполняться, поэтому вызывающий код в _on_callback
    передаёт лямбду, а не уже построенный coroutine object).

    CHECK_ALREADY_IN_PROGRESS — "acknowledge the callback without starting
    another check... No additional Telegram message is necessary" (см.
    задачу): ack уже произошёл выше, здесь просто return — ни "⏳", ни
    результат, ни CALLBACK_NOT_AUTHORIZED_TEXT (это ДРУГОЕ, уже
    существующее None-значение, см. ветку ниже) не отправляются."""
    try:
        await event.answer()
    except QueryIdInvalidError:
        pass

    hourglass_message = None

    async def _show_hourglass() -> None:
        nonlocal hourglass_message
        hourglass_message = await event.respond("⏳")

    reply = await coro_factory(_show_hourglass)

    if reply is CHECK_ALREADY_IN_PROGRESS:
        return
    if reply is None:
        await event.respond(CALLBACK_NOT_AUTHORIZED_TEXT)
        return
    await _send_reply(event, reply, is_trusted=is_trusted, edit_message=hourglass_message)


async def _send_reply(
    event, reply, *, prefer_edit: bool = False, is_trusted: bool = False, edit_message=None,
) -> None:
    """is_trusted — ТОЛЬКО для main_menu_keyboard(is_trusted=...) (см.
    "⛔ Остановить мониторинг"/"📊 Статистика"): единственное место,
    решающее, показывать ли эти две кнопки в персистентной reply-
    клавиатуре — вычисляется caller'ом (_on_message/_on_callback) через
    controller.is_trusted(event.sender_id) один раз на событие.

    edit_message — опционально, Telethon Message (см. "Fix the Georgia
    public bot Check Now flow" и _ack_hourglass_then_await_slow_check ниже):
    когда задан, первый chunk редактирует ИМЕННО ЭТО сообщение (edit_message
    .edit(...), НЕ event.edit() — это другое, более раннее сообщение, не то,
    на кнопке которого произошёл callback) вместо отправки новой — "⏳"
    чисто заменяется финальным результатом, без лишнего сообщения-хвоста.
    Игнорируется, если prefer_edit=True (тот редактирует event-сообщение,
    см. его собственный смысл выше) — оба параметра вместе не используются
    ни одним существующим call site'ом."""
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
    elif reply.debt_refresh_confirm:
        buttons = debt_refresh_confirm_keyboard()
    elif reply.debt_refresh_available:
        buttons = debt_statistics_keyboard(page=reply.debt_list_page, total_pages=reply.debt_list_total_pages)
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
    elif reply.trusted_check_now_inactive_id is not None:
        buttons = trusted_check_now_inactive_keyboard(reply.trusted_check_now_inactive_id)
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
    elif reply.my_car_inactive_id is not None:
        buttons = my_car_inactive_keyboard(reply.my_car_inactive_id)
    elif reply.inline_buttons:
        # "ВРЕМЕННЫЙ manager workflow для Georgia fines bot" (см. задачу) —
        # тот же приём, что и у cta_buttons ниже, только Button.inline(...)
        # (callback_data), а не Button.url(...) — см. BotReply.inline_buttons
        # докстрок.
        buttons = [
            [Button.inline(label, data) for label, data in row] for row in reply.inline_buttons
        ]
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
    elif reply.search_prompt:
        # Manager/trusted Search (см. задачу) — экран ввода запроса,
        # единственная кнопка "↩️ Назад".
        buttons = search_entry_keyboard()
    elif reply.search_result_shown:
        # Экран результата Search (найден он или нет) — пагинация (если
        # search_total_pages > 1) + [🔎 Новый поиск][↩️ В меню].
        buttons = search_result_keyboard(
            query_type=reply.search_query_type, query=reply.search_query,
            page=reply.search_page, total_pages=reply.search_total_pages,
        )
    elif reply.protocol_check_method_prompt:
        buttons = protocol_check_method_keyboard()
    elif reply.protocol_check_back_target == "method":
        buttons = protocol_check_back_to_method_keyboard()
    elif reply.protocol_check_back_target == "vehicle_step1":
        buttons = protocol_check_back_to_vehicle_step1_keyboard()
    elif reply.protocol_check_back_target == "protocol_step1":
        buttons = protocol_check_back_to_protocol_step1_keyboard()

    # См. _split_telegram_text() докстрок про MessageTooLongError —
    # короткий reply.text (подавляющее большинство случаев) даёт РОВНО
    # один chunk, и поведение ниже бит в бит совпадает с прежним
    # (один edit/respond, buttons на нём одном). buttons вешаются ТОЛЬКО
    # на ПОСЛЕДНИЙ chunk (см. задачу: "так что normal navigation/menu
    # remains below the result") — предыдущие chunks идут без кнопок.
    chunks = _split_telegram_text(reply.text)
    last_index = len(chunks) - 1

    send_from = 0
    if prefer_edit:
        try:
            await event.edit(chunks[0], buttons=buttons if last_index == 0 else None)
            send_from = 1
        except Exception:
            # Сообщение с кнопками могло стать недоступным для редактирования
            # (например, Telegram ограничивает срок редактирования) —
            # результат всё равно должен дойти до пользователя. send_from
            # остаётся 0 — весь текст (включая chunks[0]) уходит через
            # respond ниже, ничего не потеряно и не задвоено (edit выше
            # либо не отправил ничего вовсе, либо сам упал с исключением
            # до применения).
            logger.warning(
                "Не удалось отредактировать сообщение @ProtocolGEbot, отправляю новое",
                exc_info=True,
            )
    elif edit_message is not None:
        try:
            await edit_message.edit(chunks[0], buttons=buttons if last_index == 0 else None)
            send_from = 1
        except Exception:
            # Тот же fallback, что и у prefer_edit выше (например, "⏳"
            # удалено пользователем вручную между отправкой и готовностью
            # результата) — результат всё равно должен дойти, новым
            # сообщением, а не потеряться.
            logger.warning(
                "Не удалось отредактировать '⏳' @ProtocolGEbot, отправляю новое",
                exc_info=True,
            )

    for index in range(send_from, len(chunks)):
        await event.respond(chunks[index], buttons=buttons if index == last_index else None)
