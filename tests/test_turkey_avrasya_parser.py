"""
Тесты reader/turkey_bot/avrasya/parser.py::parse_submit_response —
намеренно узкая, evidence-based классификация (см. design report Stage 1:
"only classify response states that have actually been observed").
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.avrasya.parser import parse_submit_response  # noqa: E402


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
