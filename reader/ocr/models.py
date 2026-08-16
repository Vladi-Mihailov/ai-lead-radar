from dataclasses import dataclass

# (подпись в Telegram-сообщении, атрибут OcrResult) — единственный источник
# правды для формата "Распознано: ..." (см. reader/commands/insurance_ocr.py
# ::_format_result) И для его обратного разбора в reader/checkout/parser.py
# (там же разбирается reply оператора с исправленными полями — тот же
# формат, см. задачу про checkout). Живёт здесь, а не в insurance_ocr.py,
# именно потому что нужен ОБОИМ модулям, а checkout не должен зависеть от
# reader/commands/insurance_ocr.py (см. задачу: "не смешивай checkout-код с
# OCR service").
#
# "Номер паспорта"/"Гражданство" — добавлены для checkout tpl.ge (см. задачу:
# tpl.ge требует personal_number/citizenship страхователя — источник теперь
# определён: паспорт/ID страхователя, см. reader/ocr/prompt.py). Расположены
# именно между "Страхователь" и "Категория" — таков зафиксированный задачей
# формат Telegram-сообщения.
REPLY_FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("Собственник", "owner_full_name"),
    ("Водитель", "driver_full_name"),
    ("Страхователь", "policyholder_full_name"),
    ("Номер паспорта", "passport_number"),
    ("Гражданство", "citizenship"),
    ("Категория", "category"),
    ("Марка", "manufacturer"),
    ("Модель", "model"),
    ("VIN", "vin"),
    ("Номер шасси", "chassis_number"),
    ("Госномер", "registration_number"),
)


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

    - passport_number — номер паспорта/ID СТРАХОВАТЕЛЯ, ТОЛЬКО из паспорта/
      ID физического лица (см. reader/ocr/prompt.py) — ОТДЕЛЬНЫЙ документ от
      водительского удостоверения/техпаспорта. Ни в коем случае не VIN,
      номер водительского удостоверения, номер шасси или госномер ТС (см.
      задачу: "не путай с другими номерами документов"). Нужен для checkout
      tpl.ge (см. reader/checkout/personal_info.py) — используется как
      identification_number для ВСЕХ трёх ролей payload'а (страхователь/
      водитель/собственник), см. reader/checkout/personal_info.py про это
      бизнес-решение.
    - citizenship — гражданство, ТОЛЬКО из того же паспорта/ID страхователя,
      что и passport_number. Ожидаемый формат — название страны на английском
      (см. reader/ocr/prompt.py) — сопоставляется со справочником
      tpl.ge/api/core/countries на уровне reader/checkout/reference_data.py,
      никакой числовой id здесь не хранится.

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
    passport_number: str | None
    citizenship: str | None
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
                self.passport_number, self.citizenship,
                self.category, self.registration_number, self.vin, self.chassis_number,
                self.manufacturer, self.model,
            )
            if value
        )
