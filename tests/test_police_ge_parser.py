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

    second = records[1]
    assert second.external_fine_id == "AB999999"
    assert second.delivered_status == "Не вручено"  # activeDate = null


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


def test_non_dict_entries_in_results_are_skipped():
    raw = {
        "success": True,
        "data": {"results": ["not-a-dict", {"protocolAuto": "AA001AA", "protocolNo": "X1"}]},
    }

    records = parse_search_response(raw, car_number="AA001AA")

    assert len(records) == 1
    assert records[0].external_fine_id == "X1"


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
