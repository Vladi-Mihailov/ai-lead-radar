"""Тесты reader/checkout/tpl_client.py — реальная сеть не используется,
httpx.AsyncClient подключён к httpx.MockTransport."""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

from reader.checkout.models import PaymentBank, TplPolicyPayload  # noqa: E402
from reader.checkout.tpl_client import TplGeClient, TplGeClientError  # noqa: E402
from reader.checkout.tpl_client import _RedactEcommerceUrlFilter  # noqa: E402


def _payload(**overrides) -> TplPolicyPayload:
    fields = dict(
        u_id="f4f3f015-31d4-4bc4-93d0-dfaf72572920",
        start_date="2026-08-15",
        frame_number="WVWZZZ1KZAW123456",
        vehicle_category_id=7,
        vehicle_registration_number="AA001AA",
        vehicle_manufacturer_id=147,
        vehicle_manufacturer_name="TOYOTA",
        vehicle_model_id=10574,
        vehicle_model_name="TOYOTA CAMRY",
        product_id=2,
        insurer_title="Test Testovich Testadze",
        insurer_identification_number="01024012345",
        insurer_email="test@example.com",
        insurer_phone="599000000",
        insurer_citizenship_id=1,
        vehicle_owner_title="Owner Testovich",
        vehicle_owner_identification_number="01024012347",
        vehicle_owner_email="owner@example.com",
        vehicle_owner_phone="599000002",
        vehicle_owner_citizenship_id=1,
        vehicle_driver_title="Driver Testovich",
        vehicle_driver_identification_number="01024012346",
        vehicle_driver_email="driver@example.com",
        vehicle_driver_phone="599000001",
        vehicle_driver_citizenship_id=1,
        visitor_id="q0LScFWlVC3TNke8Hx4d",
    )
    fields.update(overrides)
    return TplPolicyPayload(**fields)


def _client_with_handler(handler) -> tuple[TplGeClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def logging_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(logging_handler)
    return TplGeClient(httpx.AsyncClient(transport=transport)), requests


# ---- create_policy ----


async def test_create_policy_posts_exact_payload_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    client, requests = _client_with_handler(handler)

    await client.create_policy(_payload())

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://web-back.tpl.ge/api/policies"

    import json

    body = json.loads(request.content)
    assert body == {
        "uId": "f4f3f015-31d4-4bc4-93d0-dfaf72572920",
        "startDate": "2026-08-15",
        "frameNumber": "WVWZZZ1KZAW123456",
        "vehicleCategoryId": 7,
        "vehicleRegistrationNumber": "AA001AA",
        "vehicleManufacturerId": 147,
        "vehicleManufacturerName": "TOYOTA",
        "vehicleModelId": 10574,
        "vehicleModelName": "TOYOTA CAMRY",
        "productId": 2,
        "insurerType": "I",
        "insurerTitle": "Test Testovich Testadze",
        "insurerIdentificationNumber": "01024012345",
        "insurerEmail": "test@example.com",
        "insurerPhone": "599000000",
        "insurerCitizenshipId": 1,
        "vehicleOwnerType": "I",
        "vehicleOwnerTitle": "Owner Testovich",
        "vehicleOwnerIdentificationNumber": "01024012347",
        "vehicleOwnerEmail": "owner@example.com",
        "vehicleOwnerPhone": "599000002",
        "vehicleOwnerCitizenshipId": 1,
        "vehicleDriverType": "I",
        "vehicleDriverTitle": "Driver Testovich",
        "vehicleDriverIdentificationNumber": "01024012346",
        "vehicleDriverEmail": "driver@example.com",
        "vehicleDriverPhone": "599000001",
        "vehicleDriverCitizenshipId": 1,
        "borderCrossId": None,
        "visitorId": "q0LScFWlVC3TNke8Hx4d",
        "lang": "ka",
    }


async def test_create_policy_raises_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400)

    client, _requests = _client_with_handler(handler)

    with pytest.raises(TplGeClientError):
        await client.create_policy(_payload())


async def test_create_policy_raises_on_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client, _requests = _client_with_handler(handler)

    with pytest.raises(TplGeClientError):
        await client.create_policy(_payload())


async def test_create_policy_raises_on_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client, _requests = _client_with_handler(handler)

    with pytest.raises(TplGeClientError):
        await client.create_policy(_payload())


# ---- get_payment_redirect_url ----


async def test_get_payment_redirect_url_returns_location_header():
    redirect_url = (
        "https://mpi.gc.ge/page1?merch_id=X&o.id=751ff69d-b9d6-4610-8135-2344a5b7f7fc&id=POS18647"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": redirect_url})

    client, requests = _client_with_handler(handler)

    result = await client.get_payment_redirect_url(
        u_id="f4f3f015-31d4-4bc4-93d0-dfaf72572920",
        bank=PaymentBank.BANK_OF_GEORGIA,
        payer_title="Test Testovich Testadze",
        payer_identification_number="01024012345",
    )

    assert result == redirect_url
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert str(request.url).startswith("https://ecommerce-api.tpl.ge/ecommerce/bog?")
    assert "policyUId=f4f3f015-31d4-4bc4-93d0-dfaf72572920" in str(request.url)
    assert "payerTitle=Test" in str(request.url)


async def test_get_payment_redirect_url_uses_uid_in_return_and_error_urls():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://mpi.gc.ge/page1"})

    client, requests = _client_with_handler(handler)

    await client.get_payment_redirect_url(
        u_id="abc-123",
        bank=PaymentBank.BANK_OF_GEORGIA,
        payer_title="Name",
        payer_identification_number="0000",
    )

    url = str(requests[0].url)
    assert "returnUrl=https%3A%2F%2Ftpl.ge%2Fka%2Fpolicies%2Fabc-123%2Fsuccess" in url
    assert "errorUrl=https%3A%2F%2Ftpl.ge%2Fka%2Fpolicies%2Fabc-123%2Ferror" in url


async def test_get_payment_redirect_url_raises_when_no_redirect_returned():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)  # ни один редирект не пришёл

    client, _requests = _client_with_handler(handler)

    with pytest.raises(TplGeClientError):
        await client.get_payment_redirect_url(
            u_id="abc-123", bank=PaymentBank.BANK_OF_GEORGIA,
            payer_title="Name", payer_identification_number="0000",
        )


