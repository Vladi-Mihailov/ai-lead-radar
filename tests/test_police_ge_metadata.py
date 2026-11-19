"""Метаданные ответа police.ge для статусов оплаты и вечерней перепроверки:
дата "Date of update of information", data.count (неполный ответ), 403/429
как отдельный сигнал блокировки, флаг refresh_page — от страницы police.ge
через REDNET check-now сервер до production-провайдеров. Реальная сеть не
используется (httpx.MockTransport / aiohttp TestClient); номера и
квитанции синтетические."""

import json
import sys
import time
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from reader.fines.collector_auth import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign
from reader.fines.collector_payload import record_to_payload
from reader.fines.models import ParsedFineRecord
from reader.fines.parser import extract_reported_count, parse_source_updated_on
from reader.fines.police_ge_circuit_breaker import PoliceGeCircuitBreaker
from reader.fines.police_ge_provider import PoliceGeProvider
from reader.fines.police_ge_session import PoliceGeSession
from reader.fines.provider import (
    FineProvider,
    FineProviderError,
    PoliceGeBlockedError,
    PoliceGeUnavailableError,
    ProviderSearchResult,
    search_detailed,
)
from reader.fines.rednet_check_now_provider import AutoFailoverCheckNowProvider, RednetCheckNowProvider
from reader.georgia_collector.check_now_server import build_check_now_app

_PAGE_URL = "https://police.ge/protocol/index.php?lang=en"
_SECRET = "test-secret"
_PATH = "/internal/georgia/check-now"


def _page(token="tok", updated="Date of update of information 12.11.2026") -> str:
    return f'<html><p>{updated}</p><input type="hidden" name="csrf_token" value="{token}"></html>'


def _entry(protocol_no="GE000001", plate="AA111BB", amount=50) -> dict:
    return {
        "protocolAuto": plate, "protocolNo": protocol_no, "protocolDate": "2026-11-01",
        "violationDate": "2026-11-01", "activeDate": None, "lastDate": "2026-12-31",
        "protocolAmount": amount, "protocolPlace": "x", "protocolLaw": "125-1-0",
        "protocolLawDescription": "y", "publishDate": "2026-11-01", "remainingDays": 40,
    }


def _search(entries, count=None) -> dict:
    return {"success": True, "message": None,
            "data": {"count": len(entries) if count is None else count, "results": entries}}


def _record(protocol_no="GE000001") -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number="AA111BB", external_fine_id=protocol_no, penalty_date=None, due_date=None,
        delivered_status="Не вручено", fingerprint=f"fp-{protocol_no}", raw_data={}, amount=50,
    )


# ---- parser ----


@pytest.mark.parametrize("html, expected", [
    ("<p>Date of update of information 22.10.2026</p>", date(2026, 10, 22)),
    ("Date of update of information: 12.11.2026", date(2026, 11, 12)),
    ("Date of update of information 1.2.2026", date(2026, 2, 1)),
    ("Date of update of information 31.02.2026", None),
    ("<p>no date here</p>", None),
    ("", None),
    (None, None),
])
def test_parse_source_updated_on(html, expected):
    assert parse_source_updated_on(html) == expected


@pytest.mark.parametrize("raw, expected", [
    ({"success": True, "data": {"count": 3, "results": []}}, 3),
    ({"success": True, "data": {"count": True, "results": []}}, None),
    ({"success": True, "data": {"count": "3", "results": []}}, None),
    ({"success": True, "data": {"results": []}}, None),
    ({"success": True, "data": []}, None),
    ([], None),
])
def test_extract_reported_count(raw, expected):
    assert extract_reported_count(raw) == expected


def test_provider_search_result_completeness():
    assert ProviderSearchResult(records=[_record()]).complete
    assert ProviderSearchResult(records=[_record()], reported_count=1).complete
    assert not ProviderSearchResult(records=[_record()], reported_count=2).complete
    assert not ProviderSearchResult(records=[], reported_count=1).complete


# ---- PoliceGeSession / PoliceGeProvider ----


def _session(handler, *, breaker=None) -> PoliceGeSession:
    client = httpx.AsyncClient(base_url="https://police.ge/protocol/", transport=httpx.MockTransport(handler))
    return PoliceGeSession(client, page_url=_PAGE_URL, request_timeout=5, circuit_breaker=breaker)


