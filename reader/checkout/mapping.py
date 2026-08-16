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
  блокирует submit, если в номере есть дефис)."""

from __future__ import annotations

import re

from reader.ocr.models import OcrResult

# suffix ключа `key` в /api/core/categories?embed=products -> наш category enum.
CATEGORY_KEY_SUFFIX = {
    "passenger_car": "/PassengerCar",
    "motorcycle": "/Motorcycle",
    "trailer": "/Trailer",
}

_REGISTRATION_NUMBER_RE = re.compile(r"^[A-Za-z0-9]+$")

# Поля OcrResult, без которых заявку в tpl.ge собрать нельзя в принципе —
# (label, attr); vin/chassis_number проверяются отдельно (см.
# required_vehicle_fields_missing) — валиден любой ОДИН из них.
_REQUIRED_VEHICLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Собственник", "owner_full_name"),
    ("Водитель", "driver_full_name"),
    ("Страхователь", "policyholder_full_name"),
    ("Категория", "category"),
    ("Марка", "manufacturer"),
    ("Модель", "model"),
    ("Госномер", "registration_number"),
)


class MappingError(Exception):
    """Данные распознаны, но не могут быть однозначно сопоставлены с
    требованиями tpl.ge (пустое обязательное поле, недопустимый формат
    номера и т.п.) — str(exc) показывается оператору."""


def required_vehicle_fields_missing(effective: OcrResult) -> list[str]:
    """Человекочитаемый список (см. reader/ocr/models.py::REPLY_FIELD_LABELS
    метки) полей, без которых нельзя даже начать сопоставление с tpl.ge —
    ПУСТОЙ список означает "можно продолжать" (см.
    reader/checkout/service.py)."""
    missing = [label for label, attr in _REQUIRED_VEHICLE_FIELDS if not getattr(effective, attr)]
    if not effective.vin and not effective.chassis_number:
        missing.append("VIN/Номер шасси")
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
