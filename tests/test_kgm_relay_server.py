"""Тесты reader/turkey_bot/kgm/relay_server.py — KGM relay на грузинском
сервере. Сеть не используется: relay работает в aiohttp TestServer, а KGM
подменён FakeKgm (httpx.MockTransport, см. tests/_kgm_relay_fakes.py),
который ведёт себя как ASP.NET и отвечает HTTP 500 на чужие cookie или
протухшие __VIEWSTATE/hdnx — так проверяется, что состояние сессии
переживает обновление CAPTCHA и повторный submit."""

import asyncio
import base64
import gc
import json
import logging
import re
import time
import tracemalloc
from datetime import datetime, timezone

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

import reader.turkey_bot.kgm.session as kgm_session
from reader.turkey_bot.kgm import relay_auth
from reader.turkey_bot.kgm.parser import parse_submit_response
from reader.turkey_bot.kgm.relay_server import build_kgm_relay_app
from reader.turkey_bot.kgm.session import KgmSession

from _kgm_relay_fakes import (
    ACCEPTED_CODE,
    HAS_DEBT_DELTA,
    OTHER_SECRET,
    SECRET,
    TEST_PLATE,
    FakeKgm,
    captcha_image,
    signed_request,
)

SESSIONS = "/internal/kgm/sessions"
HEALTH = "/internal/kgm/health"
UNKNOWN_ID = "f" * 32


@pytest.fixture(autouse=True)
def _no_throttle_delay(monkeypatch):
    monkeypatch.setattr(kgm_session, "_MIN_REQUEST_INTERVAL_SECONDS", 0.0)


class _Clock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _app(fake: FakeKgm, **kwargs):
    return build_kgm_relay_app(hmac_secret=SECRET, session_factory=fake.session_factory, **kwargs)


def _submit_body(plate: str = TEST_PLATE, code: str = ACCEPTED_CODE) -> bytes:
    return json.dumps({"plate": plate, "captcha_code": code}).encode()


async def _create(client, **kwargs) -> dict:
    response = await signed_request(client, "POST", SESSIONS, **kwargs)
    assert response.status == 201, await response.text()
    return await response.json()


async def _health(client, **kwargs) -> dict:
    response = await signed_request(client, "GET", HEALTH, **kwargs)
    assert response.status == 200
    return await response.json()


async def _wait_until(condition, *, timeout: float = 2.0) -> None:
    for _ in range(int(timeout / 0.005)):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not met in time")


# ---- аутентификация ----

_ROUTES = [
    ("POST", SESSIONS),
    ("POST", f"{SESSIONS}/{UNKNOWN_ID}/captcha"),
    ("POST", f"{SESSIONS}/{UNKNOWN_ID}/submit"),
    ("DELETE", f"{SESSIONS}/{UNKNOWN_ID}"),
    ("GET", HEALTH),
]


@pytest.mark.parametrize("method, path", _ROUTES)
async def test_every_route_requires_a_valid_signature(method, path):
    fake = FakeKgm()
    async with TestClient(TestServer(_app(fake))) as client:
        unsigned = await client.request(method, path)
        wrong_secret = await signed_request(client, method, path, secret=OTHER_SECRET)

        assert unsigned.status == 401
        assert await unsigned.json() == {"error": "unauthorized"}
        assert wrong_secret.status == 401
        assert await wrong_secret.json() == {"error": "unauthorized"}
    assert fake.requests == []
    assert fake.created_clients == 0


async def test_replayed_request_is_rejected():
    async with TestClient(TestServer(_app(FakeKgm()))) as client:
        headers = relay_auth.signed_headers(SECRET, "GET", HEALTH, b"")
        first = await client.get(HEALTH, headers=headers)
        second = await client.get(HEALTH, headers=headers)

    assert first.status == 200
    assert second.status == 401


async def test_signature_covers_the_body():
    fake = FakeKgm()
    async with TestClient(TestServer(_app(fake))) as client:
        session_id = (await _create(client))["session_id"]
        path = f"{SESSIONS}/{session_id}/submit"
        headers = relay_auth.signed_headers(SECRET, "POST", path, _submit_body())
        response = await client.post(path, data=_submit_body(plate="34TST988"), headers=headers)

    assert response.status == 401
    assert fake.submits() == []


