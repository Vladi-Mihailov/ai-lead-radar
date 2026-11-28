"""
Диагностический лог неожиданных ответов GİB (reader/turkey_bot/gib/diagnostics.py).

Цепочка проверяется целиком: GibProvider -> GibSession -> httpx.MockTransport,
реальная сеть не используется. Все ответы синтетические. Проверяется, что:
  - исход разбора не меняется (parser.py не трогается);
  - лишних запросов к GİB нет;
  - в лог попадают статус, Retry-After, длительность, категория и безопасный
    текст, но никогда госномер, персональные данные, cookies, CAPTCHA,
    тело ответа и сведения о штрафах.
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

from reader.turkey_bot.gib import diagnostics  # noqa: E402
from reader.turkey_bot.gib.provider import GibProvider  # noqa: E402
from reader.turkey_bot.gib.session import GibSession, GibTransportError  # noqa: E402

_PLATE = "ZZ9999TST"
_IMAGE_ID = "cid-secret-777"
_CAPTCHA = "qx7k2"
_COOKIE_VALUE = "TS01cookievalue0099"
_LOGGERS = ("reader.turkey_bot.gib.diagnostics", "reader.turkey_bot.gib.session")


class _Recorder:
    def __init__(self, response: httpx.Response | None = None, *, error: Exception | None = None):
        self.requests: list[httpx.Request] = []
        self._response = response
        self._error = error

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._response


def _provider(recorder: _Recorder) -> GibProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return GibProvider(GibSession(client, request_timeout=5))


def _json_response(status: int, payload, *, headers: dict | None = None) -> httpx.Response:
    all_headers = {"set-cookie": f"TS01={_COOKIE_VALUE}; Path=/"}
    all_headers.update(headers or {})
    return httpx.Response(status, json=payload, headers=all_headers)


def _gib_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name in _LOGGERS]


def _assert_no_secrets(text: str) -> None:
    for secret in (_PLATE, _IMAGE_ID, _CAPTCHA, _COOKIE_VALUE, "TS01="):
        assert secret not in text


async def _submit(provider: GibProvider):
    return await provider.submit(plate=_PLATE, image_id=_IMAGE_ID, captcha_code=_CAPTCHA)


async def test_http_429_with_retry_after_is_logged_as_rate_limit(caplog):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(_json_response(
        429,
        {"messages": [{"type": "ERROR", "text": "Çok fazla istek gönderdiniz. Lütfen 5 dakika sonra tekrar deneyiniz."}],
         "data": None},
        headers={"Retry-After": "120"},
    ))

    outcome = await _submit(_provider(recorder))

    assert outcome.kind == "unexpected"
    assert len(recorder.requests) == 1
    [line] = _gib_lines(caplog)
    assert line.startswith("GIB unexpected response: category=rate_limit http_status=429 retry_after=120 duration_ms=")
    assert "content_type=application/json" in line
    assert "keys=messages,data messages=1 types=ERROR" in line
    assert 'text="Çok fazla istek gönderdiniz. Lütfen 5 dakika sonra tekrar deneyiniz."' in line
    _assert_no_secrets(line)


async def test_retry_after_alone_hints_rate_limit_and_keeps_http_date(caplog):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(_json_response(
        200,
        {"messages": [{"type": "WARNING", "text": "İşleminiz şu anda gerçekleştirilemiyor."}]},
        headers={"Retry-After": "Fri, 28 Nov 2026 21:00:00 GMT"},
    ))

    outcome = await _submit(_provider(recorder))

    assert outcome.kind == "unexpected"
    [line] = _gib_lines(caplog)
    assert "category=rate_limit http_status=200 retry_after=Fri, 28 Nov 2026 21:00:00 GMT" in line


async def test_unsafe_retry_after_value_is_not_logged(caplog):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(_json_response(
        200, {"messages": [{"type": "ERROR", "text": "Bilinmeyen durum."}]},
        headers={"Retry-After": f"{_PLATE}<script>"},
    ))

    await _submit(_provider(recorder))

    [line] = _gib_lines(caplog)
    assert "retry_after=<invalid>" in line
    _assert_no_secrets(line)


async def test_unknown_json_object_is_unknown_format_with_key_names_only(caplog):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(_json_response(200, {"sonuc": {"kisi": "AHMET YILMAZ", "plaka": _PLATE}, "durum": "X"}))

    outcome = await _submit(_provider(recorder))

    assert outcome.kind == "unexpected"
    [line] = _gib_lines(caplog)
    assert "category=unknown_format http_status=200" in line
    assert "shape=object keys=sonuc,durum messages=absent types=- codes=- text=-" in line
    assert "AHMET" not in line and "YILMAZ" not in line
    _assert_no_secrets(line)


async def test_non_object_json_is_unknown_format(caplog):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(_json_response(200, ["unexpected", _PLATE]))

    outcome = await _submit(_provider(recorder))

    assert outcome.kind == "unexpected"
    [line] = _gib_lines(caplog)
    assert "category=unknown_format" in line and "shape=array keys=-" in line
    _assert_no_secrets(line)


async def test_temporary_service_error_with_json_body(caplog):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(_json_response(
        503,
        {"messages": [{"type": "ERROR", "code": "GW-503",
                       "text": "Sistem geçici olarak hizmet verememektedir."}]},
    ))

    outcome = await _submit(_provider(recorder))

    assert outcome.kind == "unexpected"
    [line] = _gib_lines(caplog)
    assert "category=service_error http_status=503" in line
    assert "codes=GW-503" in line
    assert 'text="Sistem geçici olarak hizmet verememektedir."' in line


async def test_temporary_service_error_with_html_body_stays_transport_error(caplog):
    caplog.set_level(logging.INFO)
    html = (
        "<html><head><title>Service Unavailable</title></head>"
        f"<body>Plate {_PLATE}, support id 123456789012</body></html>"
    )
    recorder = _Recorder(httpx.Response(
        503, text=html, headers={"content-type": "text/html; charset=utf-8", "Retry-After": "300",
                                 "set-cookie": f"TS01={_COOKIE_VALUE}"},
    ))

    with pytest.raises(GibTransportError):
        await _submit(_provider(recorder))

    assert len(recorder.requests) == 1
    [line] = _gib_lines(caplog)
    assert line.startswith("GIB submit: response is not valid JSON (status=503) category=service_error retry_after=300 duration_ms=")
    assert 'content_type=text/html' in line and 'title="Service Unavailable"' in line
    assert "support id" not in line and "123456789012" not in line
    _assert_no_secrets(line)


async def test_blocked_html_page_is_categorised_by_title(caplog):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(httpx.Response(
        200, text="<html><title>Request Rejected</title><body>The requested URL was rejected.</body></html>",
        headers={"content-type": "text/html"},
    ))

    with pytest.raises(GibTransportError):
        await _submit(_provider(recorder))

    [line] = _gib_lines(caplog)
    assert "category=blocked" in line and 'title="Request Rejected"' in line


async def test_http_403_is_blocked(caplog):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(_json_response(403, {"messages": [{"type": "ERROR", "text": "Yetkisiz."}]}))

    await _submit(_provider(recorder))

    [line] = _gib_lines(caplog)
    assert "category=blocked http_status=403" in line


async def test_response_with_personal_data_is_masked(caplog):
    caplog.set_level(logging.INFO)
    text = (
        "Sayın AHMET YILMAZ, ZZ 9999 TST plakalı araç için 12345678901 TC kimlik no, "
        "ahmet.yilmaz@example.com, tutar 3000.00 TL, protokol MC03475903, "
        "bkz. https://dijital.gib.gov.tr/x?id=42"
    )
    recorder = _Recorder(_json_response(
        200,
        {"messages": [{"type": "ERROR", "text": text}],
         "data": {"KK_HASH": "secret-hash-abc", "KK_KIMLIK": "kimlik-token-xyz",
                  "KK_TUTANAKNO": "MC03475903", "KK_BORC": "3000.00"}},
    ))

    outcome = await _submit(_provider(recorder))

    assert outcome.kind == "unexpected"
    [line] = _gib_lines(caplog)
    for leaked in ("AHMET", "YILMAZ", "9999", "TST", "12345678901", "ahmet.yilmaz", "example.com",
                   "3000", "MC03475903", "secret-hash", "kimlik-token", "id=42", "dijital.gib.gov.tr"):
        assert leaked not in line, leaked
    assert "<caps>" in line and "<plate>" in line and "<num>" in line and "<email>" in line
    assert "<id>" in line and "<url>" in line
    assert "TC kimlik no" in line and "TL" in line
    assert "keys=messages,data" in line
    _assert_no_secrets(line)


async def test_text_that_cannot_be_checked_safely_is_omitted(caplog):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(_json_response(
        200,
        {"messages": [{"type": "ERROR", "text": "x" * 600}, {"type": "WARNING", "text": f"Kısa {_PLATE}"}]},
    ))

    await _submit(_provider(recorder))

    [line] = _gib_lines(caplog)
    assert "messages=2 types=ERROR,WARNING" in line
    assert 'text=<omitted> | "Kısa <plate>"' in line
    assert "x" * 50 not in line
    _assert_no_secrets(line)


def test_non_string_text_is_omitted():
    # Через GibProvider такой ответ не доходит: существующий parser.py
    # (не меняется этой задачей) падает раньше на text.strip().
    line = diagnostics.describe_unexpected(
        {"messages": [{"type": "ERROR", "text": {"nested": _PLATE}}]}, meta=None, plate=_PLATE,
    )
    assert "text=<omitted>" in line
    _assert_no_secrets(line)


@pytest.mark.parametrize("payload", [
    {"messages": [{"type": "ERROR", "text": "Girdiğiniz plakaya ait borç bulunamadı."}], "data": None},
    {"messages": [{"type": "ERROR",
                   "text": "Güvenlik kodunu yanlış girdiniz. Lütfen kontrol ederek tekrar deneyiniz."}],
     "data": None},
    {"BORCLAR": [], "messages": None},
    {"data": []},
])
async def test_known_outcomes_add_no_log_lines(caplog, payload):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(_json_response(200, payload))

    outcome = await _submit(_provider(recorder))

    assert outcome.kind in ("no_debt", "rejected")
    assert _gib_lines(caplog) == []
    assert len(recorder.requests) == 1


async def test_diagnostics_failure_never_changes_the_outcome(caplog, monkeypatch):
    caplog.set_level(logging.INFO)

    def boom(*args, **kwargs):
        raise RuntimeError(_PLATE)

    monkeypatch.setattr(diagnostics, "describe_unexpected", boom)
    recorder = _Recorder(_json_response(200, {"messages": [{"type": "ERROR", "text": "Yeni hata."}]}))

    outcome = await _submit(_provider(recorder))

    assert outcome.kind == "unexpected"
    assert _gib_lines(caplog) == ["GIB unexpected response: diagnostics failed (RuntimeError)"]


async def test_session_without_metadata_still_logs(caplog):
    caplog.set_level(logging.INFO)

    class _BareSession:
        async def submit(self, *, plate, image_id, captcha_code):
            return {"messages": [{"type": "ERROR", "text": "Yeni hata."}]}

    outcome = await GibProvider(_BareSession()).submit(plate=_PLATE, image_id=_IMAGE_ID, captcha_code=_CAPTCHA)

    assert outcome.kind == "unexpected"
    [line] = _gib_lines(caplog)
    assert "category=undetermined http_status=- retry_after=- duration_ms=- content_type=-" in line


async def test_transport_error_logs_duration(caplog):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(error=httpx.ReadTimeout("slow"))

    with pytest.raises(GibTransportError):
        await _submit(_provider(recorder))

    [line] = _gib_lines(caplog)
    assert line.startswith("GIB submit: transport error (ReadTimeout) duration_ms=")
    assert len(recorder.requests) == 1


def test_turkish_uppercase_keywords_are_recognised():
    assert diagnostics.classify(
        status_code=200, retry_after=None, texts=["SORGU LİMİTİNİZ DOLMUŞTUR"], known_shape=True,
    ) == diagnostics.RATE_LIMIT
    assert diagnostics.classify(
        status_code=200, retry_after=None, texts=["ERİŞİMİNİZ ENGELLENMİŞTİR"], known_shape=True,
    ) == diagnostics.BLOCKED
    assert diagnostics.classify(
        status_code=200, retry_after=None, texts=["Bilinmeyen durum"], known_shape=True,
    ) == diagnostics.UNDETERMINED
