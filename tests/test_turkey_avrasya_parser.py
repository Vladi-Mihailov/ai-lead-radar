"""
Тесты reader/turkey_bot/avrasya/parser.py::parse_submit_response —
намеренно узкая, evidence-based классификация (см. design report Stage 1:
"only classify response states that have actually been observed").
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from decimal import Decimal  # noqa: E402

from reader.turkey_bot.avrasya.parser import parse_submit_response  # noqa: E402


def _real_debt_item(*, principal, total, service_file_type, penalty=None):
    item = {
        "PrincipalTaxIncludedBalanceAmount": principal,
        "TotalTaxIncludedBalanceAmount": total,
        "ExitDate": "2*************6",
        "ExitStation": "A*****A",
        "IsAuthenticate": False,
    }
    if penalty is not None:
        item["TotalFixedIncomeTaxIncludedBalanceAmount"] = penalty
    return item, service_file_type


def _real_debt_envelope(*subcription_specs) -> dict:
    """subcription_specs — каждый (debt_item_dict, service_file_type) из
    _real_debt_item() — строит РЕАЛЬНО увиденный вживую envelope (Stage
    2C, production turkey_toll_checks.id=3, 2026-09-16, plate M295YB196)."""
    return {
        "Response": "OK",
        "DcsResponse": "SUCCESSFUL",
        "Subcriptions": [
            {
                "ClientSubscriptionCustomerReferenceValue": "M295YB196",
                "DebtItems": [item],
                "DebtorContactFullName": "*******",
                "ServiceFileTypeName": service_file_type,
            }
            for item, service_file_type in subcription_specs
        ],
        "IsShowButton": False,
    }


def test_real_observed_invalid_captcha_response_is_rejected():
    """РЕАЛЬНО увиденный вживую (curl, design report Stage 1) ответ."""
    body = {
        "Messages": [
            {
                "PropertyName": "Captcha",
                "ErrorMessage": "Güvenlik kodunu doğru girdiğinizden emin olunuz.",
            }
        ],
        "ValidationResultType": 2,
    }

    outcome = parse_submit_response(400, body)

    assert outcome.kind == "rejected"
    assert outcome.status_code == 400
    assert outcome.messages[0].property_name == "Captcha"
    assert outcome.raw_data == body


def test_400_with_different_property_name_is_unexpected_not_rejected():
    """Только ИМЕННО этот увиденный текст/поле -> rejected (см. докстрок
    parser.py) — другое поле под тем же статусом не обобщается."""
    body = {
        "Messages": [{"PropertyName": "QueryValue", "ErrorMessage": "some other error"}],
        "ValidationResultType": 2,
    }

    outcome = parse_submit_response(400, body)

    assert outcome.kind == "unexpected"


def test_400_with_different_captcha_error_text_is_unexpected_not_rejected():
    body = {
        "Messages": [{"PropertyName": "Captcha", "ErrorMessage": "some other message"}],
    }

    outcome = parse_submit_response(400, body)

    assert outcome.kind == "unexpected"


def test_unknown_200_response_is_unexpected_not_has_debt_or_no_debt():
    """Реальная форма успешного ответа ("Subcriptions") ни разу не была
    увидена вживую (CAPTCHA принципиально не решается автоматически) — не
    должна ни разу превращаться в has_debt/no_debt по одной догадке."""
    outcome = parse_submit_response(200, {"Subcriptions": []})

    assert outcome.kind == "unexpected"


def test_real_observed_404_empty_body_is_no_debt():
    """РЕАЛЬНО увиденная вживую форма (Stage 2A: 3 независимых успешных
    human-CAPTCHA прохождения — 2x A123AA123, 1x A777AA777 — все дали
    ровно HTTP 404 + пустое тело, см. design report Stage 2A) — и
    подтверждено чтением реального фронтенда (см. parser.py докстрок):
    именно эта форма падает в общую "запись не найдена" ветку
    showNotFoundView."""
    outcome = parse_submit_response(404, "")

    assert outcome.kind == "no_debt"
    assert outcome.status_code == 404
    assert outcome.messages == ()
    assert outcome.raw_data == ""


def test_404_with_none_body_is_no_debt():
    """Защитно — session.py на практике никогда не отдаёт None (только
    str/dict, см. session.py::submit), но None семантически та же
    "пустота", что и реально увиденная пустая строка."""
    outcome = parse_submit_response(404, None)

    assert outcome.kind == "no_debt"
    assert outcome.raw_data is None


def test_404_with_whitespace_only_body_is_no_debt():
    """Не отдельно увиденная форма — та же "пустота", что и реально
    увиденная пустая строка, просто устойчивая к возможному пробелу."""
    outcome = parse_submit_response(404, "   ")

    assert outcome.kind == "no_debt"


def test_404_with_json_response_code_is_still_unexpected():
    """См. задачу: "do not infer no_debt merely from HTTP 404" (это
    по-прежнему верно для НЕ-пустых 404 — только пустое тело подтверждено
    вживую, см. предыдущие тесты). Реальная форма ResponseCode-веток
    (9006 и т.п.) так и не была увидена — не тот же случай."""
    outcome = parse_submit_response(404, {"ResponseCode": "1005"})

    assert outcome.kind == "unexpected"


def test_404_with_non_empty_text_body_is_unexpected():
    outcome = parse_submit_response(404, "Not Found")

    assert outcome.kind == "unexpected"


def test_500_response_is_unexpected():
    outcome = parse_submit_response(500, "Internal Server Error")

    assert outcome.kind == "unexpected"
    assert outcome.raw_data == "Internal Server Error"


def test_400_with_non_dict_body_is_unexpected():
    outcome = parse_submit_response(400, "not a dict")

    assert outcome.kind == "unexpected"


def test_400_with_missing_messages_is_unexpected():
    outcome = parse_submit_response(400, {"ValidationResultType": 2})

    assert outcome.kind == "unexpected"
    assert outcome.messages == ()


# ---- has_debt (см. design report Stage 2C — production turkey_toll_checks.id=3,
# 2026-09-16, plate M295YB196: РЕАЛЬНО увиденный вживую HTTP 200 envelope
# с найденной задолженностью) ----


def test_real_observed_debt_found_envelope_is_has_debt():
    """Точная production-форма (Stage 2C) — 3 DebtItems, 2 ServiceFileTypeName."""
    body = _real_debt_envelope(
        _real_debt_item(principal=330.0, total=330.0, service_file_type="EARLY_COLLECTION_FILE"),
        _real_debt_item(principal=225.0, total=1125.0, penalty=900.0, service_file_type="COLLECTION_FILE"),
        _real_debt_item(principal=225.0, total=1125.0, penalty=900.0, service_file_type="COLLECTION_FILE"),
    )

    outcome = parse_submit_response(200, body)

    assert outcome.kind == "has_debt"
    assert outcome.status_code == 200
    assert outcome.raw_data == body
    assert len(outcome.debt_items) == 3


def test_real_observed_debt_aggregation_matches_production_totals():
    """См. задачу: "For the observed production example the expected
    totals are: debt items: 3, principal: 780 TRY, penalty: 1800 TRY,
    total: 2580 TRY"."""
    body = _real_debt_envelope(
        _real_debt_item(principal=330.0, total=330.0, service_file_type="EARLY_COLLECTION_FILE"),
        _real_debt_item(principal=225.0, total=1125.0, penalty=900.0, service_file_type="COLLECTION_FILE"),
        _real_debt_item(principal=225.0, total=1125.0, penalty=900.0, service_file_type="COLLECTION_FILE"),
    )

    outcome = parse_submit_response(200, body)

    principal_sum = sum((item.principal_amount for item in outcome.debt_items), Decimal(0))
    penalty_sum = sum(
        (item.penalty_amount for item in outcome.debt_items if item.penalty_amount is not None),
        Decimal(0),
    )
    total_sum = sum((item.total_amount for item in outcome.debt_items), Decimal(0))

    assert principal_sum == Decimal("780.0")
    assert penalty_sum == Decimal("1800.0")
    assert total_sum == Decimal("2580.0")


