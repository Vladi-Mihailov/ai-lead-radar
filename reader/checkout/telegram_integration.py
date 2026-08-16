"""Telegram-интеграция checkout — ЕЩЁ ОДИН независимый NewMessage-handler на
том же служебном чате, что и CommandDispatcher/AlbumCollector (см.
reader/commands/insurance_ocr.py, reader/commands/album_collector.py,
reader/main.py) — тот же архитектурный приём: несколько handler'ов на одном
Telethon-клиенте с одинаковым chats=-фильтром, без изменения существующих.

Единственное отличие от CommandDispatcher: здесь не разбирается первое слово
сообщения как имя команды — триггер это REPLY на "Распознано: ..."
сообщение (см. reader/checkout/parser.py). "pay" без reply, или reply не на
наше сообщение — игнорируются молча, как и произвольные сообщения в чате у
CommandDispatcher."""

from __future__ import annotations

import io
import logging

from telethon import TelegramClient, events

from reader.checkout.parser import is_pay_trigger
from reader.checkout.service import CheckoutOutcome, CheckoutService

logger = logging.getLogger(__name__)

_OCR_RESULT_PREFIX = "Распознано:"


class CheckoutReplyHandler:
    def __init__(self, *, checkout_service: CheckoutService, allowed_user_ids: list[int]):
        self._service = checkout_service
        self._allowed_user_ids = set(allowed_user_ids)

    async def start(self, client: TelegramClient, chat_id: int | str) -> None:
        try:
            entity = await client.get_entity(chat_id)
        except Exception as exc:
            logger.error("✖ Чат для checkout '%s' не найден", chat_id)
            raise RuntimeError(
                f"Не удалось найти чат для checkout '{chat_id}'. "
                f"Убедитесь, что аккаунт состоит в этом чате. Причина: {exc}"
            ) from exc

        client.add_event_handler(self.on_new_message, events.NewMessage(chats=[entity]))
        logger.info("✔ Checkout reply handler подключён к чату")

    async def on_new_message(self, event: events.NewMessage.Event) -> None:
        if event.sender_id not in self._allowed_user_ids:
            return

        reply_to_id = getattr(event, "reply_to_msg_id", None)
        if reply_to_id is None:
            return

        replied = await event.get_reply_message()
        if replied is None:
            return

        text = (event.raw_text or "").strip()
        # code — не логируем text целиком дальше по цепочке (см. задачу:
        # "OTP никогда не логировать") — если reply адресован ожидающему
        # кода checkout'у, code передаётся в сервис и больше нигде не
        # появляется (ни в логах, ни в переменных этого метода после return).
        code_outcome = await self._service.handle_code_reply(
            chat_id=event.chat_id, prompt_message_id=reply_to_id, code=text,
        )
        if code_outcome is not None:
            await self._send_reply(event, code_outcome)
            return

        replied_text = getattr(replied, "raw_text", None) or getattr(replied, "text", None) or ""
        if not replied_text.startswith(_OCR_RESULT_PREFIX):
            return  # reply не на наше "Распознано: ..." сообщение — не наш случай

        if is_pay_trigger(text):
            outcome = await self._service.handle_pay(
                chat_id=event.chat_id,
                ocr_message_id=reply_to_id,
                ocr_message_text=replied_text,
                operator_user_id=event.sender_id,
            )
        else:
            outcome = await self._service.handle_correction(
                chat_id=event.chat_id,
                ocr_message_id=reply_to_id,
                ocr_message_text=replied_text,
                correction_text=text,
                operator_user_id=event.sender_id,
            )

        await self._send_reply(event, outcome)

    async def _send_reply(self, event: events.NewMessage.Event, outcome: CheckoutOutcome) -> None:
        if not outcome.reply_text:
            return

        if outcome.policy_document is not None:
            # PDF полиса (см. reader/checkout/policy_document.py) — прикладываем
            # как файл к тому же сообщению (text становится caption).
            file_obj = io.BytesIO(outcome.policy_document.content)
            file_obj.name = outcome.policy_document.filename
            sent = await event.reply(outcome.reply_text, file=file_obj)
        else:
            sent = await event.reply(outcome.reply_text)

        if outcome.needs_code_prompt_registration and outcome.state is not None:
            # Именно id ЭТОГО отправленного сообщения (не reply_to_id
            # исходного триггера) становится якорем для следующего reply
            # оператора с кодом (см. reader/checkout/service.py::
            # mark_code_prompt_sent и CheckoutOutcome.needs_code_prompt_registration).
            await self._service.mark_code_prompt_sent(outcome.state.id, sent.id)
