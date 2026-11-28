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

from reader.turkey_bot.gib import diagnostics, session as gib_session  # noqa: E402
from reader.turkey_bot.gib.provider import GibProvider  # noqa: E402
from reader.turkey_bot.gib.session import GibSession, GibTransportError  # noqa: E402

_PLATE = "ZZ9999TST"
_IMAGE_ID = "cid-secret-777"
_CAPTCHA = "qxkzw"
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


def _json_response(status: int, payload, *, headers: list | None = None) -> httpx.Response:
    all_headers = [("set-cookie", f"TS01={_COOKIE_VALUE}; Path=/")] + list(headers or [])
    if payload is None:  # httpx считает json=None отсутствием тела
        all_headers.append(("content-type", "application/json"))
        return httpx.Response(status, content=b"null", headers=all_headers)
    return httpx.Response(status, json=payload, headers=all_headers)


def _message(text, *, type_="ERROR", **extra) -> dict:
    return {"messages": [{"type": type_, "text": text, **extra}], "data": None}


def _gib_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name in _LOGGERS]


def _assert_no_secrets(text: str) -> None:
    for secret in (_PLATE, _IMAGE_ID, "secret-777", _CAPTCHA, _COOKIE_VALUE, "TS01="):
        assert secret not in text


async def _submit(provider: GibProvider):
    return await provider.submit(plate=_PLATE, image_id=_IMAGE_ID, captcha_code=_CAPTCHA)


async def _single_line(caplog, response: httpx.Response, *, expect_kind: str = "unexpected") -> str:
    caplog.set_level(logging.INFO)
    recorder = _Recorder(response)
    outcome = await _submit(_provider(recorder))
    assert outcome.kind == expect_kind
    assert len(recorder.requests) == 1
    [line] = _gib_lines(caplog)
    _assert_no_secrets(line)
    return line


# --- HTTP 429 / Retry-After ---------------------------------------------------

async def test_http_429_with_retry_after_is_logged_as_rate_limit(caplog):
    line = await _single_line(caplog, _json_response(
        429, _message("Çok fazla istek gönderdiniz. Lütfen 5 dakika sonra tekrar deneyiniz."),
        headers=[("Retry-After", "120")],
    ))
    assert line.startswith(
        "GIB unexpected response: category=rate_limit signal=http_429 http_status=429 retry_after=120 duration_ms="
    )
    assert "content_type=application/json" in line
    assert "keys=messages,data messages=1 types=ERROR" in line
    assert 'text="Çok fazla istek gönderdiniz. Lütfen 5 dakika sonra tekrar deneyiniz."' in line


async def test_http_429_is_rate_limit_by_status_alone(caplog):
    line = await _single_line(caplog, _json_response(429, _message("İşlem yapılamadı.")))
    assert "category=rate_limit signal=http_429 http_status=429 retry_after=-" in line


async def test_http_429_html_without_retry_after_stays_transport_error(caplog):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(httpx.Response(
        429, text="<html><head><title>429 Too Many Requests</title></head><body>x</body></html>",
        headers={"content-type": "text/html"},
    ))
    with pytest.raises(GibTransportError):
        await _submit(_provider(recorder))
    assert len(recorder.requests) == 1
    [line] = _gib_lines(caplog)
    assert line.startswith(
        "GIB submit: response is not valid JSON (status=429) category=rate_limit signal=http_429 retry_after=- duration_ms="
    )
    assert 'title="429 Too Many Requests"' in line


async def test_retry_after_alone_hints_rate_limit_and_keeps_http_date(caplog):
    line = await _single_line(caplog, _json_response(
        200, _message("İşleminiz şu anda gerçekleştirilemiyor.", type_="WARNING"),
        headers=[("Retry-After", "Fri, 28 Nov 2026 21:00:00 GMT")],
    ))
    assert "category=rate_limit signal=retry_after http_status=200 retry_after=Fri, 28 Nov 2026 21:00:00 GMT" in line


@pytest.mark.parametrize("headers", [
    [("Retry-After", _PLATE)],
    [("Retry-After", "AB1234CD")],
    [("Retry-After", "120"), ("Retry-After", _PLATE)],
    [("Retry-After", f"{_PLATE}<script>")],
])
async def test_unsafe_retry_after_value_is_not_logged(caplog, headers):
    line = await _single_line(caplog, _json_response(200, _message("Bilinmeyen durum."), headers=headers))
    assert "retry_after=<invalid>" in line
    assert "AB1234CD" not in line


# --- Unknown JSON ---------------------------------------------------------------