async def test_session_reads_source_date_from_csrf_page():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, text=_page())
        return httpx.Response(200, json=_search([_entry()]))

    session = _session(handler)
    assert session.source_updated_on is None
    await session.search_by_plate("AA111BB")
    assert session.source_updated_on == date(2026, 11, 12)


async def test_refresh_page_makes_exactly_one_get():
    calls = []
    pages = iter([_page(updated="Date of update of information 12.11.2026"),
                  _page(updated="Date of update of information 18.11.2026")])

    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, text=next(pages))
        return httpx.Response(200, json=_search([]))

    session = _session(handler)
    await session.search_by_plate("AA111BB")
    await session.refresh_page()
    assert calls == ["GET", "POST", "GET"]
    assert session.source_updated_on == date(2026, 11, 18)


@pytest.mark.parametrize("status", [403, 429])
async def test_blocking_status_raises_blocked_error_and_counts_for_breaker(status):
    breaker = PoliceGeCircuitBreaker(failure_threshold=1)

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, text=_page())
        return httpx.Response(status, text="no")

    session = _session(handler, breaker=breaker)
    with pytest.raises(PoliceGeBlockedError) as excinfo:
        await session.search_by_plate("AA111BB")
    assert excinfo.value.status_code == status
    assert not breaker.allow_request()  # для circuit breaker это по-прежнему сбой


async def test_blocking_status_on_page_get():
    def handler(request):
        return httpx.Response(403, text="forbidden")

    with pytest.raises(PoliceGeBlockedError):
        await _session(handler).search_by_plate("AA111BB")


async def test_other_http_errors_stay_generic():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, text=_page())
        return httpx.Response(500, text="oops")

    with pytest.raises(FineProviderError) as excinfo:
        await _session(handler).search_by_plate("AA111BB")
    assert not isinstance(excinfo.value, PoliceGeBlockedError)


async def test_refresh_page_respects_open_breaker():
    breaker = PoliceGeCircuitBreaker(failure_threshold=1)
    breaker.record_transport_failure()
    session = _session(lambda request: httpx.Response(200, text=_page()), breaker=breaker)
    with pytest.raises(PoliceGeUnavailableError):
        await session.refresh_page()


async def test_police_ge_provider_detailed_returns_count_and_date():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, text=_page())
        return httpx.Response(200, json=_search([_entry("GE1"), _entry("GE2")], count=3))

    provider = PoliceGeProvider(_session(handler))
    result = await provider.search_by_plate_detailed("AA111BB")
    assert [r.external_fine_id for r in result.records] == ["GE1", "GE2"]
    assert result.reported_count == 3 and not result.complete
    assert result.source_updated_on == date(2026, 11, 12)
    assert [r.external_fine_id for r in await provider.search_by_plate("AA111BB")] == ["GE1", "GE2"]


async def test_search_detailed_falls_back_for_duck_typed_providers():
    class _Plain:
        async def search_by_plate(self, plate):
            return [_record()]

    result = await search_detailed(_Plain(), "AA111BB", refresh_page=True)
    assert result.records == [_record()] and result.reported_count is None


async def test_base_provider_detailed_default():
    class _Base(FineProvider):
        async def search_by_plate(self, plate):
            return [_record()]

    result = await _Base().search_by_plate_detailed("AA111BB", refresh_page=True)
    assert result == ProviderSearchResult(records=[_record()])


# ---- REDNET check-now server ----


class _FakeSession:
    def __init__(self, *, result=None, raise_exc=None, source_updated_on=None):
        self._result = result if result is not None else _search([])
        self._raise_exc = raise_exc
        self.source_updated_on = source_updated_on
        self.calls: list[str] = []

    async def refresh_page(self):
        self.calls.append("refresh")
        self.source_updated_on = date(2026, 11, 18)

    async def search_by_plate(self, plate):
        self.calls.append(f"search:{plate}")
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._result


