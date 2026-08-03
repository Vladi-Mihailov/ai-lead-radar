"""Преобразование JSON-ответа police.ge (endpoint searchByAuto) в
ParsedFineRecord. Никакой сети/httpx здесь нет — вход уже готовый dict,
выход — доменные объекты. Полностью детерминировано, тестируется без сети.
"""

import hashlib
from datetime import date
from typing import Any

from reader.fines.models import ParsedFineRecord


class FineParseError(ValueError):
    """Ответ police.ge не соответствует ожидаемой структуре успешного
    результата (success:true, data.results — список)."""


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def compute_fingerprint(
    *,
    external_fine_id: str | None,
    violation_date: date | None,
    amount: float | None,
) -> str:
    """SHA256 только по стабильным полям записи — protocolNo/violationDate/
    protocolAmount, а не по всему JSON: порядок ключей в ответе сайта не
    должен влиять на результат, а поля вроде remainingDays (меняется каждый
    день) не должны создавать "новый" штраф при каждой проверке."""
    raw = "|".join(
        [
            external_fine_id or "",
            violation_date.isoformat() if violation_date else "",
            str(amount) if amount is not None else "",
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_entry(entry: dict[str, Any], *, fallback_car_number: str) -> ParsedFineRecord:
    external_fine_id = entry.get("protocolNo")
    violation_date = _parse_date(entry.get("violationDate"))
    amount = entry.get("protocolAmount")
    delivered_date = _parse_date(entry.get("activeDate"))

    return ParsedFineRecord(
        car_number=entry.get("protocolAuto") or fallback_car_number,
        external_fine_id=external_fine_id,
        penalty_date=_parse_date(entry.get("protocolDate")),
        due_date=_parse_date(entry.get("lastDate")),
        delivered_status="Вручено" if delivered_date else "Не вручено",
        fingerprint=compute_fingerprint(
            external_fine_id=external_fine_id,
            violation_date=violation_date,
            amount=amount,
        ),
        raw_data=entry,
    )


def parse_search_response(raw: Any, *, car_number: str) -> list[ParsedFineRecord]:
    """raw — уже распарсенный JSON (dict) от police.ge searchByAuto с
    success:true. Ответы с success:false/невалидным JSON обрабатывает
    PoliceGeSession (повторный GET+POST) — сюда такие не должны попадать;
    если всё же попали, это тоже ошибка формата, не молчаливый пропуск."""
    if not isinstance(raw, dict) or raw.get("success") is not True:
        raise FineParseError("Ожидался ответ police.ge с success:true")

    data = raw.get("data")
    if not isinstance(data, dict):
        raise FineParseError("Некорректный формат ответа police.ge: data не объект")

    results = data.get("results")
    if not isinstance(results, list):
        raise FineParseError("Некорректный формат ответа police.ge: data.results не список")

    return [
        _parse_entry(entry, fallback_car_number=car_number)
        for entry in results
        if isinstance(entry, dict)
    ]