def test_debt_item_missing_penalty_field_has_none_not_zero_or_reconstructed():
    """См. задачу: "Do not reconstruct missing penalty values from total -
    principal; only sum an explicitly returned penalty field. A missing
    penalty field should behave as zero/not supplied for that item" —
    penalty_amount is None (not Decimal("0"), not total-principal)."""
    body = _real_debt_envelope(
        _real_debt_item(principal=330.0, total=330.0, service_file_type="EARLY_COLLECTION_FILE"),
    )

    outcome = parse_submit_response(200, body)

    assert outcome.debt_items[0].penalty_amount is None


def test_debt_items_never_carry_masked_personal_or_location_fields():
    """См. задачу: не показывать ExitDate/ExitStation/DebtorContactFullName/
    UniqueId — здесь проверяется, что AvrasyaDebtItem их вообще не несёт
    структурно (не только "не отображаются")."""
    body = _real_debt_envelope(
        _real_debt_item(principal=330.0, total=330.0, service_file_type="EARLY_COLLECTION_FILE"),
    )

    outcome = parse_submit_response(200, body)

    item_fields = vars(outcome.debt_items[0])
    assert "ExitDate" not in item_fields
    assert "ExitStation" not in item_fields
    assert "DebtorContactFullName" not in item_fields
    assert "UniqueId" not in item_fields


