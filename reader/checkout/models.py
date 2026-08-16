"""Модели/состояние checkout tpl.ge — отдельно от reader/ocr/* (см. задачу:
"не смешивай checkout-код с OCR service"). Единственная точка пересечения с
OCR — reader.ocr.models.OcrResult как тип для уже распознанных/исправленных
полей (см. reader/checkout/parser.py), не более того.

Поля TplPolicyPayload и их порядок/названия — НЕ придуманы, а зафиксированы
дословно во время browser research реального запроса
POST https://web-back.tpl.ge/api/policies (см. задачу/отчёт research):
{
  "uId": "...", "startDate": "...", "frameNumber": "...",
  "vehicleCategoryId": 7, "vehicleRegistrationNumber": "...",
  "vehicleManufacturerId": 147, "vehicleManufacturerName": "TOYOTA",
  "vehicleModelId": 10574, "vehicleModelName": "TOYOTA CAMRY",
  "productId": 2,
  "insurerType": "I", "insurerTitle": "...", "insurerIdentificationNumber": "...",
  "insurerEmail": "...", "insurerPhone": "...", "insurerCitizenshipId": 1,
  "vehicleOwnerType": "I", "vehicleOwnerTitle": "...", ...
  "vehicleDriverType": "I", "vehicleDriverTitle": "...", ...
  "borderCrossId": null, "visitorId": "...", "lang": "ka"
}
Только insurerType/vehicleOwnerType/vehicleDriverType="I" (физлицо)
поддерживаются — значение для юрлица не подтверждено research'ем и не
угадывается.

Отдельных driverSameAsInsurer/ownerSameAsInsurer (или похожих bool-полей) в
реальном payload'е research НЕ зафиксировал — vehicleOwner*/vehicleDriver*
это всегда полный, дублированный набор полей. Поэтому same_as-семантика
(см. reader/ocr/models.py::OcrResult.driver_same_as_policyholder/
owner_same_as_policyholder) реализована на уровне маппинга (см.
reader/checkout/service.py::_build_payload) — при same_as=True в эти поля
подставляются те же данные, что и у страхователя, а не изобретается
отдельное bool-поле, которого нет в реальном контракте."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from reader.ocr.models import OcrResult

# Единственный подтверждённый research'ем тип роли в payload tpl.ge —
# физическое лицо. "L" (юрлицо) в реальном payload не зафиксирован, поэтому
# намеренно не поддерживается (см. docstring модуля и reader/checkout/mapping.py).
INDIVIDUAL_PERSON_TYPE = "I"


class PaymentBank(str, Enum):
    """Банк-эквайер tpl.ge, жёстко заданный конфигом (checkout.payment_bank,
    см. задачу: "банк оператор не выбирает"). Единственное подтверждённое
    browser research'ем значение — Bank of Georgia (URL-путь эквайера
    "/ecommerce/bog", см. reader/checkout/tpl_client.py). Liberty Bank виден
    в UI tpl.ge, но его путь эквайера НЕ исследовался — добавлять его сюда
    без отдельного research means угадывать API, поэтому его здесь нет."""

    BANK_OF_GEORGIA = "bank_of_georgia"


class CheckoutStatus(str, Enum):
    COLLECTING = "collecting"
    MISSING_VEHICLE_DATA = "missing_vehicle_data"
    MAPPING_FAILED = "mapping_failed"
    MISSING_PERSONAL_INFO = "missing_personal_info"
    POLICY_CREATED = "policy_created"
    PAYMENT_REDIRECT_READY = "payment_redirect_ready"
    # Реальный банковский этап (см. reader/checkout/payment_gateway.py) —
    # браузер уже открыт/открывается и взаимодействует с mpi.gc.ge. Занимает
    # оба "прогонных" промежутка задачи: до OTP и после отправки кода, пока
    # ждём от банка финальный результат (см. задачу: "PAYMENT_REDIRECT_READY
    # → PAYMENT_IN_PROGRESS → WAITING_FOR_CONFIRMATION_CODE →
    # PAYMENT_IN_PROGRESS → COMPLETED").
    PAYMENT_IN_PROGRESS = "payment_in_progress"
    WAITING_FOR_CONFIRMATION_CODE = "waiting_for_confirmation_code"
    COMPLETED = "completed"
    FAILED = "failed"


class FailureReason(str, Enum):
    """Классификация CheckoutStatus.FAILED — см. задачу: "ошибочные состояния
    должны корректно покрывать как минимум..." Не отдельные CheckoutStatus
    (не хотим взрывать enum статусов) — отдельное поле
    CheckoutState.failure_reason рядом с уже человекочитаемым reply_text."""

    CARD_DECLINED = "card_declined"
    INVALID_OTP = "invalid_otp"  # терминально неверный код (попытки исчерпаны банком)
    OTP_EXPIRED = "otp_expired"
    PAYMENT_TIMEOUT = "payment_timeout"
    BROWSER_CRASHED = "browser_crashed"
    UNEXPECTED_BANK_PAGE = "unexpected_bank_page"
    PAYMENT_CANCELLED = "payment_cancelled"
    STATUS_MISMATCH = "status_mismatch"  # tpl.ge/банк дали разные финальные статусы
    # Наша собственная граница исследования (см. итоговый research-отчёт):
    # форма карты подтверждена и заполняется, но что происходит ПОСЛЕ submit
    # (OTP/успех/отказ — их DOM/redirect) НЕ подтверждено — код сознательно
    # останавливается ДО submit, а не угадывает поведение банка дальше.
    UNCONFIRMED_POST_SUBMIT_FLOW = "unconfirmed_post_submit_flow"
    # tpl.ge-этап (до банка) — тело/redirect запроса отклонены (см.
    # reader/checkout/tpl_client.py) — единственные FAILED-причины, которые
    # НЕ означают, что был реальный банковский платёжный запрос, поэтому не
    # блокируют повторный "pay" (см. is_locked_status ниже).
    TPL_GE_POLICY_CREATE_ERROR = "tpl_ge_policy_create_error"
    TPL_GE_REDIRECT_ERROR = "tpl_ge_redirect_error"
    # Восстановление после перезапуска процесса, см. reader/checkout/service.py
    # и reader/checkout/lock_repository.py — сессия браузера физически не
    # могла пережить перезапуск, статус реальной оплаты неизвестен.
    RESTART_RECOVERY_UNKNOWN_STATUS = "restart_recovery_unknown_status"


# FAILED-причины, которые НЕ означают, что был реальный запрос к банку —
# только к самому tpl.ge (создание заявки/получение платёжной ссылки).
# Единственные, после которых повторный "pay" безопасен (см.
# is_locked_status) — во всех остальных FAILED-случаях либо точно был
# банковский запрос, либо мы не знаем наверняка (после перезапуска), и
# автоматический повтор рискует задвоить платёж.
_RETRIABLE_FAILURE_REASONS = frozenset(
    {FailureReason.TPL_GE_POLICY_CREATE_ERROR, FailureReason.TPL_GE_REDIRECT_ERROR}
)

# Статусы, с которых оформление уже "занято" этим OCR-сообщением — повторный
# "pay"/edited-reply на то же сообщение должен быть отклонён как дубль (см.
# задачу: "защита от двойного pay/двойного создания policy", "не разрешать
# второму pay создавать новую policy, пока checkout уже находится в payment
# flow"). MISSING_*/MAPPING_FAILED сюда намеренно не входят: это точки, где
# заявка ещё не ушла на tpl.ge и повторная попытка (например, после того как
# оператор пришлёт исправленные данные) должна быть возможна.
_LOCKED_STATUSES = frozenset(
    {
        CheckoutStatus.POLICY_CREATED,
        CheckoutStatus.PAYMENT_REDIRECT_READY,
        CheckoutStatus.PAYMENT_IN_PROGRESS,
        CheckoutStatus.WAITING_FOR_CONFIRMATION_CODE,
        CheckoutStatus.COMPLETED,
    }
)


def is_locked_status(status: CheckoutStatus, failure_reason: FailureReason | None = None) -> bool:
    """FAILED — заблокирован, если failure_reason НЕ входит в
    _RETRIABLE_FAILURE_REASONS (см. её докстрок) — то есть по умолчанию
    заблокирован (fail-closed): неизвестная/отсутствующая причина считается
    "мог быть реальный банковский запрос", а не наоборот."""
    if status in _LOCKED_STATUSES:
        return True
    if status == CheckoutStatus.FAILED:
        return failure_reason not in _RETRIABLE_FAILURE_REASONS
    return False


@dataclass
class PersonalInfo:
    """Данные, которые tpl.ge требует для каждой из трёх ролей (страхователь/
    водитель/владелец) — личный номер, гражданство (id страны tpl.ge, см.
    reader/checkout/reference_data.py), телефон, email. См.
    reader/checkout/personal_info.py::OcrPersonalInfoProvider — источник:
    identification_number/citizenship_id всегда с паспорта страхователя
    (OcrResult.passport_number/citizenship); phone/email — из
    OcrResult.phone/email (checkout settings по умолчанию, correction-reply
    может их изменить). Водитель/владелец используют то же самое значение,
    только когда driver_same_as_policyholder/owner_same_as_policyholder —
    иначе checkout блокируется, а не изобретает отдельные данные."""

    identification_number: str
    citizenship_id: int
    phone: str
    email: str


@dataclass
class RolePersonalInfo:
    insurer: PersonalInfo
    driver: PersonalInfo
    owner: PersonalInfo


@dataclass
class TplPolicyPayload:
    """Тело POST https://web-back.tpl.ge/api/policies — 1:1 с зафиксированным
    во время research запросом (см. docstring модуля)."""

    u_id: str
    start_date: str  # "YYYY-MM-DD"
    frame_number: str  # VIN или номер шасси — то же поле, что и вживую (см. research)
    vehicle_category_id: int
    vehicle_registration_number: str
    vehicle_manufacturer_id: int
    vehicle_manufacturer_name: str
    vehicle_model_id: int
    vehicle_model_name: str
    product_id: int
    insurer_title: str
    insurer_identification_number: str
    insurer_email: str
    insurer_phone: str
    insurer_citizenship_id: int
    vehicle_owner_title: str
    vehicle_owner_identification_number: str
    vehicle_owner_email: str
    vehicle_owner_phone: str
    vehicle_owner_citizenship_id: int
    vehicle_driver_title: str
    vehicle_driver_identification_number: str
    vehicle_driver_email: str
    vehicle_driver_phone: str
    vehicle_driver_citizenship_id: int
    visitor_id: str
    lang: str = "ka"

    def to_json(self) -> dict:
        """Точное имя и порядок ключей — как в реальном запросе (см. docstring
        модуля) — тело намеренно не полагается на порядок в самом dict (JSON
        не гарантирует его), но совпадение с реальным payload упрощает
        сверку в логах/тестах."""
        return {
            "uId": self.u_id,
            "startDate": self.start_date,
            "frameNumber": self.frame_number,
            "vehicleCategoryId": self.vehicle_category_id,
            "vehicleRegistrationNumber": self.vehicle_registration_number,
            "vehicleManufacturerId": self.vehicle_manufacturer_id,
            "vehicleManufacturerName": self.vehicle_manufacturer_name,
            "vehicleModelId": self.vehicle_model_id,
            "vehicleModelName": self.vehicle_model_name,
            "productId": self.product_id,
            "insurerType": INDIVIDUAL_PERSON_TYPE,
            "insurerTitle": self.insurer_title,
            "insurerIdentificationNumber": self.insurer_identification_number,
            "insurerEmail": self.insurer_email,
            "insurerPhone": self.insurer_phone,
            "insurerCitizenshipId": self.insurer_citizenship_id,
            "vehicleOwnerType": INDIVIDUAL_PERSON_TYPE,
            "vehicleOwnerTitle": self.vehicle_owner_title,
            "vehicleOwnerIdentificationNumber": self.vehicle_owner_identification_number,
            "vehicleOwnerEmail": self.vehicle_owner_email,
            "vehicleOwnerPhone": self.vehicle_owner_phone,
            "vehicleOwnerCitizenshipId": self.vehicle_owner_citizenship_id,
            "vehicleDriverType": INDIVIDUAL_PERSON_TYPE,
            "vehicleDriverTitle": self.vehicle_driver_title,
            "vehicleDriverIdentificationNumber": self.vehicle_driver_identification_number,
            "vehicleDriverEmail": self.vehicle_driver_email,
            "vehicleDriverPhone": self.vehicle_driver_phone,
            "vehicleDriverCitizenshipId": self.vehicle_driver_citizenship_id,
            "borderCrossId": None,
            "visitorId": self.visitor_id,
            "lang": self.lang,
        }


@dataclass
class CheckoutState:
    """Состояние ОДНОГО checkout — привязано к:
    - Telegram-чату (chat_id) и исходному OCR-сообщению (ocr_message_id,
      "Распознано: ..." — именно на него отвечает оператор "pay"/исправлениями);
    - оператору (operator_user_id);
    - собственному id (uId, тот же самый, что уходит в TplPolicyPayload.u_id —
      см. research: backend НИЧЕГО не возвращает в ответ на POST /api/policies,
      клиент сам генерирует uId ДО запроса, см. reader/checkout/tpl_client.py);
    - текущему статусу (CheckoutStatus).

    Хранится в reader/checkout/store.py::CheckoutStore (in-memory — см. задачу:
    код подтверждения "не хранить дольше, чем требуется для текущего
    checkout" — вообще не сохраняется как атрибут, см. reader/checkout/service.py)."""

    id: str  # = TplPolicyPayload.u_id после успешной сборки payload
    chat_id: int
    ocr_message_id: int
    operator_user_id: int | None
    status: CheckoutStatus
    effective_fields: OcrResult
    created_at: datetime
    missing_fields: tuple[str, ...] = field(default_factory=tuple)
    payload: TplPolicyPayload | None = None
    payment_redirect_url: str | None = None
    # id Telegram-сообщения "Введите код подтверждения ..." — по нему
    # find_awaiting_code() ищет состояние, когда оператор отвечает кодом
    # (см. reader/checkout/store.py и reader/checkout/telegram_integration.py).
    # Переприсваивается при повторном запросе кода (см. неверный OTP + retry).
    code_prompt_message_id: int | None = None
    # Классификация CheckoutStatus.FAILED (см. FailureReason выше) — None,
    # пока checkout не завершился с ошибкой.
    failure_reason: FailureReason | None = None
    # Номер полиса/order id банка — заполняется ТОЛЬКО если получен из
    # подтверждённого источника (см. задачу: "добавь номер полиса только
    # если он действительно получен из подтверждённого источника") — на
    # данный момент ни один такой источник не подтверждён research'ом,
    # поэтому всегда None (см. итоговый research-отчёт).
    order_reference: str | None = None
