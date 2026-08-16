"""Правила сопоставления OcrResult -> данные для tpl.ge, зафиксированные
browser research'ом (см. задачу и итоговый research-отчёт) — НИЧЕГО здесь
не угадано:

- category -> tpl.ge vehicleType/vehicleCategoryId: сопоставление НЕ по
  захардкоженным числовым id (они могут смениться на стороне tpl.ge), а по
  суффиксу поля `key` в ответе GET /api/core/categories?embed=products
  (см. reader/checkout/reference_data.py) — во время research эти суффиксы
  были: .../PassengerCar, .../Motorcycle, .../Trailer (bus/truck/special
  vehicle тоже существуют на сайте, но не входят в enum нашего OCR — см.
  reader/ocr/service.py::_VehicleFieldsSchema.category — соответственно
  здесь не поддерживаются).

- VIN/номер шасси -> единственное поле `frameNumber`: research подтвердил,
  что форма tpl.ge физически хранит ОДНО значение с переключателем типа
  (VIN/шасси), а не два независимых поля. Если OCR распознал оба значения,
  используется VIN (приоритет), номер шасси — fallback, если VIN не
  распознан. Это осознанное умолчание разработчика (не бизнес-значение
  вроде личных данных) — если понадобится другое поведение, это одна
  функция (_select_frame_number).

- сарегистрационный номер (registrationNumber) -> tpl.ge принимает только
  латиницу и цифры, БЕЗ дефисов/пробелов (см. research: форма tpl.ge молча
  блокирует submit, если в номере есть дефис).

- payment_bank ("bog"/"liberty" в Telegram-поле) -> reader/checkout/models.py
  ::PaymentBank: "bog" — единственный подтверждённый research'ом эквайер
  (Bank of Georgia, см. reader/checkout/tpl_client.py). "liberty" — реальный
  выбор в UI tpl.ge, добавлен как PaymentBank.LIBERTY_BANK, но его путь
  эквайера НЕ подтверждён research'ом — тот же _BANK_PATH_SEGMENT в
  tpl_client.py (не тронут этой задачей) честно откажет на этапе получения
  ссылки на оплату, а не угадывает URL.

- policy_period ("15"/"30"/"90" в Telegram-поле) -> "<N>-D" — тот же формат,
  что уже принимает reader/checkout/reference_data.py::parse_policy_period
  (никакой новой бизнес-логики периода здесь не заведено, только форматирование
  строки). "1-Y" в Telegram-flow сознательно не поддерживается (см. задачу).

- period_start ("ДД.ММ.ГГГГ" в Telegram-поле, см. reader/checkout/parser.py
  про валидацию формата) -> "YYYY-MM-DD", формат, который реально ожидает
  TplPolicyPayload.start_date (см. reader/checkout/models.py)."""

from __future__ import annotations

import re
from datetime import datetime

from reader.checkout.models import PaymentBank
from reader.ocr.models import OcrResult

# suffix ключа `key` в /api/core/categories?embed=products -> наш category enum.
CATEGORY_KEY_SUFFIX = {
    "passenger_car": "/PassengerCar",
    "motorcycle": "/Motorcycle",
    "trailer": "/Trailer",
}

_REGISTRATION_NUMBER_RE = re.compile(r"^[A-Za-z0-9]+$")

# Telegram-значение "Банк:" -> реальный PaymentBank (см. docstring модуля).
_PAYMENT_BANK_BY_VALUE = {
    "bog": PaymentBank.BANK_OF_GEORGIA,
    "liberty": PaymentBank.LIBERTY_BANK,
}

# Telegram-значение "Период:" -> tpl.ge periodType (см. docstring модуля) —
# "1-Y" здесь намеренно нет (не поддерживается в Telegram-flow).
_POLICY_PERIOD_DAYS = frozenset({"15", "30", "90"})

_PERIOD_START_INPUT_FORMAT = "%d.%m.%Y"

# Поля OcrResult, без которых заявку в tpl.ge собрать нельзя в принципе,
# ВСЕГДА (без условий) — (label, attr); vin/chassis_number проверяются
# отдельно (см. required_vehicle_fields_missing) — валиден любой ОДИН из
# них. Водитель/владелец сюда не входят — они требуются только когда
# соответствующий same_as_policyholder=False (см. ниже) — ~99% заявок эта
# роль совпадает со страхователем и отдельного ФИО не требует.
_REQUIRED_VEHICLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Страхователь", "policyholder_full_name"),
    ("Категория", "category"),
    ("Марка", "manufacturer"),
    ("Модель", "model"),
    ("Госномер", "registration_number"),
    ("Банк", "payment_bank"),
    ("Период", "policy_period"),
    ("Начало периода", "period_start"),
)


