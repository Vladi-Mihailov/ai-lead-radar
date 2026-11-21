"""Клиент суммы к оплате Georgian Card (reader/fines/georgian_card_client.py)
на mock-транспорте httpx: ни одного реального запроса.

Главное — что клиент делает только то, что делает браузер до ввода карты:
GET страниц Georgian Card и один POST .../start с пустым телом; никаких
полей карты, оплаты/авторизации и запросов к police.ge или чужим хостам."""

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

from reader.fines.georgian_card_client import (  # noqa: E402
    AMOUNT_UNIT_MINOR,
    GeorgianCardAmountClient,
    GeorgianCardConfig,
    GeorgianCardSafetyError,
    _guard_request,
    build_page1_url,
)
from reader.fines.payable_amount import (  # noqa: E402
    OUTCOME_BLOCKED,
    OUTCOME_CURRENCY_MISMATCH,
    OUTCOME_ERROR,
    OUTCOME_INVALID_RESPONSE,
    OUTCOME_OK,
    OUTCOME_RECEIPT_UNCONFIRMED,
    OUTCOME_REFUSED,
    OUTCOME_TIMEOUT,
)

_RECEIPT = "0099887766"
_CAR = "AA111BB"
_CONFIG = GeorgianCardConfig(merchant_id="MERCH1", page_id="PAGE1")
_CARD_PAGE = "https://mpi.gc.ge/v4/MERCHX/payment/TOKEN123"
_START_PATH = "/open/api//v4/MERCHX/payment/TOKEN123/start"


def _card_html(*, receipt_visible=True, button: str | None = None) -> str:
    receipt = f"<p>ჯარიმის ქვითარი {_RECEIPT}</p>" if receipt_visible else ""
    hidden = f'<input type="hidden" name="back" value="https://police.ge/protocol/fail.php?id={_RECEIPT}">'
    pay = f"<button>{button}</button>" if button else ""
    return f"<html><body>{receipt}{hidden}<input id='src-pan'>{pay}</body></html>"


class _Gateway:
    """Mock Georgian Card: записывает каждый запрос и отвечает по сценарию."""

    def __init__(self, *, page1=None, card_page=None, start=None):
        self.requests: list[httpx.Request] = []
        self.page1 = page1 or (lambda request: httpx.Response(302, headers={"location": _CARD_PAGE}))
        self.card_page = card_page or (lambda request: httpx.Response(200, text=_card_html()))
        self.start = start or (lambda request: httpx.Response(200, json={
            "state": "offer", "amount": 150, "currency": "GEL", "description": f"ჯარიმა {_RECEIPT}",
        }))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "mpi.gc.ge", f"request to a foreign host: {request.url.host}"
        if request.url.path == "/page1/":
            return self.page1(request)
        if request.url.path == "/v4/MERCHX/payment/TOKEN123":
            return self.card_page(request)
        if request.url.path == _START_PATH:
            return self.start(request)
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")


def _fetch(gateway: _Gateway, *, config=_CONFIG, base_amount=50.0):
    client = GeorgianCardAmountClient(config, transport=httpx.MockTransport(gateway.handler))
    return asyncio.run(client.fetch_amount(car_number=_CAR, protocol_no=_RECEIPT, base_amount=base_amount))


def _assert_only_safe_requests(gateway: _Gateway) -> None:
    for request in gateway.requests:
        assert request.url.host == "mpi.gc.ge"
        if request.method == "POST":
            assert request.url.path == _START_PATH
            assert request.content == b"{}"
        else:
            assert request.method == "GET" and request.content == b""
        body = request.content.decode()
        for card_field in ("pan", "cvc", "cvv", "expiry", "card"):
            assert card_field not in body.lower()


def test_amount_from_the_start_endpoint():
    gateway = _Gateway()
    quote = _fetch(gateway)
    assert (quote.outcome, quote.amount, quote.currency) == (OUTCOME_OK, 150.0, "GEL")
    assert [request.method for request in gateway.requests] == ["GET", "GET", "POST"]
    _assert_only_safe_requests(gateway)


def test_page1_url_is_the_police_ge_payment_link():
    url = build_page1_url(_CONFIG, car_number=_CAR, protocol_no=_RECEIPT)
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert (parts.scheme, parts.hostname, parts.path) == ("https", "mpi.gc.ge", "/page1/")
    assert query["merch_id"] == ["MERCH1"] and query["page_id"] == ["PAGE1"]
    assert query["o.protocolId"] == [_RECEIPT] and query["o.vehicleNumber"] == [_CAR]
    assert query["o.resourceAlias"] == ["1"]
    assert query["back_url_f"] == [f"https://police.ge/protocol/fail.php?id={_RECEIPT}"]


def test_amount_from_the_pay_button_alone():
    gateway = _Gateway(
        page1=lambda request: httpx.Response(200, text=_card_html(button="გადახდა (150.00 ₾)")),
    )
    quote = _fetch(gateway)
    assert (quote.outcome, quote.amount) == (OUTCOME_OK, 150.0)
    assert [request.method for request in gateway.requests] == ["GET"]


