from dataclasses import dataclass


@dataclass(frozen=True)
class OcrResult:
    """Результат распознавания документов автомобиля — по аналогии со
    схемой auto-insurance (app/ocr/models.py::OcrResult), но с тремя
    ролями ФИО вместо одного full_name (см. задачу: три РАЗНЫЕ бизнес-роли
    с РАЗНЫМИ источниками-документами, а не один и тот же человек по
    умолчанию):

    - owner_full_name — ТОЛЬКО из техпаспорта; null, если собственник в
      техпаспорте — юридическое лицо (см. reader/ocr/prompt.py).
    - driver_full_name — ТОЛЬКО из водительского удостоверения.
    - policyholder_full_name — ТОЛЬКО из водительского удостоверения,
      извлекается НЕЗАВИСИМО от driver_full_name (для текущего сценария
      обычно совпадает с ним по значению, но это два отдельных поля без
      кода-уровня fallback — см. reader/commands/insurance_ocr.py, там нет
      ни одного места, которое присваивало бы одному полю значение
      другого).

    category — категория ТС (passenger_car/motorcycle/trailer/null),
    определяется ТОЛЬКО по техпаспорту (тип/назначение/марка/модель и
    другие признаки самого документа — см. reader/ocr/prompt.py), НЕ
    подставляется программно по умолчанию: null означает, что по документу
    нельзя определить категорию достаточно надёжно, а не "скорее всего
    passenger_car". Ограничено фиксированным enum'ом на уровне Structured
    Outputs schema (см. reader/ocr/service.py::_VehicleFieldsSchema) —
    модель физически не может вернуть никакое другое значение.

    registration_number/vin/chassis_number/manufacturer/model — ТОЛЬКО из
    техпаспорта, не изменились по смыслу с прошлой версии schema.

    Каждое поле — None означает "не найдено/не распознано/не тот
    источник", а не предположение (см. reader/ocr/prompt.py — модель прямо
    просят не угадывать и не смешивать документы). Ничего не
    нормализуется/не сверяется со справочником (в отличие от
    auto-insurance/app/ocr/parser.py — это web/checkout-специфика, здесь
    не нужна)."""

    owner_full_name: str | None
    driver_full_name: str | None
    policyholder_full_name: str | None
    category: str | None
    registration_number: str | None
    vin: str | None
    chassis_number: str | None
    manufacturer: str | None
    model: str | None

    @property
    def fields_found_count(self) -> int:
        return sum(
            1
            for value in (
                self.owner_full_name, self.driver_full_name, self.policyholder_full_name,
                self.category, self.registration_number, self.vin, self.chassis_number,
                self.manufacturer, self.model,
            )
            if value
        )
