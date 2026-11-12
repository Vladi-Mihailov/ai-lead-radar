"""Тесты reader/turkey_bot/kgm/relay_client.py — RelayKgmSession на стороне
production. Relay подменён httpx.MockTransport, который проверяет каждую
подпись настоящим relay_auth.verify (с NonceCache) и отвечает по контракту
relay_server.py. Любой сбой обязан стать KgmTransportError с одной
WARNING-строкой без госномера, CAPTCHA-кода, тела, секрета, подписи и
session_id; aclose() не бросает Exception; отмена не перехватывается."""

import asyncio
import base64
import json
import logging

import httpx
import pytest

from reader.turkey_bot.kgm import relay_auth, relay_client, relay_server
from reader.turkey_bot.kgm.relay_client import OPERATION_READ_TIMEOUTS, RelayKgmSession, _relay_error_suffix
from reader.turkey_bot.kgm.session import KgmTransportError

from _kgm_relay_fakes import ACCEPTED_CODE, HAS_DEBT_DELTA, OTHER_SECRET, SECRET, TEST_PLATE, captcha_image

BASE_URL = "http://10.50.0.2:8767"
SESSION_ID = "0123456789abcdef0123456789abcdef"
SESSIONS = "/internal/kgm/sessions"
_LOGGER = "reader.turkey_bot.kgm.relay_client"
# Глубже лимита рекурсии разбора JSON и на 3.12 (счётчик), и на 3.14 (стек).
_DEEP_JSON = b"[" * 100_000 + b"]" * 100_000


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
        suffix = path.removeprefix(SESSIONS).replace(SESSION_ID, "{id}") or "/"
        self.calls.append({
            "method": request.method, "path": path, "suffix": suffix, "ok": ok, "reason": reason,
            "body": request.content, "nonce": request.headers.get("X-Kgm-Nonce"),
            "signature": request.headers.get("X-Kgm-Signature"), "timeout": request.extensions.get("timeout"),
        })
        if not ok:
            return httpx.Response(401, json={"error": "unauthorized"})
        override = self.overrides.get((request.method, suffix))
        if isinstance(override, BaseException):
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


def _warning_heads(caplog) -> list[str]:
    return [w.split(" after ")[0] for w in _warnings(caplog)]


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


async def test_each_operation_has_its_own_read_timeout_and_a_5s_connect_timeout():
    relay = _FakeRelay()
    session = _session(relay)
    await session.start()
    await session.refresh_captcha()
    await session.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
    await session.health()
    await session.aclose()

    timeouts = {(c["method"], c["suffix"]): c["timeout"] for c in relay.calls}
    expected_read = {
        ("POST", "/"): 70.0,
        ("POST", "/{id}/captcha"): 40.0,
        ("POST", "/{id}/submit"): 40.0,
        ("GET", "/internal/kgm/health"): 5.0,
        ("DELETE", "/{id}"): 5.0,
    }
    for key, read in expected_read.items():
        assert timeouts[key] == {"connect": 5.0, "read": read, "write": read, "pool": read}, key


def test_client_read_timeouts_exceed_the_relay_operation_deadlines():
    assert OPERATION_READ_TIMEOUTS == {"start": 70.0, "captcha": 40.0, "submit": 40.0, "close": 5.0, "health": 5.0}
    for operation, deadline in relay_server.OPERATION_DEADLINES.items():
        assert OPERATION_READ_TIMEOUTS[operation] > deadline, operation


async def test_proxies_from_the_environment_are_ignored(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    session = RelayKgmSession(BASE_URL, SECRET)
    try:
        assert session._client._trust_env is False
        assert session._client.timeout.read == 5.0
    finally:
        await session.aclose()


def test_constructor_refuses_a_short_secret():
    with pytest.raises(ValueError):
        RelayKgmSession(BASE_URL, "x" * 31)


# ---- aclose: DELETE best effort, никогда не бросает Exception ----

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
    httpx.Response(500, content=_DEEP_JSON),
    httpx.Response(418, content=b"\x00garbage"),
    httpx.ConnectError("refused"),
    httpx.ReadTimeout("slow"),
    RuntimeError(f"unexpected failure for {TEST_PLATE}"),
])
async def test_aclose_never_raises_when_delete_fails(failure, caplog):
    relay = _FakeRelay({("DELETE", "/{id}"): failure})
    session = _session(relay)
    await session.start()

    await session.aclose()

    assert session._client.is_closed
    assert len(_warnings(caplog)) == 1
    assert _warnings(caplog)[0].startswith("KGM relay close:")
    assert TEST_PLATE not in caplog.text


