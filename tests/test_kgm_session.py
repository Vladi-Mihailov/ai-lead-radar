"""
Тесты KgmSession — HTTP-транспорт webihlaltakip.kgm.gov.tr. Реальная сеть
не используется: httpx.AsyncClient подключён к httpx.MockTransport (тот
же приём, что и tests/test_turkey_avrasya_session.py/
test_turkey_gib_session.py), который отдаёт заготовленные ответы и
запоминает, что именно было запрошено.
"""

import sys
from pathlib import Path
from urllib.parse import parse_qs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

import reader.turkey_bot.kgm.session as kgm_session  # noqa: E402
from reader.turkey_bot.kgm.session import KgmSession, KgmTransportError  # noqa: E402

_PAGE_URL = "https://webihlaltakip.kgm.gov.tr/WebIhlalSorgulama/Sayfalar/Sorgulama.aspx?lang=tr"
_CAPTCHA_URL = "https://webihlaltakip.kgm.gov.tr/WebIhlalSorgulama/Captcha/CaptchaImage.aspx"

_SAMPLE_JPEG = bytes.fromhex("ffd8ffe000104a4649460001010100480048" + "00" * 20 + "ffd9")

_FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"
_SUCCESS_DELTA = (_FIXTURES_DIR / "kgm_sorgulama_m295yb196_delta.txt").read_text(encoding="utf-8")

_SAMPLE_PAGE_HTML = """
<html><body><form>
<input type="hidden" name="__EVENTTARGET" id="__EVENTTARGET" value="" />
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="INITIAL-VIEWSTATE" />
<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="ABCDEF01" />
<input type="hidden" name="__VIEWSTATEENCRYPTED" id="__VIEWSTATEENCRYPTED" value="" />
<input type="hidden" name="hdnx" id="hdnx" value="111111111" />
</form></body></html>
"""


@pytest.fixture(autouse=True)
def _no_throttle_delay(monkeypatch):
    monkeypatch.setattr(kgm_session, "_MIN_REQUEST_INTERVAL_SECONDS", 0.0)


class _CallLog:
    def __init__(self):
        self.calls: list[dict] = []


def _make_session(handler, *, timeout: float = 5) -> KgmSession:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return KgmSession(client, request_timeout=timeout)


def _handler_for(*, page_status: int = 200, page_html: str = _SAMPLE_PAGE_HTML,
                  captcha_responses: list[httpx.Response], log: _CallLog):
    captcha_iter = iter(captcha_responses)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _PAGE_URL:
            log.calls.append({"url": url, "method": request.method})
            return httpx.Response(page_status, text=page_html)
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


async def test_start_captures_viewstate_and_hdnx_from_html_page():
    log = _CallLog()
    handler = _handler_for(
        captcha_responses=[
            httpx.Response(200, content=_SAMPLE_JPEG, headers={"content-type": "image/jpeg"})
        ],
        log=log,
    )
    session = _make_session(handler)

    await session.start()

    assert session._viewstate == "INITIAL-VIEWSTATE"
    assert session._viewstate_generator == "ABCDEF01"
    assert session._viewstate_encrypted == ""
    assert session._hdnx == "111111111"


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

    with pytest.raises(KgmTransportError):
        await session.start()


async def test_refresh_captcha_raises_when_content_type_is_not_image():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not an image</html>")

    session = _make_session(handler)

    with pytest.raises(KgmTransportError):
        await session.refresh_captcha()


async def test_refresh_captcha_raises_transport_error_on_connection_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    session = _make_session(handler)

    with pytest.raises(KgmTransportError):
        await session.refresh_captcha()


def _submit_session(handler, *, viewstate="VS", generator="GEN", hdnx="123") -> KgmSession:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    session = KgmSession(client, request_timeout=5)
    session._viewstate = viewstate
    session._viewstate_generator = generator
    session._viewstate_encrypted = ""
    session._hdnx = hdnx
    return session


async def test_submit_sends_expected_async_postback_fields_and_headers():
    """См. design report "Реализация KGM provider" п.2 — ВСЕ поля ниже
    реально увидены вживую в HAR (plate M295YB196), ни одно не придумано."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url).split("?")[0]
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["form"] = {
            k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()
        }
        return httpx.Response(200, text=_SUCCESS_DELTA, headers={"content-type": "text/plain"})

    session = _submit_session(handler, viewstate="REAL-VS", generator="REAL-GEN", hdnx="292753556")

    await session.submit(plate="M295YB196", captcha_code="15 53893")

    assert captured["method"] == "POST"
    assert captured["headers"]["x-microsoftajax"] == "Delta=true"
    assert captured["headers"]["x-requested-with"] == "XMLHttpRequest"
    form = captured["form"]
    assert form["ScriptManager1"] == "pnlUpdate|btnSorgula"
    assert form["txtPlk"] == "M295YB196"
    assert form["txtimgcode"] == "15 53893"
    assert form["chkGuvenlikUyari"] == "on"
    assert form["txtGuvenlikCheck"] == "Okay"
    assert form["__ASYNCPOST"] == "true"
    assert form["btnSorgula"] == "Sorgula"
    # Динамические поля НЕ хардкодятся — берутся из состояния сессии (см.
    # задачу: "НЕ хардкодить динамические ViewState/hdnx/cookies").
    assert form["__VIEWSTATE"] == "REAL-VS"
    assert form["__VIEWSTATEGENERATOR"] == "REAL-GEN"
    assert form["hdnx"] == "292753556"


async def test_submit_returns_raw_response_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_SUCCESS_DELTA, headers={"content-type": "text/plain"})

    session = _submit_session(handler)

    response_text = await session.submit(plate="M295YB196", captcha_code="1 2")

    assert response_text == _SUCCESS_DELTA


async def test_submit_updates_viewstate_and_hdnx_from_delta_response_for_next_request():
    """См. design report: после submit() (например, "rejected" -> нужен
    ещё один submit в той же сессии) следующий запрос должен нести
    ОБНОВЛЁННЫЕ __VIEWSTATE/hdnx, а не значения с первого GET."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_SUCCESS_DELTA, headers={"content-type": "text/plain"})

    session = _submit_session(handler, viewstate="OLD-VS", generator="OLD-GEN", hdnx="000")

    await session.submit(plate="M295YB196", captcha_code="1 2")

    assert session._viewstate == "FAKE_VIEWSTATE_PLACEHOLDER_NOT_REAL"
    assert session._viewstate_generator == "FAKEGEN01"
    # hdnx живёт ВНУТРИ pnlSayfa-фрагмента, а не отдельным hiddenField-
    # чанком (см. design report/session.py докстрок) — новое значение
    # взято из реальной (sanitized) фикстуры.
    assert session._hdnx == "SANITIZED"


async def test_submit_raises_transport_error_on_connection_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    session = _submit_session(handler)

    with pytest.raises(KgmTransportError):
        await session.submit(plate="M295YB196", captcha_code="1 2")


async def test_submit_raises_transport_error_on_unexpected_content_type():
    """Content-Type не text/* (например, случайно вернулась HTML-страница
    с другим типом или бинарные данные) — транспортная ошибка, ДО того,
    как parser.py вообще увидит этот ответ (см. design report: session.py
    только транспорт, форма ответа — забота parser.py)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x00\x01", headers={"content-type": "application/octet-stream"})

    session = _submit_session(handler)

    with pytest.raises(KgmTransportError):
        await session.submit(plate="M295YB196", captcha_code="1 2")
