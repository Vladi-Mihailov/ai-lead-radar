"""Telegram-сторона привязки MAX-аккаунта (см. reader/max_bot/linking.py):
«/max_link КОД» в личном чате с Telegram-ботом той же страны показывает, к
какому MAX-аккаунту будет привязка, и только кнопка «✅ Привязать»
привязывает. Подтверждение защищает от подсунутой команды: человек видит
имя чужого MAX-аккаунта до привязки.

⚠️ Модуль НИГДЕ не подключён: Telegram-боты в этой сборке не меняются.
Включение — отдельное решение (GO): одна строка в reader/public_bot/main.py
и reader/turkey_bot/main.py после register(...):

    register_max_link_command(client, MaxTelegramLinkService(MaxBotStorage(db_path, bot="ge")))

Обработчики отвечают только на эту команду и свою кнопку и ничего не меняют
в Telegram-подписках.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from telethon import Button, TelegramClient, events

from reader.max_bot.linking import LINK_RESULT_TEXTS, MaxTelegramLinkService, normalize_code

logger = logging.getLogger(__name__)

MAX_LINK_COMMAND_RE = re.compile(r"^/max_link(?:@\w+)?(?:\s+(?P<code>[A-Za-z0-9-]{4,20}))?\s*$")
MAX_LINK_USAGE_TEXT = "Отправьте код из MAX-бота: /max_link ABCD-EFGH"
CONFIRM_PREFIX = b"maxlink:"
CONFIRM_LABEL = "✅ Привязать"
CANCEL_DATA = b"maxlink-cancel"
CANCEL_LABEL = "Отмена"
CANCELLED_TEXT = "Привязка отменена."


@dataclass(frozen=True)
class LinkPrompt:
    text: str
    # (подпись, callback data) — None: кнопок нет.
    buttons: list[tuple[str, bytes]] | None = None


def handle_max_link_text(
    text: str, *, telegram_user_id: int, link_service: MaxTelegramLinkService,
) -> LinkPrompt | None:
    """Ответ на «/max_link КОД» (None — это не эта команда). Код проверяется,
    но не гасится: привязка — только по кнопке (handle_max_link_confirm)."""
    match = MAX_LINK_COMMAND_RE.match(text.strip())
    if match is None:
        return None
    code = match.group("code")
    if not code:
        return LinkPrompt(MAX_LINK_USAGE_TEXT)
    preview = link_service.preview(code, telegram_user_id=telegram_user_id)
    if preview.status != "valid":
        return LinkPrompt(LINK_RESULT_TEXTS[preview.status])
    who = f"«{preview.max_display_name}»" if preview.max_display_name else "без имени"
    return LinkPrompt(
        f"Привязать аккаунт MAX {who} к этому Telegram-аккаунту?\n\n"
        "Владелец MAX-аккаунта сможет перенести в MAX ваши автомобили и получать по ним уведомления. "
        "Если вы не запрашивали код сами — нажмите «Отмена».",
        buttons=[(CONFIRM_LABEL, CONFIRM_PREFIX + normalize_code(code).encode("ascii")), (CANCEL_LABEL, CANCEL_DATA)],
    )


def handle_max_link_confirm(
    data: bytes, *, telegram_user_id: int, link_service: MaxTelegramLinkService,
) -> str | None:
    """Текст после нажатия кнопки (None — не наша кнопка)."""
    if data == CANCEL_DATA:
        return CANCELLED_TEXT
    if not data.startswith(CONFIRM_PREFIX):
        return None
    result = link_service.confirm(data[len(CONFIRM_PREFIX):].decode("ascii", "ignore"), telegram_user_id=telegram_user_id)
    if result.status == "linked":
        logger.info("MAX link: Telegram-аккаунт привязан к MAX-аккаунту")
    return LINK_RESULT_TEXTS[result.status]


def register_max_link_command(client: TelegramClient, link_service: MaxTelegramLinkService) -> None:
    @client.on(events.NewMessage(incoming=True, pattern=r"^/max_link", func=lambda e: e.is_private))
    async def _on_max_link(event: events.NewMessage.Event) -> None:
        prompt = handle_max_link_text(event.raw_text or "", telegram_user_id=event.sender_id, link_service=link_service)
        if prompt is None:
            return
        buttons = [[Button.inline(label, data) for label, data in prompt.buttons]] if prompt.buttons else None
        await event.respond(prompt.text, buttons=buttons)
        raise events.StopPropagation

    @client.on(events.CallbackQuery(func=lambda e: e.is_private and bool(e.data) and e.data.startswith(b"maxlink")))
    async def _on_max_link_confirm(event: events.CallbackQuery.Event) -> None:
        text = handle_max_link_confirm(event.data, telegram_user_id=event.sender_id, link_service=link_service)
        if text is None:
            return
        await event.answer()
        await event.edit(text, buttons=None)
        raise events.StopPropagation