def test_build_app_refuses_a_short_secret():
    with pytest.raises(ValueError):
        build_kgm_relay_app(hmac_secret="x" * 31)


# ---- контракт: состояние сессии и сырой ответ KGM ----

async def test_full_flow_keeps_cookie_and_viewstate_across_captcha_refresh_and_repeated_submit():
    fake = FakeKgm(reject_first=1)
    async with TestClient(TestServer(_app(fake))) as client:
        created = await _create(client)
        session_id = created["session_id"]
        assert set(created) == {"session_id", "captcha_png_b64", "expires_in"}
        assert re.fullmatch(r"[0-9a-f]{32}", session_id)
        assert created["expires_in"] == 120
        assert base64.b64decode(created["captcha_png_b64"]) == captcha_image(1, 1)

        refreshed = await signed_request(client, "POST", f"{SESSIONS}/{session_id}/captcha")
        assert refreshed.status == 200
        assert await refreshed.json() == {"captcha_png_b64": base64.b64encode(captcha_image(1, 2)).decode()}

        rejected = await signed_request(client, "POST", f"{SESSIONS}/{session_id}/submit", body=_submit_body())
        assert rejected.status == 200
        rejected_json = await rejected.json()
        assert set(rejected_json) == {"response_text", "submit_seconds"}
        assert isinstance(rejected_json["submit_seconds"], float)
        assert parse_submit_response(rejected_json["response_text"]).kind == "rejected"

        refreshed = await signed_request(client, "POST", f"{SESSIONS}/{session_id}/captcha")
        assert base64.b64decode((await refreshed.json())["captcha_png_b64"]) == captcha_image(1, 3)

        accepted = await signed_request(client, "POST", f"{SESSIONS}/{session_id}/submit", body=_submit_body())
        assert accepted.status == 200
        response_text = (await accepted.json())["response_text"]
        # Сырой текст KGM без потерь — включая турецкие буквы и "₺".
        assert response_text == HAS_DEBT_DELTA
        assert parse_submit_response(response_text).kind == "has_debt"

        assert (await signed_request(client, "DELETE", f"{SESSIONS}/{session_id}")).status == 204
        assert (await signed_request(client, "DELETE", f"{SESSIONS}/{session_id}")).status == 204
        assert (await signed_request(client, "POST", f"{SESSIONS}/{session_id}/captcha")).status == 404

    # Одна ASP.NET-сессия: тот же cookie на всех запросах после GET страницы,
    # а каждый submit несёт ПОСЛЕДНИЕ выданные KGM __VIEWSTATE/hdnx.
    assert [r["kind"] for r in fake.requests] == ["page", "captcha", "captcha", "submit", "captcha", "submit"]
    assert {r["cookie"] for r in fake.requests[1:]} == {"fakeaspnetsession0001"}
    first_submit, second_submit = fake.submits()
    assert (first_submit["form"]["__VIEWSTATE"], first_submit["form"]["hdnx"]) == ("VS-1-0", "HX-1-0")
    assert (second_submit["form"]["__VIEWSTATE"], second_submit["form"]["hdnx"]) == ("VS-1-1", "HX-1-1")
    assert second_submit["form"]["__VIEWSTATEGENERATOR"] == "GEN0001"
    assert (second_submit["form"]["txtPlk"], second_submit["form"]["txtimgcode"]) == (TEST_PLATE, ACCEPTED_CODE)
    assert fake.created_clients == 1
    assert fake.clients[0].is_closed


async def test_two_sessions_keep_separate_asp_net_state():
    fake = FakeKgm(reject_first=1)
    async with TestClient(TestServer(_app(fake))) as client:
        first = (await _create(client))["session_id"]
        second = (await _create(client))["session_id"]
        await signed_request(client, "POST", f"{SESSIONS}/{first}/submit", body=_submit_body())
        response = await signed_request(client, "POST", f"{SESSIONS}/{second}/submit", body=_submit_body())

        assert response.status == 200
    cookies = [r["cookie"] for r in fake.submits()]
    viewstates = [r["form"]["__VIEWSTATE"] for r in fake.submits()]
    assert cookies == ["fakeaspnetsession0001", "fakeaspnetsession0002"]
    assert viewstates == ["VS-1-0", "VS-2-0"]


