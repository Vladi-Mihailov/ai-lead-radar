"""Тесты reader/checkout/telegram_integration.py::CheckoutReplyHandler —
Telethon полностью фейковый (тот же приём, что и
tests/test_insurance_ocr_command.py/tests/test_album_collector.py)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.checkout.models import CheckoutStatus  # noqa: E402
from reader.checkout.policy_document import PolicyDocument  # noqa: E402
from reader.checkout.service import CheckoutOutcome  # noqa: E402
from reader.checkout.telegram_integration import CheckoutReplyHandler  # noqa: E402

_USER_ID = 111
_OTHER_USER_ID = 999
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
    def __init__(self, checkout_id: str, *, status: CheckoutStatus = CheckoutStatus.COMPLETED):
        self.id = checkout_id
        # _log_outcome (см. reader/checkout/telegram_integration.py) читает
        # state.status для normal/WARNING логирования — реальный
        # CheckoutState всегда его содержит.
        self.status = status


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


def _handler(service=None) -> tuple[CheckoutReplyHandler, _FakeCheckoutService]:
    service = service or _FakeCheckoutService()
    return CheckoutReplyHandler(checkout_service=service), service


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


async def test_pay_reply_from_any_user_in_the_chat_is_processed():
    """Допуск к checkout больше не ограничен ocr.allowed_user_ids (см.
    задачу: production показал, что sender_id вне списка молча игнорировался
    даже в правильном чате) — единственная граница это сам чат."""
    handler, service = _handler()
    event = _FakeEvent(
        raw_text="pay", sender_id=_OTHER_USER_ID, reply_to_msg_id=42,
        reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT),
    )

    await handler.on_new_message(event)

    assert len(service.pay_calls) == 1
    assert service.pay_calls[0]["operator_user_id"] == _OTHER_USER_ID
    assert event.replies == ["pay-ok"]


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


async def test_correction_reply_from_any_user_in_the_chat_is_processed():
    handler, service = _handler()
    event = _FakeEvent(
        raw_text="Марка: Honda", sender_id=_OTHER_USER_ID, reply_to_msg_id=42,
        reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT),
    )

    await handler.on_new_message(event)

    assert len(service.correction_calls) == 1
    assert service.correction_calls[0]["operator_user_id"] == _OTHER_USER_ID
    assert event.replies == ["correction-ok"]


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


# ---- reply из другого чата не обрабатывается (см. start(): chats=[entity]
# — граница по чату остаётся единственной; здесь просто фиксируем, что
# chat_id из события идёт как есть в сервис, без какой-либо доп. проверки
# по sender_id/allowed_user_ids) ----


async def test_reply_uses_event_chat_id_as_is_no_sender_authorization_left():
    """Регресс: raw_text/chat_id/sender_id идут в сервис напрямую — нет
    больше self._allowed_user_ids/проверки в CheckoutReplyHandler (см.
    задачу). Единственная граница допуска — то, что start() регистрирует
    handler с chats=[настроенный чат] (см. test_start_* ниже и
    tests/test_telegram_event_filters.py)."""
    handler, _service = _handler()
    assert not hasattr(handler, "_allowed_user_ids")


# ---- код подтверждения ----


async def test_code_reply_is_routed_to_handle_code_reply_first():
    service = _FakeCheckoutService(code_outcome=CheckoutOutcome(reply_text="код принят", state=None))
    handler, _svc = _handler(service)
    event = _FakeEvent(raw_text="123456", reply_to_msg_id=555, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT))

    await handler.on_new_message(event)

    assert len(service.code_calls) == 1
    assert service.code_calls[0] == {
        "chat_id": _CHAT_ID, "prompt_message_id": 555, "code": "123456", "operator_user_id": _USER_ID,
    }
    assert event.replies == ["код принят"]
    # раз это оказался код — pay/correction вообще не вызываются
    assert service.pay_calls == []
    assert service.correction_calls == []


async def test_code_reply_passes_actual_sender_as_operator_user_id():
    """CheckoutReplyHandler сам не решает, чей это код — просто передаёт
    фактического отправителя дальше; проверка "тот ли это оператор,
    который запустил pay" — забота CheckoutService.handle_code_reply (см.
    reader/checkout/service.py и tests/test_checkout_service.py)."""
    service = _FakeCheckoutService(code_outcome=CheckoutOutcome(reply_text="код принят", state=None))
    handler, _svc = _handler(service)
    event = _FakeEvent(
        raw_text="123456", sender_id=_OTHER_USER_ID, reply_to_msg_id=555,
        reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT),
    )

    await handler.on_new_message(event)

    assert service.code_calls[0]["operator_user_id"] == _OTHER_USER_ID


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
    state = _FakeState("checkout-otp-1", status=CheckoutStatus.WAITING_FOR_CONFIRMATION_CODE)
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
    state = _FakeState("checkout-otp-2", status=CheckoutStatus.WAITING_FOR_CONFIRMATION_CODE)
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
    state = _FakeState("checkout-done", status=CheckoutStatus.COMPLETED)
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
    state = _FakeState("checkout-pdf-1", status=CheckoutStatus.COMPLETED)
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
    state = _FakeState("checkout-no-pdf", status=CheckoutStatus.COMPLETED)
    service = _FakeCheckoutService(
        pay_outcome=CheckoutOutcome(reply_text="✅ Оплата успешно завершена.", state=state),
    )
    handler, _svc = _handler(service)
    event = _FakeEvent(raw_text="pay", reply_to_msg_id=42, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT))

    await handler.on_new_message(event)

    assert event.sent_files == [None]


# ---- нормальное логирование (см. задачу: "видно без PII/secrets") ----


async def test_logs_reply_received_metadata_for_every_reply(caplog):
    handler, _service = _handler()
    event = _FakeEvent(
        raw_text="pay", sender_id=_OTHER_USER_ID, reply_to_msg_id=42,
        reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT),
    )

    with caplog.at_level("INFO", logger="reader.checkout.telegram_integration"):
        await handler.on_new_message(event)

    assert f"chat_id={_CHAT_ID}" in caplog.text
    assert f"sender_id={_OTHER_USER_ID}" in caplog.text
    assert "reply_to_msg_id=42" in caplog.text
    assert "command=pay" in caplog.text


async def test_logs_pay_outcome_with_status_and_reason(caplog):
    """Missing-vehicle-data — "проблемный" статус, ожидаем WARNING (см.
    _REJECTED_STATUSES в reader/checkout/telegram_integration.py) с
    checkout_id/status/reason (=reply_text, уже человекочитаем)."""
    state = _FakeState("checkout-log-1", status=CheckoutStatus.MISSING_VEHICLE_DATA)
    service = _FakeCheckoutService(
        pay_outcome=CheckoutOutcome(reply_text="Не хватает данных для оформления: Марка.", state=state),
    )
    handler, _svc = _handler(service)
    event = _FakeEvent(raw_text="pay", reply_to_msg_id=42, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT))

    with caplog.at_level("WARNING", logger="reader.checkout.telegram_integration"):
        await handler.on_new_message(event)

    assert "Checkout pay outcome" in caplog.text
    assert "checkout-log-1" in caplog.text
    assert "missing_vehicle_data" in caplog.text
    assert "Марка" in caplog.text


async def test_logs_otp_accepted_outcome_at_info_level(caplog):
    state = _FakeState("checkout-otp-log-1", status=CheckoutStatus.COMPLETED)
    service = _FakeCheckoutService(
        code_outcome=CheckoutOutcome(reply_text="✅ Оплата успешно завершена.", state=state),
    )
    handler, _svc = _handler(service)
    event = _FakeEvent(raw_text="123456", reply_to_msg_id=555, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT))

    with caplog.at_level("INFO", logger="reader.checkout.telegram_integration"):
        await handler.on_new_message(event)

    otp_records = [r for r in caplog.records if "Checkout otp outcome" in r.getMessage()]
    assert len(otp_records) == 1
    assert "checkout-otp-log-1" in otp_records[0].getMessage()
    assert "completed" in otp_records[0].getMessage()
    # OTP сам никогда не появляется в логе.
    assert "123456" not in caplog.text


async def test_logs_completed_pay_outcome_at_info_level(caplog):
    state = _FakeState("checkout-log-2", status=CheckoutStatus.COMPLETED)
    service = _FakeCheckoutService(
        pay_outcome=CheckoutOutcome(reply_text="✅ Оплата успешно завершена.", state=state),
    )
    handler, _svc = _handler(service)
    event = _FakeEvent(raw_text="pay", reply_to_msg_id=42, reply_message=_FakeMessage(raw_text=_OCR_MESSAGE_TEXT))

    with caplog.at_level("INFO", logger="reader.checkout.telegram_integration"):
        await handler.on_new_message(event)

    info_records = [r for r in caplog.records if r.levelname == "INFO" and "Checkout pay outcome" in r.getMessage()]
    assert len(info_records) == 1
    assert "completed" in info_records[0].getMessage()
