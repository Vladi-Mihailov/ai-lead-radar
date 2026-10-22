"""
Тесты parser.py — преобразование уже распарсенного JSON-ответа
police.ge в ParsedFineRecord. Полностью без сети: вход — обычный dict
(при необходимости загруженный из tests/fixtures/*.json).
"""

import json
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.parser import (  # noqa: E402
    FineParseError,
    compute_fingerprint,
    extract_published_protocols_results,
    parse_published_protocol_entry,
    parse_search_response,
)

_FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_parses_successful_response_with_results():
    raw = _load("police_ge_success.json")

    records = parse_search_response(raw, car_number="B957MA09")

    assert len(records) == 2

    first = records[0]
    assert first.car_number == "B957MA09"
    assert first.external_fine_id == "AB123456"
    assert first.penalty_date == date(2026, 8, 6)
    assert first.due_date == date(2026, 8, 20)
    assert first.delivered_status == "Вручено"  # activeDate заполнен
    assert first.raw_data["protocolNo"] == "AB123456"
    assert first.violation_date == date(2026, 8, 5)
    assert first.amount == 100.0
    assert first.place == "Test place"
    assert first.violation_description == "Test violation"

    second = records[1]
    assert second.external_fine_id == "AB999999"
    assert second.delivered_status == "Не вручено"  # activeDate = null


# ---- расширенные поля (violationDate/protocolAmount/protocolPlace/
# protocolLawDescription) — присутствуют в сыром ответе police.ge, но
# раньше не сохранялись как typed fields (только внутри raw_data), см.
# задачу про новый формат уведомлений. ----


def test_parser_extracts_violation_date():
    raw = _load("police_ge_success.json")

    [record, _] = parse_search_response(raw, car_number="B957MA09")

    assert record.violation_date == date(2026, 8, 5)


def test_parser_extracts_amount():
    raw = _load("police_ge_success.json")

    [record, _] = parse_search_response(raw, car_number="B957MA09")

    assert record.amount == 100.0


def test_parser_extracts_place():
    raw = _load("police_ge_success.json")

    [record, _] = parse_search_response(raw, car_number="B957MA09")

    assert record.place == "Test place"


def test_parser_extracts_violation_description():
    raw = _load("police_ge_success.json")

    [record, _] = parse_search_response(raw, car_number="B957MA09")

    assert record.violation_description == "Test violation"


def test_extended_fields_are_none_when_absent_not_invented():
    """"Ничего не придумывать, если поле отсутствует" — entry без
    violationDate/protocolAmount/protocolPlace/protocolLawDescription
    должен дать None, а не выдуманное значение."""
    entry = {"protocolAuto": "AA001AA", "protocolNo": "X1"}
    [record] = parse_search_response(
        {"success": True, "data": {"results": [entry]}}, car_number="AA001AA"
    )

    assert record.violation_date is None
    assert record.amount is None
    assert record.place is None
    assert record.violation_description is None


def test_amount_extraction_does_not_change_fingerprint():
    """Критично для backward compatibility (см. задачу про безопасную
    миграцию): compute_fingerprint должен получать сырое protocolAmount
    (как и раньше), а не преобразованное в float record.amount — иначе
    fingerprint уже сохранённых detected_fines изменился бы, и все старые
    штрафы "нашлись" бы заново при следующей проверке."""
    entry = {
        "protocolAuto": "AA001AA",
        "protocolNo": "X1",
        "violationDate": "2026-08-05",
        "protocolAmount": 100,
    }
    [record] = parse_search_response(
        {"success": True, "data": {"results": [entry]}}, car_number="AA001AA"
    )

    expected_fingerprint = compute_fingerprint(
        external_fine_id="X1", violation_date=date(2026, 8, 5), amount=100,
    )
    assert record.fingerprint == expected_fingerprint
    assert record.amount == 100.0  # typed field — float, но fingerprint не затронут