async def test_unicode_printable_captcha_code_still_reaches_kgm():
    fake = FakeKgm()
    async with TestClient(TestServer(_app(fake))) as client:
        session_id = (await _create(client))["session_id"]
        response = await signed_request(
            client, "POST", f"{SESSIONS}/{session_id}/submit", body=_submit_body(code="22 0371З"),
        )

        assert response.status == 200
        assert parse_submit_response((await response.json())["response_text"]).kind == "rejected"
    assert fake.submits()[0]["form"]["txtimgcode"] == "22 0371З"


# ---- TTL и фоновая очистка ----

async def test_session_expires_after_ttl_with_404_and_closed_client():
    fake = FakeKgm()
    clock = _Clock()
    async with TestClient(TestServer(_app(fake, clock=clock))) as client:
        session_id = (await _create(client))["session_id"]
        clock.advance(120.5)

        captcha = await signed_request(client, "POST", f"{SESSIONS}/{session_id}/captcha")
        assert captcha.status == 404
        assert await captcha.json() == {"error": "session_not_found"}
        assert fake.clients[0].is_closed
        submit = await signed_request(client, "POST", f"{SESSIONS}/{session_id}/submit", body=_submit_body())
        assert submit.status == 404
        assert (await _health(client))["open_sessions"] == 0


async def test_ttl_counts_from_the_last_use():
    fake = FakeKgm()
    clock = _Clock()
    async with TestClient(TestServer(_app(fake, clock=clock))) as client:
        session_id = (await _create(client))["session_id"]
        path = f"{SESSIONS}/{session_id}/captcha"
        clock.advance(100)
        assert (await signed_request(client, "POST", path)).status == 200
        clock.advance(100)
        assert (await signed_request(client, "POST", path)).status == 200
        clock.advance(121)
        assert (await signed_request(client, "POST", path)).status == 404


async def test_background_cleanup_closes_idle_sessions_without_any_request():
    fake = FakeKgm()
    clock = _Clock()
    async with TestClient(TestServer(_app(fake, clock=clock, cleanup_interval_seconds=0.01))) as client:
        await _create(client)
        await asyncio.sleep(0.05)
        assert not fake.clients[0].is_closed

        clock.advance(121)
        await _wait_until(lambda: fake.clients[0].is_closed)
        assert (await _health(client))["open_sessions"] == 0


async def test_session_in_use_is_never_expired_under_a_running_operation():
    fake = FakeKgm()
    clock = _Clock()
    async with TestClient(TestServer(_app(fake, clock=clock))) as client:
        session_id = (await _create(client))["session_id"]
        fake.delay = 0.2
        submit = asyncio.create_task(
            signed_request(client, "POST", f"{SESSIONS}/{session_id}/submit", body=_submit_body()),
        )
        await _wait_until(lambda: fake.in_flight > 0)
        clock.advance(500)
        fake.delay = 0.0
        assert (await _health(client))["open_sessions"] == 1
        assert not fake.clients[0].is_closed

        assert (await submit).status == 200
        # TTL отсчитывается от конца операции.
        assert (await _health(client))["open_sessions"] == 1
        clock.advance(121)
        assert (await _health(client))["open_sessions"] == 0
    assert fake.clients[0].is_closed


async def test_delete_during_an_operation_closes_the_client_only_after_it_finishes():
    fake = FakeKgm()
    async with TestClient(TestServer(_app(fake))) as client:
        session_id = (await _create(client))["session_id"]
        fake.delay = 0.2
        submit = asyncio.create_task(
            signed_request(client, "POST", f"{SESSIONS}/{session_id}/submit", body=_submit_body()),
        )
        await _wait_until(lambda: fake.in_flight > 0)

        assert (await signed_request(client, "DELETE", f"{SESSIONS}/{session_id}")).status == 204
        assert not fake.clients[0].is_closed
        assert (await submit).status == 200
        assert fake.clients[0].is_closed