async def test_aclose_swallows_an_unexpected_error_from_the_delete_step(monkeypatch, caplog):
    session = _session(_FakeRelay())
    await session.start()

    async def broken_delete():
        raise ValueError(f"broken for {TEST_PLATE}")

    monkeypatch.setattr(session, "_delete_session", broken_delete)
    await session.aclose()

    assert session._client.is_closed
    assert _warnings(caplog) == ["KGM relay close: ValueError, relay will expire the session by TTL"]


async def test_cancellation_during_delete_propagates_and_still_closes_the_client():
    session = _session(_FakeRelay({("DELETE", "/{id}"): asyncio.CancelledError()}))
    await session.start()

    with pytest.raises(asyncio.CancelledError):
        await session.aclose()
    assert session._client.is_closed


async def test_cancellation_during_submit_propagates_instead_of_becoming_a_transport_error():
    relay = _FakeRelay()
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.raw_path.endswith(b"/submit"):
            entered.set()
            await asyncio.Event().wait()
        return relay.handler(request)

    session = RelayKgmSession(BASE_URL, SECRET, transport=httpx.MockTransport(handler))
    await session.start()
    submit = asyncio.create_task(session.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE))
    await entered.wait()

    submit.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submit
    await session.aclose()


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


# ---- статусы, транспорт и форма ответа -> KgmTransportError ----

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
    pytest.param(httpx.Response(503, content=_DEEP_JSON), "HTTP 503", id="503-deep-json"),
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
    assert _warning_heads(caplog) == [f"KGM relay submit: HTTP {status} error={error}"]


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
    (RuntimeError(f"weird failure for {TEST_PLATE}"), "RuntimeError"),
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
    assert TEST_PLATE not in warnings[0]