def test_parses_empty_results_as_empty_list():
    raw = _load("police_ge_empty.json")

    records = parse_search_response(raw, car_number="AA001AA")

    assert records == []


def test_success_false_is_rejected_as_parse_error():
    raw = _load("police_ge_failure.json")

    with pytest.raises(FineParseError):
        parse_search_response(raw, car_number="AA001AA")


def test_missing_data_key_is_rejected():
    with pytest.raises(FineParseError):
        parse_search_response({"success": True}, car_number="AA001AA")


def test_results_not_a_list_is_rejected():
    with pytest.raises(FineParseError):
        parse_search_response(
            {"success": True, "data": {"results": "oops"}}, car_number="AA001AA"
        )


def test_non_dict_entries_in_results_raise_parse_error():
    """См. задачу "guard Georgia debt against false zero results" п.6 —
    молчаливый пропуск не-объектных элементов мог превратить
    results=[garbage] в SUCCESS(0), неотличимый от настоящего "штрафов
    нет" — теперь это FineParseError, а не тихо укороченный список."""
    raw = {
        "success": True,
        "data": {"results": ["not-a-dict", {"protocolAuto": "AA001AA", "protocolNo": "X1"}]},
    }

    with pytest.raises(FineParseError):
        parse_search_response(raw, car_number="AA001AA")


def test_genuinely_empty_results_list_is_still_a_valid_zero_candidate():
    raw = {"success": True, "data": {"results": []}}

    records = parse_search_response(raw, car_number="AA001AA")

    assert records == []


def test_fingerprint_is_deterministic_for_same_stable_fields():
    fp1 = compute_fingerprint(
        external_fine_id="AB123456", violation_date=date(2026, 8, 5), amount=100
    )
    fp2 = compute_fingerprint(
        external_fine_id="AB123456", violation_date=date(2026, 8, 5), amount=100
    )

    assert fp1 == fp2


def test_fingerprint_is_independent_of_json_key_order():
    # compute_fingerprint строится из явных именованных аргументов, а не
    # хешем всего словаря — порядок ключей в исходном ответе сайта не может
    # на него повлиять. Проверяем это через parse_search_response на двух
    # записях с одинаковыми стабильными полями, но разным порядком ключей
    # и разными "нестабильными" полями (remainingDays, protocolPlace).
    entry_a = {
        "protocolAuto": "AA001AA",
        "protocolNo": "X1",
        "violationDate": "2026-08-05",
        "protocolAmount": 100,
        "remainingDays": 5,
        "protocolPlace": "Place A",
    }
    entry_b = {
        "protocolPlace": "Place B",
        "remainingDays": 0,
        "protocolAmount": 100,
        "violationDate": "2026-08-05",
        "protocolNo": "X1",
        "protocolAuto": "AA001AA",
    }

    [record_a] = parse_search_response(
        {"success": True, "data": {"results": [entry_a]}}, car_number="AA001AA"
    )
    [record_b] = parse_search_response(
        {"success": True, "data": {"results": [entry_b]}}, car_number="AA001AA"
    )

    assert record_a.fingerprint == record_b.fingerprint


def test_fingerprint_differs_for_different_external_fine_id():
    fp1 = compute_fingerprint(
        external_fine_id="AB123456", violation_date=date(2026, 8, 5), amount=100
    )
    fp2 = compute_fingerprint(
        external_fine_id="AB999999", violation_date=date(2026, 8, 5), amount=100
    )

    assert fp1 != fp2


def test_fingerprint_differs_for_different_amount():
    fp1 = compute_fingerprint(
        external_fine_id="AB123456", violation_date=date(2026, 8, 5), amount=100
    )
    fp2 = compute_fingerprint(
        external_fine_id="AB123456", violation_date=date(2026, 8, 5), amount=200
    )

    assert fp1 != fp2


# ==== Публичный feed "Receipts of fines published" (см. задачу "Georgia
# fines incremental public-feed monitoring", audit report) ====


