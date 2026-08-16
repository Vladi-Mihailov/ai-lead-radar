"""Тесты reader/checkout/telegram_integration.py::CheckoutReplyHandler —
Telethon полностью фейковый (тот же приём, что и
tests/test_insurance_ocr_command.py/tests/test_album_collector.py)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.checkout.policy_document import PolicyDocument  # noqa: E402
from reader.checkout.service import CheckoutOutcome  # noqa: E402
from reader.checkout.telegram_integration import CheckoutReplyHandler  # noqa: E402

_USER_ID = 111
_CHAT_ID = -100999


class _FakeMessage:
    def __init__(self, *, raw_text: str):
        self.raw_text = raw_text


class _SentMessage:
    """То, что реально возвращает event.reply() в Telethon — Message с id
    отправленного сообщения (см. CheckoutReplyHandler._send_reply)."""

    _next_id = 1000

    def __init__(self):
        _SentMessage._next_id += 1
        self.id = _SentMessage._next_id


class _FakeEvent:
    def __init__(
        self, *, raw_text: str, sender_id=_USER_ID, chat_id=_CHAT_ID,
        reply_to_msg_id=None, reply_message=None,
    ):
        self.raw_text = raw_text
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.reply_to_msg_id = reply_to_msg_id
        self._reply_message = reply_message
        self.replies: list[str] = []
        self.sent_messages: list[_SentMessage] = []
        self.sent_files: list = []

    async def get_reply_message(self):
        return self._reply_message

    async def reply(self, text, file=None):
        self.replies.append(text)
        self.sent_files.append(file)
        sent = _SentMessage()
        self.sent_messages.append(sent)
        return sent


class _FakeState:
    def __init__(self, checkout_id: str):
        self.id = checkout_id


class _FakeCheckoutService:
    def __init__(
        self, *, pay_outcome=None, correction_outcome=None, code_outcome=None,
    ):
        self._pay_outcome = pay_outcome or CheckoutOutcome(reply_text="pay-ok", state=None)
        self._correction_outcome = correction_outcome or CheckoutOutcome(reply_text="correction-ok", state=None)
        self._code_outcome = code_outcome  # None по умолчанию — "это не про код"
        self.pay_calls: list[dict] = []
        self.correction_calls: list[dict] = []
        self.code_calls: list[dict] = []
        self.mark_code_prompt_sent_calls: list[tuple] = []

    async def handle_pay(self, **kwargs):
        self.pay_calls.append(kwargs)
        return self._pay_outcome

    async def handle_correction(self, **kwargs):
        self.correction_calls.append(kwargs)
        return self._correction_outcome

    async def handle_code_reply(self, **kwargs):
        self.code_calls.append(kwargs)
        return self._code_outcome

    async def mark_code_prompt_sent(self, checkout_id, message_id):
        self.mark_code_prompt_sent_calls.append((checkout_id, message_id))


def _handler(service=None, *, allowed_user_ids=(_USER_ID,)) -> tuple[CheckoutReplyHandler, _FakeCheckoutService]:
    service = service or _FakeCheckoutService()
    return CheckoutReplyHandler(checkout_service=service, allowed_user_ids=list(allowed_user_ids)), service


_OCR_MESSAGE_TEXT = (
    "Распознано:\n\nСобственник: Ivanov Ivan\nВодитель: Petrov Petr\n"
    "Страхователь: Petrov Petr\nКатегория: passenger_car\nМарка: Toyota\n"
    "Модель: Camry\nVIN: WVWZZZ1KZAW123456\nНомер шасси: не распознано\n"
    "Госномер: AA001AA\n\nПроверь данные."
)


# ---- "pay" reply ----


async def test_pay_reply_triggers_handle_pay_with_replied_message_text():
    handler, service = _handler()
    event = _FakeEvent(
        raw_text="pay", reply_to_msg_id=42,
        reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT),
    )

    await handler.on_new_message(event)

    assert len(service.pay_calls) == 1
    call = service.pay_calls[0]
    assert call["chat_id"] == _CHAT_ID
    assert call["ocr_message_id"] == 42
    assert call["ocr_message_text"] == _OCR_MESSAGE_TEXT
    assert call["operator_user_id"] == _USER_ID
    assert event.replies == ["pay-ok"]
    assert service.correction_calls == []


async def test_pay_reply_is_case_insensitive():
    handler, service = _handler()
    event = _FakeEvent(
        raw_text="  PAY  ", reply_to_msg_id=1, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT),
    )

    await handler.on_new_message(event)

    assert len(service.pay_calls) == 1


# ---- edited-data reply ----


async def test_correction_reply_triggers_handle_correction():
    handler, service = _handler()
    event = _FakeEvent(
        raw_text="Марка: Honda\nМодель: Accord", reply_to_msg_id=42,
        reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT),
    )

    await handler.on_new_message(event)

    assert len(service.correction_calls) == 1
    call = service.correction_calls[0]
    assert call["correction_text"] == "Марка: Honda\nМодель: Accord"
    assert call["ocr_message_text"] == _OCR_MESSAGE_TEXT
    assert event.replies == ["correction-ok"]
    assert service.pay_calls == []


# ---- игнорируется, если это не reply на наше сообщение ----


async def test_reply_not_targeting_ocr_message_is_ignored():
    handler, service = _handler()
    event = _FakeEvent(
        raw_text="pay", reply_to_msg_id=1,
        reply_message=_FakeMessage(raw_text="просто другое сообщение в чате"),
    )

    await handler.on_new_message(event)

    assert service.pay_calls == []
    assert service.correction_calls == []
    assert event.replies == []


async def test_non_reply_message_is_ignored():
    handler, service = _handler()
    event = _FakeEvent(raw_text="pay", reply_to_msg_id=None)

    await handler.on_new_message(event)

    assert service.pay_calls == []
    assert event.replies == []


async def test_reply_to_deleted_message_is_ignored():
    """event.get_reply_message() может вернуть None (сообщение удалено) —
    не должно падать."""
    handler, service = _handler()
    event = _FakeEvent(raw_text="pay", reply_to_msg_id=1, reply_message=None)

    await handler.on_new_message(event)

    assert service.pay_calls == []
    assert event.replies == []


# ---- доступ ----


async def test_unauthorized_sender_is_ignored():
    handler, service = _handler(allowed_user_ids=(_USER_ID,))
    event = _FakeEvent(
        raw_text="pay", sender_id=999, reply_to_msg_id=42,
        reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT),
    )

    await handler.on_new_message(event)

    assert service.pay_calls == []
    assert event.replies == []


# ---- код подтверждения ----


async def test_code_reply_is_routed_to_handle_code_reply_first():
    service = _FakeCheckoutService(code_outcome=CheckoutOutcome(reply_text="код принят", state=None))
    handler, _svc = _handler(service)
    event = _FakeEvent(raw_text="123456", reply_to_msg_id=555, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT))

    await handler.on_new_message(event)

    assert len(service.code_calls) == 1
    assert service.code_calls[0] == {"chat_id": _CHAT_ID, "prompt_message_id": 555, "code": "123456"}
    assert event.replies == ["код принят"]
    # раз это оказался код — pay/correction вообще не вызываются
    assert service.pay_calls == []
    assert service.correction_calls == []


async def test_non_code_reply_falls_through_to_ocr_message_check_when_code_outcome_is_none():
    """handle_code_reply вернул None ("это не про код", см.
    CheckoutService.handle_code_reply) — обработка продолжается как обычно."""
    handler, service = _handler()  # code_outcome=None по умолчанию
    event = _FakeEvent(raw_text="pay", reply_to_msg_id=42, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT))

    await handler.on_new_message(event)

    assert len(service.code_calls) == 1
    assert len(service.pay_calls) == 1
    assert event.replies == ["pay-ok"]


async def test_no_reply_sent_when_outcome_reply_text_is_none():
    service = _FakeCheckoutService(pay_outcome=CheckoutOutcome(reply_text=None, state=None))
    handler, _svc = _handler(service)
    event = _FakeEvent(raw_text="pay", reply_to_msg_id=42, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT))

    await handler.on_new_message(event)

    assert event.replies == []


# ---- запрос кода подтверждения — регистрация id отправленного сообщения ----


async def test_pay_outcome_awaiting_code_registers_sent_message_as_code_prompt():
    state = _FakeState("checkout-otp-1")
    service = _FakeCheckoutService(
        pay_outcome=CheckoutOutcome(
            reply_text="Введите код подтверждения — reply на это сообщение.",
            state=state, needs_code_prompt_registration=True,
        ),
    )
    handler, _svc = _handler(service)
    event = _FakeEvent(raw_text="pay", reply_to_msg_id=42, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT))

    await handler.on_new_message(event)

    assert len(event.sent_messages) == 1
    sent_id = event.sent_messages[0].id
    assert service.mark_code_prompt_sent_calls == [("checkout-otp-1", sent_id)]


async def test_code_retry_outcome_also_registers_new_prompt_message():
    state = _FakeState("checkout-otp-2")
    service = _FakeCheckoutService(
        code_outcome=CheckoutOutcome(
            reply_text="Неверный код, попробуйте ещё раз.", state=state, needs_code_prompt_registration=True,
        ),
    )
    handler, _svc = _handler(service)
    event = _FakeEvent(raw_text="000000", reply_to_msg_id=555, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT))

    await handler.on_new_message(event)

    assert len(event.sent_messages) == 1
    assert service.mark_code_prompt_sent_calls == [("checkout-otp-2", event.sent_messages[0].id)]


async def test_completed_outcome_does_not_register_code_prompt():
    state = _FakeState("checkout-done")
    service = _FakeCheckoutService(
        pay_outcome=CheckoutOutcome(reply_text="✅ Оплата успешно завершена.", state=state),
    )
    handler, _svc = _handler(service)
    event = _FakeEvent(raw_text="pay", reply_to_msg_id=42, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT))

    await handler.on_new_message(event)

    assert service.mark_code_prompt_sent_calls == []


async def test_confirmation_code_text_is_never_logged_or_stored_by_handler(caplog):
    """CheckoutReplyHandler передаёт code напрямую в handle_code_reply и
    нигде не сохраняет/не логирует его сам (само значение кода нигде не
    появляется в коде обработчика, кроме передачи как аргумента)."""
    import logging

    service = _FakeCheckoutService(code_outcome=CheckoutOutcome(reply_text="код принят", state=None))
    handler, _svc = _handler(service)
    secret_code = "918273"
    event = _FakeEvent(
        raw_text=secret_code, reply_to_msg_id=555, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT),
    )

    with caplog.at_level(logging.DEBUG):
        await handler.on_new_message(event)

    for record in caplog.records:
        assert secret_code not in record.getMessage()


# ---- PDF полиса (см. reader/checkout/policy_document.py) ----


async def test_policy_document_is_attached_as_file_to_the_reply():
    state = _FakeState("checkout-pdf-1")
    document = PolicyDocument(filename="policy.pdf", content=b"%PDF-fake-content")
    service = _FakeCheckoutService(
        pay_outcome=CheckoutOutcome(
            reply_text="✅ Оплата успешно завершена.", state=state, policy_document=document,
        ),
    )
    handler, _svc = _handler(service)
    event = _FakeEvent(raw_text="pay", reply_to_msg_id=42, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT))

    await handler.on_new_message(event)

    assert event.replies == ["✅ Оплата успешно завершена."]
    assert len(event.sent_files) == 1
    sent_file = event.sent_files[0]
    assert sent_file is not None
    assert sent_file.name == "policy.pdf"
    assert sent_file.read() == b"%PDF-fake-content"


async def test_no_file_sent_when_policy_document_is_absent():
    state = _FakeState("checkout-no-pdf")
    service = _FakeCheckoutService(
        pay_outcome=CheckoutOutcome(reply_text="✅ Оплата успешно завершена.", state=state),
    )
    handler, _svc = _handler(service)
    event = _FakeEvent(raw_text="pay", reply_to_msg_id=42, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT))

    await handler.on_new_message(event)

    assert event.sent_files == [None]