async def test_shutdown_closes_every_open_session():
    fake = FakeKgm()
    client = TestClient(TestServer(_app(fake)))
    await client.start_server()
    try:
        await _create(client)
        await _create(client)
        assert not any(c.is_closed for c in fake.clients)
    finally:
        await client.close()

    assert len(fake.clients) == 2
    assert all(c.is_closed for c in fake.clients)


# ---- лимиты ----

async def test_third_session_is_busy_while_two_are_open():
    fake = FakeKgm()
    async with TestClient(TestServer(_app(fake))) as client:
        first = await _create(client)
        await _create(client)

        busy = await signed_request(client, "POST", SESSIONS)
        assert busy.status == 503
        assert await busy.json() == {"error": "busy"}

        assert (await signed_request(client, "DELETE", f"{SESSIONS}/{first['session_id']}")).status == 204
        assert (await signed_request(client, "POST", SESSIONS)).status == 201
    assert fake.created_clients == 3


async def test_session_being_created_counts_towards_the_busy_limit():
    fake = FakeKgm(delay=0.2)
    async with TestClient(TestServer(_app(fake, max_open_sessions=1))) as client:
        pending = asyncio.create_task(signed_request(client, "POST", SESSIONS))
        await _wait_until(lambda: fake.in_flight > 0)

        busy = await signed_request(client, "POST", SESSIONS)
        assert busy.status == 503
        assert (await pending).status == 201
    assert fake.created_clients == 1


async def test_token_bucket_allows_a_burst_of_three_then_one_per_two_seconds():
    fake = FakeKgm()
    clock = _Clock()
    async with TestClient(TestServer(_app(fake, clock=clock, max_open_sessions=10))) as client:
        for _ in range(3):
            assert (await signed_request(client, "POST", SESSIONS)).status == 201

        limited = await signed_request(client, "POST", SESSIONS)
        assert limited.status == 429
        assert await limited.json() == {"error": "rate_limited"}

        clock.advance(2.0)
        assert (await signed_request(client, "POST", SESSIONS)).status == 201
        assert (await signed_request(client, "POST", SESSIONS)).status == 429
    assert fake.created_clients == 4


async def test_daily_cap_counts_per_utc_day():
    fake = FakeKgm()
    wall = _Clock(datetime(2026, 11, 11, 23, 58, tzinfo=timezone.utc).timestamp())
    app = _app(fake, wall_clock=wall, daily_session_cap=2, session_burst=10, max_open_sessions=10)
    async with TestClient(TestServer(app)) as client:
        for _ in range(2):
            await _create(client, now=wall())

        capped = await signed_request(client, "POST", SESSIONS, now=wall())
        assert capped.status == 429
        assert await capped.json() == {"error": "rate_limited"}
        assert (await _health(client, now=wall()))["sessions_today"] == 2

        wall.advance(180)  # 00:01 UTC следующих суток
        await _create(client, now=wall())
        assert (await _health(client, now=wall()))["sessions_today"] == 1
    assert fake.created_clients == 3


async def test_kgm_operations_are_serialized_by_the_semaphore():
    fake = FakeKgm(delay=0.05)
    async with TestClient(TestServer(_app(fake))) as client:
        responses = await asyncio.gather(
            signed_request(client, "POST", SESSIONS), signed_request(client, "POST", SESSIONS),
        )

    assert [r.status for r in responses] == [201, 201]
    assert fake.max_in_flight == 1


# ---- размер тела и валидация ----

async def test_oversized_body_is_rejected_with_413():
    fake = FakeKgm()
    async with TestClient(TestServer(_app(fake))) as client:
        session_id = (await _create(client))["session_id"]
        body = json.dumps({"plate": TEST_PLATE, "captcha_code": "x" * (17 * 1024)}).encode()

        signed = await signed_request(client, "POST", f"{SESSIONS}/{session_id}/submit", body=body)
        unsigned = await client.post(SESSIONS, data=b"x" * (17 * 1024))

        assert signed.status == 413
        assert await signed.json() == {"error": "payload_too_large"}
        assert unsigned.status == 413
    assert fake.submits() == []