def test_amount_in_tetri_when_configured():
    gateway = _Gateway(start=lambda request: httpx.Response(200, json={
        "state": "offer", "amount": 15000, "currency": "981", "description": f"N {_RECEIPT}",
    }))
    quote = _fetch(gateway, config=GeorgianCardConfig(merchant_id="M", page_id="P", amount_unit=AMOUNT_UNIT_MINOR))
    assert (quote.outcome, quote.amount) == (OUTCOME_OK, 150.0)


def test_wrong_unit_is_rejected_as_implausible_not_saved():
    gateway = _Gateway(start=lambda request: httpx.Response(200, json={
        "state": "offer", "amount": 15000, "currency": "GEL", "description": f"N {_RECEIPT}",
    }))
    quote = _fetch(gateway)  # major, а пришли тетри
    assert quote.outcome == OUTCOME_INVALID_RESPONSE and quote.amount is None


def test_currency_other_than_gel_is_rejected():
    gateway = _Gateway(start=lambda request: httpx.Response(200, json={
        "state": "offer", "amount": 150, "currency": "USD", "description": f"N {_RECEIPT}",
    }))
    assert _fetch(gateway).outcome == OUTCOME_CURRENCY_MISMATCH


def test_receipt_only_in_our_own_links_does_not_confirm_the_answer():
    gateway = _Gateway(
        card_page=lambda request: httpx.Response(200, text=_card_html(receipt_visible=False)),
        start=lambda request: httpx.Response(200, json={
            "state": "offer", "amount": 150, "currency": "GEL",
            "backUrl": f"https://police.ge/protocol/fail.php?id={_RECEIPT}",
        }),
    )
    assert _fetch(gateway).outcome == OUTCOME_RECEIPT_UNCONFIRMED


def test_state_other_than_offer_is_not_an_amount():
    gateway = _Gateway(start=lambda request: httpx.Response(200, json={
        "state": "rejected", "amount": 150, "currency": "GEL", "description": f"N {_RECEIPT}",
    }))
    quote = _fetch(gateway)
    assert quote.outcome == OUTCOME_INVALID_RESPONSE and quote.detail == "state_rejected"


def test_redirect_to_the_fail_page_is_a_refusal_and_police_ge_is_never_requested():
    gateway = _Gateway(page1=lambda request: httpx.Response(
        302, headers={"location": f"https://police.ge/protocol/fail.php?id={_RECEIPT}"},
    ))
    quote = _fetch(gateway)
    assert quote.outcome == OUTCOME_REFUSED
    assert [request.url.host for request in gateway.requests] == ["mpi.gc.ge"]


def test_redirect_to_the_success_page_is_never_treated_as_an_amount():
    gateway = _Gateway(page1=lambda request: httpx.Response(
        302, headers={"location": "https://police.ge/protocol/success.php"},
    ))
    quote = _fetch(gateway)
    assert quote.outcome == OUTCOME_INVALID_RESPONSE and quote.detail == "success_redirect"
    assert len(gateway.requests) == 1


def test_redirect_to_a_foreign_host_is_not_followed():
    gateway = _Gateway(page1=lambda request: httpx.Response(302, headers={"location": "https://evil.example/x"}))
    quote = _fetch(gateway)
    assert quote.outcome == OUTCOME_INVALID_RESPONSE and quote.detail == "foreign_redirect"
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("status", [403, 429])
def test_rate_limit_or_block_is_reported(status):
    gateway = _Gateway(page1=lambda request: httpx.Response(status))
    assert _fetch(gateway).outcome == OUTCOME_BLOCKED
    gateway = _Gateway(start=lambda request: httpx.Response(status))
    assert _fetch(gateway).outcome == OUTCOME_BLOCKED


