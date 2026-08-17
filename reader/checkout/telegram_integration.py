"""Telegram-интеграция checkout — ЕЩЁ ОДИН независимый NewMessage-handler на
том же служебном чате, что и CommandDispatcher/AlbumCollector (см.
reader/commands/insurance_ocr.py, reader/commands/album_collector.py,
reader/main.py) — тот же архитектурный приём: несколько handler'ов на одном
Telethon-клиенте с одинаковым chats=-фильтром, без изменения существующих.

Единственное отличие от CommandDispatcher: здесь не разбирается первое слово
сообщения как имя команды — триггер это REPLY на "Распознано: ..."
сообщение (см. reader/checkout/parser.py). "pay" без reply, или reply не на
наше сообщение — игнорируются молча, как и произвольные сообщения в чате у
CommandDispatcher.

Допуск: любой участник настроенного чата может прислать документы на OCR,
исправить draft и отправить "pay"/код подтверждения — allowed_user_ids
здесь БОЛЬШЕ НЕ используется как authorization (см. задачу: production
показал, что sender_id, отличный от ocr.allowed_user_ids, приходил в
правильный чат, но checkout его молча игнорировал). Единственная граница
доступа — сам чат (см. start(): chats=[entity]). Ownership конкретного
checkout/payment flow при этом определяется НЕ списком allowed-пользователей,
а тем, кто фактически отправил "pay" (см. CheckoutState.operator_user_id и
CheckoutService.handle_code_reply — код подтверждения принимается только от
того же sender_id, что запустил именно этот checkout)."""

from __future__ import annotations

import io
import logging

from telethon import TelegramClient, events

from reader.checkout.models import CheckoutStatus
from reader.checkout.parser import is_pay_trigger
from reader.checkout.service import CheckoutOutcome, CheckoutService

logger = logging.getLogger(__name__)

_OCR_RESULT_PREFIX = "Распознано:"

# CheckoutStatus, при которых reply/OTP закончился НЕ успехом/продолжением —
# логируются на уровне WARNING, а не INFO (см. задачу: "pay accepted/rejected
# + reason", "OTP accepted/rejected + reason" должны быть видны в проде).
_REJECTED_STATUSES = frozenset(
    {
        CheckoutStatus.MISSING_VEHICLE_DATA,
        CheckoutStatus.MAPPING_FAILED,
        CheckoutStatus.MISSING_PERSONAL_INFO,
        CheckoutStatus.FAILED,
    }
)


class CheckoutReplyHandler:
    def __init__(self, *, checkout_service: CheckoutService):
        self._service = checkout_service

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
        text = (event.raw_text or "").strip()
        reply_to_id = getattr(event, "reply_to_msg_id", None)

        # Нормальное (не diagnostic-only) логирование получения reply — см.
        # задачу: "checkout reply received: chat_id, sender_id,
        # reply_to_msg_id, command type". Без текста/кода/содержимого —
        # только грубая классификация по тому, "pay" ли это, ещё до того,
        # как понятно, наш ли это вообще reply.
        logger.info(
            "Checkout reply received\nchat_id=%s\nsender_id=%s\nreply_to_msg_id=%s\ncommand=%s",
            event.chat_id,
            event.sender_id,
            reply_to_id,
            "pay" if is_pay_trigger(text) else "correction_or_code",
        )

        if reply_to_id is None:
            return

        replied = await event.get_reply_message()
        if replied is None:
            return

        # code — не логируем text целиком дальше по цепочке (см. задачу:
        # "OTP никогда не логировать") — если reply адресован ожидающему
        # кода checkout'у, code передаётся в сервис и больше нигде не
        # появляется (ни в логах, ни в переменных этого метода после return).
        # operator_user_id=event.sender_id — код подтверждения принимается
        # ТОЛЬКО от того же отправителя, что запустил именно этот checkout
        # (см. reader/checkout/service.py::handle_code_reply), а не от
        # любого участника чата.
        code_outcome = await self._service.handle_code_reply(
            chat_id=event.chat_id, prompt_message_id=reply_to_id, code=text, operator_user_id=event.sender_id,
        )
        if code_outcome is not None:
            self._log_outcome(event=event, kind="otp", outcome=code_outcome)
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
            self._log_outcome(event=event, kind="pay", outcome=outcome)
        else:
            outcome = await self._service.handle_correction(
                chat_id=event.chat_id,
                ocr_message_id=reply_to_id,
                ocr_message_text=replied_text,
                correction_text=text,
                operator_user_id=event.sender_id,
            )
            self._log_outcome(event=event, kind="correction", outcome=outcome)

        await self._send_reply(event, outcome)

    @staticmethod
    def _log_outcome(*, event: events.NewMessage.Event, kind: str, outcome: CheckoutOutcome) -> None:
        """"pay accepted/rejected + reason" / "OTP accepted/rejected +
        reason" (см. задачу) — reply_text уже человекочитаемо объясняет
        причину (то же самое сообщение, которое уходит оператору), поэтому
        отдельно причину не переизобретаем. Без OTP/номера карты/содержимого
        документов — их в reply_text никогда не бывает (см. reader/checkout/
        service.py)."""
        state = outcome.state
        status = state.status.value if state is not None else None
        log = logger.warning if (state is not None and state.status in _REJECTED_STATUSES) else logger.info
        log(
            "Checkout %s outcome\nchat_id=%s\nsender_id=%s\ncheckout_id=%s\nstatus=%s\nreason=%s",
            kind,
            event.chat_id,
            event.sender_id,
            state.id if state is not None else None,
            status,
            outcome.reply_text,
        )

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
