"""Источник данных, которые tpl.ge требует (личный номер, гражданство,
телефон, email для страхователя/водителя/собственника — см.
reader/checkout/models.py::PersonalInfo), но которых нет в
reader/ocr/models.py::OcrResult напрямую как готовых id/подтверждённых
значений.

Источники определены бизнесом (см. задачу):
- identification_number/citizenship — ОДНО значение с паспорта СТРАХОВАТЕЛЯ
  (OcrResult.passport_number/citizenship, см. reader/ocr/prompt.py),
  используется как identification_number/citizenship_id ДЛЯ ВСЕХ ТРЁХ РОЛЕЙ
  payload'а (insurer=driver=owner) — так решил бизнес для текущей итерации,
  раздельных номеров для водителя/собственника пока нет;
- phone/email — ОДНО и то же значение для всех заявок (см. задачу: "Для
  всех заявок"), из reader/settings.py::CheckoutSettings, не из OCR.

citizenship — свободный текст от OCR (название страны), поэтому
преобразуется в citizenship_id ЧЕРЕЗ реальный справочник tpl.ge (см.
reader/checkout/reference_data.py::resolve_country) — никакой id здесь не
хардкодится (см. задачу: "не придумывай ID")."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from reader.checkout.models import PersonalInfo, RolePersonalInfo
from reader.checkout.reference_data import TplReferenceDataClient
from reader.ocr.models import OcrResult


@dataclass(frozen=True)
class PersonalInfoResolution:
    """Ровно одно из двух: либо `info` (все данные найдены), либо
    непустой `missing` (человекочитаемое описание того, чего не хватает) —
    reader/checkout/service.py не пытается угадывать, что делать с частичным
    результатом."""

    info: RolePersonalInfo | None
    missing: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        return self.info is not None


class PersonalInfoProvider(Protocol):
    async def resolve(self, effective: OcrResult) -> PersonalInfoResolution: ...


class OcrPersonalInfoProvider:
    """Реальная (production) реализация — см. docstring модуля про то, откуда
    берётся каждое поле. Блокирует checkout (missing непустой), если:
    - паспорт страхователя не распознан (passport_number is None);
    - гражданство не распознано (citizenship is None);
    - гражданство распознано, но не найдено в справочнике tpl.ge
      (reference_data.resolve_country() вернул None) — например, опечатка
      или страна, которой нет в справочнике tpl.ge."""

    def __init__(self, *, reference_data: TplReferenceDataClient, phone: str, email: str):
        self._reference_data = reference_data
        self._phone = phone
        self._email = email

    async def resolve(self, effective: OcrResult) -> PersonalInfoResolution:
        missing: list[str] = []

        if not effective.passport_number:
            missing.append("Страхователь: номер паспорта не распознан")

        citizenship_id: int | None = None
        if not effective.citizenship:
            missing.append("Страхователь: гражданство не распознано")
        else:
            country = await self._reference_data.resolve_country(effective.citizenship)
            if country is None:
                missing.append(
                    f"Страхователь: гражданство '{effective.citizenship}' не найдено "
                    "в справочнике tpl.ge"
                )
            else:
                citizenship_id = country.id

        if missing:
            return PersonalInfoResolution(info=None, missing=tuple(missing))

        # citizenship_id гарантированно не None здесь — иначе выше добавили
        # бы запись в missing и вернули бы раньше.
        assert citizenship_id is not None
        person = PersonalInfo(
            identification_number=effective.passport_number,
            citizenship_id=citizenship_id,
            phone=self._phone,
            email=self._email,
        )
        # Одно и то же значение для всех трёх ролей (см. docstring модуля) —
        # намеренно НЕ три разных объекта с одинаковыми полями, а один и тот
        # же PersonalInfo, чтобы не создавать видимость, будто у ролей может
        # быть разный identification_number/citizenship в этой реализации.
        return PersonalInfoResolution(info=RolePersonalInfo(insurer=person, driver=person, owner=person))


class NoPersonalInfoProvider:
    """Fail-closed заглушка — НЕ используется production-wiring'ом (см.
    reader/main.py::build_checkout_components, который передаёт
    OcrPersonalInfoProvider), но остаётся default-значением конструктора
    CheckoutService (см. reader/checkout/service.py) на случай, если
    вызывающий код не передал provider явно — тогда checkout честно
    блокируется, а не падает с ошибкой атрибута."""

    async def resolve(self, effective: OcrResult) -> PersonalInfoResolution:
        missing = (
            "Страхователь: личный номер, гражданство, телефон, email",
            "Водитель: личный номер, гражданство, телефон, email",
            "Собственник: личный номер, гражданство, телефон, email",
        )
        return PersonalInfoResolution(info=None, missing=missing)