async def _post(client, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    ts = str(time.time())
    headers = {
        TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: sign(_SECRET, method="POST", path=_PATH, timestamp=ts, body=body),
        "Content-Type": "application/json",
    }
    return await client.post(_PATH, data=body, headers=headers)


async def test_server_success_includes_count_and_source_date():
    session = _FakeSession(result=_search([_entry()], count=1), source_updated_on=date(2026, 11, 12))
    async with TestClient(TestServer(build_check_now_app(session, hmac_secret=_SECRET))) as client:
        resp = await _post(client, {"plate": "AA111BB"})
        data = await resp.json()
    assert resp.status == 200
    assert data["count"] == 1 and data["source_updated_on"] == "2026-11-12"
    assert session.calls == ["search:AA111BB"]  # без флага страница не перечитывается


async def test_server_refresh_page_flag():
    session = _FakeSession()
    async with TestClient(TestServer(build_check_now_app(session, hmac_secret=_SECRET))) as client:
        resp = await _post(client, {"plate": "AA111BB", "refresh_page": True})
        data = await resp.json()
    assert session.calls == ["refresh", "search:AA111BB"]
    assert data["source_updated_on"] == "2026-11-18"


async def test_server_blocked_returns_502_with_http_status():
    session = _FakeSession(raise_exc=PoliceGeBlockedError("x", status_code=429))
    async with TestClient(TestServer(build_check_now_app(session, hmac_secret=_SECRET))) as client:
        resp = await _post(client, {"plate": "AA111BB"})
        data = await resp.json()
    assert resp.status == 502
    assert data == {"error": "blocked", "http_status": 429}


async def test_server_unknown_source_date_is_null():
    session = _FakeSession(result=_search([]))
    async with TestClient(TestServer(build_check_now_app(session, hmac_secret=_SECRET))) as client:
        data = await (await _post(client, {"plate": "AA111BB"})).json()
    assert data["source_updated_on"] is None and data["count"] == 0


# ---- RednetCheckNowProvider / AutoFailover ----


def _rednet(handler) -> RednetCheckNowProvider:
    client = httpx.AsyncClient(base_url="http://10.50.0.2:8766", transport=httpx.MockTransport(handler))
    return RednetCheckNowProvider(client, hmac_secret="s")


async def test_rednet_detailed_parses_new_fields():
    def handler(request):
        return httpx.Response(200, json={
            "records": [record_to_payload(_record())], "count": 2, "source_updated_on": "2026-11-12",
        })

    result = await _rednet(handler).search_by_plate_detailed("AA111BB")
    assert result.reported_count == 2 and result.source_updated_on == date(2026, 11, 12)
    assert not result.complete


async def test_rednet_old_server_without_fields_is_treated_as_complete():
    def handler(request):
        return httpx.Response(200, json={"records": [record_to_payload(_record())]})

    result = await _rednet(handler).search_by_plate_detailed("AA111BB")
    assert result.reported_count is None and result.source_updated_on is None and result.complete


async def test_rednet_body_unchanged_without_refresh_flag_and_flag_when_requested():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"records": []})

    provider = _rednet(handler)
    await provider.search_by_plate("AA111BB")
    await provider.search_by_plate_detailed("AA111BB", refresh_page=True)
    assert bodies == [{"plate": "AA111BB"}, {"plate": "AA111BB", "refresh_page": True}]


async def test_rednet_blocked_maps_to_blocked_error():
    def handler(request):
        return httpx.Response(502, json={"error": "blocked", "http_status": 403})

    with pytest.raises(PoliceGeBlockedError) as excinfo:
        await _rednet(handler).search_by_plate("AA111BB")
    assert excinfo.value.status_code == 403


@pytest.mark.parametrize("response", [
    httpx.Response(502, json={"error": "provider_error"}),
    httpx.Response(502, text="not json"),
    httpx.Response(502, json={"error": "blocked", "http_status": "403"}),
])
async def test_rednet_other_errors_stay_generic(response):
    with pytest.raises(FineProviderError) as excinfo:
        await _rednet(lambda request: response).search_by_plate("AA111BB")
    assert not isinstance(excinfo.value, PoliceGeBlockedError)


async def test_rednet_503_still_unavailable():
    with pytest.raises(PoliceGeUnavailableError):
        await _rednet(lambda request: httpx.Response(503, json={"error": "circuit_open"})).search_by_plate("X1")


async def test_auto_failover_detailed_uses_rednet_metadata_after_local_failure():
    class _FailingLocal:
        async def search_by_plate(self, plate):
            raise FineProviderError("blocked from DE")

    def handler(request):
        return httpx.Response(200, json={"records": [], "count": 0, "source_updated_on": "2026-11-12"})

    auto = AutoFailoverCheckNowProvider(_FailingLocal(), _rednet(handler))
    result = await auto.search_by_plate_detailed("AA111BB", refresh_page=True)
    assert result.source_updated_on == date(2026, 11, 12) and result.reported_count == 0
