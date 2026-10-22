"""Преобразование JSON-ответа police.ge (endpoint searchByAuto, а также
публичного feed "Receipts of fines published" — endpoint protocols, см.
parse_published_protocols_response ниже) в ParsedFineRecord. Никакой
сети/httpx здесь нет — вход уже готовый dict, выход — доменные объекты.
Полностью детерминировано, тестируется без сети.
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


def _parse_amount(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_entry(entry: dict[str, Any], *, fallback_car_number: str) -> ParsedFineRecord:
    external_fine_id = entry.get("protocolNo")
    violation_date = _parse_date(entry.get("violationDate"))
    # raw_amount (не _parse_amount(raw_amount)!) идёт в compute_fingerprint —
    # менять на что-либо, кроме сырого JSON-значения, нельзя: это изменило
    # бы fingerprint уже сохранённых detected_fines и вызвало бы повторную
    # "находку" всех старых штрафов при следующей проверке (см. задачу про
    # безопасную миграцию/backward compatibility). amount (типизированное
    # float-поле для показа клиенту) вычисляется отдельно, ниже.
    raw_amount = entry.get("protocolAmount")
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
            amount=raw_amount,
        ),
        raw_data=entry,
        violation_date=violation_date,
        amount=_parse_amount(raw_amount),
        place=entry.get("protocolPlace") or None,
        violation_description=entry.get("protocolLawDescription") or None,
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

    # См. задачу "guard Georgia debt against false zero results" п.6 —
    # НЕ отбрасывать молча не-объектные элементы (это могло превратить
    # results=[garbage] в SUCCESS(0) неотличимо от настоящего "штрафов
    # нет"). Настоящий results=[] по-прежнему валиден и означает zero
    # candidate — см. FineCheckService.check_task() false-zero guard,
    # который решает, authoritative ли этот ноль.
    if any(not isinstance(entry, dict) for entry in results):
        raise FineParseError(
            "Некорректный формат ответа police.ge: data.results содержит не-объектные элементы"
        )

    return [_parse_entry(entry, fallback_car_number=car_number) for entry in results]


def extract_published_protocols_results(raw: Any) -> list[dict]:
    """Публичный feed (см. задачу "Georgia fines incremental public-feed
    monitoring", PoliceGeSession.fetch_published_protocols) — ТОТ ЖЕ
    конверт {success, data: {results}}, что и searchByAuto (см.
    parse_search_response докстрок) — валидирует его и возвращает СЫРЫЕ
    записи (ещё НЕ ParsedFineRecord).

    Сознательно НЕ парсит каждую запись сразу (в отличие от
    parse_search_response) — GeorgiaPublicFineFeedJob сначала сверяет
    СЫРОЙ protocolAuto с отслеживаемым множеством номеров и только для
    РЕАЛЬНО совпавших строк вызывает parse_published_protocol_entry()
    ниже (см. задачу "DATA MINIMIZATION": несовпавшие строки не должны
    порождать ничего, кроме мгновенно отброшенного локального значения).
    publishDate (здесь есть, у ParsedFineRecord — нет) читается
    вызывающим кодом прямо из сырых записей для курсора/overlap-окна (см.
    reader/fines/public_feed_state_repository.py)."""
    if not isinstance(raw, dict) or raw.get("success") is not True:
        raise FineParseError("Ожидался ответ police.ge с success:true")

    data = raw.get("data")
    if not isinstance(data, dict):
        raise FineParseError("Некорректный формат ответа police.ge: data не объект")

    results = data.get("results")
    if not isinstance(results, list):
        raise FineParseError("Некорректный формат ответа police.ge: data.results не список")

    if any(not isinstance(entry, dict) for entry in results):
        raise FineParseError(
            "Некорректный формат ответа police.ge: data.results содержит не-объектные элементы"
        )

    return results


def parse_published_protocol_entry(entry: dict) -> ParsedFineRecord:
    """ОДНА уже провалидированная (см. extract_published_protocols_results)
    запись feed'а -> ParsedFineRecord. Вызывается ТОЛЬКО для строк,
    реально совпавших с отслеживаемым номером (см. GeorgiaPublicFineFeedJob)
    — проверено вживую (audit report): все поля, нужные _parse_entry()
    (protocolAuto/protocolNo/violationDate/protocolAmount/protocolDate/
    lastDate/activeDate/protocolPlace/protocolLawDescription), уже
    присутствуют напрямую в каждой записи feed'а — ни одного отдельного
    detail-запроса к police.ge не требуется. fallback_car_number =
    protocolAuto самой записи (а не "чей-то один номер", как у
    searchByAuto) — список покрывает множество разных машин сразу."""
    return _parse_entry(entry, fallback_car_number=entry.get("protocolAuto") or "")