async def test_unknown_json_object_is_unknown_format_with_key_names_only(caplog):
    line = await _single_line(caplog, _json_response(
        200, {"sonuc": {"kisi": "AHMET YILMAZ", "plaka": _PLATE}, "durum": "X"},
    ))
    assert "category=unknown_format signal=shape http_status=200" in line
    assert "shape=object keys=sonuc,durum messages=absent types=- codes=- text=-" in line
    assert "AHMET" not in line and "YILMAZ" not in line


@pytest.mark.parametrize("payload, shape", [
    (["unexpected", _PLATE], "array"),
    (None, "null"),
    (42, "scalar"),
    (_PLATE, "string"),
])
async def test_non_object_json_is_unknown_format(caplog, payload, shape):
    line = await _single_line(caplog, _json_response(200, payload))
    assert "category=unknown_format" in line and f"shape={shape} keys=-" in line


@pytest.mark.parametrize("messages, expected", [(None, "messages=null"), ("oops", "messages=invalid")])
async def test_messages_null_or_not_a_list(caplog, messages, expected):
    line = await _single_line(caplog, _json_response(200, {"messages": messages, "pageDetail": None}))
    assert "category=unknown_format" in line and expected in line and "keys=messages,pageDetail" in line


async def test_keys_with_digits_or_equal_to_secrets_are_hidden(caplog):
    line = await _single_line(caplog, _json_response(
        200, {"AB1234CD": 1, _PLATE: 2, "ZZ_9999_TST": 3, "messages": None},
    ))
    assert "keys=?,?,?,messages" in line
    assert "AB1234CD" not in line


# --- Temporary service error ----------------------------------------------------

async def test_temporary_service_error_with_json_body(caplog):
    line = await _single_line(caplog, _json_response(
        503, _message("Sistem geçici olarak hizmet verememektedir.", code="GW-503"),
    ))
    assert "category=service_error signal=http_503 http_status=503" in line
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
    assert line.startswith(
        "GIB submit: response is not valid JSON (status=503) category=service_error signal=http_503 retry_after=300 duration_ms="
    )
    assert "content_type=text/html" in line and 'title="Service Unavailable"' in line
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
    assert "category=blocked signal=keyword:request_rejected" in line and 'title="Request Rejected"' in line


async def test_unknown_html_title_with_personal_data_is_omitted(caplog):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(httpx.Response(
        200, text="<html><title>Hoş geldiniz Ahmet Yılmaz</title></html>", headers={"content-type": "text/html"},
    ))
    with pytest.raises(GibTransportError):
        await _submit(_provider(recorder))
    [line] = _gib_lines(caplog)
    assert "title=<omitted:capitalised_word>" in line
    assert "Ahmet" not in line and "Yılmaz" not in line


async def test_http_403_is_blocked(caplog):
    line = await _single_line(caplog, _json_response(403, _message("Yetkisiz.")))
    assert "category=blocked signal=http_403 http_status=403" in line


# --- Personal data ---------------------------------------------------------------

@pytest.mark.parametrize("text, reason, leaked", [
    ("Sayın AHMET YILMAZ, işleminiz yapılamadı.", "capitalised_word", ("AHMET", "YILMAZ")),
    ("Sayın Ahmet Yılmaz, işleminiz yapılamadı.", "capitalised_word", ("Ahmet", "Yılmaz")),
    ("Müşteri McDONALD için işlem yapılamadı.", "capitalised_word", ("McDONALD",)),
    ("Telefon 0532 123 45 67 doğrulanamadı.", "number", ("0532", "45 67")),
    ("Kimlik 123 456 789 01 doğrulanamadı.", "number", ("123 456",)),
    ("Adres atatürk caddesi no 15 kadıköy İstanbul olarak kayıtlı.", "number", ("kadıköy", "no 15")),
    ("Adres Atatürk Caddesi Kadıköy İstanbul olarak kayıtlı.", "capitalised_word", ("Atatürk", "Kadıköy")),
    ("Borç tutarı 3000 TL.", "number", ("3000",)),
    ("Bilgi için ahmet.yilmaz@example.com adresine yazınız.", "email", ("ahmet.yilmaz", "example.com")),
    ("Protokol MC00000017 bulunamadı.", "id_like", ("MC00000017",)),
    ("Plaka 34 ABC 123 kayıtlı değil.", "number", ("ABC", "34 ABC")),
    ("Plaka AB.1234.CD kayıtlı değil.", "number", ("AB.1234", "1234.CD")),
    ("Ad: Ahmet; Soyad: Yılmaz", "capitalised_word", ("Ahmet", "Yılmaz")),
    ("Araç sahibi: Ahmet; soyadı: Yılmaz.", "capitalised_word", ("Ahmet", "Yılmaz")),
    ("Araç sahibi ვლადიმერ ბერიძე olarak kayıtlı.", "non_latin", ("ვლადიმერ", "ბერიძე")),
    ("Araç sahibi محمد رضایی olarak kayıtlı.", "non_latin", ("محمد", "رضایی")),
])
async def test_text_with_possible_personal_data_is_omitted(caplog, text, reason, leaked):
    line = await _single_line(caplog, _json_response(200, _message(text)))
    assert f"text=<omitted:{reason}>" in line
    for value in leaked:
        assert value not in line, value