_BAD_BODIES = [
    pytest.param(b"not json", id="not-json"),
    pytest.param(b"\xff\xfe\x00", id="not-utf8"),
    pytest.param(b"[]", id="list"),
    pytest.param(b"", id="empty"),
    pytest.param(json.dumps({"plate": TEST_PLATE}).encode(), id="missing-code"),
    pytest.param(json.dumps({"captcha_code": ACCEPTED_CODE}).encode(), id="missing-plate"),
    pytest.param(
        json.dumps({"plate": TEST_PLATE, "captcha_code": ACCEPTED_CODE, "extra": 1}).encode(), id="extra-field",
    ),
    pytest.param(json.dumps({"plate": 34, "captcha_code": ACCEPTED_CODE}).encode(), id="plate-not-string"),
    pytest.param(json.dumps({"plate": TEST_PLATE, "captcha_code": 22}).encode(), id="code-not-string"),
    pytest.param(json.dumps({"plate": "", "captcha_code": ACCEPTED_CODE}).encode(), id="empty-plate"),
    pytest.param(json.dumps({"plate": "A" * 21, "captcha_code": ACCEPTED_CODE}).encode(), id="plate-too-long"),
    pytest.param(json.dumps({"plate": "34-TST-987", "captcha_code": ACCEPTED_CODE}).encode(), id="plate-dash"),
    pytest.param(json.dumps({"plate": "34ТСТ987", "captcha_code": ACCEPTED_CODE}).encode(), id="plate-cyrillic"),
    pytest.param(json.dumps({"plate": TEST_PLATE, "captcha_code": ""}).encode(), id="empty-code"),
    pytest.param(json.dumps({"plate": TEST_PLATE, "captcha_code": "1" * 33}).encode(), id="code-too-long"),
    pytest.param(json.dumps({"plate": TEST_PLATE, "captcha_code": "22\n03713"}).encode(), id="code-control-char"),
    pytest.param(b"[" * 5000 + b"]" * 5000, id="deeply-nested"),
]


@pytest.mark.parametrize("body", _BAD_BODIES)
async def test_invalid_submit_body_is_rejected_with_400_and_the_session_stays_usable(body):
    fake = FakeKgm()
    async with TestClient(TestServer(_app(fake))) as client:
        session_id = (await _create(client))["session_id"]
        path = f"{SESSIONS}/{session_id}/submit"

        response = await signed_request(client, "POST", path, body=body)
        assert response.status == 400
        assert await response.json() == {"error": "bad_request"}
        assert (await signed_request(client, "POST", path, body=_submit_body())).status == 200
    assert len(fake.submits()) == 1


@pytest.mark.parametrize("session_id", [UNKNOWN_ID, "F" * 32, "g" * 32, "abc"])
async def test_unknown_or_malformed_session_id_is_404(session_id):
    fake = FakeKgm()
    async with TestClient(TestServer(_app(fake))) as client:
        await _create(client)
        captcha = await signed_request(client, "POST", f"{SESSIONS}/{session_id}/captcha")
        submit = await signed_request(client, "POST", f"{SESSIONS}/{session_id}/submit", body=_submit_body())
        delete = await signed_request(client, "DELETE", f"{SESSIONS}/{session_id}")

        assert captcha.status == 404
        assert await captcha.json() == {"error": "session_not_found"}
        assert submit.status == 404
        assert delete.status == 204
        assert (await _health(client))["open_sessions"] == 1


async def test_unknown_route_and_wrong_method_return_json_errors():
    async with TestClient(TestServer(_app(FakeKgm()))) as client:
        unknown = await signed_request(client, "GET", "/internal/kgm/unknown")
        wrong_method = await signed_request(client, "PUT", SESSIONS)

        assert unknown.status == 404
        assert await unknown.json() == {"error": "not_found"}
        assert wrong_method.status == 405
        assert await wrong_method.json() == {"error": "method_not_allowed"}


# ---- ошибки KGM -> 502 ----