def test_timeout():
    def page1(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert _fetch(_Gateway(page1=page1)).outcome == OUTCOME_TIMEOUT


def test_server_error_and_unknown_page():
    assert _fetch(_Gateway(page1=lambda request: httpx.Response(502))).outcome == OUTCOME_ERROR
    quote = _fetch(_Gateway(page1=lambda request: httpx.Response(200, text="<html>მიმდინარეობს</html>")))
    assert quote.outcome == OUTCOME_INVALID_RESPONSE and quote.detail == "amount_not_found"


def test_start_answer_that_is_not_json():
    gateway = _Gateway(start=lambda request: httpx.Response(200, text="<html>oops</html>"))
    assert _fetch(gateway).outcome == OUTCOME_INVALID_RESPONSE


def test_pay_button_and_start_amount_must_agree():
    gateway = _Gateway(card_page=lambda request: httpx.Response(200, text=_card_html(button="გადახდა (100 ₾)")))
    quote = _fetch(gateway)
    assert quote.outcome == OUTCOME_INVALID_RESPONSE and quote.detail == "amount_mismatch"


def test_zero_amount_is_not_an_amount():
    gateway = _Gateway(start=lambda request: httpx.Response(200, json={
        "state": "offer", "amount": 0, "currency": "GEL", "description": f"N {_RECEIPT}",
    }))
    assert _fetch(gateway).outcome == OUTCOME_INVALID_RESPONSE


def test_not_configured_sends_nothing():
    gateway = _Gateway()
    quote = _fetch(gateway, config=GeorgianCardConfig())
    assert quote.request_sent is False and quote.outcome == OUTCOME_ERROR
    assert gateway.requests == []


def test_too_many_redirects():
    gateway = _Gateway(page1=lambda request: httpx.Response(302, headers={"location": "https://mpi.gc.ge/page1/"}))
    assert _fetch(gateway).outcome == OUTCOME_INVALID_RESPONSE


@pytest.mark.parametrize("method,url,body", [
    ("POST", "https://mpi.gc.ge/open/api//v4/M/payment/T/pay", b"{}"),
    ("POST", "https://mpi.gc.ge/open/api//v4/M/payment/T/start", b'{"pan": "4000000000000001"}'),
    ("POST", "https://mpi.gc.ge/page1/", b""),
    ("PUT", "https://mpi.gc.ge/open/api//v4/M/payment/T/start", b"{}"),
    ("GET", "https://police.ge/protocol/fail.php", None),
    ("GET", "http://mpi.gc.ge/page1/", None),
    ("GET", "https://mpi.gc.ge.evil.example/page1/", None),
])
def test_guard_blocks_anything_but_reading_the_payment_page(method, url, body):
    with pytest.raises(GeorgianCardSafetyError):
        _guard_request(method, url, gc_host="mpi.gc.ge", body=body)


def test_guard_allows_exactly_the_browser_steps():
    _guard_request("GET", "https://mpi.gc.ge/page1/?a=1", gc_host="mpi.gc.ge", body=None)
    _guard_request("POST", "https://mpi.gc.ge" + _START_PATH, gc_host="mpi.gc.ge", body=b"{}")


def test_detail_never_contains_the_receipt_or_plate():
    for gateway in (
        _Gateway(page1=lambda request: httpx.Response(403)),
        _Gateway(start=lambda request: httpx.Response(200, json={"state": f"x{_RECEIPT}", "amount": 1})),
        _Gateway(card_page=lambda request: httpx.Response(200, text=_card_html(receipt_visible=False)),
                 start=lambda request: httpx.Response(200, json={"state": "offer", "amount": 150, "currency": "GEL"})),
    ):
        quote = _fetch(gateway)
        assert _RECEIPT not in (quote.detail or "") and _CAR not in (quote.detail or "")
        assert json.dumps(quote.__dict__, default=str).find(_RECEIPT) == -1


# ---- После независимого review ----


def test_httpx_request_log_never_contains_the_plate_receipt_or_session_token(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="httpx")
    gateway = _Gateway()
    assert _fetch(gateway).outcome == OUTCOME_OK
    assert "HTTP Request" in caplog.text  # httpx действительно пишет каждую строку запроса
    for secret in (_RECEIPT, _CAR, "TOKEN123", "MERCH1", "PAGE1"):
        assert secret not in caplog.text
    assert "https://mpi.gc.ge/<redacted>" in caplog.text


@pytest.mark.parametrize("base_url", ["https://police.ge", "https://mpi.gc.ge.evil.example", "http://mpi.gc.ge",
                                      "https://mpi.gc.ge:8443"])
def test_misconfigured_gateway_host_sends_nothing(base_url):
    gateway = _Gateway()
    quote = _fetch(gateway, config=GeorgianCardConfig(base_url=base_url, merchant_id="M", page_id="P"))
    assert quote.request_sent is False and gateway.requests == []


def test_guard_refuses_a_non_georgian_card_configuration_and_other_ports():
    with pytest.raises(GeorgianCardSafetyError):
        _guard_request("GET", "https://police.ge/protocol/index.php", gc_host="police.ge", body=None)
    with pytest.raises(GeorgianCardSafetyError):
        _guard_request("GET", "https://mpi.gc.ge:8443/page1/", gc_host="mpi.gc.ge", body=None)


def test_small_fine_read_in_the_wrong_unit_is_rejected():
    gateway = _Gateway(start=lambda request: httpx.Response(200, json={
        "state": "offer", "amount": 1000, "currency": "GEL", "description": f"N {_RECEIPT}",
    }))
    assert _fetch(gateway, base_amount=10.0).outcome == OUTCOME_INVALID_RESPONSE


def _chain(hops: int):
    def page1(request):
        step = int(request.url.params.get("hop", "0"))
        if step < hops:
            return httpx.Response(302, headers={"location": f"https://mpi.gc.ge/page1/?hop={step + 1}"})
        return httpx.Response(200, text=_card_html(button="გადახდა (150 ₾)"))

    return page1


def test_exactly_the_allowed_number_of_redirects_is_followed():
    assert _fetch(_Gateway(page1=_chain(6))).outcome == OUTCOME_OK
    quote = _fetch(_Gateway(page1=_chain(7)))
    assert quote.outcome == OUTCOME_INVALID_RESPONSE and quote.detail == "too_many_redirects"
