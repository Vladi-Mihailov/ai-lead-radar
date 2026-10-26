"""Tests for REDNET's inbound check-now server (reader/georgia_collector/
check_now_server.py) — see "Fix the Georgia public bot Check Now flow".
Auth itself is covered by tests/test_collector_http_auth.py (the shared
middleware); this file exercises routing, plate validation, and the three
provider outcomes (success / circuit-open / other failure) through a real
aiohttp test client, using a fake PoliceGeSession (same fixture shape as
tests/test_georgia_collector_job.py's _FakeSession)."""

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from aiohttp.test_utils import TestClient, TestServer

from reader.fines.collector_auth import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign
from reader.fines.provider import FineProviderError, PoliceGeUnavailableError
from reader.georgia_collector.check_now_server import build_check_now_app

pytestmark = pytest.mark.asyncio

_SECRET = "test-shared-secret"
_PATH = "/internal/georgia/check-now"


def _entry(*, protocol_no="AB000001", plate="AA001AA", amount=50) -> dict:
    return {
        "protocolAuto": plate,
        "protocolNo": protocol_no,
        "protocolDate": "2026-10-22",
        "activeDate": None,
        "violationDate": "2026-10-22",
        "protocolPlace": "ქუჩა",
        "protocolLaw": "125-1",
        "protocolLawDescription": "აღწერა",
        "protocolAmount": amount,
        "publishDate": "2026-10-22",
        "lastDate": "2026-12-21",
        "remainingDays": 60,
    }


def _search_response(entries: list[dict]) -> dict:
    return {"success": True, "data": {"count": len(entries), "results": entries}}


class _FakeSession:
    def __init__(self, *, result=None, raise_exc=None):
        self._result = result if result is not None else _search_response([])
        self._raise_exc = raise_exc
        self.search_calls: list[str] = []

    async def search_by_plate(self, plate: str) -> dict:
        self.search_calls.append(plate)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._result


def _signed_headers(method: str, path: str, body: bytes, *, secret=_SECRET, timestamp=None):
    ts = timestamp if timestamp is not None else str(time.time())
    signature = sign(secret, method=method, path=path, timestamp=ts, body=body)
    return {TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: signature, "Content-Type": "application/json"}


async def _post(client, body: bytes, *, secret=_SECRET):
    headers = _signed_headers("POST", _PATH, body, secret=secret)
    return await client.post(_PATH, data=body, headers=headers)


async def test_requires_auth():
    app = build_check_now_app(_FakeSession(), hmac_secret=_SECRET)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(_PATH, data=b'{"plate": "AA001AA"}')
        assert resp.status == 401


async def test_success_returns_records():
    session = _FakeSession(result=_search_response([_entry(plate="AA001AA")]))
    app = build_check_now_app(session, hmac_secret=_SECRET)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, b'{"plate": "AA001AA"}')
        assert resp.status == 200
        data = await resp.json()
        assert len(data["records"]) == 1
        assert data["records"][0]["car_number"] == "AA001AA"
        assert data["records"][0]["external_fine_id"] == "AB000001"
        assert session.search_calls == ["AA001AA"]


async def test_zero_fines_returns_empty_records():
    session = _FakeSession(result=_search_response([]))
    app = build_check_now_app(session, hmac_secret=_SECRET)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, b'{"plate": "AA001AA"}')
        assert resp.status == 200
        assert (await resp.json())["records"] == []


async def test_plate_is_normalized_before_search():
    session = _FakeSession()
    app = build_check_now_app(session, hmac_secret=_SECRET)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, b'{"plate": "aa 001-aa"}')
        assert resp.status == 200
        assert session.search_calls == ["AA001AA"]


async def test_invalid_plate_rejected_with_400():
    session = _FakeSession()
    app = build_check_now_app(session, hmac_secret=_SECRET)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, b'{"plate": ""}')
        assert resp.status == 400
        assert session.search_calls == []


async def test_malformed_payload_rejected_with_400():
    session = _FakeSession()
    app = build_check_now_app(session, hmac_secret=_SECRET)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, b'{}')
        assert resp.status == 400
        assert session.search_calls == []


async def test_circuit_open_returns_503_with_distinguishable_error():
    session = _FakeSession(raise_exc=PoliceGeUnavailableError("circuit open"))
    app = build_check_now_app(session, hmac_secret=_SECRET)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, b'{"plate": "AA001AA"}')
        assert resp.status == 503
        assert (await resp.json())["error"] == "circuit_open"


async def test_other_provider_error_returns_502():
    session = _FakeSession(raise_exc=FineProviderError("transport failed"))
    app = build_check_now_app(session, hmac_secret=_SECRET)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, b'{"plate": "AA001AA"}')
        assert resp.status == 502
        assert (await resp.json())["error"] == "provider_error"


async def test_unparseable_police_ge_response_returns_502():
    session = _FakeSession(result={"success": False})
    app = build_check_now_app(session, hmac_secret=_SECRET)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, b'{"plate": "AA001AA"}')
        assert resp.status == 502
        assert (await resp.json())["error"] == "parse_error"