def test_empty_subcriptions_does_not_become_has_debt():
    """См. задачу: "empty Subcriptions does NOT accidentally become
    has_debt"."""
    body = {"Response": "OK", "DcsResponse": "SUCCESSFUL", "Subcriptions": [], "IsShowButton": False}

    outcome = parse_submit_response(200, body)

    assert outcome.kind == "unexpected"


def test_200_with_confirmed_response_fields_but_no_subcriptions_key_is_unexpected():
    body = {"Response": "OK", "DcsResponse": "SUCCESSFUL"}

    outcome = parse_submit_response(200, body)

    assert outcome.kind == "unexpected"


def test_200_with_different_response_value_is_unexpected():
    """См. задачу: "Do not classify arbitrary HTTP 200 responses as
    has_debt" — только ТОЧНОЕ "OK"/"SUCCESSFUL" (реально увиденные
    значения) считаются подтверждённым envelope."""
    body = _real_debt_envelope(
        _real_debt_item(principal=100.0, total=100.0, service_file_type="COLLECTION_FILE"),
    )
    body["Response"] = "FAIL"

    outcome = parse_submit_response(200, body)

    assert outcome.kind == "unexpected"


def test_200_with_different_dcs_response_value_is_unexpected():
    body = _real_debt_envelope(
        _real_debt_item(principal=100.0, total=100.0, service_file_type="COLLECTION_FILE"),
    )
    body["DcsResponse"] = "PENDING"

    outcome = parse_submit_response(200, body)

    assert outcome.kind == "unexpected"


def test_confirmed_envelope_with_malformed_debt_items_is_unexpected_not_has_debt():
    """См. задачу: "malformed debt items -> conservative behavior /
    unexpected, as appropriate" — envelope структурно подтверждён, но НИ
    ОДНА запись не несёт обязательных полей."""
    body = {
        "Response": "OK", "DcsResponse": "SUCCESSFUL",
        "Subcriptions": [
            {"ServiceFileTypeName": "COLLECTION_FILE", "DebtItems": [{"SomeOtherField": 1}]},
        ],
        "IsShowButton": False,
    }

    outcome = parse_submit_response(200, body)

    assert outcome.kind == "unexpected"
    assert outcome.debt_items == ()


def test_confirmed_envelope_skips_malformed_items_but_keeps_usable_ones():
    """Один битый DebtItem среди валидных не должен обнулять весь ответ —
    он просто пропускается (см. parser.py::_extract_debt_items)."""
    body = {
        "Response": "OK", "DcsResponse": "SUCCESSFUL",
        "Subcriptions": [
            {"ServiceFileTypeName": "COLLECTION_FILE", "DebtItems": [
                {"PrincipalTaxIncludedBalanceAmount": 100.0, "TotalTaxIncludedBalanceAmount": 100.0},
                {"SomeOtherField": 1},  # битая запись - должна быть пропущена
            ]},
        ],
        "IsShowButton": False,
    }

    outcome = parse_submit_response(200, body)

    assert outcome.kind == "has_debt"
    assert len(outcome.debt_items) == 1
    assert outcome.debt_items[0].principal_amount == Decimal("100.0")


def test_confirmed_envelope_with_non_list_debt_items_is_unexpected():
    body = {
        "Response": "OK", "DcsResponse": "SUCCESSFUL",
        "Subcriptions": [{"ServiceFileTypeName": "COLLECTION_FILE", "DebtItems": "not-a-list"}],
        "IsShowButton": False,
    }

    outcome = parse_submit_response(200, body)

    assert outcome.kind == "unexpected"


def test_confirmed_envelope_with_missing_service_file_type_skips_that_subscription():
    body = {
        "Response": "OK", "DcsResponse": "SUCCESSFUL",
        "Subcriptions": [
            {"DebtItems": [{"PrincipalTaxIncludedBalanceAmount": 100.0, "TotalTaxIncludedBalanceAmount": 100.0}]},
        ],
        "IsShowButton": False,
    }

    outcome = parse_submit_response(200, body)

    assert outcome.kind == "unexpected"
    assert outcome.debt_items == ()


def test_decimal_amounts_have_no_binary_float_artifacts():
    """См. задачу: "decimal-safe monetary handling; do not introduce
    floating-point display artifacts"."""
    body = _real_debt_envelope(
        _real_debt_item(principal=225.0, total=1125.0, penalty=900.0, service_file_type="COLLECTION_FILE"),
    )

    outcome = parse_submit_response(200, body)

    item = outcome.debt_items[0]
    assert str(item.principal_amount) == "225.0"
    assert str(item.penalty_amount) == "900.0"
    assert str(item.total_amount) == "1125.0"
