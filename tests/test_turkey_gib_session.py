"""
Тесты GibSession — HTTP-транспорт dijital.gib.gov.tr. Реальная сеть не
используется: httpx.AsyncClient подключён к httpx.MockTransport (тот же
приём, что и tests/test_police_ge_session.py), который отдаёт заготовленные
ответы и запоминает, что именно было запрошено (url/метод/тело).
"""

import base64
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

from reader.turkey_bot.gib.session import GibSession, GibTransportError  # noqa: E402

_PAGE_URL = "https://dijital.gib.gov.tr/hizliOdemeler/yabanciAracOdemeleri"
_CAPTCHA_URL = "https://dijital.gib.gov.tr/apigateway/captcha/getnewcaptcha"
_SUBMIT_URL = "https://dijital.gib.gov.tr/apigateway/payment/verification/with-mys-borc-list"

# 1x1 PNG - минимальный валидный образ, реальный формат неважен для этих
# тестов (см. design report Stage 1 - реальная картинка уже проверена
# вживую и сохранена отдельно, здесь важна только механика транспорта).
_SAMPLE_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763f8ffff3f0005fe02fea739663d0000000049454e44ae426082"
)


class _CallLog:
    def __init__(self):
        self.calls: list[dict] = []


def _captcha_json(*, image_id: str = "cid-1", image_png: bytes = _SAMPLE_PNG) -> dict:
    return {
        "captchaImgBase64": base64.b64encode(image_png).decode("ascii"),
        "cid": image_id,
        "messages": None,
    }


def _make_session(handler, *, timeout: float = 5) -> GibSession:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return GibSession(client, request_timeout=timeout)


def _handler_for(*, page_status: int = 200, captcha_responses: list[httpx.Response], log: _CallLog):
    captcha_iter = iter(captcha_responses)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _PAGE_URL:
            log.calls.append({"url": url, "method": request.method})
            return httpx.Response(page_status, text="<html></html>")
        if url == _CAPTCHA_URL:
            log.calls.append({"url": url, "method": request.method})
            return next(captcha_iter)
        raise AssertionError(f"unexpected request to {url}")

    return handler


async def test_start_loads_page_then_captcha_and_decodes_image():
    log = _CallLog()
    handler = _handler_for(
        captcha_responses=[httpx.Response(200, json=_captcha_json())], log=log,
    )
    session = _make_session(handler)

    challenge = await session.start()

    assert challenge.image_id == "cid-1"
    assert challenge.image_png == _SAMPLE_PNG
    assert [c["url"] for c in log.calls] == [_PAGE_URL, _CAPTCHA_URL]


async def test_refresh_captcha_does_not_reload_page():
    log = _CallLog()
    handler = _handler_for(
        captcha_responses=[httpx.Response(200, json=_captcha_json(image_id="cid-2"))], log=log,
    )
    session = _make_session(handler)

    challenge = await session.refresh_captcha()

    assert challenge.image_id == "cid-2"
    assert [c["url"] for c in log.calls] == [_CAPTCHA_URL]


async def test_start_raises_transport_error_when_page_load_fails():
    log = _CallLog()
    handler = _handler_for(page_status=500, captcha_responses=[], log=log)
    session = _make_session(handler)

    with pytest.raises(GibTransportError):
        await session.start()

    # CAPTCHA не запрашивается вовсе, если сама страница не загрузилась.
    assert [c["url"] for c in log.calls] == [_PAGE_URL]


async def test_refresh_captcha_raises_on_invalid_json():
    log = _CallLog()
    handler = _handler_for(
        captcha_responses=[httpx.Response(200, text="not json")], log=log,
    )
    session = _make_session(handler)

    with pytest.raises(GibTransportError):
        await session.refresh_captcha()


async def test_refresh_captcha_raises_when_cid_missing():
    log = _CallLog()
    payload = _captcha_json()
    del payload["cid"]
    handler = _handler_for(captcha_responses=[httpx.Response(200, json=payload)], log=log)
    session = _make_session(handler)

    with pytest.raises(GibTransportError):
        await session.refresh_captcha()


async def test_refresh_captcha_raises_when_image_base64_missing():
    log = _CallLog()
    payload = _captcha_json()
    del payload["captchaImgBase64"]
    handler = _handler_for(captcha_responses=[httpx.Response(200, json=payload)], log=log)
    session = _make_session(handler)

    with pytest.raises(GibTransportError):
        await session.refresh_captcha()


async def test_refresh_captcha_raises_on_invalid_base64():
    log = _CallLog()
    payload = _captcha_json()
    payload["captchaImgBase64"] = "%%%not-base64%%%"
    handler = _handler_for(captcha_responses=[httpx.Response(200, json=payload)], log=log)
    session = _make_session(handler)

    with pytest.raises(GibTransportError):
        await session.refresh_captcha()


def _submit_session(handler) -> GibSession:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return GibSession(client, request_timeout=5)


async def test_submit_sends_expected_payload_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content.decode())
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"data": []})

    session = _submit_session(handler)

    await session.submit(plate="A123AA123", image_id="cid-9", captcha_code="g8fyx")

    assert captured["url"] == _SUBMIT_URL
    assert captured["method"] == "POST"
    assert captured["body"] == {
        "meta": {"pagination": {"pageNo": 1, "pageSize": 15}},
        "data": {
            "plaka": "A123AA123",
            "pasaport": "",
            "ulkeKodu": "",
            "sposEkranTipi": "0",
            "kkOrtam": "TDVD_WB_OUT",
            "imageId": "cid-9",
            "securityCode": "g8fyx",
        },
    }
    assert captured["headers"]["accept-language"] == "tr-TR"


async def test_submit_returns_body_even_on_non_200_status():
    """См. design report Stage 2: фронтенд разбирает JSON независимо от
    HTTP-статуса (общий envelope апигейта) - GibSession должен вести себя
    так же, не превращая "не-200 с валидным JSON" в транспортную ошибку."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"messages": [{"type": "ERROR", "text": "bad code"}]})

    session = _submit_session(handler)

    result = await session.submit(plate="A123AA123", image_id="cid-9", captcha_code="wrong")

    assert result == {"messages": [{"type": "ERROR", "text": "bad code"}]}


async def test_submit_raises_transport_error_on_invalid_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    session = _submit_session(handler)

    with pytest.raises(GibTransportError):
        await session.submit(plate="A123AA123", image_id="cid-9", captcha_code="g8fyx")


async def test_submit_raises_transport_error_on_connection_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    session = _submit_session(handler)

    with pytest.raises(GibTransportError):
        await session.submit(plate="A123AA123", image_id="cid-9", captcha_code="g8fyx")