async def test_response_with_personal_data_and_fine_data_logs_metadata_only(caplog):
    text = (
        "Sayın AHMET YILMAZ, ZZ 9999 TST plakalı araç için 12345678901 TC kimlik no, "
        "ahmet.yilmaz@example.com, tutar 3000.00 TL, protokol MC00000017, "
        "bkz. https://dijital.gib.gov.tr/x?id=42"
    )
    line = await _single_line(caplog, _json_response(
        200,
        {"messages": [{"type": "ERROR", "text": text}],
         "data": {"KK_HASH": "secret-hash-abc", "KK_KIMLIK": "kimlik-token-xyz",
                  "KK_TUTANAKNO": "MC00000017", "KK_BORC": "3000.00"}},
    ))
    for leaked in ("AHMET", "YILMAZ", "9999", "TST", "12345678901", "ahmet.yilmaz", "example.com",
                   "3000", "MC00000017", "secret-hash", "kimlik-token", "id=42", "dijital.gib.gov.tr"):
        assert leaked not in line, leaked
    assert "text=<omitted:email>" in line
    assert "category=undetermined" in line and "keys=messages,data" in line


async def test_requested_plate_and_captcha_are_masked_in_otherwise_safe_text(caplog):
    line = await _single_line(caplog, _json_response(
        200, _message(f"Sorgulanan araç zz-9999-tst için kod {_CAPTCHA.upper()} geçersiz."),
    ))
    assert 'text="Sorgulanan araç <plate> için kod <captcha> geçersiz."' in line


@pytest.mark.parametrize("code", [
    _PLATE, "AB1234CD", _CAPTCHA, "ZZ-9999-TST", "123456789", "AHMET_YILMAZ", "XY-9876-ZW", "AHMET",
])
async def test_codes_that_could_be_data_are_not_logged(caplog, code):
    line = await _single_line(caplog, _json_response(
        200, {"messages": [{"type": "ERROR", "text": "Hata.", "code": code}], "status": code},
    ))
    assert "codes=-" in line
    for value in ("AB1234CD", "123456789", "AHMET", "XY-9876-ZW"):
        assert value not in line


@pytest.mark.parametrize("code, logged", [("GW-503", "GW-503"), ("E1001", "E1001"), (429, "429")])
async def test_short_error_codes_are_logged(caplog, code, logged):
    line = await _single_line(caplog, _json_response(
        200, {"messages": [{"type": "ERROR", "text": "Hata.", "code": code}]},
    ))
    assert f"codes={logged}" in line


async def test_turkish_letters_pass_the_allowlist(caplog):
    line = await _single_line(caplog, _json_response(
        200, _message("İşleminiz şu an gerçekleştirilemiyor; lütfen daha sonra tekrar deneyiniz. Çağrı ömrü üç gün."),
    ))
    assert 'text="İşleminiz şu an gerçekleştirilemiyor; lütfen daha sonra tekrar deneyiniz. Çağrı ömrü üç gün."' in line


async def test_unknown_html_title_after_colon_is_omitted(caplog):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(httpx.Response(200, text="<html><title>Hata: Ahmet</title></html>"))
    with pytest.raises(GibTransportError):
        await _submit(_provider(recorder))
    [line] = _gib_lines(caplog)
    assert "title=<omitted:capitalised_word>" in line and "Ahmet" not in line


async def test_text_that_cannot_be_checked_safely_is_omitted(caplog):
    line = await _single_line(caplog, _json_response(
        200,
        {"messages": [{"type": "ERROR", "text": "x" * 600}, {"type": "WARNING", "text": f"Kısa {_PLATE}"}]},
    ))
    assert "messages=2 types=ERROR,WARNING" in line
    assert 'text=<omitted:too_long> | "Kısa <plate>"' in line
    assert "x" * 50 not in line


def test_non_string_text_is_omitted():
    # Через GibProvider такой ответ не доходит: существующий parser.py
    # (не меняется этой задачей) падает раньше на text.strip().
    line = diagnostics.describe_unexpected(
        {"messages": [{"type": "ERROR", "text": {"nested": _PLATE}}]}, meta=None, plate=_PLATE,
    )
    assert "text=<omitted:not_text>" in line
    _assert_no_secrets(line)


# --- No behaviour change -------------------------------------------------------

