"""Unit tests for the extracted shared HMAC middleware (reader/fines/
collector_http_auth.py) — see "Fix the Georgia public bot Check Now flow".
End-to-end coverage of collector_api.py's own routes through this middleware
already exists in tests/test_georgia_collector_api.py (unchanged by the
extraction, still green); this file tests the middleware in isolation,
against a minimal aiohttp app, so both callers (collector_api.py and the new
check_now_server.py) are provably covered by one shared implementation."""

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from reader.fines.collector_auth import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign
from reader.fines.collector_http_auth import build_hmac_middleware

pytestmark = pytest.mark.asyncio

_SECRET = "test-shared-secret"


def _make_app(*, secret=_SECRET):
    app = web.Application(middlewares=[build_hmac_middleware(secret)])

    async def ping(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app.router.add_get("/ping", ping)
    app.router.add_post("/ping", ping)
    return app


def _signed_headers(method: str, path: str, body: bytes, *, secret=_SECRET, timestamp=None):
    ts = timestamp if timestamp is not None else str(time.time())
    signature = sign(secret, method=method, path=path, timestamp=ts, body=body)
    return {TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: signature}


async def test_valid_signature_allows_request():
    app = _make_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/ping", headers=_signed_headers("GET", "/ping", b""))
        assert resp.status == 200
        assert await resp.json() == {"status": "ok"}


async def test_invalid_signature_rejected():
    app = _make_app()
    async with TestClient(TestServer(app)) as client:
        ts = str(time.time())
        resp = await client.get("/ping", headers={TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: "0" * 64})
        assert resp.status == 401
        assert await resp.json() == {"error": "unauthorized"}


async def test_missing_timestamp_rejected():
    app = _make_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/ping", headers={SIGNATURE_HEADER: "0" * 64})
        assert resp.status == 401


async def test_stale_timestamp_rejected():
    app = _make_app()
    async with TestClient(TestServer(app)) as client:
        stale_ts = str(time.time() - 1000)
        headers = _signed_headers("GET", "/ping", b"", timestamp=stale_ts)
        resp = await client.get("/ping", headers=headers)
        assert resp.status == 401


async def test_wrong_secret_rejected():
    app = _make_app(secret=_SECRET)
    async with TestClient(TestServer(app)) as client:
        headers = _signed_headers("GET", "/ping", b"", secret="a-different-secret")
        resp = await client.get("/ping", headers=headers)
        assert resp.status == 401


async def test_signature_bound_to_method_and_path():
    """A valid signature for POST /ping must not authorize GET /ping, and
    vice versa — the canonical string binds method+path (see
    collector_auth.canonical_string())."""
    app = _make_app()
    async with TestClient(TestServer(app)) as client:
        headers = _signed_headers("POST", "/ping", b"")
        resp = await client.get("/ping", headers=headers)
        assert resp.status == 401


async def test_signature_bound_to_body():
    app = _make_app()
    async with TestClient(TestServer(app)) as client:
        headers = _signed_headers("POST", "/ping", b'{"a": 1}')
        resp = await client.post("/ping", data=b'{"a": 2}', headers=headers)
        assert resp.status == 401


async def test_handler_never_called_on_auth_failure():
    calls = []
    app = web.Application(middlewares=[build_hmac_middleware(_SECRET)])

    async def handler(request: web.Request) -> web.Response:
        calls.append(1)
        return web.json_response({"status": "ok"})

    app.router.add_get("/ping", handler)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/ping", headers={TIMESTAMP_HEADER: str(time.time()), SIGNATURE_HEADER: "0" * 64})
        assert resp.status == 401
        assert calls == []
