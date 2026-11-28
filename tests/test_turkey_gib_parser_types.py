"""
Нестандартные типы полей "messages" в ответе GİB.

Раньше parser.py падал (AttributeError на text.strip(), TypeError на
нехешируемом type) или молча возвращал "no_debt". Теперь такой ответ —
"unexpected": проверка GİB становится ошибкой, не сравнивается с прошлым
результатом и не даёт уведомлений. Реальная сеть не используется.
"""

import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

from reader.turkey_bot.gib.parser import parse_submit_response  # noqa: E402
from reader.turkey_bot.gib.provider import GibProvider  # noqa: E402
from reader.turkey_bot.gib.session import GibSession  # noqa: E402
from reader.turkey_bot.monitoring.change_detector import detect_changes  # noqa: E402
from reader.turkey_bot.unified.check_service import _gib_outcome_to_result  # noqa: E402
from reader.turkey_bot.unified.models import (  # noqa: E402
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    total_amount_for,
)

_NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
_PLATE = "ZZ9999TST"
_NO_DEBT_TEXT = "Girdiğiniz plakaya ait borç bulunamadı."

_BAD_VALUES = [None, 5, 5.5, True, ["ERROR"], {"a": 1}]
_BAD_VALUE_IDS = ["null", "int", "float", "bool", "list", "dict"]

# Формы, которые без нестандартного элемента разобрались бы как "no_debt".
_NO_DEBT_SHAPES = [
    {"BORCLAR": []},
    {"data": []},
    {"data": None},
]


def _with_messages(base: dict, *items) -> dict:
    return {**base, "messages": list(items)}


@pytest.mark.parametrize("shape", _NO_DEBT_SHAPES)
@pytest.mark.parametrize("value", _BAD_VALUES, ids=_BAD_VALUE_IDS)
def test_non_string_text_is_unexpected_not_no_debt(shape, value):
    outcome = parse_submit_response(_with_messages(shape, {"type": "ERROR", "text": value}))
    assert outcome.kind == "unexpected"
    assert outcome.messages == ()


@pytest.mark.parametrize("shape", _NO_DEBT_SHAPES)
@pytest.mark.parametrize("value", _BAD_VALUES, ids=_BAD_VALUE_IDS)
def test_non_string_type_is_unexpected_not_no_debt(shape, value):
    outcome = parse_submit_response(_with_messages(shape, {"type": value, "text": _NO_DEBT_TEXT}))
    assert outcome.kind == "unexpected"


@pytest.mark.parametrize("item", [{"type": "ERROR"}, {"text": _NO_DEBT_TEXT}, {}])
def test_missing_type_or_text_is_unexpected(item):
    assert parse_submit_response(_with_messages({"BORCLAR": []}, item)).kind == "unexpected"


@pytest.mark.parametrize("item", ["ERROR", 1, None, ["x"]], ids=["str", "int", "null", "list"])
def test_non_object_message_item_is_unexpected(item):
    assert parse_submit_response(_with_messages({"BORCLAR": []}, item)).kind == "unexpected"


def test_one_bad_item_wins_over_a_known_no_debt_message():
    payload = _with_messages(
        {"data": None},
        {"type": "ERROR", "text": _NO_DEBT_TEXT},
        {"type": "ERROR", "text": 42},
    )
    assert parse_submit_response(payload).kind == "unexpected"


@pytest.mark.parametrize("payload, kind", [
    ({"messages": [{"type": "ERROR", "text": _NO_DEBT_TEXT}], "data": None}, "no_debt"),
    ({"messages": [{"type": "ERROR",
                    "text": "Güvenlik kodunu yanlış girdiniz. Lütfen kontrol ederek tekrar deneyiniz."}],
      "data": None}, "rejected"),
    ({"BORCLAR": [], "messages": None}, "no_debt"),
    ({"BORCLAR": [], "messages": []}, "no_debt"),
    ({"data": []}, "no_debt"),
    ({"messages": [{"type": "ERROR", "text": "Yeni hata."}]}, "unexpected"),
])
def test_standard_responses_are_classified_as_before(payload, kind):
    assert parse_submit_response(payload).kind == kind


async def test_provider_logs_safe_diagnostics_for_bad_types(caplog):
    caplog.set_level(logging.INFO)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "messages": [{"type": ["ERROR"], "text": {"plate": _PLATE}}, {"type": "ERROR", "text": None}],
            "BORCLAR": [],
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    outcome = await GibProvider(GibSession(client)).submit(plate=_PLATE, image_id="cid-1", captcha_code="abcde")

    assert outcome.kind == "unexpected"
    assert len(requests) == 1
    [line] = [r.getMessage() for r in caplog.records if r.name == "reader.turkey_bot.gib.diagnostics"]
    assert "messages=2 types=?,ERROR" in line
    assert "text=<omitted:not_text> | <omitted:no_text>" in line
    assert _PLATE not in line


def _provider_result(provider: str, status: ProviderStatus, total: Decimal) -> ProviderCheckResult:
    return ProviderCheckResult(
        provider=provider, status=status, debt_count=1 if status == ProviderStatus.HAS_DEBT else 0,
        principal_amount=total, penalty_amount=Decimal(0), total_amount=total,
        items=(), error_type=None, checked_at=_NOW,
    )


@pytest.mark.parametrize("previous_status, previous_total", [
    (ProviderStatus.HAS_DEBT, Decimal("3000")),
    (ProviderStatus.NO_DEBT, Decimal(0)),
])
def test_bad_types_never_produce_a_payment_or_debt_change(previous_status, previous_total):
    outcome = parse_submit_response(_with_messages({"BORCLAR": []}, {"type": "ERROR", "text": 0}))
    gib = _gib_outcome_to_result(outcome, checked_at=_NOW)
    assert gib.status == ProviderStatus.ERROR and gib.error_type == "unexpected"

    providers = (gib,)
    current = UnifiedCheckResult(
        plate=_PLATE, started_at=_NOW, finished_at=_NOW,
        overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers),
        providers=providers,
    )
    previous = {"gib": _provider_result("gib", previous_status, previous_total)}
    assert detect_changes(previous, current) == []