@pytest.mark.parametrize("payload, kind", [
    ({"messages": [{"type": "ERROR", "text": "Girdiğiniz plakaya ait borç bulunamadı."}], "data": None}, "no_debt"),
    ({"messages": [{"type": "ERROR",
                    "text": "Güvenlik kodunu yanlış girdiniz. Lütfen kontrol ederek tekrar deneyiniz."}],
      "data": None}, "rejected"),
    ({"BORCLAR": [], "messages": None}, "no_debt"),
    ({"data": []}, "no_debt"),
])
async def test_known_outcomes_add_no_log_lines(caplog, payload, kind):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(_json_response(200, payload))
    outcome = await _submit(_provider(recorder))
    assert outcome.kind == kind
    assert _gib_lines(caplog) == []
    assert len(recorder.requests) == 1


async def test_diagnostics_failure_never_changes_the_outcome(caplog, monkeypatch):
    caplog.set_level(logging.INFO)

    def boom(*args, **kwargs):
        raise RuntimeError(_PLATE)

    monkeypatch.setattr(diagnostics, "describe_unexpected", boom)
    recorder = _Recorder(_json_response(200, _message("Yeni hata.")))
    outcome = await _submit(_provider(recorder))
    assert outcome.kind == "unexpected"
    assert _gib_lines(caplog) == ["GIB unexpected response: diagnostics failed (RuntimeError)"]


async def test_response_meta_failure_never_changes_the_outcome(caplog, monkeypatch):
    caplog.set_level(logging.INFO)

    def boom(*args, **kwargs):
        raise RuntimeError(_PLATE)

    monkeypatch.setattr(gib_session, "response_meta", boom)
    recorder = _Recorder(_json_response(200, _message("Yeni hata.")))
    outcome = await _submit(_provider(recorder))
    assert outcome.kind == "unexpected"
    [line] = _gib_lines(caplog)
    assert "http_status=- retry_after=- duration_ms=-" in line
    _assert_no_secrets(line)


async def test_non_json_diagnostics_failure_still_raises_transport_error(caplog, monkeypatch):
    caplog.set_level(logging.INFO)

    def boom(*args, **kwargs):
        raise RuntimeError(_PLATE)

    monkeypatch.setattr(gib_session, "describe_non_json", boom)
    recorder = _Recorder(httpx.Response(502, text="<html>bad</html>"))
    with pytest.raises(GibTransportError):
        await _submit(_provider(recorder))
    assert _gib_lines(caplog) == [
        "GIB submit: response is not valid JSON (status=502) diagnostics failed (RuntimeError)"
    ]


async def test_session_without_metadata_still_logs(caplog):
    caplog.set_level(logging.INFO)

    class _BareSession:
        async def submit(self, *, plate, image_id, captcha_code):
            return {"messages": [{"type": "ERROR", "text": "Yeni hata."}]}

    outcome = await GibProvider(_BareSession()).submit(plate=_PLATE, image_id=_IMAGE_ID, captcha_code=_CAPTCHA)
    assert outcome.kind == "unexpected"
    [line] = _gib_lines(caplog)
    assert "category=undetermined signal=- http_status=- retry_after=- duration_ms=- content_type=-" in line


async def test_transport_error_logs_duration(caplog):
    caplog.set_level(logging.INFO)
    recorder = _Recorder(error=httpx.ReadTimeout("slow"))
    with pytest.raises(GibTransportError):
        await _submit(_provider(recorder))
    [line] = _gib_lines(caplog)
    assert line.startswith("GIB submit: transport error (ReadTimeout) duration_ms=")
    assert len(recorder.requests) == 1


# --- Classification -------------------------------------------------------------

@pytest.mark.parametrize("text, category", [
    ("SORGU LİMİTİNİZ AŞILDI", diagnostics.RATE_LIMIT),
    ("Günlük sorgu sınırını aştınız.", diagnostics.RATE_LIMIT),
    ("ERİŞİMİNİZ ENGELLENMİŞTİR", diagnostics.BLOCKED),
    ("ACCESS DENIED", diagnostics.BLOCKED),
    ("FORBIDDEN", diagnostics.BLOCKED),
    ("RATE LIMIT EXCEEDED", diagnostics.RATE_LIMIT),
    ("SERVICE UNAVAILABLE", diagnostics.SERVICE_ERROR),
    ("Internal Server Error", diagnostics.SERVICE_ERROR),
    ("GATEWAY TIMEOUT", diagnostics.SERVICE_ERROR),
    ("Aracın sınır kapısı giriş kaydı bulunamadı.", diagnostics.UNDETERMINED),
    ("Kart ödeme limitiniz yetersiz.", diagnostics.UNDETERMINED),
    ("Bilinmeyen durum", diagnostics.UNDETERMINED),
])
def test_keywords_turkish_and_english(text, category):
    found, _signal = diagnostics.classify(status_code=200, retry_after=None, texts=[text], known_shape=True)
    assert found == category