async def test_get_payment_redirect_url_raises_when_location_header_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)  # редирект без Location — не должно бывать, но проверим

    client, _requests = _client_with_handler(handler)

    with pytest.raises(TplGeClientError):
        await client.get_payment_redirect_url(
            u_id="abc-123", bank=PaymentBank.BANK_OF_GEORGIA,
            payer_title="Name", payer_identification_number="0000",
        )


async def test_get_payment_redirect_url_raises_on_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client, _requests = _client_with_handler(handler)

    with pytest.raises(TplGeClientError):
        await client.get_payment_redirect_url(
            u_id="abc-123", bank=PaymentBank.BANK_OF_GEORGIA,
            payer_title="Name", payer_identification_number="0000",
        )


# ---- _RedactEcommerceUrlFilter (утечка PII в httpx INFO-логах) ----


def _make_httpx_request_record(url: str) -> logging.LogRecord:
    """Форма record'а, которую реально создаёт httpx._client при логировании
    запроса (см. docstring модуля tpl_client): logger.info('HTTP Request: %s
    %s "%s %d %s"', method, url, http_version, status_code, reason_phrase)."""
    return logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("GET", url, "HTTP/1.1", 302, "Found"),
        exc_info=None,
    )


def test_redact_filter_strips_query_string_from_ecommerce_url():
    record = _make_httpx_request_record(
        "https://ecommerce-api.tpl.ge/ecommerce/bog?lang=ka&policyUId=abc-123"
        "&payerTitle=Test+Testovich&payerIdentificationNumber=01024012345"
    )

    result = _RedactEcommerceUrlFilter().filter(record)

    assert result is True  # фильтр никогда не должен подавлять сам record
    formatted = record.getMessage()
    assert "payerTitle" not in formatted
    assert "01024012345" not in formatted
    assert "Test+Testovich" not in formatted
    assert "policyUId" not in formatted
    assert formatted == 'HTTP Request: GET https://ecommerce-api.tpl.ge/ecommerce/bog?<redacted> "HTTP/1.1 302 Found"'


def test_redact_filter_leaves_unrelated_requests_untouched():
    record = _make_httpx_request_record("https://web-back.tpl.ge/api/policies")

    result = _RedactEcommerceUrlFilter().filter(record)

    assert result is True
    assert record.getMessage() == (
        'HTTP Request: GET https://web-back.tpl.ge/api/policies "HTTP/1.1 302 Found"'
    )


def test_redact_filter_leaves_ecommerce_url_without_query_untouched():
    # На случай вызова без query-параметров — маркер есть, но нет "?", редактировать нечего.
    record = _make_httpx_request_record("https://ecommerce-api.tpl.ge/ecommerce/bog")

    _RedactEcommerceUrlFilter().filter(record)

    assert record.getMessage() == (
        'HTTP Request: GET https://ecommerce-api.tpl.ge/ecommerce/bog "HTTP/1.1 302 Found"'
    )


def test_redact_filter_ignores_records_without_args():
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg="some message without args", args=None, exc_info=None,
    )

    result = _RedactEcommerceUrlFilter().filter(record)

    assert result is True
    assert record.getMessage() == "some message without args"


async def test_ecommerce_request_pii_is_redacted_in_real_httpx_log_output(caplog):
    """End-to-end: реальный httpx-логгер, реальный AsyncClient — подтверждаем,
    что фильтр действительно установлен на logging.getLogger('httpx') и что
    payerTitle/payerIdentificationNumber не попадают в лог при настоящем
    вызове клиента, а НЕ только в изолированном юнит-тесте фильтра выше."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://mpi.gc.ge/page1"})

    client, _requests = _client_with_handler(handler)

    with caplog.at_level(logging.INFO, logger="httpx"):
        await client.get_payment_redirect_url(
            u_id="f4f3f015-31d4-4bc4-93d0-dfaf72572920",
            bank=PaymentBank.BANK_OF_GEORGIA,
            payer_title="Test Testovich Testadze",
            payer_identification_number="01024012345",
        )

    assert "payerTitle" not in caplog.text
    assert "01024012345" not in caplog.text
    assert "Test Testovich Testadze" not in caplog.text
    assert "ecommerce-api.tpl.ge/ecommerce/bog?<redacted>" in caplog.text


async def test_policies_request_log_unaffected_by_ecommerce_redaction(caplog):
    """Убеждаемся, что фильтр не глушит/не искажает логи НЕ-ecommerce запросов
    (см. задачу: "не отключай всё HTTP-логирование вслепую")."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    client, _requests = _client_with_handler(handler)

    with caplog.at_level(logging.INFO, logger="httpx"):
        await client.create_policy(_payload())

    assert "https://web-back.tpl.ge/api/policies" in caplog.text
    assert "<redacted>" not in caplog.text