async def test_kgm_unreachable_on_create_maps_to_502_and_leaves_nothing_open():
    fake = FakeKgm(page_error=httpx.ConnectError)
    async with TestClient(TestServer(_app(fake))) as client:
        response = await signed_request(client, "POST", SESSIONS)

        assert response.status == 502
        assert await response.json() == {"error": "kgm_transport", "detail": "ConnectError"}
        assert (await _health(client))["open_sessions"] == 0
    assert fake.clients[0].is_closed


async def test_kgm_timeout_on_create_maps_to_502():
    fake = FakeKgm(page_error=httpx.ConnectTimeout)
    async with TestClient(TestServer(_app(fake))) as client:
        response = await signed_request(client, "POST", SESSIONS)

        assert response.status == 502
        assert await response.json() == {"error": "kgm_transport", "detail": "ConnectTimeout"}


async def test_failed_captcha_refresh_maps_to_502_and_closes_the_session():
    fake = FakeKgm(captcha_failures_after=1)
    async with TestClient(TestServer(_app(fake))) as client:
        session_id = (await _create(client))["session_id"]

        response = await signed_request(client, "POST", f"{SESSIONS}/{session_id}/captcha")
        assert response.status == 502
        assert await response.json() == {"error": "kgm_transport", "detail": "KgmTransportError"}
        after = await signed_request(client, "POST", f"{SESSIONS}/{session_id}/submit", body=_submit_body())
        assert after.status == 404
    assert fake.clients[0].is_closed


async def test_kgm_http_error_on_submit_maps_to_502_with_status_only():
    fake = FakeKgm()
    async with TestClient(TestServer(_app(fake))) as client:
        session_id = (await _create(client))["session_id"]
        fake.states["fakeaspnetsession0001"].viewstate = "CHANGED-ON-THE-SERVER"

        response = await signed_request(client, "POST", f"{SESSIONS}/{session_id}/submit", body=_submit_body())
        assert response.status == 502
        assert await response.json() == {"error": "kgm_transport", "detail": "HTTPStatusError status=500"}
        assert (await _health(client))["open_sessions"] == 0
    assert fake.clients[0].is_closed


async def test_slow_kgm_hits_the_operation_deadline_and_maps_to_502():
    fake = FakeKgm(delay=1.0)
    async with TestClient(TestServer(_app(fake, operation_timeout_seconds=0.05))) as client:
        response = await signed_request(client, "POST", SESSIONS)

        assert response.status == 502
        assert await response.json() == {"error": "kgm_transport", "detail": "TimeoutError"}
        assert (await _health(client))["open_sessions"] == 0
    assert fake.clients[0].is_closed


# ---- health ----

async def test_health_reports_open_sessions_daily_count_and_version():
    async with TestClient(TestServer(_app(FakeKgm()))) as client:
        assert await _health(client) == {"ok": True, "open_sessions": 0, "sessions_today": 0, "version": "1"}
        await _create(client)
        assert await _health(client) == {"ok": True, "open_sessions": 1, "sessions_today": 1, "version": "1"}


# ---- журнал и ответы без секретов ----

