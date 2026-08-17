"""Тесты reader/checkout/service.py — CheckoutService целиком с фейковыми
TplGeClient/TplReferenceDataClient/BankGatewayClient (ни один реальный
HTTP-запрос/браузер не выполняется). PersonalInfoProvider — реальный
OcrPersonalInfoProvider (см. reader/checkout/personal_info.py), только
reference_data под ним фейковый. lock_repository — реальный
CheckoutLockRepository поверх sqlite ":memory:" (см.
reader/checkout/lock_repository.py) — используется по-настоящему, чтобы
restart/recovery-тесты проверяли реальную persistence-логику, а не её
имитацию."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import logging

import pytest  # noqa: E402

from reader.checkout.lock_repository import CheckoutLockRepository  # noqa: E402
from reader.checkout.models import CheckoutStatus, FailureReason, PaymentBank  # noqa: E402
from reader.checkout.payment_gateway import BankGatewayError, BankGatewayOutcome, BankGatewayResult  # noqa: E402
from reader.checkout.personal_info import OcrPersonalInfoProvider  # noqa: E402
from reader.checkout.policy_document import PolicyDocument, PolicyDocumentError  # noqa: E402
from reader.checkout.reference_data import (  # noqa: E402
    CategoryProduct,
    CountryMatch,
    ManufacturerMatch,
    ModelMatch,
    ReferenceDataError,
)
from reader.checkout.service import CheckoutService  # noqa: E402
from reader.checkout.store import CheckoutStore  # noqa: E402
from reader.checkout.tpl_client import TplGeClientError  # noqa: E402

_CHAT_ID = -100999
_OPERATOR_ID = 111

# Ровно то, что задача зафиксировала как текущую production-конфигурацию
# (см. config/config.yaml::checkout.phone/email).
_SETTINGS_PHONE = "925000000000"
_SETTINGS_EMAIL = "tplgee@mail.ru"


_DEFAULT_PERIOD_START = "16.08.2026"


def _ocr_message(**overrides) -> str:
    fields = dict(
        policyholder="Petrov Petr",
        passport_number="AB1234567", citizenship="Georgia",
        category="passenger_car", manufacturer="Toyota", model="Camry",
        vin="WVWZZZ1KZAW123456", chassis="не распознано", plate="AA001AA",
        email=_SETTINGS_EMAIL, phone=_SETTINGS_PHONE,
        bank="bog", period="15", period_start=_DEFAULT_PERIOD_START,
        # Happy-path по умолчанию: и водитель, и владелец совпадают со
        # страхователем (~99% реальных заявок, см. reader/ocr/models.py) —
        # отдельные ФИО не нужны.
        driver_flag="+", driver="не распознано",
        owner_flag="+", owner="не распознано",
    )
    fields.update(overrides)
    return (
        "Распознано:\n\n"
        f"Страхователь: {fields['policyholder']}\n"
        f"Номер паспорта: {fields['passport_number']}\n"
        f"Гражданство: {fields['citizenship']}\n"
        f"Категория: {fields['category']}\n"
        f"Марка: {fields['manufacturer']}\n"
        f"Модель: {fields['model']}\n"
        f"VIN: {fields['vin']}\n"
        f"Номер шасси: {fields['chassis']}\n"
        f"Госномер: {fields['plate']}\n"
        f"Email: {fields['email']}\n"
        f"Телефон: {fields['phone']}\n"
        f"Банк: {fields['bank']}\n"
        f"Период: {fields['period']}\n"
        f"Начало периода: {fields['period_start']}\n\n"
        f"Водитель = страхователь: {fields['driver_flag']}\n"
        f"Водитель: {fields['driver']}\n\n"
        f"Владелец = страхователь: {fields['owner_flag']}\n"
        f"Владелец: {fields['owner']}\n\n"
        "Проверь данные."
    )


class _FakeTplClient:
    def __init__(self, *, create_error=None, redirect_url="https://mpi.gc.ge/page1?o.id=abc", redirect_error=None):
        self.create_error = create_error
        self.redirect_url = redirect_url
        self.redirect_error = redirect_error
        self.created_payloads: list = []
        self.redirect_calls: list = []

    async def create_policy(self, payload):
        self.created_payloads.append(payload)
        if self.create_error is not None:
            raise self.create_error

    async def get_payment_redirect_url(self, **kwargs):
        self.redirect_calls.append(kwargs)
        if self.redirect_error is not None:
            raise self.redirect_error
        return self.redirect_url


class _FakeReferenceData:
    def __init__(
        self, *, manufacturer_found=True, model_found=True, category_error=None, country_found=True,
    ):
        self.manufacturer_found = manufacturer_found
        self.model_found = model_found
        self.category_error = category_error
        self.country_found = country_found
        self.category_product_calls: list = []

    async def category_product(self, category, policy_period):
        self.category_product_calls.append((category, policy_period))
        if self.category_error is not None:
            raise self.category_error
        return CategoryProduct(vehicle_category_id=7, product_id=2, price=50.0)

    async def resolve_manufacturer(self, name):
        if not self.manufacturer_found:
            return None
        return ManufacturerMatch(id=147, name="TOYOTA")

    async def resolve_model(self, manufacturer_id, name):
        if not self.model_found:
            return None
        return ModelMatch(id=10574, name="TOYOTA CAMRY")

    async def resolve_country(self, name):
        if not self.country_found:
            return None
        return CountryMatch(id=1, name="Georgia")


def _real_personal_info_provider(reference_data) -> OcrPersonalInfoProvider:
    """То, что reader/main.py::build_checkout_components реально передаёт в
    CheckoutService — только reference_data здесь фейковый."""
    return OcrPersonalInfoProvider(reference_data=reference_data, phone=_SETTINGS_PHONE, email=_SETTINGS_EMAIL)


class _FakeBankGateway:
    """start()/submit_confirmation_code() возвращают элементы из
    заранее заданных очередей — по одному результату на вызов (см.
    start_results/code_results) — так тестируется retry OTP (несколько
    вызовов submit_confirmation_code подряд)."""

    def __init__(self, *, start_results=None, code_results=None, start_error=None, code_error=None):
        self._start_results = list(start_results or [])
        self._code_results = list(code_results or [])
        self.start_error = start_error
        self.code_error = code_error
        self.received_codes: list[str] = []
        self.start_calls = 0
        self.cancel_calls = 0

    async def start(self, state, redirect_url):
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        if self._start_results:
            return self._start_results.pop(0)
        return BankGatewayResult(outcome=BankGatewayOutcome.COMPLETED, message="Готово.")

    async def submit_confirmation_code(self, state, code):
        self.received_codes.append(code)
        if self.code_error is not None:
            raise self.code_error
        if self._code_results:
            return self._code_results.pop(0)
        return BankGatewayResult(outcome=BankGatewayOutcome.COMPLETED, message="Готово.")

    async def cancel(self, state):
        self.cancel_calls += 1


class _FakePolicyDocumentProvider:
    def __init__(self, *, document=None, error=None):
        self._document = document or PolicyDocument(filename="policy.pdf", content=b"%PDF-fake-content")
        self._error = error
        self.fetch_calls = 0

    async def fetch(self, state):
        self.fetch_calls += 1
        if self._error is not None:
            raise self._error
        return self._document


def _memory_lock_repository() -> CheckoutLockRepository:
    return CheckoutLockRepository(":memory:")


def _service(
    *, tpl_client=None, reference_data=None, personal_info_provider=None, bank_gateway=None,
    lock_repository=None, store=None, policy_document_provider=None, clock=None,
):
    tpl_client = tpl_client or _FakeTplClient()
    reference_data = reference_data or _FakeReferenceData()
    lock_repository = lock_repository or _memory_lock_repository()
    service = CheckoutService(
        tpl_client=tpl_client,
        reference_data=reference_data,
        lock_repository=lock_repository,
        personal_info_provider=personal_info_provider,
        bank_gateway=bank_gateway,
        policy_document_provider=policy_document_provider,
        store=store,
        clock=clock,
    )
    return service, tpl_client, reference_data, lock_repository


def _service_with_real_personal_info(**kwargs):
    reference_data = kwargs.pop("reference_data", None) or _FakeReferenceData()
    return _service(
        reference_data=reference_data,
        personal_info_provider=_real_personal_info_provider(reference_data),
        **kwargs,
    )


def _completed_gateway() -> _FakeBankGateway:
    return _FakeBankGateway(start_results=[BankGatewayResult(outcome=BankGatewayOutcome.COMPLETED, message="ok")])


# ---- pay reply: happy path — теперь доходит до реального банковского этапа ----


async def test_pay_happy_path_reaches_completed_via_bank_gateway():
    bank_gateway = _completed_gateway()
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert outcome.reply_text == "✅ Оплата успешно завершена."
    assert bank_gateway.start_calls == 1
    assert len(tpl_client.created_payloads) == 1


async def test_pay_happy_path_includes_order_reference_when_confirmed():
    bank_gateway = _FakeBankGateway(
        start_results=[BankGatewayResult(outcome=BankGatewayOutcome.COMPLETED, message="ok", order_reference="POL-1")],
    )
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert "POL-1" in outcome.reply_text
    assert outcome.state.order_reference == "POL-1"


# ---- PDF полиса после успешной оплаты (см. reader/checkout/policy_document.py) ----


async def test_completed_payment_attaches_pdf_when_provider_succeeds():
    document_provider = _FakePolicyDocumentProvider()
    service, _tpl, _ref, _lock = _service_with_real_personal_info(
        bank_gateway=_completed_gateway(), policy_document_provider=document_provider,
    )

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert outcome.policy_document is not None
    assert outcome.policy_document.filename == "policy.pdf"
    assert document_provider.fetch_calls == 1


async def test_completed_payment_without_pdf_provider_still_succeeds():
    """Default (NotImplementedPolicyDocumentProvider) — источник PDF не
    подтверждён research'ом (см. TODO в reader/checkout/policy_document.py):
    оплата остаётся COMPLETED, просто без вложения."""
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert outcome.reply_text == "✅ Оплата успешно завершена."
    assert outcome.policy_document is None


async def test_completed_payment_survives_unexpected_pdf_provider_crash():
    document_provider = _FakePolicyDocumentProvider(error=RuntimeError("pdf backend boom"))
    service, _tpl, _ref, _lock = _service_with_real_personal_info(
        bank_gateway=_completed_gateway(), policy_document_provider=document_provider,
    )

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert outcome.policy_document is None


async def test_completed_payment_with_expected_pdf_error_still_succeeds():
    document_provider = _FakePolicyDocumentProvider(error=PolicyDocumentError("not implemented yet"))
    service, _tpl, _ref, _lock = _service_with_real_personal_info(
        bank_gateway=_completed_gateway(), policy_document_provider=document_provider,
    )

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert outcome.policy_document is None


async def test_pay_happy_path_fills_passport_citizenship_phone_email_for_all_three_roles():
    bank_gateway = _completed_gateway()
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    payload = tpl_client.created_payloads[0]
    for role in ("insurer", "vehicle_owner", "vehicle_driver"):
        assert getattr(payload, f"{role}_identification_number") == "AB1234567"
        assert getattr(payload, f"{role}_citizenship_id") == 1
        assert getattr(payload, f"{role}_phone") == _SETTINGS_PHONE
        assert getattr(payload, f"{role}_email") == _SETTINGS_EMAIL


# ---- OTP: успешный код на первый раз ----


async def test_pay_reaches_waiting_for_code_and_completes_after_correct_code():
    bank_gateway = _FakeBankGateway(
        start_results=[BankGatewayResult(outcome=BankGatewayOutcome.AWAITING_CODE, message="ждём код")],
        code_results=[BankGatewayResult(outcome=BankGatewayOutcome.COMPLETED, message="Готово.")],
    )
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    started = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    assert started.state.status == CheckoutStatus.WAITING_FOR_CONFIRMATION_CODE
    assert started.reply_text == "Введите код подтверждения — reply на это сообщение."
    assert started.needs_code_prompt_registration is True

    await service.mark_code_prompt_sent(started.state.id, prompt_message_id := 555)

    outcome = await service.handle_code_reply(
        chat_id=_CHAT_ID, prompt_message_id=prompt_message_id, code="123456", operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert outcome.reply_text == "✅ Оплата успешно завершена."
    assert bank_gateway.received_codes == ["123456"]


async def test_code_message_correlation_ignores_replies_to_other_messages():
    bank_gateway = _FakeBankGateway(
        start_results=[BankGatewayResult(outcome=BankGatewayOutcome.AWAITING_CODE, message="ждём код")],
    )
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    started = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    await service.mark_code_prompt_sent(started.state.id, 555)

    # reply на СЛУЧАЙНОЕ другое сообщение с цифрами — не должен считаться кодом.
    outcome = await service.handle_code_reply(
        chat_id=_CHAT_ID, prompt_message_id=999999, code="123456", operator_user_id=_OPERATOR_ID,
    )

    assert outcome is None
    assert bank_gateway.received_codes == []


# ---- неверный OTP + retry ----


async def test_invalid_otp_with_retry_stays_in_waiting_for_code():
    bank_gateway = _FakeBankGateway(
        start_results=[BankGatewayResult(outcome=BankGatewayOutcome.AWAITING_CODE, message="ждём код")],
        code_results=[
            BankGatewayResult(outcome=BankGatewayOutcome.RETRY_CODE, message="Неверный код, попробуйте ещё раз."),
            BankGatewayResult(outcome=BankGatewayOutcome.COMPLETED, message="Готово."),
        ],
    )
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    started = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    await service.mark_code_prompt_sent(started.state.id, 555)

    first_attempt = await service.handle_code_reply(
        chat_id=_CHAT_ID, prompt_message_id=555, code="000000", operator_user_id=_OPERATOR_ID,
    )
    assert first_attempt.state.status == CheckoutStatus.WAITING_FOR_CONFIRMATION_CODE
    assert first_attempt.needs_code_prompt_registration is True
    assert "ещё раз" in first_attempt.reply_text

    # Telegram-интеграция отправила бы новый prompt и зарегистрировала его id.
    await service.mark_code_prompt_sent(started.state.id, 777)

    second_attempt = await service.handle_code_reply(
        chat_id=_CHAT_ID, prompt_message_id=777, code="123456", operator_user_id=_OPERATOR_ID,
    )
    assert second_attempt.state.status == CheckoutStatus.COMPLETED
    assert bank_gateway.received_codes == ["000000", "123456"]


async def test_stale_otp_prompt_no_longer_active_after_success_is_ignored_by_second_reply():
    """"Повторный OTP не создаёт новую policy/payment" — reply на СТАРЫЙ
    (уже неактуальный, тот же id) prompt после того, как checkout уже
    COMPLETED, не запускает повторную попытку."""
    bank_gateway = _FakeBankGateway(
        start_results=[BankGatewayResult(outcome=BankGatewayOutcome.AWAITING_CODE, message="ждём код")],
        code_results=[BankGatewayResult(outcome=BankGatewayOutcome.COMPLETED, message="Готово.")],
    )
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    started = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    await service.mark_code_prompt_sent(started.state.id, 555)
    await service.handle_code_reply(
        chat_id=_CHAT_ID, prompt_message_id=555, code="123456", operator_user_id=_OPERATOR_ID,
    )

    second_reply = await service.handle_code_reply(
        chat_id=_CHAT_ID, prompt_message_id=555, code="999999", operator_user_id=_OPERATOR_ID,
    )

    assert second_reply.state.status == CheckoutStatus.COMPLETED
    assert bank_gateway.received_codes == ["123456"]  # второй код НЕ ушёл в gateway


# ---- OTP ownership: код принимается ТОЛЬКО от того, кто запустил pay ----
# (см. задачу: checkout/pay больше не ограничен ocr.allowed_user_ids —
# допуск к самому чату открыт любому участнику, поэтому ownership
# конкретного платежа обязана определяться operator_user_id заявки, а не
# общим списком allowed-пользователей).

_OTHER_OPERATOR_ID = 222


async def test_otp_from_same_operator_who_sent_pay_is_accepted():
    bank_gateway = _FakeBankGateway(
        start_results=[BankGatewayResult(outcome=BankGatewayOutcome.AWAITING_CODE, message="ждём код")],
        code_results=[BankGatewayResult(outcome=BankGatewayOutcome.COMPLETED, message="Готово.")],
    )
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    started = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=60, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    await service.mark_code_prompt_sent(started.state.id, 555)

    outcome = await service.handle_code_reply(
        chat_id=_CHAT_ID, prompt_message_id=555, code="123456", operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert bank_gateway.received_codes == ["123456"]


async def test_otp_from_different_user_than_who_sent_pay_is_rejected():
    """user A -> pay (operator_user_id=A); user B присылает OTP -> должен
    быть отклонён с понятной ошибкой, код НЕ уходит в bank_gateway (не
    "любой участник чата может подтвердить чужой платёж")."""
    bank_gateway = _FakeBankGateway(
        start_results=[BankGatewayResult(outcome=BankGatewayOutcome.AWAITING_CODE, message="ждём код")],
    )
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    started = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=61, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    await service.mark_code_prompt_sent(started.state.id, 555)

    outcome = await service.handle_code_reply(
        chat_id=_CHAT_ID, prompt_message_id=555, code="123456", operator_user_id=_OTHER_OPERATOR_ID,
    )

    assert outcome is not None  # это НАШ prompt (не "вообще не про код") — просто отклонён
    assert outcome.state.status == CheckoutStatus.WAITING_FOR_CONFIRMATION_CODE  # не продвинулся
    assert bank_gateway.received_codes == []
    assert "оператор" in outcome.reply_text.lower()


async def test_otp_from_wrong_user_then_correct_user_still_completes():
    """После отказа user B корректный код от user A (того же, кто слал
    pay) всё ещё должен нормально пройти — отказ не портит state."""
    bank_gateway = _FakeBankGateway(
        start_results=[BankGatewayResult(outcome=BankGatewayOutcome.AWAITING_CODE, message="ждём код")],
        code_results=[BankGatewayResult(outcome=BankGatewayOutcome.COMPLETED, message="Готово.")],
    )
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    started = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=62, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    await service.mark_code_prompt_sent(started.state.id, 555)

    rejected = await service.handle_code_reply(
        chat_id=_CHAT_ID, prompt_message_id=555, code="000000", operator_user_id=_OTHER_OPERATOR_ID,
    )
    assert rejected.state.status == CheckoutStatus.WAITING_FOR_CONFIRMATION_CODE

    accepted = await service.handle_code_reply(
        chat_id=_CHAT_ID, prompt_message_id=555, code="123456", operator_user_id=_OPERATOR_ID,
    )
    assert accepted.state.status == CheckoutStatus.COMPLETED
    assert bank_gateway.received_codes == ["123456"]  # код от "чужого" оператора не дошёл до банка


async def test_operator_user_id_is_whoever_actually_sent_pay_not_who_sent_ocr_documents():
    """"Не связывать ownership с тем, кто первоначально отправил документы
    на OCR" (см. задачу) — этот сервис вообще не знает, кто слал документы
    (это на уровне InsuranceOcrCommand, не CheckoutService), поэтому
    operator_user_id заявки — это buквально тот, кто вызвал handle_pay,
    вне зависимости от того, кто ранее прислал OCR-черновик."""
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=63, ocr_message_text=_ocr_message(), operator_user_id=_OTHER_OPERATOR_ID,
    )

    assert outcome.state.operator_user_id == _OTHER_OPERATOR_ID


async def test_two_concurrent_checkouts_do_not_mix_operator_or_otp():
    """Два РАЗНЫХ checkout (разные ocr_message_id) в одном чате, каждый со
    своим оператором и своим кодом — код одного НЕ должен подтвердить
    другой (см. задачу: "Concurrent flows")."""
    bank_gateway_a = _FakeBankGateway(
        start_results=[BankGatewayResult(outcome=BankGatewayOutcome.AWAITING_CODE, message="ждём код")],
        code_results=[BankGatewayResult(outcome=BankGatewayOutcome.COMPLETED, message="Готово.")],
    )
    bank_gateway_b = _FakeBankGateway(
        start_results=[BankGatewayResult(outcome=BankGatewayOutcome.AWAITING_CODE, message="ждём код")],
        code_results=[BankGatewayResult(outcome=BankGatewayOutcome.COMPLETED, message="Готово.")],
    )
    # Общий store — оба checkout из одного и того же реального процесса/чата.
    store = CheckoutStore()

    service_a, _tpl_a, ref_a, _lock_a = _service_with_real_personal_info(bank_gateway=bank_gateway_a, store=store)
    # Тот же store переиспользуем для "второго checkout", подменяя только
    # bank_gateway (имитирует два параллельных pay в одном процессе/store).
    service_b, _tpl_b, _ref_b, _lock_b = _service(
        reference_data=ref_a, personal_info_provider=service_a._personal_info_provider,
        bank_gateway=bank_gateway_b, store=store,
    )

    started_a = await service_a.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=70, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    started_b = await service_b.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=71, ocr_message_text=_ocr_message(), operator_user_id=_OTHER_OPERATOR_ID,
    )
    assert started_a.state.status == CheckoutStatus.WAITING_FOR_CONFIRMATION_CODE
    assert started_b.state.status == CheckoutStatus.WAITING_FOR_CONFIRMATION_CODE

    await service_a.mark_code_prompt_sent(started_a.state.id, 900)
    await service_b.mark_code_prompt_sent(started_b.state.id, 901)

    # Код заявки B (по её собственному prompt_message_id), отправленный
    # оператором заявки A — не должен пройти ни как "чужой чат/сообщение"
    # (id 901 принадлежит B), ни быть перепутан с заявкой A.
    outcome = await service_b.handle_code_reply(
        chat_id=_CHAT_ID, prompt_message_id=901, code="123456", operator_user_id=_OPERATOR_ID,
    )
    assert outcome.state.id == started_b.state.id  # это точно заявка B, не A
    assert outcome.state.status == CheckoutStatus.WAITING_FOR_CONFIRMATION_CODE  # отклонён (чужой operator)
    assert bank_gateway_b.received_codes == []

    # Правильный оператор B со своим кодом на СВОЙ prompt — проходит и не
    # трогает состояние A.
    completed_b = await service_b.handle_code_reply(
        chat_id=_CHAT_ID, prompt_message_id=901, code="999999", operator_user_id=_OTHER_OPERATOR_ID,
    )
    assert completed_b.state.status == CheckoutStatus.COMPLETED
    assert bank_gateway_b.received_codes == ["999999"]

    # Заявка A вообще не была затронута — её собственный prompt/оператор
    # по-прежнему нужен для её собственного завершения.
    completed_a = await service_a.handle_code_reply(
        chat_id=_CHAT_ID, prompt_message_id=900, code="123456", operator_user_id=_OPERATOR_ID,
    )
    assert completed_a.state.id == started_a.state.id
    assert completed_a.state.status == CheckoutStatus.COMPLETED
    assert bank_gateway_a.received_codes == ["123456"]


# ---- OTP expired ----


async def test_otp_expired_marks_checkout_failed():
    bank_gateway = _FakeBankGateway(
        start_results=[BankGatewayResult(outcome=BankGatewayOutcome.AWAITING_CODE, message="ждём код")],
        code_results=[
            BankGatewayResult(
                outcome=BankGatewayOutcome.FAILED, message="Код подтверждения просрочен.",
                failure_reason=FailureReason.OTP_EXPIRED,
            ),
        ],
    )
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    started = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    await service.mark_code_prompt_sent(started.state.id, 555)

    outcome = await service.handle_code_reply(
        chat_id=_CHAT_ID, prompt_message_id=555, code="123456", operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.FAILED
    assert outcome.state.failure_reason == FailureReason.OTP_EXPIRED


# ---- card declined ----


async def test_card_declined_marks_checkout_failed():
    bank_gateway = _FakeBankGateway(
        start_results=[
            BankGatewayResult(
                outcome=BankGatewayOutcome.FAILED, message="Оплата отклонена банком.",
                failure_reason=FailureReason.CARD_DECLINED,
            ),
        ],
    )
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.FAILED
    assert outcome.state.failure_reason == FailureReason.CARD_DECLINED
    assert "отклонена" in outcome.reply_text
    assert len(tpl_client.created_payloads) == 1  # policy на tpl.ge уже была создана


# ---- timeout ----


async def test_payment_timeout_marks_checkout_failed():
    bank_gateway = _FakeBankGateway(
        start_results=[
            BankGatewayResult(
                outcome=BankGatewayOutcome.FAILED, message="Банк не ответил вовремя.",
                failure_reason=FailureReason.PAYMENT_TIMEOUT,
            ),
        ],
    )
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.failure_reason == FailureReason.PAYMENT_TIMEOUT


# ---- browser crash ----


async def test_browser_crash_during_start_marks_checkout_failed():
    bank_gateway = _FakeBankGateway(start_error=BankGatewayError("browser crashed"))
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.FAILED
    assert outcome.state.failure_reason == FailureReason.BROWSER_CRASHED


async def test_unexpected_exception_from_bank_gateway_start_is_caught():
    class _ExplodingGateway(_FakeBankGateway):
        async def start(self, state, redirect_url):
            raise RuntimeError("boom")

    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=_ExplodingGateway())

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.FAILED
    assert outcome.state.failure_reason == FailureReason.BROWSER_CRASHED
    assert "boom" not in outcome.reply_text


# ---- duplicate pay во время банковского flow ----


async def test_duplicate_pay_while_payment_in_progress_or_waiting_for_code_is_rejected():
    bank_gateway = _FakeBankGateway(
        start_results=[BankGatewayResult(outcome=BankGatewayOutcome.AWAITING_CODE, message="ждём код")],
    )
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    second = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert len(tpl_client.created_payloads) == 1
    assert bank_gateway.start_calls == 1
    assert "уже" in second.reply_text.lower()


async def test_duplicate_pay_after_completed_does_not_create_second_policy():
    bank_gateway = _completed_gateway()
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    second = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert len(tpl_client.created_payloads) == 1
    assert second.state.status == CheckoutStatus.COMPLETED


async def test_duplicate_pay_after_card_declined_is_rejected_not_retried():
    """CARD_DECLINED — реальный банковский запрос УЖЕ был, поэтому это НЕ
    входит в "безопасные для повтора" причины (см.
    reader/checkout/models.py::is_locked_status) — второй pay не пытается
    списать снова."""
    bank_gateway = _FakeBankGateway(
        start_results=[
            BankGatewayResult(
                outcome=BankGatewayOutcome.FAILED, message="Оплата отклонена банком.",
                failure_reason=FailureReason.CARD_DECLINED,
            ),
        ],
    )
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    second = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert len(tpl_client.created_payloads) == 1
    assert bank_gateway.start_calls == 1
    assert "уже" in second.reply_text.lower()


async def test_pay_retriable_after_tpl_ge_policy_create_error():
    """tpl.ge-этап (ДО банка) — HTTP-ошибка НЕ означает, что был банковский
    запрос, поэтому повтор безопасен и разрешён (см.
    FailureReason.TPL_GE_POLICY_CREATE_ERROR в _RETRIABLE_FAILURE_REASONS)."""
    tpl_client = _FakeTplClient(create_error=TplGeClientError("HTTP 500"))
    bank_gateway = _completed_gateway()
    service, _tpl, _ref, _lock = _service_with_real_personal_info(tpl_client=tpl_client, bank_gateway=bank_gateway)

    first = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    assert first.state.failure_reason == FailureReason.TPL_GE_POLICY_CREATE_ERROR

    tpl_client.create_error = None  # "починили" tpl.ge
    second = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    assert second.state.status == CheckoutStatus.COMPLETED


# ---- restart/recovery ----


async def test_restart_during_payment_in_progress_blocks_duplicate_and_gives_recovery_message():
    """Симулируем перезапуск процесса: НОВЫЙ CheckoutService с ПУСТЫМ
    in-memory CheckoutStore, но с ТЕМ ЖЕ lock_repository (единственное, что
    реально переживает restart, см. reader/checkout/lock_repository.py)."""
    # Симулируем именно момент "процесс убит после _persist_lock(PAYMENT_IN_PROGRESS),
    # но до того как bank_gateway.start() успел вернуть результат" — пишем
    # lock напрямую, как это сделал бы CheckoutService._persist_lock() в
    # реальном (прерванном) прогоне.
    lock_repository = _memory_lock_repository()
    lock_repository.upsert(
        chat_id=_CHAT_ID, ocr_message_id=1, checkout_id="crashed-checkout-id",
        status=CheckoutStatus.PAYMENT_IN_PROGRESS.value, failure_reason=None,
    )

    # "Новый процесс" — новый CheckoutService с чистым in-memory Store, тот
    # же lock_repository.
    fresh_bank_gateway = _completed_gateway()
    service_after, tpl_client_after, _ref2, _lock2 = _service_with_real_personal_info(
        bank_gateway=fresh_bank_gateway, lock_repository=lock_repository,
    )

    outcome = await service_after.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=1, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state is None  # новую policy не создаём вовсе
    assert "crashed-checkout-id" in outcome.reply_text
    assert "перезапуск" in outcome.reply_text.lower() or "перезапущ" in outcome.reply_text.lower()
    assert tpl_client_after.created_payloads == []
    assert fresh_bank_gateway.start_calls == 0


async def test_restart_after_waiting_for_confirmation_code_also_blocks_duplicate():
    lock_repository = _memory_lock_repository()
    lock_repository.upsert(
        chat_id=_CHAT_ID, ocr_message_id=2, checkout_id="crashed-checkout-2",
        status=CheckoutStatus.WAITING_FOR_CONFIRMATION_CODE.value, failure_reason=None,
    )

    service, tpl_client, _ref, _lock = _service_with_real_personal_info(
        bank_gateway=_completed_gateway(), lock_repository=lock_repository,
    )

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=2, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state is None
    assert tpl_client.created_payloads == []


async def test_restart_after_retriable_tpl_ge_failure_does_not_block():
    """Persisted lock с retriable-причиной (tpl_ge_policy_create_error) НЕ
    должен блокировать новую попытку после restart — банковского запроса
    ещё не было."""
    lock_repository = _memory_lock_repository()
    lock_repository.upsert(
        chat_id=_CHAT_ID, ocr_message_id=3, checkout_id="old-checkout-3",
        status=CheckoutStatus.FAILED.value, failure_reason=FailureReason.TPL_GE_POLICY_CREATE_ERROR.value,
    )

    service, tpl_client, _ref, _lock = _service_with_real_personal_info(
        bank_gateway=_completed_gateway(), lock_repository=lock_repository,
    )

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=3, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state is not None
    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert len(tpl_client.created_payloads) == 1


# ---- edited-data reply (поля ТС) ----


async def test_correction_reply_overrides_original_field():
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=2, ocr_message_text=_ocr_message(manufacturer="Toyota", model="Corolla"),
        correction_text="Модель: Camry",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert outcome.state.effective_fields.model == "Camry"


async def test_correction_reply_can_fix_wrong_category():
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=3, ocr_message_text=_ocr_message(category="motorcycle"),
        correction_text="Категория: passenger_car",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.effective_fields.category == "passenger_car"


# ---- неправильный reply ----


async def test_correction_reply_with_unrecognized_text_replies_with_usage_hint():
    service, tpl_client, _ref, _lock = _service()

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=4, ocr_message_text=_ocr_message(),
        correction_text="случайный текст без меток",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state is None
    assert "pay" in outcome.reply_text
    assert tpl_client.created_payloads == []


async def test_pay_reply_referencing_non_ocr_message_is_rejected():
    service, tpl_client, _ref, _lock = _service()

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=5, ocr_message_text="просто случайное сообщение",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state is None
    assert tpl_client.created_payloads == []


# ---- missing fields (поля ТС/паспорт/гражданство) ----


async def test_pay_reply_missing_required_vehicle_field_stops_before_reference_data():
    calls = []

    class _CountingReferenceData(_FakeReferenceData):
        async def category_product(self, category, policy_period):
            calls.append(1)
            return await super().category_product(category, policy_period)

    service, tpl_client, _ref, _lock = _service(reference_data=_CountingReferenceData())

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=6,
        ocr_message_text=_ocr_message(manufacturer="не распознано"),
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.MISSING_VEHICLE_DATA
    assert "Марка" in outcome.reply_text
    assert calls == []
    assert tpl_client.created_payloads == []


async def test_pay_reply_missing_both_vin_and_chassis():
    service, _tpl, _ref, _lock = _service()

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=7,
        ocr_message_text=_ocr_message(vin="не распознано", chassis="не распознано"),
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.MISSING_VEHICLE_DATA
    assert "VIN/Номер шасси" in outcome.reply_text


async def test_pay_reply_missing_passport_number_blocks_checkout():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info()

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=21,
        ocr_message_text=_ocr_message(passport_number="не распознано"),
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.MISSING_PERSONAL_INFO
    assert "номер паспорта" in outcome.reply_text
    assert tpl_client.created_payloads == []


async def test_pay_reply_missing_citizenship_blocks_checkout():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info()

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=22,
        ocr_message_text=_ocr_message(citizenship="не распознано"),
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.MISSING_PERSONAL_INFO
    assert "гражданство" in outcome.reply_text
    assert tpl_client.created_payloads == []


async def test_pay_reply_unknown_citizenship_not_in_tpl_ge_catalog_blocks_checkout():
    reference_data = _FakeReferenceData(country_found=False)
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(reference_data=reference_data)

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=23,
        ocr_message_text=_ocr_message(citizenship="Narnia"),
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.MISSING_PERSONAL_INFO
    assert "Narnia" in outcome.reply_text
    assert tpl_client.created_payloads == []


async def test_operator_can_fix_passport_number_via_correction_reply():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=24,
        ocr_message_text=_ocr_message(passport_number="не распознано"),
        operator_user_id=_OPERATOR_ID,
    )
    second = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=24,
        ocr_message_text=_ocr_message(passport_number="не распознано"),
        correction_text="Номер паспорта: XY9998887",
        operator_user_id=_OPERATOR_ID,
    )

    assert second.state.status == CheckoutStatus.COMPLETED
    assert tpl_client.created_payloads[0].insurer_identification_number == "XY9998887"


async def test_operator_can_fix_citizenship_via_correction_reply():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=25,
        ocr_message_text=_ocr_message(citizenship="не распознано"),
        operator_user_id=_OPERATOR_ID,
    )
    second = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=25,
        ocr_message_text=_ocr_message(citizenship="не распознано"),
        correction_text="Гражданство: Georgia",
        operator_user_id=_OPERATOR_ID,
    )

    assert second.state.status == CheckoutStatus.COMPLETED
    assert tpl_client.created_payloads[0].insurer_citizenship_id == 1


async def test_pay_retriable_after_missing_vehicle_data_status_not_locked():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=10,
        ocr_message_text=_ocr_message(manufacturer="не распознано"),
        operator_user_id=_OPERATOR_ID,
    )
    second = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=10, ocr_message_text=_ocr_message(manufacturer="не распознано"),
        correction_text="Марка: Toyota", operator_user_id=_OPERATOR_ID,
    )

    assert second.state.status == CheckoutStatus.COMPLETED
    assert len(tpl_client.created_payloads) == 1


async def test_pay_retriable_after_missing_personal_info_status_not_locked():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=26,
        ocr_message_text=_ocr_message(passport_number="не распознано"),
        operator_user_id=_OPERATOR_ID,
    )
    second = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=26,
        ocr_message_text=_ocr_message(passport_number="не распознано"),
        correction_text="Номер паспорта: XY9998887",
        operator_user_id=_OPERATOR_ID,
    )

    assert second.state.status == CheckoutStatus.COMPLETED
    assert len(tpl_client.created_payloads) == 1


# ---- Водитель/Владелец = страхователь: новая бизнес-логика ролей ----


async def test_driver_plus_does_not_require_separate_full_name_and_uses_insurer_title():
    """"+" (по умолчанию) — не требует отдельного ФИО, tpl.ge payload
    получает то же title, что и у страхователя (см.
    reader/checkout/service.py::_build_payload — реальный payload
    дублирует поля, отдельного driverSameAsInsurer-флага research не
    подтвердил)."""
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=30, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    payload = tpl_client.created_payloads[0]
    assert payload.vehicle_driver_title == payload.insurer_title == "Petrov Petr"


async def test_owner_plus_does_not_require_separate_full_name_and_uses_insurer_title():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=31, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    payload = tpl_client.created_payloads[0]
    assert payload.vehicle_owner_title == payload.insurer_title == "Petrov Petr"


async def test_correction_driver_flag_minus_without_full_name_blocks_checkout():
    """"-" без отдельного ФИО блокирует checkout с понятным сообщением."""
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=32, ocr_message_text=_ocr_message(),
        correction_text="Водитель = страхователь: -",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.MISSING_VEHICLE_DATA
    assert "Водитель" in outcome.reply_text
    assert tpl_client.created_payloads == []


async def test_correction_driver_flag_minus_with_full_name_reaches_missing_personal_info():
    """"-" С отдельным ФИО проходит проверку обязательных vehicle-полей, но
    checkout всё равно останавливается на personal_info — отдельных
    identification_number/citizenship для водителя сейчас нет (см.
    reader/checkout/personal_info.py)."""
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=33, ocr_message_text=_ocr_message(),
        correction_text="Водитель = страхователь: -\nВодитель: Sidorov Petr",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.MISSING_PERSONAL_INFO
    assert "Водитель" in outcome.reply_text
    assert tpl_client.created_payloads == []


async def test_correction_owner_flag_minus_without_full_name_blocks_checkout():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=34, ocr_message_text=_ocr_message(),
        correction_text="Владелец = страхователь: -",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.MISSING_VEHICLE_DATA
    assert "Владелец" in outcome.reply_text
    assert tpl_client.created_payloads == []


async def test_correction_owner_flag_minus_with_full_name_reaches_missing_personal_info():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=35, ocr_message_text=_ocr_message(),
        correction_text="Владелец = страхователь: -\nВладелец: Sidorov Petr",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.MISSING_PERSONAL_INFO
    assert "Владелец" in outcome.reply_text
    assert tpl_client.created_payloads == []


async def test_correction_invalid_flag_value_is_rejected_before_starting_checkout():
    service, tpl_client, _ref, _lock = _service()

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=36, ocr_message_text=_ocr_message(),
        correction_text="Водитель = страхователь: yes",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state is None
    assert "+" in outcome.reply_text and "-" in outcome.reply_text
    assert tpl_client.created_payloads == []


# ---- Email/Телефон: default из settings, редактируемы correction-reply'ем ----


async def test_email_and_phone_from_settings_are_used_in_payload_by_default():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=37, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    payload = tpl_client.created_payloads[0]
    assert payload.insurer_email == _SETTINGS_EMAIL
    assert payload.insurer_phone == _SETTINGS_PHONE


async def test_operator_can_change_email_and_phone_via_correction_reply():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=38, ocr_message_text=_ocr_message(),
        correction_text="Email: operator@example.com\nТелефон: 599111222",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    payload = tpl_client.created_payloads[0]
    assert payload.insurer_email == "operator@example.com"
    assert payload.insurer_phone == "599111222"
    assert payload.vehicle_owner_email == "operator@example.com"
    assert payload.vehicle_driver_phone == "599111222"


# ---- Банк/Период/Начало периода: поля конкретной заявки (не runtime config) ----


async def test_default_bank_reaches_redirect_call_as_bank_of_georgia():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=40, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert tpl_client.redirect_calls[0]["bank"] == PaymentBank.BANK_OF_GEORGIA


async def test_correction_bank_liberty_is_resolved_and_used_for_redirect_call():
    """Оператор может изменить bog -> liberty; преобразование в реальный
    PaymentBank — см. reader/checkout/mapping.py::resolve_payment_bank."""
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=41, ocr_message_text=_ocr_message(),
        correction_text="Банк: liberty",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert tpl_client.redirect_calls[0]["bank"] == PaymentBank.LIBERTY_BANK


async def test_correction_invalid_bank_is_rejected_before_starting_checkout():
    service, tpl_client, _ref, _lock = _service()

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=42, ocr_message_text=_ocr_message(),
        correction_text="Банк: tbc",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state is None
    assert tpl_client.created_payloads == []


async def test_default_policy_period_resolves_to_tpl_ge_format():
    service, _tpl, reference_data, _lock = _service_with_real_personal_info(
        bank_gateway=_completed_gateway(),
    )

    await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=43, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert reference_data.category_product_calls == [("passenger_car", "15-D")]


@pytest.mark.parametrize("value,expected", [("30", "30-D"), ("90", "90-D")])
async def test_correction_policy_period_resolves_to_tpl_ge_format(value, expected):
    service, _tpl, reference_data, _lock = _service_with_real_personal_info(
        bank_gateway=_completed_gateway(),
    )

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=44, ocr_message_text=_ocr_message(),
        correction_text=f"Период: {value}",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert reference_data.category_product_calls == [("passenger_car", expected)]


@pytest.mark.parametrize("value", ["1-Y", "365"])
async def test_correction_invalid_policy_period_is_rejected(value):
    """1-Y (годовой полис) и любое другое значение вне 15/30/90 не
    поддерживаются в Telegram-flow."""
    service, tpl_client, _ref, _lock = _service()

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=45, ocr_message_text=_ocr_message(),
        correction_text=f"Период: {value}",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state is None
    assert tpl_client.created_payloads == []


async def test_default_period_start_reaches_payload_start_date():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=46, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert tpl_client.created_payloads[0].start_date == "2026-08-16"


async def test_correction_period_start_reaches_payload_start_date():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(bank_gateway=_completed_gateway())

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=47, ocr_message_text=_ocr_message(),
        correction_text="Начало периода: 01.09.2026",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert tpl_client.created_payloads[0].start_date == "2026-09-01"


async def test_correction_invalid_period_start_is_rejected():
    service, tpl_client, _ref, _lock = _service()

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=48, ocr_message_text=_ocr_message(),
        correction_text="Начало периода: 31.02.2026",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state is None
    assert tpl_client.created_payloads == []


async def test_pay_after_midnight_does_not_change_period_start():
    """Заявка создана ДО полуночи (period_start уже зафиксирован в тексте
    Telegram-сообщения — см. reader/commands/insurance_ocr.py), оператор
    подтверждает "pay" ПОСЛЕ полуночи: checkout не должен звать today()
    заново — start_date payload'а берётся из сохранённого значения, а не из
    текущих часов сервиса (см. reader/checkout/service.py::_build_payload,
    resolve_period_start)."""
    from datetime import datetime, timezone

    def later_clock():  # на день позже полуночи относительно period_start заявки
        return datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)

    service, tpl_client, _ref, _lock = _service_with_real_personal_info(
        bank_gateway=_completed_gateway(), clock=later_clock,
    )

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=49,
        ocr_message_text=_ocr_message(period_start="16.08.2026"),
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert tpl_client.created_payloads[0].start_date == "2026-08-16"


async def test_all_three_new_fields_reach_real_checkout_payload_together():
    service, tpl_client, reference_data, _lock = _service_with_real_personal_info(
        bank_gateway=_completed_gateway(),
    )

    outcome = await service.handle_correction(
        chat_id=_CHAT_ID, ocr_message_id=50, ocr_message_text=_ocr_message(),
        correction_text="Банк: liberty\nПериод: 90\nНачало периода: 01.12.2026",
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.COMPLETED
    assert tpl_client.redirect_calls[0]["bank"] == PaymentBank.LIBERTY_BANK
    assert reference_data.category_product_calls == [("passenger_car", "90-D")]
    assert tpl_client.created_payloads[0].start_date == "2026-12-01"


# ---- mapping / reference data ошибки ----


async def test_manufacturer_not_found_in_tpl_ge_catalog():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(
        reference_data=_FakeReferenceData(manufacturer_found=False),
    )

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=11, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.MAPPING_FAILED
    assert tpl_client.created_payloads == []


async def test_model_not_found_in_tpl_ge_catalog():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(
        reference_data=_FakeReferenceData(model_found=False),
    )

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=12, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.MAPPING_FAILED
    assert tpl_client.created_payloads == []


async def test_invalid_registration_number_is_a_mapping_error():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info()

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=13,
        ocr_message_text=_ocr_message(plate="АА001АА"),  # кириллица
        operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.MAPPING_FAILED
    assert tpl_client.created_payloads == []


async def test_reference_data_error_from_category_lookup():
    service, tpl_client, _ref, _lock = _service_with_real_personal_info(
        reference_data=_FakeReferenceData(category_error=ReferenceDataError("нет тарифа")),
    )

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=14, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.MAPPING_FAILED
    assert tpl_client.created_payloads == []


# ---- tpl.ge HTTP ошибки ----


async def test_create_policy_http_error_marks_checkout_failed():
    tpl_client = _FakeTplClient(create_error=TplGeClientError("HTTP 500"))
    service, _tpl, _ref, _lock = _service_with_real_personal_info(tpl_client=tpl_client)

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=15, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.FAILED
    assert outcome.state.failure_reason == FailureReason.TPL_GE_POLICY_CREATE_ERROR
    assert "tpl.ge отклонил" in outcome.reply_text


async def test_payment_redirect_error_after_policy_already_created():
    tpl_client = _FakeTplClient(redirect_error=TplGeClientError("timeout"))
    service, _tpl, _ref, _lock = _service_with_real_personal_info(tpl_client=tpl_client)

    outcome = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=16, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )

    assert outcome.state.status == CheckoutStatus.FAILED
    assert outcome.state.failure_reason == FailureReason.TPL_GE_REDIRECT_ERROR
    assert len(tpl_client.created_payloads) == 1
    assert outcome.state.id in outcome.reply_text


# ---- секреты/логирование ----


async def test_confirmation_code_is_never_logged(caplog):
    bank_gateway = _FakeBankGateway(
        start_results=[BankGatewayResult(outcome=BankGatewayOutcome.AWAITING_CODE, message="ждём код")],
    )
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)

    started = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=20, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    await service.mark_code_prompt_sent(started.state.id, 321)

    secret_code = "987654"
    with caplog.at_level(logging.DEBUG):
        await service.handle_code_reply(
            chat_id=_CHAT_ID, prompt_message_id=321, code=secret_code, operator_user_id=_OPERATOR_ID,
        )

    for record in caplog.records:
        assert secret_code not in record.getMessage()


async def test_confirmation_code_never_appears_in_exception_text(caplog):
    class _ExplodingGateway(_FakeBankGateway):
        async def submit_confirmation_code(self, state, code):
            raise RuntimeError(f"boom with code {code}")  # намеренно "утечка", чтобы тест мог её поймать

    bank_gateway = _FakeBankGateway(
        start_results=[BankGatewayResult(outcome=BankGatewayOutcome.AWAITING_CODE, message="ждём код")],
    )
    service, _tpl, _ref, _lock = _service_with_real_personal_info(bank_gateway=bank_gateway)
    started = await service.handle_pay(
        chat_id=_CHAT_ID, ocr_message_id=27, ocr_message_text=_ocr_message(), operator_user_id=_OPERATOR_ID,
    )
    await service.mark_code_prompt_sent(started.state.id, 654)

    service._bank_gateway = _ExplodingGateway()
    secret_code = "135790"
    with caplog.at_level(logging.DEBUG):
        outcome = await service.handle_code_reply(
            chat_id=_CHAT_ID, prompt_message_id=654, code=secret_code, operator_user_id=_OPERATOR_ID,
        )

    assert secret_code not in outcome.reply_text
    for record in caplog.records:
        assert secret_code not in record.getMessage()


async def test_checkout_state_has_no_attribute_storing_the_code():
    """Структурная гарантия: CheckoutState вообще не имеет поля для кода —
    его негде "случайно" сохранить дольше вызова submit_confirmation_code
    (см. reader/checkout/models.py::CheckoutState)."""
    from reader.checkout.models import CheckoutState

    field_names = {f for f in CheckoutState.__dataclass_fields__}
    assert not any("code" in name and "prompt" not in name for name in field_names)


async def test_lock_repository_never_receives_code_or_card_data():
    """_persist_lock() персистит только (chat_id, ocr_message_id, checkout_id,
    status, failure_reason) — структурно не может содержать секреты."""
    from reader.checkout.lock_repository import CheckoutLockRepository

    fields = {f for f in CheckoutLockRepository.upsert.__code__.co_varnames}
    assert "code" not in fields
    assert "card_number" not in fields
    assert "cvv" not in fields
