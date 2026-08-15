from dataclasses import dataclass


@dataclass(frozen=True)
class OcrResult:
    """Результат распознавания документа(ов) автомобиля — по аналогии со
    схемой auto-insurance (app/ocr/models.py::OcrResult), но с добавленным
    full_name (см. задачу: сознательное отличие от клиентского checkout-флоу
    auto-insurance, где ФИО намеренно не извлекается).

    Каждое поле — None означает "не найдено/не распознано", а не
    предположение (см. reader/ocr/prompt.py — модель прямо просят не
    угадывать). Ничего не нормализуется/не сверяется со справочником (в
    отличие от auto-insurance/app/ocr/parser.py — это web/checkout-специфика,
    здесь не нужна)."""

    registration_number: str | None
    vin: str | None
    chassis_number: str | None
    manufacturer: str | None
    model: str | None
    full_name: str | None

    @property
    def fields_found_count(self) -> int:
        return sum(
            1
            for value in (
                self.registration_number, self.vin, self.chassis_number,
                self.manufacturer, self.model, self.full_name,
            )
            if value
        )
