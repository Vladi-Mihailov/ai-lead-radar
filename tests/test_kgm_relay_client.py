"""Тесты reader/turkey_bot/kgm/relay_client.py — RelayKgmSession на стороне
production. Relay подменён httpx.MockTransport, который проверяет каждую
подпись настоящим relay_auth.verify (с NonceCache) и отвечает по контракту
relay_server.py. Любой сбой обязан стать KgmTransportError с одной
WARNING-строкой без госномера, CAPTCHA-кода, тела, секрета и подписи."""

import base64
import json
import logging

import httpx
import pytest

from reader.turkey_bot.kgm import relay_auth
from reader.turkey_bot.kgm.relay_client import RelayKgmSession, _relay_error_suffix
from reader.turkey_bot.kgm.session import KgmTransportError

from _kgm_relay_fakes import ACCEPTED_CODE, HAS_DEBT_DELTA, OTHER_SECRET, SECRET, TEST_PLATE, captcha_image

BASE_URL = "http://10.50.0.2:8767"
SESSION_ID = "0123456789abcdef0123456789abcdef"
SESSIONS = "/internal/kgm/sessions"
_LOGGER = "reader.turkey_bot.kgm.relay_client"


class _FakeRelay:
    """Минимальная эмуляция relay: проверка подписи + ответы по контракту.
    overrides[(method, path_suffix)] — httpx.Response или исключение."""

    def __init__(self, overrides: dict | None = None):
        self.nonces = relay_auth.NonceCache()
        self.calls: list[dict] = []
        self.overrides = overrides or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        ok, reason = relay_auth.verify(
            SECRET, method=request.method, path=path, body=request.content,
            timestamp=request.headers.get("X-Kgm-Timestamp"),
            nonce=request.headers.get("X-Kgm-Nonce"),
            signature=request.headers.get("X-Kgm-Signature"),
            nonce_cache=self.nonces,
        )
        self.calls.append({
            "method": request.method, "path": path, "ok": ok, "reason": reason,
            "body": request.content, "nonce": request.headers.get("X-Kgm-Nonce"),
            "signature": request.headers.get("X-Kgm-Signature"),
        })
        if not ok:
            return httpx.Response(401, json={"error": "unauthorized"})
        suffix = path.removeprefix(SESSIONS).replace(SESSION_ID, "{id}") or "/"
        override = self.overrides.get((request.method, suffix))
        if isinstance(override, Exception):
            raise override
        if override is not None:
            return override
        if (request.method, suffix) == ("POST", "/"):
            return httpx.Response(201, json={
                "session_id": SESSION_ID, "captcha_png_b64": base64.b64encode(captcha_image(1, 1)).decode(),
                "expires_in": 120,
            })
        if (request.method, suffix) == ("POST", "/{id}/captcha"):
            return httpx.Response(200, json={"captcha_png_b64": base64.b64encode(captcha_image(1, 2)).decode()})
        if (request.method, suffix) == ("POST", "/{id}/submit"):
            return httpx.Response(200, json={"response_text": HAS_DEBT_DELTA, "submit_seconds": 0.042})
        if (request.method, suffix) == ("DELETE", "/{id}"):
            return httpx.Response(204)
        if (request.method, path) == ("GET", "/internal/kgm/health"):
            return httpx.Response(200, json={"ok": True, "open_sessions": 0, "sessions_today": 3, "version": "1"})
        raise AssertionError(f"unexpected {request.method} {path}")


def _session(relay: _FakeRelay, *, secret: str = SECRET) -> RelayKgmSession:
    return RelayKgmSession(BASE_URL, secret, transport=httpx.MockTransport(relay.handler))


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING]


async def test_full_flow_is_signed_with_fresh_nonces_and_passes_relay_verification():
    relay = _FakeRelay()
    session = _session(relay)

    challenge = await session.start()
    refreshed = await session.refresh_captcha()
    response_text = await session.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
    await session.aclose()

    assert challenge.image_png == captcha_image(1, 1)
    assert refreshed.image_png == captcha_image(1, 2)
    assert response_text == HAS_DEBT_DELTA
    assert session.last_submit_seconds == 0.042
    assert [(c["method"], c["path"]) for c in relay.calls] == [
        ("POST", SESSIONS),
        ("POST", f"{SESSIONS}/{SESSION_ID}/captcha"),
        ("POST", f"{SESSIONS}/{SESSION_ID}/submit"),
        ("DELETE", f"{SESSIONS}/{SESSION_ID}"),
    ]
    assert all(c["ok"] for c in relay.calls)
    assert len({c["nonce"] for c in relay.calls}) == 4


async def test_submit_sends_exactly_plate_and_captcha_code_as_json():
    relay = _FakeRelay()
    session = _session(relay)
    await session.start()

    await session.submit(plate=TEST_PLATE, captcha_code="22 0371З")
    await session.aclose()

    submit = relay.calls[1]
    assert json.loads(submit["body"]) == {"plate": TEST_PLATE, "captcha_code": "22 0371З"}