class MappingError(Exception):
    """Данные распознаны, но не могут быть однозначно сопоставлены с
    требованиями tpl.ge (пустое обязательное поле, недопустимый формат
    номера и т.п.) — str(exc) показывается оператору."""


def required_vehicle_fields_missing(effective: OcrResult) -> list[str]:
    """Человекочитаемый список (см. reader/ocr/models.py::REPLY_FIELD_LABELS
    метки) полей, без которых нельзя даже начать сопоставление с tpl.ge —
    ПУСТОЙ список означает "можно продолжать" (см.
    reader/checkout/service.py). Водитель/владелец — required только когда
    оператор явно указал "... = страхователь: -" (иначе checkout использует
    те же данные, что у страхователя, см. reader/checkout/personal_info.py)."""
    missing = [label for label, attr in _REQUIRED_VEHICLE_FIELDS if not getattr(effective, attr)]
    if not effective.vin and not effective.chassis_number:
        missing.append("VIN/Номер шасси")
    if not effective.driver_same_as_policyholder and not effective.driver_full_name:
        missing.append("Водитель")
    if not effective.owner_same_as_policyholder and not effective.owner_full_name:
        missing.append("Владелец")
    return missing


def select_frame_number(effective: OcrResult) -> str:
    """VIN в приоритете, номер шасси — fallback (см. docstring модуля).
    Вызывающий код (reader/checkout/service.py) обязан убедиться, что хотя
    бы одно из двух значений есть, ДО вызова этой функции — иначе
    MappingError."""
    if effective.vin:
        return effective.vin
    if effective.chassis_number:
        return effective.chassis_number
    raise MappingError("Не распознаны ни VIN, ни номер шасси.")


def sanitize_registration_number(raw: str) -> str:
    """AA-001-AA -> AA001AA (см. docstring модуля про требования tpl.ge к
    формату). MappingError, если после удаления пробелов/дефисов остаются
    символы вне [A-Za-z0-9] (например, кириллица) — недостаточно надёжно
    молча "исправлять" такое, лучше явно попросить оператора прислать
    госномер латиницей."""
    cleaned = re.sub(r"[\s-]", "", raw or "")
    if not cleaned or not _REGISTRATION_NUMBER_RE.match(cleaned):
        raise MappingError(
            f"Госномер '{raw}' содержит недопустимые символы — tpl.ge принимает "
            "только латиницу и цифры."
        )
    return cleaned.upper()


def resolve_payment_bank(value: str) -> PaymentBank:
    """"bog"/"liberty" -> PaymentBank (см. docstring модуля). Значение уже
    провалидировано reader/checkout/parser.py — MappingError здесь чисто
    defense-in-depth (тот же приём, что и у select_frame_number), в
    нормальном потоке не срабатывает."""
    bank = _PAYMENT_BANK_BY_VALUE.get(value)
    if bank is None:
        raise MappingError(
            f"Банк '{value}' не поддержан — допустимо: "
            + ", ".join(sorted(_PAYMENT_BANK_BY_VALUE))
        )
    return bank


def resolve_policy_period(value: str) -> str:
    """"15"/"30"/"90" -> "15-D"/"30-D"/"90-D" — тот же формат, что уже
    принимает reader/checkout/reference_data.py::parse_policy_period (см.
    docstring модуля). Значение уже провалидировано parser.py — MappingError
    здесь defense-in-depth."""
    if value not in _POLICY_PERIOD_DAYS:
        raise MappingError(
            f"Период '{value}' не поддержан — допустимо: "
            + ", ".join(sorted(_POLICY_PERIOD_DAYS, key=int))
        )
    return f"{value}-D"


def resolve_period_start(value: str) -> str:
    """"ДД.ММ.ГГГГ" -> "YYYY-MM-DD" (формат TplPolicyPayload.start_date, см.
    docstring модуля). Формат уже провалидирован parser.py — MappingError
    здесь defense-in-depth (тот же приём, что и у остальных resolve_*/
    select_frame_number)."""
    try:
        # Календарная дата (ДД.ММ.ГГГГ), не момент времени — часовой пояс
        # здесь неприменим, naive datetime намеренный.
        parsed = datetime.strptime(value, _PERIOD_START_INPUT_FORMAT)  # noqa: DTZ007
    except ValueError as exc:
        raise MappingError(
            f"Начало периода '{value}' не является датой в формате ДД.ММ.ГГГГ"
        ) from exc
    return parsed.date().isoformat()