async def test_logs_and_error_responses_never_contain_plate_captcha_secrets_session_id_or_kgm_body(caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeKgm(reject_first=1)
    signatures: list[str] = []
    error_bodies: list[str] = []

    async with TestClient(TestServer(_app(fake))) as client:
        async def call(method: str, path: str, body: bytes = b"", *, forge: bool = False):
            headers = relay_auth.signed_headers(SECRET, method, path, body)
            if forge:
                headers["X-Kgm-Signature"] = headers["X-Kgm-Signature"][::-1]
            signatures.append(headers["X-Kgm-Signature"])
            response = await client.request(method, path, data=body or None, headers=headers)
            text = await response.text()
            if response.status >= 400:
                error_bodies.append(text)
            return text

        session_id = json.loads(await call("POST", SESSIONS))["session_id"]
        base = f"{SESSIONS}/{session_id}"
        await call("POST", f"{base}/captcha")
        await call("POST", f"{base}/submit", _submit_body())
        await call("POST", f"{base}/captcha")
        await call("POST", f"{base}/submit", _submit_body())
        await call("POST", f"{base}/submit", json.dumps({"plate": TEST_PLATE, "captcha_code": "1" * 40}).encode())
        await call("POST", f"{base}/submit", _submit_body(), forge=True)
        fake.states["fakeaspnetsession0001"].viewstate = "CHANGED-ON-THE-SERVER"
        await call("POST", f"{base}/submit", _submit_body())
        await call("POST", f"{base}/captcha")
        await call("DELETE", base)

    assert len(error_bodies) == 4
    forbidden = [
        TEST_PLATE, ACCEPTED_CODE, SECRET, session_id, "fakeaspnetsession", "VS-1-", "HX-1-", "GEN0001",
        "FAKE_VIEWSTATE_PLACEHOLDER_NOT_REAL", "lblGenelCezaToplam", "metin hatası", "CHANGED-ON-THE-SERVER",
        "1" * 40, *signatures,
    ]
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "KGM relay: POST /internal/kgm/sessions/{session_id}/submit -> 200" in log_text
    for value in forbidden:
        assert value not in log_text, f"{value!r} leaked into the log"
        for body in error_bodies:
            assert value not in body, f"{value!r} leaked into an error response"


# ---- утечки ресурсов ----

async def test_expired_sessions_are_all_closed():
    fake = FakeKgm()
    clock = _Clock()
    app = _app(fake, clock=clock, max_open_sessions=1000, session_rate_per_second=1e6, session_burst=1000)
    async with TestClient(TestServer(app)) as client:
        for _ in range(50):
            await _create(client)
        assert (await _health(client))["open_sessions"] == 50

        clock.advance(121)
        assert (await _health(client))["open_sessions"] == 0
    assert fake.created_clients == fake.closed_clients == 50


async def test_create_delete_cycles_leave_no_sessions_clients_or_memory_behind():
    """Драйвер — httpx, а не aiohttp TestClient: тот сам держит каждый
    ClientResponse до закрытия (~10 KB на цикл). wall_clock сдвигается на
    121 с за цикл, чтобы nonce истекали, — иначе линейно растёт NonceCache
    (он ограничен 4096 записями и это не утечка). Память сравнивается между
    двумя пачками по 200 циклов: первая оплачивает прогрев кэшей (yarl, re),
    вторая у чистого relay даёт ~10 KB, а утечка сессии на каждом цикле —
    ~800 KB (замерено на этой реализации с отключённым DELETE)."""
    fake = FakeKgm(track_clients=False, record=False)
    wall = _Clock(time.time())
    app = _app(fake, wall_clock=wall, session_rate_per_second=1e6, session_burst=1000, daily_session_cap=10**6)
    async with TestServer(app) as server:
        async with httpx.AsyncClient(base_url=f"http://{server.host}:{server.port}") as http:
            async def call(method: str, path: str) -> httpx.Response:
                headers = relay_auth.signed_headers(SECRET, method, path, b"", now=wall())
                return await http.request(method, path, headers=headers)

            async def cycle() -> None:
                created = await call("POST", SESSIONS)
                assert created.status_code == 201
                deleted = await call("DELETE", f"{SESSIONS}/{created.json()['session_id']}")
                assert deleted.status_code == 204
                wall.advance(121)

            for _ in range(20):
                await cycle()
            gc.collect()
            tracemalloc.start()
            try:
                for _ in range(200):
                    await cycle()
                gc.collect()
                after_first_batch = tracemalloc.get_traced_memory()[0]
                for _ in range(200):
                    await cycle()
                gc.collect()
                second_batch_growth = tracemalloc.get_traced_memory()[0] - after_first_batch
            finally:
                tracemalloc.stop()
            health = (await call("GET", HEALTH)).json()

    assert health["open_sessions"] == 0
    assert fake.created_clients == fake.closed_clients == 420
    live_sessions = [
        obj for obj in gc.get_objects()
        if isinstance(obj, KgmSession) and getattr(obj._client, "_fake", None) is fake
    ]
    assert live_sessions == []
    assert second_batch_growth < 200_000, f"memory grew by {second_batch_growth} bytes over 200 more cycles"