async def test_timeouts_are_connect_5s_read_40s_and_proxies_from_env_are_ignored(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    session = RelayKgmSession(BASE_URL, SECRET)
    try:
        assert session._client.timeout.connect == 5.0
        assert session._client.timeout.read == 40.0
        assert session._client._trust_env is False
    finally:
        await session.aclose()


def test_constructor_refuses_a_short_secret():
    with pytest.raises(ValueError):
        RelayKgmSession(BASE_URL, "x" * 31)


async def test_aclose_deletes_the_relay_session_once_and_closes_the_client():
    relay = _FakeRelay()
    session = _session(relay)
    await session.start()

    await session.aclose()
    await session.aclose()

    assert [c["method"] for c in relay.calls] == ["POST", "DELETE"]
    assert session._client.is_closed


async def test_aclose_without_a_session_sends_nothing():
    relay = _FakeRelay()
    session = _session(relay)

    await session.aclose()

    assert relay.calls == []
    assert session._client.is_closed


@pytest.mark.parametrize("failure", [
    httpx.Response(500, json={"error": "internal"}),
    httpx.ConnectError("refused"),
    httpx.ReadTimeout("slow"),
])
async def test_aclose_is_best_effort_when_delete_fails(failure, caplog):
    relay = _FakeRelay({("DELETE", "/{id}"): failure})
    session = _session(relay)
    await session.start()

    await session.aclose()

    assert session._client.is_closed
    assert len(_warnings(caplog)) == 1
    assert _warnings(caplog)[0].startswith("KGM relay close:")


async def test_start_again_releases_the_previous_relay_session():
    relay = _FakeRelay()
    session = _session(relay)
    await session.start()

    await session.start()
    await session.aclose()

    assert [c["method"] for c in relay.calls] == ["POST", "DELETE", "POST", "DELETE"]


@pytest.mark.parametrize("operation", ["refresh_captcha", "submit"])
async def test_operations_before_start_raise_transport_error(operation, caplog):
    relay = _FakeRelay()
    session = _session(relay)
    try:
        with pytest.raises(KgmTransportError):
            if operation == "submit":
                await session.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
            else:
                await session.refresh_captcha()
    finally:
        await session.aclose()
    assert relay.calls == []
    assert len(_warnings(caplog)) == 1


async def test_use_after_close_raises_transport_error():
    session = _session(_FakeRelay())
    await session.aclose()

    with pytest.raises(KgmTransportError):
        await session.start()


_STATUS_CASES = [
    pytest.param(httpx.Response(401, json={"error": "unauthorized"}), "HTTP 401 error=unauthorized", id="401"),
    pytest.param(httpx.Response(404, json={"error": "session_not_found"}), "HTTP 404 error=session_not_found", id="404"),
    pytest.param(httpx.Response(429, json={"error": "rate_limited"}), "HTTP 429 error=rate_limited", id="429"),
    pytest.param(
        httpx.Response(502, json={"error": "kgm_transport", "detail": "ConnectTimeout"}),
        "HTTP 502 error=kgm_transport detail=ConnectTimeout", id="502",
    ),
    pytest.param(httpx.Response(503, json={"error": "busy"}), "HTTP 503 error=busy", id="503"),
    pytest.param(httpx.Response(500, text="<html>Internal Server Error</html>"), "HTTP 500", id="500-html"),
    pytest.param(httpx.Response(200, json={"session_id": SESSION_ID}), "HTTP 200", id="200-instead-of-201"),
]


@pytest.mark.parametrize("response, expected", _STATUS_CASES)
async def test_unexpected_status_on_start_becomes_transport_error_with_one_warning(response, expected, caplog):
    relay = _FakeRelay({("POST", "/"): response})
    session = _session(relay)

    with pytest.raises(KgmTransportError):
        await session.start()
    await session.aclose()

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0].startswith(f"KGM relay start: {expected} after ")
    assert "<html>" not in warnings[0]


@pytest.mark.parametrize("status, error", [(404, "session_not_found"), (502, "kgm_transport"), (503, "busy")])
async def test_unexpected_status_on_submit_becomes_transport_error(status, error, caplog):
    relay = _FakeRelay({("POST", "/{id}/submit"): httpx.Response(status, json={"error": error})})
    session = _session(relay)
    await session.start()

    with pytest.raises(KgmTransportError):
        await session.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
    await session.aclose()

    assert session.last_submit_seconds is None
    assert [w.split(" after ")[0] for w in _warnings(caplog)] == [f"KGM relay submit: HTTP {status} error={error}"]


async def test_unexpected_status_on_captcha_refresh_becomes_transport_error():
    relay = _FakeRelay({("POST", "/{id}/captcha"): httpx.Response(502, json={"error": "kgm_transport"})})
    session = _session(relay)
    await session.start()

    with pytest.raises(KgmTransportError):
        await session.refresh_captcha()
    await session.aclose()


