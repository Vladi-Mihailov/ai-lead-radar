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
    decode_period_callback,
    main_menu_keyboard,
    period_choice_keyboard,
)

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


def register(client: TelegramClient, controller: ConversationController) -> None:
    """Регистрирует NewMessage/CallbackQuery handlers на уже
    сконфигурированном bot-mode TelegramClient (см. reader/public_bot/
    main.py). incoming=True + e.is_private — тот же принцип, что и у
    CommandDispatcher (реагировать только на реальные входящие сообщения
    пользователя в приватном чате с ботом, не на служебные апдейты/группы —
    @GEShtrafbot не предназначен для групповых чатов)."""

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def _on_message(event: events.NewMessage.Event) -> None:
        text = event.raw_text
        if not text or not text.strip():
            return

        username, _first_name, _last_name = await _sender_names(event)

        reply = controller.handle_text(
            text,
            chat_id=event.chat_id,
            telegram_user_id=event.sender_id,
            username=username,
        )

        await _send_reply(event, reply)

    @client.on(events.CallbackQuery(func=lambda e: e.is_private))
    async def _on_callback(event: events.CallbackQuery.Event) -> None:
        days = decode_period_callback(event.data)
        if days is None:
            await event.answer("Неизвестная или устаревшая кнопка", alert=True)
            return

        _username, first_name, last_name = await _sender_names(event)

        reply = await controller.handle_period_choice(
            days,
            chat_id=event.chat_id,
            telegram_user_id=event.sender_id,
            first_name=first_name,
            last_name=last_name,
        )

        if reply is None:
            # Диалог этого chat_id не в шаге "выбор периода", либо
            # принадлежит другому telegram_user_id (см. security-инвариант
            # в reader/public_bot/keyboards.py) — ничего не создаём/не
            # меняем, только уведомляем нажавшего.
            await event.answer("Эта кнопка недоступна — начните заново через «➕ Добавить авто»", alert=True)
            return

        await event.answer()
        await _send_reply(event, reply, prefer_edit=True)

    logger.info("✔ @GEShtrafbot handlers зарегистрированы")


async def _send_reply(event, reply, *, prefer_edit: bool = False) -> None:
    buttons = None
    if reply.show_main_menu:
        buttons = main_menu_keyboard()
    elif reply.show_period_buttons:
        buttons = period_choice_keyboard()

    if prefer_edit:
        try:
            await event.edit(reply.text, buttons=buttons)
            return
        except Exception:
            # Сообщение с кнопками могло стать недоступным для редактирования
            # (например, Telegram ограничивает срок редактирования) —
            # результат Add Car flow всё равно должен дойти до пользователя.
            logger.warning(
                "Не удалось отредактировать сообщение @GEShtrafbot, отправляю новое",
                exc_info=True,
            )

    await event.respond(reply.text, buttons=buttons)
