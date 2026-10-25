"""Tests for ProductionApiClient (reader/georgia_collector/
production_client.py) — the REDNET collector's bounded-retry HTTP client
toward production's internal collector API. See "Georgia remote collector
architecture (REDNET)", requirement G: "collector retries with bounded
backoff... does not hammer police.ge" (this client's retries only affect
calls to PRODUCTION, never police.ge directly)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import pytest

from reader.georgia_collector.production_client import (
    ProductionApiClient,
    ProductionApiError,
)

pytestmark = pytest.mark.asyncio


def _client_with_transport(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="https://production.example", transport=transport)


async def test_successful_request_returns_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"plates": ["AA001AA"]})

    async with _client_with_transport(httpx.MockTransport(handler)) as http_client:
        client = ProductionApiClient(http_client, hmac_secret="s")
        plates = await client.get_active_plates()
        assert plates == ["AA001AA"]


async def test_transport_failure_retries_then_succeeds():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"plates": []})

    async with _client_with_transport(httpx.MockTransport(handler)) as http_client:
        client = ProductionApiClient(http_client, hmac_secret="s", backoff_seconds=0.01)
        plates = await client.get_active_plates()
        assert plates == []
        assert attempts["count"] == 2


async def test_transport_failure_exhausts_retries_and_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with _client_with_transport(httpx.MockTransport(handler)) as http_client:
        client = ProductionApiClient(http_client, hmac_secret="s", max_attempts=2, backoff_seconds=0.01)
        with pytest.raises(ProductionApiError):
            await client.get_active_plates()


async def test_http_error_status_raises_after_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    async with _client_with_transport(httpx.MockTransport(handler)) as http_client:
        client = ProductionApiClient(http_client, hmac_secret="s", max_attempts=1)
        with pytest.raises(ProductionApiError):
            await client.get_active_plates()


async def test_submit_feed_match_sends_signed_headers():
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"status": "ok", "tasks_matched": 1})

    async with _client_with_transport(httpx.MockTransport(handler)) as http_client:
        client = ProductionApiClient(http_client, hmac_secret="s")
        result = await client.submit_feed_match({"car_number": "AA001AA"})
        assert result == {"status": "ok", "tasks_matched": 1}
        assert "x-collector-timestamp" in seen_headers
        assert "x-collector-signature" in seen_headers
