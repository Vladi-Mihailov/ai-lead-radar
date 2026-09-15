"""
Тесты AvrasyaSession — HTTP-транспорт avrasyatuneli.com. Реальная сеть не
используется: httpx.AsyncClient подключён к httpx.MockTransport (тот же
приём, что и tests/test_turkey_gib_session.py), который отдаёт
заготовленные ответы и запоминает, что именно было запрошено.

_MIN_REQUEST_INTERVAL_SECONDS патчится на 0 во всех тестах — иначе каждый
тест с несколькими запросами реально ждал бы секунды (см. design report
Stage 1: лимит "1 запрос/сек" применяется КОНСЕРВАТИВНО перед каждым
запросом, включая первый, см. session.py::_throttle) — механика throttle
сама по себе покрыта отдельным тестом ниже через monkeypatch time.monotonic.
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

import reader.turkey_bot.avrasya.session as avrasya_session  # noqa: E402
from reader.turkey_bot.avrasya.session import (  # noqa: E402
    AvrasyaRateLimitedError,
    AvrasyaSession,
    AvrasyaTransportError,
)

_PAGE_URL = "https://www.avrasyatuneli.com/ihlalli-gecis-odemesi/"
_CAPTCHA_URL = "https://www.avrasyatuneli.com/Captcha.ashx?uid=DebtQueryByPlate"
_QUERY_URL = "https://www.avrasyatuneli.com/api/debt/query"

_SAMPLE_JPEG = bytes.fromhex("ffd8ffe000104a4649460001010100480048" + "00" * 20 + "ffd9")


@pytest.fixture(autouse=True)
def _no_throttle_delay(monkeypatch):
    monkeypatch.setattr(avrasya_session, "_MIN_REQUEST_INTERVAL_SECONDS", 0.0)


class _CallLog:
    def __init__(self):
        self.calls: list[dict] = []


def _make_session(handler, *, timeout: float = 5) -> AvrasyaSession:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return AvrasyaSession(client, request_timeout=timeout)


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


async def test_start_loads_page_then_captcha_and_returns_raw_bytes():
    log = _CallLog()
    handler = _handler_for(
        captcha_responses=[
            httpx.Response(200, content=_SAMPLE_JPEG, headers={"content-type": "image/jpeg"})
        ],
        log=log,
    )
    session = _make_session(handler)

    challenge = await session.start()

    assert challenge.image_png == _SAMPLE_JPEG
    assert [c["url"] for c in log.calls] == [_PAGE_URL, _CAPTCHA_URL]


async def test_refresh_captcha_does_not_reload_page():
    log = _CallLog()
    handler = _handler_for(
        captcha_responses=[
            httpx.Response(200, content=_SAMPLE_JPEG, headers={"content-type": "image/jpeg"})
        ],
        log=log,
    )
    session = _make_session(handler)

    await session.refresh_captcha()

    assert [c["url"] for c in log.calls] == [_CAPTCHA_URL]


async def test_start_raises_transport_error_when_page_load_fails():
    log = _CallLog()
    handler = _handler_for(page_status=500, captcha_responses=[], log=log)
    session = _make_session(handler)

    with pytest.raises(AvrasyaTransportError):
        await session.start()

    assert [c["url"] for c in log.calls] == [_PAGE_URL]


async def test_refresh_captcha_raises_when_content_type_is_not_image():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not an image</html>")

    session = _make_session(handler)

    with pytest.raises(AvrasyaTransportError):
        await session.refresh_captcha()


async def test_refresh_captcha_raises_transport_error_on_connection_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    session = _make_session(handler)

    with pytest.raises(AvrasyaTransportError):
        await session.refresh_captcha()


def _submit_session(handler) -> AvrasyaSession:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return AvrasyaSession(client, request_timeout=5)


async def test_submit_sends_expected_payload_shape_and_headers():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = request.content
        captured["headers"] = dict(request.headers)
        return httpx.Response(400, json={"Messages": []})

    session = _submit_session(handler)

    await session.submit(plate="A123AA123", captcha_code="123456")

    assert captured["url"] == _QUERY_URL
    assert captured["method"] == "POST"
    assert json.loads(captured["body"]) == {
        "queryType": "ByPlate",
        "queryValue": "A123AA123",
        "queryValue2": "",
        "queryValue3": "",
        "captcha": "123456",
    }
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["headers"]["lang"] == "tr"
    assert captured["headers"]["referer"] == _PAGE_URL
    # Секрет здесь и не мог оказаться: анонимный запрос отправляет
    # буквальную строку "Bearer null" (см. session.py докстрок) — не
    # настоящий токен.
    assert captured["headers"]["authorization"] == "Bearer null"


async def test_submit_returns_status_and_json_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"Messages": [{"PropertyName": "Captcha"}]})

    session = _submit_session(handler)

    status_code, body = await session.submit(plate="A123AA123", captcha_code="000000")

    assert status_code == 400
    assert body == {"Messages": [{"PropertyName": "Captcha"}]}


async def test_submit_returns_status_and_text_body_when_not_json():
    """См. design report Stage 1: HTTP 429 приходит обычным текстом, не
    JSON — но 429 конкретно перехватывается ОТДЕЛЬНО (см. следующий тест),
    здесь проверяется общий fallback на текст для ЛЮБОГО другого
    нестандартного статуса/тела."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    session = _submit_session(handler)

    status_code, body = await session.submit(plate="A123AA123", captcha_code="000000")

    assert status_code == 500
    assert body == "Internal Server Error"


async def test_submit_raises_rate_limited_error_on_429():
    """РЕАЛЬНО увиденный вживую (curl, design report Stage 1) ответ:
    "API calls quota exceeded! maximum admitted 1 per Second." (обычный
    текст) — не должен попасть в parser.py как бизнес-исход."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, text="API calls quota exceeded! maximum admitted 1 per Second."
        )

    session = _submit_session(handler)

    with pytest.raises(AvrasyaRateLimitedError):
        await session.submit(plate="A123AA123", captcha_code="000000")


async def test_submit_raises_transport_error_on_connection_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    session = _submit_session(handler)

    with pytest.raises(AvrasyaTransportError):
        await session.submit(plate="A123AA123", captcha_code="000000")


async def test_throttle_waits_at_least_the_minimum_interval(monkeypatch):
    """Механика _throttle сама по себе (не мокая её нулём, в отличие от
    остальных тестов файла) — реальный тест на секунды был бы медленным,
    поэтому здесь монки патчатся time.monotonic (детерминированные тики)
    и asyncio.sleep (чтобы не спать по-настоящему), а проверяется именно
    ЗАПРОШЕННАЯ длительность сна."""
    monkeypatch.setattr(avrasya_session, "_MIN_REQUEST_INTERVAL_SECONDS", 1.5)

    fake_clock = {"t": 0.0}

    def fake_monotonic():
        return fake_clock["t"]

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        fake_clock["t"] += seconds

    monkeypatch.setattr(avrasya_session.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(avrasya_session.asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_SAMPLE_JPEG, headers={"content-type": "image/jpeg"})

    session = _make_session(handler)

    await session.refresh_captcha()
    fake_clock["t"] += 0.1  # только 0.1s прошло между запросами
    await session.refresh_captcha()

    assert sleeps == [pytest.approx(1.4)]
