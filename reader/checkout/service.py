"""Оркестрация checkout tpl.ge — от "pay"/исправленных полей до банковского
этапа (Bank of Georgia, см. reader/checkout/payment_gateway.py) и
результата. Чистая бизнес-логика: ничего не знает про Telethon/Telegram
(см. reader/checkout/telegram_integration.py, который вызывает эти методы
и сам решает, что и куда отвечать) — тот же слоеный подход, что и
Command/CommandDispatcher (reader/commands/).

State machine (см. задачу):
  COLLECTING -> MISSING_VEHICLE_DATA | MAPPING_FAILED | MISSING_PERSONAL_INFO
             -> POLICY_CREATED -> PAYMENT_REDIRECT_READY -> PAYMENT_IN_PROGRESS
             -> WAITING_FOR_CONFIRMATION_CODE -> PAYMENT_IN_PROGRESS -> COMPLETED
             (или FAILED с конкретным FailureReason на любом банковском шаге)

Restart/recovery (см. задачу, раздел 7): reader/checkout/lock_repository.py
хранит МИНИМАЛЬНУЮ запись (chat_id, ocr_message_id) -> (checkout_id, status,
failure_reason), переживающую перезапуск процесса — НЕ полное состояние и
уж тем более не браузерную сессию (её пережить нельзя). Если после
перезапуска приходит "pay" на сообщение, чья persisted-запись говорит
"платёж мог быть в процессе" — checkout НЕ создаёт новую policy, а честно
отвечает, что статус неизвестен и нужна ручная проверка."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from reader.checkout.lock_repository import CheckoutLockRepository
from reader.checkout.mapping import (
    MappingError,
    required_vehicle_fields_missing,
    resolve_payment_bank,
    resolve_period_start,
    resolve_policy_period,
    sanitize_registration_number,
    select_frame_number,
)
from reader.checkout.models import (
    CheckoutState,
    CheckoutStatus,
    FailureReason,
    PaymentBank,
    TplPolicyPayload,
    is_locked_status,
)
from reader.checkout.parser import ReplyParseError, apply_corrections, parse_correction_reply, parse_ocr_message
from reader.checkout.payment_gateway import (
    BankGatewayClient,
    BankGatewayError,
    BankGatewayOutcome,
    BankGatewayResult,
    NotImplementedBankGatewayClient,
)
from reader.checkout.personal_info import NoPersonalInfoProvider, PersonalInfoProvider
from reader.checkout.policy_document import (
    NotImplementedPolicyDocumentProvider,
    PolicyDocument,
    PolicyDocumentError,
    PolicyDocumentProvider,
)
from reader.checkout.reference_data import ReferenceDataError, TplReferenceDataClient
from reader.checkout.store import CheckoutStore
from reader.checkout.tpl_client import TplGeClient, TplGeClientError
from reader.ocr.models import OcrResult
from reader.time_display import TBILISI_TZ

logger = logging.getLogger(__name__)

_BANK_DISPLAY_NAME = {
    PaymentBank.BANK_OF_GEORGIA: "Bank of Georgia",
    PaymentBank.LIBERTY_BANK: "Liberty Bank",
}

_CODE_PROMPT_TEXT = "Введите код подтверждения — reply на это сообщение."
_SUCCESS_TEXT = "✅ Оплата успешно завершена."

_STATUS_RU = {
    CheckoutStatus.COMPLETED: "завершено",
    CheckoutStatus.WAITING_FOR_CONFIRMATION_CODE: "ожидает код подтверждения",
    CheckoutStatus.PAYMENT_IN_PROGRESS: "выполняется",
    CheckoutStatus.FAILED: "завершилось ошибкой",
}


class _PersonalInfoMissing(Exception):
    def __init__(self, missing: tuple[str, ...]):
        super().__init__("personal info missing")
        self.missing = missing


@dataclass(frozen=True)
class CheckoutOutcome:
    """reply_text — None означает "ничего не отвечать" (используется
    telegram_integration.py, которое само решает, вызывать ли event.reply).

    needs_code_prompt_registration=True — reply_text это ЗАПРОС кода
    подтверждения (первый раз или повтор после неверного кода);
    telegram_integration.py обязано после отправки этого reply вызвать
    CheckoutService.mark_code_prompt_sent(state.id, <id отправленного
    сообщения>), иначе `handle_code_reply` не сможет сопоставить будущий
    reply оператора с этим checkout (см. reader/checkout/store.py).

    policy_document — PDF полиса, если его удалось получить (см.
    reader/checkout/policy_document.py) — None не означает ошибку оплаты
    (см. _apply_bank_result): источник PDF пока не подтверждён research'ом,
    поэтому обычно отсутствует, а reply_text уже сообщает об успехе."""

    reply_text: str | None
    state: CheckoutState | None
    needs_code_prompt_registration: bool = False
    policy_document: PolicyDocument | None = None


def _duplicate_message(state: CheckoutState) -> str:
    status_word = _STATUS_RU.get(state.status, "уже начато")
    return f"Оформление по этому сообщению уже {status_word} (uId={state.id}), повторно не запускаю."


def _recovery_message(lock_checkout_id: str) -> str:
    return (
        f"Оформление по этому сообщению уже было начато (uId={lock_checkout_id}), но, "
        "похоже, было прервано перезапуском сервиса — достоверный статус оплаты "
        "неизвестен. Автоматический повтор заблокирован во избежание повторного "
        "платежа. Проверьте статус вручную (tpl.ge/выписка банка) и обратитесь к "
        "администратору, если нужно продолжить или отменить эту заявку."
    )


class CheckoutService:
    """payment_bank/policy_period/period_start больше НЕ конструкторные
    настройки — теперь это поля конкретной заявки (Telegram "Банк:"/
    "Период:"/"Начало периода:", см. reader/ocr/models.py::OcrResult),
    оператор может менять их per-request через correction-reply. Резолв в
    реальные PaymentBank/tpl.ge-значения — см. reader/checkout/mapping.py и
    _build_payload ниже."""

    def __init__(
        self,
        *,
        tpl_client: TplGeClient,
        reference_data: TplReferenceDataClient,
        lock_repository: CheckoutLockRepository,
        personal_info_provider: PersonalInfoProvider | None = None,
        bank_gateway: BankGatewayClient | None = None,
        policy_document_provider: PolicyDocumentProvider | None = None,
        store: CheckoutStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._tpl_client = tpl_client
        self._reference_data = reference_data
        self._lock_repository = lock_repository
        self._personal_info_provider = personal_info_provider or NoPersonalInfoProvider()
        self._bank_gateway = bank_gateway or NotImplementedBankGatewayClient()
        self._policy_document_provider = policy_document_provider or NotImplementedPolicyDocumentProvider()
        self._store = store or CheckoutStore()
        self._clock = clock or (lambda: datetime.now(TBILISI_TZ))

    async def handle_pay(
        self, *, chat_id: int, ocr_message_id: int, ocr_message_text: str, operator_user_id: int | None,
    ) -> CheckoutOutcome:
        blocked = self._check_duplicate(chat_id, ocr_message_id)
        if blocked is not None:
            return blocked

        try:
            effective = parse_ocr_message(ocr_message_text)
        except ReplyParseError as exc:
            return CheckoutOutcome(reply_text=str(exc), state=None)

        return await self._start(chat_id, ocr_message_id, operator_user_id, effective)

    async def handle_correction(
        self,
        *,
        chat_id: int,
        ocr_message_id: int,
        ocr_message_text: str,
        correction_text: str,
        operator_user_id: int | None,
    ) -> CheckoutOutcome:
        blocked = self._check_duplicate(chat_id, ocr_message_id)
        if blocked is not None:
            return blocked

        try:
            original = parse_ocr_message(ocr_message_text)
            corrections = parse_correction_reply(correction_text)
        except ReplyParseError as exc:
            return CheckoutOutcome(reply_text=str(exc), state=None)

        effective = apply_corrections(original, corrections)
        return await self._start(chat_id, ocr_message_id, operator_user_id, effective)

    def _check_duplicate(self, chat_id: int, ocr_message_id: int) -> CheckoutOutcome | None:
        existing = self._store.get_by_ocr_message(chat_id, ocr_message_id)
        if existing is not None and is_locked_status(existing.status, existing.failure_reason):
            return CheckoutOutcome(reply_text=_duplicate_message(existing), state=existing)
        if existing is not None:
            return None  # в памяти есть, но не заперт (например MISSING_*) — не проверяем lock повторно

        # В памяти ничего нет — либо это правда первый reply, либо процесс
        # перезапустился, пока checkout был "в полёте" (см. докстрок модуля,
        # раздел restart/recovery). Persisted lock — единственный способ это
        # различить.
        lock = self._lock_repository.get(chat_id, ocr_message_id)
        if lock is None:
            return None

        try:
            lock_status = CheckoutStatus(lock.status)
        except ValueError:
            return None  # неизвестный/устаревший статус в БД — не блокируем на всякий случай
        lock_reason = FailureReason(lock.failure_reason) if lock.failure_reason else None

        if is_locked_status(lock_status, lock_reason):
            return CheckoutOutcome(reply_text=_recovery_message(lock.checkout_id), state=None)
        return None

    async def mark_code_prompt_sent(self, checkout_id: str, message_id: int) -> None:
        """Вызывается telegram_integration.py СРАЗУ после того, как оно
        отправило reply_text из CheckoutOutcome(needs_code_prompt_registration=True)
        — регистрирует id ИМЕННО этого отправленного сообщения как якорь для
        будущего reply оператора с кодом (см. handle_code_reply)."""
        state = self._store.get_by_id(checkout_id)
        if state is None:
            raise ValueError(f"Неизвестный checkout id={checkout_id}")
        self._store.register_code_prompt_message(state, message_id)

    async def handle_code_reply(self, *, chat_id: int, prompt_message_id: int, code: str) -> CheckoutOutcome | None:
        """None — это вообще не reply на наш "код подтверждения" prompt
        (см. reader/checkout/telegram_integration.py, которое в этом случае
        не отвечает ничего). code нигде не логируется и не сохраняется как
        атрибут состояния — передаётся в bank_gateway одним вызовом и
        забывается (см. задачу: "OTP никогда не логировать")."""
        state = self._store.get_by_code_prompt_message(chat_id, prompt_message_id)
        if state is None:
            return None
        if state.status != CheckoutStatus.WAITING_FOR_CONFIRMATION_CODE:
            # Оператор ответил на СТАРЫЙ prompt уже после того, как checkout
            # завершился (успешно/с ошибкой) — не создаёт новую попытку (см.
            # задачу: "повторный OTP не создаёт новую policy/payment"),
            # просто честно сообщаем текущий статус.
            return CheckoutOutcome(reply_text=_duplicate_message(state), state=state)

        try:
            result = await self._bank_gateway.submit_confirmation_code(state, code)
        except BankGatewayError as exc:
            state.status = CheckoutStatus.FAILED
            state.failure_reason = FailureReason.BROWSER_CRASHED
            self._persist_lock(state)
            return CheckoutOutcome(reply_text=str(exc), state=state)
        except Exception:  # noqa: BLE001 — код никогда не логируется, exc тоже не логируем целиком
            logger.warning("Checkout %s: bank_gateway.submit_confirmation_code упал неожиданно", state.id)
            state.status = CheckoutStatus.FAILED
            state.failure_reason = FailureReason.BROWSER_CRASHED
            self._persist_lock(state)
            return CheckoutOutcome(
                reply_text="Техническая ошибка при подтверждении оплаты.", state=state,
            )

        return await self._apply_bank_result(state, result)

    async def _apply_bank_result(self, state: CheckoutState, result: BankGatewayResult) -> CheckoutOutcome:
        if result.outcome == BankGatewayOutcome.COMPLETED:
            state.status = CheckoutStatus.COMPLETED
            state.order_reference = result.order_reference
            self._persist_lock(state)
            reply = _SUCCESS_TEXT
            if result.order_reference:
                reply += f"\nПолис: {result.order_reference}"

            # PDF — best-effort (см. reader/checkout/policy_document.py):
            # отсутствие источника PDF НЕ откатывает уже подтверждённую
            # успешную оплату, просто файл не прикладывается.
            policy_document = None
            try:
                policy_document = await self._policy_document_provider.fetch(state)
            except PolicyDocumentError as exc:
                logger.info("Checkout %s: PDF полиса не получен (%s)", state.id, exc)
            except Exception:  # noqa: BLE001 — тот же защитный барьер, что и у bank_gateway
                logger.warning("Checkout %s: получение PDF полиса упало неожиданно", state.id)

            return CheckoutOutcome(reply_text=reply, state=state, policy_document=policy_document)

        if result.outcome in (BankGatewayOutcome.AWAITING_CODE, BankGatewayOutcome.RETRY_CODE):
            state.status = CheckoutStatus.WAITING_FOR_CONFIRMATION_CODE
            self._persist_lock(state)
            reply = result.message if result.outcome == BankGatewayOutcome.RETRY_CODE else _CODE_PROMPT_TEXT
            return CheckoutOutcome(reply_text=reply, state=state, needs_code_prompt_registration=True)

        # BankGatewayOutcome.FAILED
        state.status = CheckoutStatus.FAILED
        state.failure_reason = result.failure_reason
        self._persist_lock(state)
        return CheckoutOutcome(reply_text=result.message, state=state)

    def _persist_lock(self, state: CheckoutState) -> None:
        """Персистится ТОЛЬКО минимум, нужный для restart-recovery (см.
        докстрок модуля) — не card/OTP данные, не сам redirect_url/payload."""
        self._lock_repository.upsert(
            chat_id=state.chat_id,
            ocr_message_id=state.ocr_message_id,
            checkout_id=state.id,
            status=state.status.value,
            failure_reason=state.failure_reason.value if state.failure_reason else None,
        )

    async def _start(
        self, chat_id: int, ocr_message_id: int, operator_user_id: int | None, effective: OcrResult,
    ) -> CheckoutOutcome:
        checkout_id = str(uuid.uuid4())
        state = CheckoutState(
            id=checkout_id,
            chat_id=chat_id,
            ocr_message_id=ocr_message_id,
            operator_user_id=operator_user_id,
            status=CheckoutStatus.COLLECTING,
            effective_fields=effective,
            created_at=self._clock(),
        )
        # Слот занимается СРАЗУ (до любых await ниже) — второй "pay" на то
        # же сообщение, пришедший, пока этот ещё обрабатывается, увидит уже
        # сохранённое состояние (см. задачу: "защита от двойного pay").
        self._store.save(state)

        missing_vehicle = required_vehicle_fields_missing(effective)
        if missing_vehicle:
            state.status = CheckoutStatus.MISSING_VEHICLE_DATA
            state.missing_fields = tuple(missing_vehicle)
            return CheckoutOutcome(
                reply_text="Не хватает данных для оформления: " + ", ".join(missing_vehicle) + ".",
                state=state,
            )

        try:
            payload, payment_bank = await self._build_payload(checkout_id, effective)
        except (MappingError, ReferenceDataError) as exc:
            state.status = CheckoutStatus.MAPPING_FAILED
            return CheckoutOutcome(reply_text=f"Не удалось сопоставить данные с tpl.ge: {exc}", state=state)
        except _PersonalInfoMissing as exc:
            state.status = CheckoutStatus.MISSING_PERSONAL_INFO
            state.missing_fields = exc.missing
            return CheckoutOutcome(
                reply_text=(
                    "Данные ТС и роли распознаны и сопоставлены с tpl.ge, но оформление "
                    "остановлено на этом шаге: tpl.ge требует данные, которых сейчас нет "
                    "ни в OCR, ни в системе — " + "; ".join(exc.missing) + "."
                ),
                state=state,
            )

        state.payload = payload

        try:
            await self._tpl_client.create_policy(payload)
        except TplGeClientError as exc:
            state.status = CheckoutStatus.FAILED
            state.failure_reason = FailureReason.TPL_GE_POLICY_CREATE_ERROR
            return CheckoutOutcome(reply_text=f"tpl.ge отклонил заявку: {exc}", state=state)

        state.status = CheckoutStatus.POLICY_CREATED
        self._persist_lock(state)

        try:
            redirect_url = await self._tpl_client.get_payment_redirect_url(
                u_id=checkout_id,
                bank=payment_bank,
                payer_title=payload.insurer_title,
                payer_identification_number=payload.insurer_identification_number,
            )
        except TplGeClientError as exc:
            state.status = CheckoutStatus.FAILED
            state.failure_reason = FailureReason.TPL_GE_REDIRECT_ERROR
            return CheckoutOutcome(
                reply_text=f"Заявка создана (uId={checkout_id}), но не удалось получить ссылку на оплату: {exc}",
                state=state,
            )

        state.payment_redirect_url = redirect_url
        state.status = CheckoutStatus.PAYMENT_REDIRECT_READY
        self._persist_lock(state)

        return await self._run_bank_gateway(state, redirect_url, payment_bank)

    async def _run_bank_gateway(
        self, state: CheckoutState, redirect_url: str, payment_bank: PaymentBank,
    ) -> CheckoutOutcome:
        state.status = CheckoutStatus.PAYMENT_IN_PROGRESS
        self._persist_lock(state)

        try:
            result = await self._bank_gateway.start(state, redirect_url)
        except BankGatewayError as exc:
            state.status = CheckoutStatus.FAILED
            state.failure_reason = FailureReason.BROWSER_CRASHED
            self._persist_lock(state)
            bank_name = _BANK_DISPLAY_NAME.get(payment_bank, payment_bank.value)
            return CheckoutOutcome(
                reply_text=(
                    f"Заявка создана (uId={state.id}), ссылка на оплату через {bank_name} готова, "
                    f"но автоматический банковский этап недоступен: {exc}"
                ),
                state=state,
            )
        except Exception:  # noqa: BLE001 — защитный барьер от неожиданной ошибки gateway
            logger.warning("Checkout %s: bank_gateway.start упал неожиданно", state.id)
            state.status = CheckoutStatus.FAILED
            state.failure_reason = FailureReason.BROWSER_CRASHED
            self._persist_lock(state)
            return CheckoutOutcome(
                reply_text=f"Заявка создана (uId={state.id}), но произошла техническая ошибка при оплате.",
                state=state,
            )

        return await self._apply_bank_result(state, result)

    async def _build_payload(self, checkout_id: str, effective: OcrResult) -> tuple[TplPolicyPayload, PaymentBank]:
        registration_number = sanitize_registration_number(effective.registration_number)
        frame_number = select_frame_number(effective)
        # Банк/период/дата начала — поля ИМЕННО ЭТОЙ заявки (см.
        # reader/ocr/models.py::OcrResult и CheckoutService docstring), не
        # конструкторные настройки — резолвятся здесь же, вместе с
        # остальной синхронной валидацией, до первого await.
        payment_bank = resolve_payment_bank(effective.payment_bank)
        policy_period = resolve_policy_period(effective.policy_period)
        start_date = resolve_period_start(effective.period_start)

        category_product = await self._reference_data.category_product(effective.category, policy_period)

        manufacturer = await self._reference_data.resolve_manufacturer(effective.manufacturer)
        if manufacturer is None:
            raise ReferenceDataError(f"Марка '{effective.manufacturer}' не найдена в справочнике tpl.ge")

        model = await self._reference_data.resolve_model(manufacturer.id, effective.model)
        if model is None:
            raise ReferenceDataError(
                f"Модель '{effective.model}' не найдена в справочнике tpl.ge для марки {manufacturer.name}"
            )

        resolution = await self._personal_info_provider.resolve(effective)
        if not resolution.is_complete:
            raise _PersonalInfoMissing(resolution.missing)
        info = resolution.info
        assert info is not None  # для type checker — is_complete уже это гарантирует

        payload = TplPolicyPayload(
            u_id=checkout_id,
            start_date=start_date,
            frame_number=frame_number,
            vehicle_category_id=category_product.vehicle_category_id,
            vehicle_registration_number=registration_number,
            vehicle_manufacturer_id=manufacturer.id,
            vehicle_manufacturer_name=manufacturer.name,
            vehicle_model_id=model.id,
            vehicle_model_name=model.name,
            product_id=category_product.product_id,
            insurer_title=effective.policyholder_full_name,
            insurer_identification_number=info.insurer.identification_number,
            insurer_email=info.insurer.email,
            insurer_phone=info.insurer.phone,
            insurer_citizenship_id=info.insurer.citizenship_id,
            # tpl.ge не подтвердил research'ом отдельные driverSameAsInsurer/
            # ownerSameAsInsurer поля в payload'е — при same_as=True реальный
            # payload дублирует данные страхователя в эти поля (см.
            # reader/checkout/models.py::TplPolicyPayload docstring).
            vehicle_owner_title=(
                effective.policyholder_full_name
                if effective.owner_same_as_policyholder
                else effective.owner_full_name
            ),
            vehicle_owner_identification_number=info.owner.identification_number,
            vehicle_owner_email=info.owner.email,
            vehicle_owner_phone=info.owner.phone,
            vehicle_owner_citizenship_id=info.owner.citizenship_id,
            vehicle_driver_title=(
                effective.policyholder_full_name
                if effective.driver_same_as_policyholder
                else effective.driver_full_name
            ),
            vehicle_driver_identification_number=info.driver.identification_number,
            vehicle_driver_email=info.driver.email,
            vehicle_driver_phone=info.driver.phone,
            vehicle_driver_citizenship_id=info.driver.citizenship_id,
            visitor_id=str(uuid.uuid4()),
        )
        return payload, payment_bank