@pytest.mark.parametrize("response, detail", [
    pytest.param(httpx.Response(201, text="not json"), "malformed JSON (JSONDecodeError)", id="not-json"),
    pytest.param(httpx.Response(201, content=_DEEP_JSON), "malformed JSON (RecursionError)", id="deep-json"),
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

    assert _warning_heads(caplog) == [f"KGM relay start: {detail}"]
    assert relay.calls[-1]["method"] == "POST"  # malformed session_id -> никакого DELETE


@pytest.mark.parametrize("payload", [
    {"submit_seconds": 0.1},
    {"response_text": 42, "submit_seconds": 0.1},
    {"response_text": "x"},
    {"response_text": "x", "submit_seconds": "0.1"},
    {"response_text": "x", "submit_seconds": True},
    {"response_text": "x", "submit_seconds": None},
    {"response_text": "x", "submit_seconds": 10**400},
])
async def test_malformed_submit_response_becomes_transport_error(payload, caplog):
    relay = _FakeRelay({("POST", "/{id}/submit"): httpx.Response(200, json=payload)})
    session = _session(relay)
    await session.start()

    with pytest.raises(KgmTransportError):
        await session.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
    await session.aclose()

    assert session.last_submit_seconds is None
    assert len(_warnings(caplog)) == 1


async def test_deeply_nested_json_on_submit_becomes_transport_error():
    relay = _FakeRelay({("POST", "/{id}/submit"): httpx.Response(200, content=_DEEP_JSON)})
    session = _session(relay)
    await session.start()

    with pytest.raises(KgmTransportError):
        await session.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
    await session.aclose()


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


# ---- журнал: белый список detail, session_id в строках httpx ----

@pytest.mark.parametrize("detail, expected", [
    ("ConnectError errno=111 Connection refused", "ConnectError errno=111"),
    ("ConnectError errno=-3 Temporary failure in name resolution", "ConnectError errno=-3"),
    ("HTTPStatusError status=500", "HTTPStatusError status=500"),
    ("ConnectTimeout", "ConnectTimeout"),
    ("TimeoutError", "TimeoutError"),
    ("KgmTransportError", "KgmTransportError"),
    ("ConnectionRefusedError errno=111", "ConnectionRefusedError errno=111"),
    ("HTTP 503", "HTTP 503"),
    ("plate TESTPLATE0 <html>", "other"),
    ("TESTPLATE0 leaked", "other"),
    ("SomethingElse", "other"),
    ("", "other"),
])
def test_relay_detail_passes_only_a_whitelist(detail, expected):
    response = httpx.Response(502, json={"error": "kgm_transport", "detail": detail})

    assert _relay_error_suffix(response) == f" error=kgm_transport detail={expected}"


def test_relay_error_suffix_never_raises():
    assert _relay_error_suffix(httpx.Response(502, json={"error": "kgm_transport"})) == " error=kgm_transport"
    assert _relay_error_suffix(httpx.Response(502, json={"error": "Not A Code!"})) == ""
    assert _relay_error_suffix(httpx.Response(502, content=_DEEP_JSON)) == ""
    assert _relay_error_suffix(httpx.Response(502, content=b"\xff\xfe")) == ""
    assert _relay_error_suffix(httpx.Response(502, json=[1, 2])) == ""


async def test_httpx_request_lines_carry_a_session_id_placeholder_instead_of_the_id(caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    session = _session(_FakeRelay())
    await session.start()
    await session.refresh_captcha()
    await session.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
    await session.aclose()

    httpx_lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert len(httpx_lines) == 4
    assert not [line for line in httpx_lines if SESSION_ID in line]
    assert any("/internal/kgm/sessions/{session_id}/submit" in line for line in httpx_lines)
    assert any('"HTTP/1.1 204 No Content"' in line for line in httpx_lines)


def test_session_id_filter_redacts_message_and_arguments_and_always_passes_the_record():
    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, f"GET /internal/kgm/sessions/{SESSION_ID} %s %d",
        (f"http://10.50.0.2:8767/internal/kgm/sessions/{SESSION_ID}/submit", 200), None,
    )

    assert relay_client._RedactRelaySessionIdFilter().filter(record) is True
    assert record.getMessage() == (
        "GET /internal/kgm/sessions/{session_id} http://10.50.0.2:8767/internal/kgm/sessions/{session_id}/submit 200"
    )


def test_session_id_filter_leaves_the_record_unchanged_when_an_argument_cannot_be_stringified():
    class Unprintable:
        def __str__(self):
            raise RuntimeError("no str")

    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, f"/internal/kgm/sessions/{SESSION_ID} %s %s",
        (f"/internal/kgm/sessions/{SESSION_ID}", Unprintable()), None,
    )
    msg, args = record.msg, record.args

    assert relay_client._RedactRelaySessionIdFilter().filter(record) is True
    assert record.msg is msg
    assert record.args is args


def test_session_id_redaction_filter_is_installed_once():
    relay_client._install_session_id_log_redaction()
    relay_client._install_session_id_log_redaction()

    filters = [f for f in logging.getLogger("httpx").filters if isinstance(f, relay_client._RedactRelaySessionIdFilter)]
    assert len(filters) == 1


async def test_logs_never_contain_plate_captcha_body_secret_signature_or_session_id(caplog):
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
        SECRET, TEST_PLATE, ACCEPTED_CODE, SESSION_ID, "lblGenelCezaToplam", "FAKE_VIEWSTATE_PLACEHOLDER_NOT_REAL",
        "<html>", *(c["signature"] for c in relay.calls),
    ]
    for value in forbidden:
        assert value not in log_text, f"{value!r} leaked into the log"
    assert "KGM relay submit: HTTP 502 error=kgm_transport detail=other after " in log_text