def _feed_entry(
    *, protocol_no="AB123456", plate="AA001AA", publish_date="2026-10-22",
    violation_date="2026-10-22", amount=50,
) -> dict:
    """Форма записи РЕАЛЬНО подтверждена live-запросом (см. audit report) —
    те же ключи, что и у searchByAuto, плюс publishDate/protocolLaw,
    которых searchByAuto не отдаёт."""
    return {
        "protocolAuto": plate,
        "protocolNo": protocol_no,
        "protocolDate": "2026-10-22",
        "activeDate": None,
        "violationDate": violation_date,
        "protocolPlace": "ქუჩა",
        "protocolLaw": "125-1",
        "protocolLawDescription": "სისწრაფის გადაჭარბება",
        "protocolAmount": amount,
        "publishDate": publish_date,
        "lastDate": "2026-12-21",
        "remainingDays": 60,
    }


def test_extract_published_protocols_results_returns_raw_dicts():
    raw = {"success": True, "data": {"count": 2, "results": [_feed_entry(), _feed_entry(plate="BB002BB")]}}

    results = extract_published_protocols_results(raw)

    assert len(results) == 2
    assert results[0]["protocolAuto"] == "AA001AA"
    assert results[1]["protocolAuto"] == "BB002BB"
    # Сырые dict — publishDate присутствует (ParsedFineRecord его не несёт).
    assert results[0]["publishDate"] == "2026-10-22"


def test_extract_published_protocols_results_rejects_non_success():
    with pytest.raises(FineParseError):
        extract_published_protocols_results({"success": False})


def test_extract_published_protocols_results_rejects_non_list_results():
    with pytest.raises(FineParseError):
        extract_published_protocols_results({"success": True, "data": {"results": "garbage"}})


def test_extract_published_protocols_results_rejects_non_object_entries():
    with pytest.raises(FineParseError):
        extract_published_protocols_results({"success": True, "data": {"results": ["garbage"]}})


def test_parse_published_protocol_entry_produces_parsed_fine_record():
    entry = _feed_entry(protocol_no="AB123456", plate="AA001AA", amount=50)

    record = parse_published_protocol_entry(entry)

    assert record.car_number == "AA001AA"
    assert record.external_fine_id == "AB123456"
    assert record.amount == 50
    assert record.violation_date == date(2026, 10, 22)


def test_parse_published_protocol_entry_fingerprint_matches_search_by_auto_fingerprint():
    """Критично для dedup (см. задачу "DEDUPE STRATEGY") — один и тот же
    штраф, увиденный ЛИБО через searchByAuto, ЛИБО через feed, должен
    получить ОДИНАКОВЫЙ fingerprint, иначе один и тот же штраф задвоился
    бы в detected_fines в зависимости от того, какой путь его нашёл первым."""
    feed_entry = _feed_entry(protocol_no="AB123456", plate="AA001AA", amount=50)
    search_entry = dict(feed_entry)  # searchByAuto отдаёт те же ключи для общих полей

    feed_record = parse_published_protocol_entry(feed_entry)
    [search_record] = parse_search_response(
        {"success": True, "data": {"results": [search_entry]}}, car_number="AA001AA"
    )

    assert feed_record.fingerprint == search_record.fingerprint


def test_different_protocol_no_same_plate_same_date_get_different_fingerprints():
    """Главный regression-критерий аудита: "plate + penalty_date is NOT a
    valid unique key" — две РАЗНЫЕ записи одной машины в один день
    (разные protocolNo) должны давать РАЗНЫЕ fingerprint."""
    entry_a = _feed_entry(protocol_no="AB111111", plate="AA001AA", violation_date="2026-10-22")
    entry_b = _feed_entry(protocol_no="AB222222", plate="AA001AA", violation_date="2026-10-22")

    record_a = parse_published_protocol_entry(entry_a)
    record_b = parse_published_protocol_entry(entry_b)

    assert record_a.fingerprint != record_b.fingerprint