@pytest.mark.parametrize("exception, expected", [
    (httpx.ConnectError("[Errno 111] Connection refused"), "ConnectError"),
    (httpx.ConnectTimeout("timed out"), "ConnectTimeout"),
    (httpx.ReadTimeout("timed out"), "ReadTimeout"),
    (httpx.RemoteProtocolError("illegal status line: b'HTTP/1.1 200 ASP.NET_SessionId=leak'"), "RemoteProtocolError"),
])
async def test_transport_errors_become_kgm_transport_error(exception, expected, caplog):
    relay = _FakeRelay({("POST", "/"): exception})
    session = _session(relay)

    with pytest.raises(KgmTransportError):
        await session.start()
    await session.aclose()

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0].startswith(f"KGM relay start: {expected} after ")
    assert "ASP.NET_SessionId" not in warnings[0]


@pytest.mark.parametrize("response, detail", [
    pytest.param(httpx.Response(201, text="not json"), "malformed JSON", id="not-json"),
    pytest.param(httpx.Response(201, json=["a"]), "malformed JSON", id="json-list"),
    pytest.param(httpx.Response(201, json={"captcha_png_b64": "AAAA"}), "malformed session_id", id="no-session-id"),
    pytest.param(
        httpx.Response(201, json={"session_id": SESSION_ID.upper(), "captcha_png_b64": "AAAA"}),
        "malformed session_id", id="uppercase-session-id",
    ),
    pytest.param(httpx.Response(201, json={"session_id": SESSION_ID}), "captcha missing", id="no-captcha"),
    pytest.param(
        httpx.Response(201, json={"session_id": SESSION_ID, "captcha_png_b64": "!!not-base64!!"}),
        "invalid base64", id="bad-base64",
    ),
    pytest.param(
        httpx.Response(201, json={"session_id": SESSION_ID, "captcha_png_b64": "ЖЖЖЖ"}),
        "invalid base64", id="non-ascii-base64",
    ),
])
async def test_malformed_create_response_becomes_transport_error(response, detail, caplog):
    relay = _FakeRelay({("POST", "/"): response})
    session = _session(relay)

    with pytest.raises(KgmTransportError):
        await session.start()
    await session.aclose()

    assert [w.split(" after ")[0] for w in _warnings(caplog)] == [f"KGM relay start: {detail}"]
    assert relay.calls[-1]["method"] == "POST"  # malformed session_id -> никакого DELETE


@pytest.mark.parametrize("payload", [
    {"submit_seconds": 0.1},
    {"response_text": 42, "submit_seconds": 0.1},
    {"response_text": "x"},
    {"response_text": "x", "submit_seconds": "0.1"},
    {"response_text": "x", "submit_seconds": True},
    {"response_text": "x", "submit_seconds": None},
])
async def test_malformed_submit_response_becomes_transport_error(payload):
    relay = _FakeRelay({("POST", "/{id}/submit"): httpx.Response(200, json=payload)})
    session = _session(relay)
    await session.start()

    with pytest.raises(KgmTransportError):
        await session.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
    await session.aclose()

    assert session.last_submit_seconds is None


async def test_wrong_secret_is_rejected_by_the_relay_and_becomes_transport_error():
    relay = _FakeRelay()
    session = _session(relay, secret=OTHER_SECRET)

    with pytest.raises(KgmTransportError):
        await session.start()
    await session.aclose()

    assert relay.calls[0]["reason"] == "invalid_signature"


async def test_health_returns_the_relay_status():
    relay = _FakeRelay()
    session = _session(relay)

    health = await session.health()
    await session.aclose()

    assert health == {"ok": True, "open_sessions": 0, "sessions_today": 3, "version": "1"}
    assert relay.calls[0]["ok"]


async def test_logs_never_contain_plate_captcha_body_secret_or_signature(caplog):
    caplog.set_level(logging.DEBUG)
    relay = _FakeRelay()
    session = _session(relay)
    await session.start()
    await session.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
    relay.overrides[("POST", "/{id}/submit")] = httpx.Response(
        502, json={"error": "kgm_transport", "detail": f"plate {TEST_PLATE} <html>"},
    )
    with pytest.raises(KgmTransportError):
        await session.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
    relay.overrides[("POST", "/{id}/submit")] = httpx.ConnectError(f"refused for {TEST_PLATE}")
    with pytest.raises(KgmTransportError):
        await session.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
    await session.aclose()

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    forbidden = [
        SECRET, TEST_PLATE, ACCEPTED_CODE, "lblGenelCezaToplam", "FAKE_VIEWSTATE_PLACEHOLDER_NOT_REAL", "<html>",
        *(c["signature"] for c in relay.calls),
    ]
    for value in forbidden:
        assert value not in log_text, f"{value!r} leaked into the log"
    assert "KGM relay submit: HTTP 502 error=kgm_transport detail=plate after " in log_text


def test_relay_detail_is_reduced_to_type_status_and_errno():
    def suffix(detail):
        return _relay_error_suffix(httpx.Response(502, json={"error": "kgm_transport", "detail": detail}))

    assert suffix("ConnectError errno=111 Connection refused") == " error=kgm_transport detail=ConnectError errno=111"
    assert suffix("HTTPStatusError status=500") == " error=kgm_transport detail=HTTPStatusError status=500"
    assert suffix("34TST987 leaked") == " error=kgm_transport"
    assert suffix(None) == " error=kgm_transport"
    assert _relay_error_suffix(httpx.Response(502, json={"error": "Not A Code!"})) == ""
