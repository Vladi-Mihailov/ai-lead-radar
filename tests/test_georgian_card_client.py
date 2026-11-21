"""Клиент суммы к оплате Georgian Card (reader/fines/georgian_card_client.py)
на mock-транспорте httpx: ни одного реального запроса.

Протокол — как у самой страницы mpi.gc.ge/page1/ (приложение): GET page1,
GET settings.js, POST /token, POST /payment/{token}/start; сумма в тетри.
Главное — клиент делает только шаги браузера до формы карты: никаких полей
карты, accept/decline/3ds, запросов к police.ge или чужим хостам."""

import asyncio
import logging
import sys
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

from reader.fines.georgian_card_client import (  # noqa: E402
    AMOUNT_UNIT_MAJOR,
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

_RECEIPT = "კბ0099887766"
_CAR = "AA111BB"
_TOKEN = "TOKEN0123456789"
_CONFIG = GeorgianCardConfig(merchant_id="MERCH1", page_id="PAGE1")
_SETTINGS = '''window.settings = {
    baseUrl: "page1/",
    openApi: {
    baseUrl: "https://mpi.gc.ge/open/api/",
    version: "v4",
    portal: "PORTAL1",
  },
  enableApiLangParam: true,
};'''
_TOKEN_PATH = "/open/api//v4/PORTAL1/token"
_START_PATH = f"/open/api//v4/PORTAL1/payment/{_TOKEN}/start"
_SHELL = "<html><head><title>Bank of Georgia</title></head><body><div id='app'></div></body></html>"


def _offer(**overrides):
    payload = {
        "state": "offer", "amount": 15000, "currency": "981",
        "description": f"ჯარიმის ქვითარი {_RECEIPT}", "options": {"lang": "ka"},
    }
    payload.update(overrides)
    return payload


class _Gateway:
    """Mock Georgian Card: записывает каждый запрос и отвечает по сценарию."""

    def __init__(self, *, page1=None, settings=None, token=None, start=None):
        self.requests: list[httpx.Request] = []
        self.page1 = page1 or (lambda request: httpx.Response(200, text=_SHELL))
        self.settings = settings or (lambda request: httpx.Response(200, text=_SETTINGS))
        self.token = token or (lambda request: httpx.Response(200, json={"token": _TOKEN}))
        self.start = start or (lambda request: httpx.Response(200, json=_offer()))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "mpi.gc.ge", f"request to a foreign host: {request.url.host}"
        path = request.url.raw_path.decode().split("?")[0]
        if path == "/page1/" and request.method == "GET":
            return self.page1(request)
        if path == "/page1/settings.js" and request.method == "GET":
            return self.settings(request)
        if path == _TOKEN_PATH and request.method == "POST":
            return self.token(request)
        if path == _START_PATH and request.method == "POST":
            return self.start(request)
        raise AssertionError(f"unexpected request {request.method} {path}")

    def start_body(self) -> dict:
        start = [r for r in self.requests if r.url.raw_path.decode().startswith(_START_PATH)]
        return dict(parse_qsl(start[-1].content.decode("utf-8"), keep_blank_values=True))


def _fetch(gateway: _Gateway, *, config=_CONFIG, base_amount=50.0):
    client = GeorgianCardAmountClient(config, transport=httpx.MockTransport(gateway.handler))
    return asyncio.run(client.fetch_amount(car_number=_CAR, protocol_no=_RECEIPT, base_amount=base_amount))


def test_amount_is_read_through_the_page_api_like_the_browser():
    gateway = _Gateway()
    quote = _fetch(gateway)
    assert (quote.outcome, quote.amount, quote.currency) == (OUTCOME_OK, 150.0, "GEL")  # 15000 тетри
    assert [(r.method, r.url.raw_path.decode().split("?")[0]) for r in gateway.requests] == [
        ("GET", "/page1/"), ("GET", "/page1/settings.js"), ("POST", _TOKEN_PATH), ("POST", _START_PATH),
    ]
    assert gateway.requests[2].content == b""  # /token без тела
    for request in gateway.requests[2:]:  # как у страницы: форма + XMLHttpRequest
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded; charset=UTF-8"
    assert gateway.requests[3].content.startswith(b"merchantId=MERCH1&state.redirect=post_params&")


def test_start_body_is_exactly_what_the_page_sends_and_carries_no_card_fields():
    gateway = _Gateway()
    _fetch(gateway)
    body = gateway.start_body()
    assert body == {
        "merchantId": "MERCH1", "state.redirect": "post_params", "state.in_progress": "no",
        "back_url_s": "https://police.ge/protocol/success.php",
        "back_url_f": f"https://police.ge/protocol/fail.php?id={_RECEIPT}",
        "lang": "ka", "3ds2.supported": "true",
        "params.protocolId": _RECEIPT, "params.vehicleNumber": _CAR, "params.resourceAlias": "1",
        "returnUrl": body["returnUrl"],
    }
    assert body["returnUrl"].endswith(f"#{_TOKEN}") and body["returnUrl"].startswith("https://mpi.gc.ge/page1/?")
    for request in gateway.requests:
        text = request.content.decode("utf-8", errors="ignore").lower()
        for card_field in ('"pan"', "expiry", '"csc"', "cvc", "cardholder", "src.type"):
            assert card_field not in text


def test_page1_url_is_the_police_ge_payment_link():
    url = build_page1_url(_CONFIG, car_number=_CAR, protocol_no=_RECEIPT)
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert (parts.scheme, parts.hostname, parts.path) == ("https", "mpi.gc.ge", "/page1/")
    assert query["merch_id"] == ["MERCH1"] and query["page_id"] == ["PAGE1"] and query["lang_code"] == ["ka"]
    assert query["o.protocolId"] == [_RECEIPT] and query["o.vehicleNumber"] == [_CAR]
    assert query["o.resourceAlias"] == ["1"]
    assert query["back_url_s"] == ["https://police.ge/protocol/success.php"]
    assert query["back_url_f"] == [f"https://police.ge/protocol/fail.php?id={_RECEIPT}"]
    assert "back_url_f=https://police.ge/protocol/fail.php?id=" in url  # как склеивает police.ge


def test_amount_unit_lari_when_configured():
    gateway = _Gateway(start=lambda request: httpx.Response(200, json=_offer(amount="150.00")))
    quote = _fetch(gateway, config=GeorgianCardConfig(merchant_id="M", page_id="P", amount_unit=AMOUNT_UNIT_MAJOR))
    assert (quote.outcome, quote.amount) == (OUTCOME_OK, 150.0)


def test_wrong_unit_is_rejected_as_implausible_not_saved():
    gateway = _Gateway(start=lambda request: httpx.Response(200, json=_offer(amount=15000)))
    quote = _fetch(gateway, config=GeorgianCardConfig(merchant_id="M", page_id="P", amount_unit=AMOUNT_UNIT_MAJOR))
    assert quote.outcome == OUTCOME_INVALID_RESPONSE and quote.amount is None


def test_small_fine_read_in_the_wrong_unit_is_rejected():
    gateway = _Gateway(start=lambda request: httpx.Response(200, json=_offer(amount=1000)))
    quote = _fetch(gateway, config=GeorgianCardConfig(merchant_id="M", page_id="P", amount_unit=AMOUNT_UNIT_MAJOR),
                   base_amount=10.0)
    assert quote.outcome == OUTCOME_INVALID_RESPONSE


@pytest.mark.parametrize("currency,outcome", [("GEL", OUTCOME_OK), ("981", OUTCOME_OK), ("USD", OUTCOME_CURRENCY_MISMATCH),
                                              ("840", OUTCOME_CURRENCY_MISMATCH)])
def test_only_gel_is_accepted(currency, outcome):
    gateway = _Gateway(start=lambda request: httpx.Response(200, json=_offer(currency=currency)))
    assert _fetch(gateway).outcome == outcome


def test_receipt_only_in_our_own_links_does_not_confirm_the_answer():
    gateway = _Gateway(start=lambda request: httpx.Response(200, json={
        "state": "offer", "amount": 15000, "currency": "981",
        "returnUrl": f"https://mpi.gc.ge/page1/?o.protocolId={_RECEIPT}",
        "backUrl": f"https://police.ge/protocol/fail.php?id={_RECEIPT}",
    }))
    assert _fetch(gateway).outcome == OUTCOME_RECEIPT_UNCONFIRMED


def test_receipt_echoed_in_order_params_confirms_the_answer():
    gateway = _Gateway(start=lambda request: httpx.Response(200, json={
        "state": "offer", "amount": 15000, "currency": "GEL", "params": {"protocolId": _RECEIPT},
    }))
    assert _fetch(gateway).outcome == OUTCOME_OK


def test_redirect_state_to_the_fail_page_is_a_refusal():
    gateway = _Gateway(start=lambda request: httpx.Response(200, json={
        "state": "redirect", "url": f"https://police.ge/protocol/fail.php?id={_RECEIPT}", "post": True,
    }))
    assert _fetch(gateway).outcome == OUTCOME_REFUSED


def test_result_state_without_a_card_is_a_refusal_never_a_payment():
    gateway = _Gateway(start=lambda request: httpx.Response(200, json={"state": "result", "status": "FAILED"}))
    quote = _fetch(gateway)
    assert quote.outcome == OUTCOME_REFUSED and quote.amount is None


def test_unknown_state_is_not_an_amount():
    gateway = _Gateway(start=lambda request: httpx.Response(200, json=_offer(state="iframe")))
    quote = _fetch(gateway)
    assert quote.outcome == OUTCOME_INVALID_RESPONSE and quote.detail == "state_iframe"


def test_start_error_is_reported_without_digits():
    gateway = _Gateway(start=lambda request: httpx.Response(400, json={"error": f"BAD_ORDER {_RECEIPT}"}))
    quote = _fetch(gateway)
    assert quote.outcome == OUTCOME_INVALID_RESPONSE
    assert quote.detail.startswith("start_http_400_") and "0099887766" not in quote.detail


@pytest.mark.parametrize("step", ["page1", "settings", "token", "start"])
def test_rate_limit_or_block_at_any_step_is_reported(step):
    gateway = _Gateway(**{step: lambda request: httpx.Response(403)})
    assert _fetch(gateway).outcome == OUTCOME_BLOCKED


def test_waf_page_is_a_block_and_nothing_else_is_requested():
    gateway = _Gateway(page1=lambda request: httpx.Response(
        200, text="<title>The URL you requested has been blocked</title> Attack ID: 20000051",
    ))
    quote = _fetch(gateway)
    assert quote.outcome == OUTCOME_BLOCKED and quote.detail == "waf_block"
    assert len(gateway.requests) == 1


def test_unrecognized_settings_stop_before_any_api_call():
    gateway = _Gateway(settings=lambda request: httpx.Response(200, text="window.settings = {};"))
    assert _fetch(gateway).outcome == OUTCOME_INVALID_RESPONSE
    assert [r.method for r in gateway.requests] == ["GET", "GET"]


def test_api_on_a_foreign_host_in_settings_is_never_called():
    gateway = _Gateway(settings=lambda request: httpx.Response(200, text=_SETTINGS.replace(
        "https://mpi.gc.ge/open/api/", "https://evil.example/open/api/",
    )))
    quote = _fetch(gateway)
    assert quote.outcome == OUTCOME_INVALID_RESPONSE and quote.detail == "settings_foreign_api"
    assert [r.method for r in gateway.requests] == ["GET", "GET"]


@pytest.mark.parametrize("payload", [{}, {"token": "bad token!"}, {"token": 12345678}])
def test_missing_or_malformed_token_stops_before_start(payload):
    gateway = _Gateway(token=lambda request: httpx.Response(200, json=payload))
    assert _fetch(gateway).outcome == OUTCOME_INVALID_RESPONSE
    assert all(not r.url.raw_path.decode().endswith("/start") for r in gateway.requests)


def test_timeout():
    def token(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert _fetch(_Gateway(token=token)).outcome == OUTCOME_TIMEOUT


def test_zero_amount_and_missing_amount_are_not_an_amount():
    assert _fetch(_Gateway(start=lambda r: httpx.Response(200, json=_offer(amount=0)))).outcome == OUTCOME_INVALID_RESPONSE
    payload = _offer()
    del payload["amount"]
    assert _fetch(_Gateway(start=lambda r: httpx.Response(200, json=payload))).outcome == OUTCOME_INVALID_RESPONSE


def test_start_answer_that_is_not_json():
    gateway = _Gateway(start=lambda request: httpx.Response(200, text="<html>oops</html>"))
    assert _fetch(gateway).outcome == OUTCOME_INVALID_RESPONSE


def test_not_configured_sends_nothing():
    gateway = _Gateway()
    quote = _fetch(gateway, config=GeorgianCardConfig())
    assert quote.request_sent is False and quote.outcome == OUTCOME_ERROR
    assert gateway.requests == []


@pytest.mark.parametrize("base_url", ["https://police.ge", "https://mpi.gc.ge.evil.example", "http://mpi.gc.ge",
                                      "https://mpi.gc.ge:8443"])
def test_misconfigured_gateway_host_sends_nothing(base_url):
    gateway = _Gateway()
    quote = _fetch(gateway, config=GeorgianCardConfig(base_url=base_url, merchant_id="M", page_id="P"))
    assert quote.request_sent is False and gateway.requests == []


def test_redirect_to_the_fail_page_is_a_refusal_and_police_ge_is_never_requested():
    gateway = _Gateway(page1=lambda request: httpx.Response(
        302, headers={"location": "https://police.ge/protocol/fail.php?id=1"},
    ))
    assert _fetch(gateway).outcome == OUTCOME_REFUSED
    assert [request.url.host for request in gateway.requests] == ["mpi.gc.ge"]


def test_redirect_to_a_foreign_host_is_not_followed():
    gateway = _Gateway(page1=lambda request: httpx.Response(302, headers={"location": "https://evil.example/x"}))
    quote = _fetch(gateway)
    assert quote.outcome == OUTCOME_INVALID_RESPONSE and len(gateway.requests) == 1


def _chain(hops: int):
    def page1(request):
        step = int(request.url.params.get("hop", "0"))
        if step < hops:
            return httpx.Response(302, headers={"location": f"https://mpi.gc.ge/page1/?hop={step + 1}"})
        return httpx.Response(200, text=_SHELL)

    return page1


def test_exactly_the_allowed_number_of_redirects_is_followed():
    assert _fetch(_Gateway(page1=_chain(6))).outcome == OUTCOME_OK
    quote = _fetch(_Gateway(page1=_chain(7)))
    assert quote.outcome == OUTCOME_INVALID_RESPONSE and quote.detail == "too_many_redirects"


_ALLOWED_BODY = {"merchantId": "M", "state.redirect": "post_params", "params.protocolId": "1", "3ds2.supported": True}


@pytest.mark.parametrize("method,url,body", [
    ("POST", "https://mpi.gc.ge/open/api//v4/P/payment/TOKEN12345/accept", {}),
    ("POST", "https://mpi.gc.ge/open/api//v4/P/payment/TOKEN12345/decline", None),
    ("POST", "https://mpi.gc.ge/open/api//v4/P/payment/TOKEN12345/3ds2-prepare/accept", {}),
    ("POST", "https://mpi.gc.ge/open/api//v4/P/payment/TOKEN12345/start", {**_ALLOWED_BODY, "pan": "4000000000000001"}),
    ("POST", "https://mpi.gc.ge/open/api//v4/P/payment/TOKEN12345/start", {**_ALLOWED_BODY, "src.type": "card"}),
    ("POST", "https://mpi.gc.ge/open/api//v4/P/payment/TOKEN12345/start", {"merchantId": {"nested": 1}}),
    ("POST", "https://mpi.gc.ge/open/api//v4/P/token", {"x": "1"}),
    ("POST", "https://mpi.gc.ge/page1/", None),
    ("PUT", "https://mpi.gc.ge/open/api//v4/P/token", None),
    ("GET", "https://police.ge/protocol/fail.php", None),
    ("GET", "http://mpi.gc.ge/page1/", None),
    ("GET", "https://mpi.gc.ge:8443/page1/", None),
    ("GET", "https://mpi.gc.ge.evil.example/page1/", None),
])
def test_guard_blocks_anything_but_reading_the_amount(method, url, body):
    with pytest.raises(GeorgianCardSafetyError):
        _guard_request(method, url, gc_host="mpi.gc.ge", json_body=body)


def test_guard_allows_exactly_the_browser_steps_and_refuses_a_bad_config_host():
    _guard_request("GET", "https://mpi.gc.ge/page1/?a=1", gc_host="mpi.gc.ge", json_body=None)
    _guard_request("POST", "https://mpi.gc.ge" + _TOKEN_PATH, gc_host="mpi.gc.ge", json_body=None)
    _guard_request("POST", "https://mpi.gc.ge" + _START_PATH, gc_host="mpi.gc.ge", json_body=_ALLOWED_BODY)
    with pytest.raises(GeorgianCardSafetyError):
        _guard_request("GET", "https://police.ge/protocol/index.php", gc_host="police.ge", json_body=None)


def test_httpx_request_log_never_contains_the_plate_receipt_or_session_token(caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    gateway = _Gateway()
    assert _fetch(gateway).outcome == OUTCOME_OK
    assert "HTTP Request" in caplog.text
    for secret in ("AA111BB", _TOKEN, "MERCH1", "PAGE1", "0099887766"):
        assert secret not in caplog.text
    assert "https://mpi.gc.ge/<redacted>" in caplog.text


def test_trace_and_detail_never_contain_the_plate_receipt_or_token():
    for gateway in (
        _Gateway(),
        _Gateway(start=lambda request: httpx.Response(200, json=_offer(state=f"x{_RECEIPT}"))),
        _Gateway(start=lambda request: httpx.Response(200, json={"state": "offer", "amount": 15000, "currency": "981"})),
        _Gateway(page1=lambda request: httpx.Response(403)),
    ):
        quote = _fetch(gateway)
        text = " ".join(quote.trace) + " " + (quote.detail or "")
        for secret in ("AA111BB", _TOKEN, "0099887766"):
            assert secret not in text
    assert any("state offer" in step and "amount 15000" in step for step in _fetch(_Gateway()).trace)
