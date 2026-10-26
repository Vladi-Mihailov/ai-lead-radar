"""Tests for reader/fines/rednet_check_now_provider.py (RednetCheckNowProvider
and AutoFailoverCheckNowProvider) — see "Fix the Georgia public bot Check Now
flow". AutoFailoverCheckNowProvider's tests are the important ones here: they
prove REDNET is a FALLBACK transport, not a mandatory dependency — local
police.ge is always tried first, REDNET is only used on failure, and a real
zero-fines result never triggers a fallback."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import pytest

from reader.fines.collector_payload import record_to_payload
from reader.fines.models import ParsedFineRecord
from reader.fines.police_ge_circuit_breaker import PoliceGeCircuitBreaker
from reader.fines.police_ge_provider import PoliceGeProvider
from reader.fines.police_ge_session import PoliceGeSession
from reader.fines.provider import (
    FineProvider,
    FineProviderError,
    PoliceGeUnavailableError,
)
from reader.fines.rednet_check_now_provider import (
    AutoFailoverCheckNowProvider,
    RednetCheckNowProvider,
)

pytestmark = pytest.mark.asyncio


def _client_with_transport(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://10.50.0.2:8766", transport=transport)


def _record(plate="AA001AA") -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=plate, external_fine_id="AB000001", penalty_date=None, due_date=None,
        delivered_status="Не вручено", fingerprint="fp-1", raw_data={}, violation_date=None,
        amount=50, place=None, violation_description=None,
    )


# ==== RednetCheckNowProvider (the HTTP transport itself) ====


async def test_rednet_provider_success_returns_records():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"records": [record_to_payload(_record())]})

    async with _client_with_transport(httpx.MockTransport(handler)) as http_client:
        provider = RednetCheckNowProvider(http_client, hmac_secret="s")
        records = await provider.search_by_plate("AA001AA")
        assert len(records) == 1
        assert records[0].car_number == "AA001AA"


async def test_rednet_provider_zero_fines_returns_empty_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"records": []})

    async with _client_with_transport(httpx.MockTransport(handler)) as http_client:
        provider = RednetCheckNowProvider(http_client, hmac_secret="s")
        assert await provider.search_by_plate("AA001AA") == []


async def test_rednet_provider_circuit_open_raises_police_ge_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "circuit_open"})

    async with _client_with_transport(httpx.MockTransport(handler)) as http_client:
        provider = RednetCheckNowProvider(http_client, hmac_secret="s")
        with pytest.raises(PoliceGeUnavailableError):
            await provider.search_by_plate("AA001AA")


async def test_rednet_provider_other_error_status_raises_fine_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "provider_error"})

    async with _client_with_transport(httpx.MockTransport(handler)) as http_client:
        provider = RednetCheckNowProvider(http_client, hmac_secret="s")
        with pytest.raises(FineProviderError):
            await provider.search_by_plate("AA001AA")


async def test_rednet_provider_transport_failure_raises_fine_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with _client_with_transport(httpx.MockTransport(handler)) as http_client:
        provider = RednetCheckNowProvider(http_client, hmac_secret="s")
        with pytest.raises(FineProviderError):
            await provider.search_by_plate("AA001AA")


async def test_rednet_provider_malformed_body_raises_fine_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    async with _client_with_transport(httpx.MockTransport(handler)) as http_client:
        provider = RednetCheckNowProvider(http_client, hmac_secret="s")
        with pytest.raises(FineProviderError):
            await provider.search_by_plate("AA001AA")


async def test_rednet_provider_signs_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, json={"records": []})

    async with _client_with_transport(httpx.MockTransport(handler)) as http_client:
        provider = RednetCheckNowProvider(http_client, hmac_secret="s")
        await provider.search_by_plate("AA001AA")

    assert "X-Collector-Timestamp" in captured["headers"]
    assert "X-Collector-Signature" in captured["headers"]


# ==== AutoFailoverCheckNowProvider ====


class _StubProvider(FineProvider):
    def __init__(self, *, result=None, raise_exc=None, available=True):
        self._result = result if result is not None else []
        self._raise_exc = raise_exc
        self._available = available
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return self._available

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        self.calls.append(plate)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._result


async def test_auto_uses_local_when_local_succeeds():
    local = _StubProvider(result=[_record()])
    rednet = _StubProvider()
    auto = AutoFailoverCheckNowProvider(local, rednet)

    records = await auto.search_by_plate("AA001AA")

    assert records == [_record()]
    assert local.calls == ["AA001AA"]
    assert rednet.calls == []


async def test_auto_does_not_fall_back_on_legitimate_zero_fines():
    local = _StubProvider(result=[])
    rednet = _StubProvider()
    auto = AutoFailoverCheckNowProvider(local, rednet)

    records = await auto.search_by_plate("AA001AA")

    assert records == []
    assert local.calls == ["AA001AA"]
    assert rednet.calls == []  # a real empty result is not a failure


async def test_auto_falls_back_to_rednet_on_local_transport_failure():
    local = _StubProvider(raise_exc=FineProviderError("transport failed"))
    rednet = _StubProvider(result=[_record()])
    auto = AutoFailoverCheckNowProvider(local, rednet)

    records = await auto.search_by_plate("AA001AA")

    assert records == [_record()]
    assert local.calls == ["AA001AA"]
    assert rednet.calls == ["AA001AA"]


async def test_auto_falls_back_to_rednet_when_local_circuit_breaker_already_open():
    """local here is a REAL PoliceGeProvider/PoliceGeSession with a forced-
    open circuit breaker — proves the existing breaker's own "reject
    instantly, no request_timeout wait" behavior (see
    PoliceGeSession.search_by_plate()'s docstring) is what AUTO relies on,
    and that local's underlying HTTP transport is never touched once open."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("local HTTP transport must not be called while the breaker is open")

    breaker = PoliceGeCircuitBreaker(failure_threshold=1)
    breaker.record_transport_failure()
    assert breaker.allow_request() is False

    async with httpx.AsyncClient(
        base_url="https://police.ge/protocol/", transport=httpx.MockTransport(handler),
    ) as http_client:
        local = PoliceGeProvider(
            PoliceGeSession(
                http_client, page_url="https://police.ge/protocol/",
                request_timeout=30.0, circuit_breaker=breaker,
            ),
        )
        rednet = _StubProvider(result=[_record()])
        auto = AutoFailoverCheckNowProvider(local, rednet)

        records = await auto.search_by_plate("AA001AA")

        assert records == [_record()]
        assert rednet.calls == ["AA001AA"]


async def test_auto_both_fail_raises_exactly_one_error():
    local = _StubProvider(raise_exc=FineProviderError("local down"))
    rednet = _StubProvider(raise_exc=FineProviderError("rednet also down"))
    auto = AutoFailoverCheckNowProvider(local, rednet)

    with pytest.raises(FineProviderError):
        await auto.search_by_plate("AA001AA")

    assert local.calls == ["AA001AA"]
    assert rednet.calls == ["AA001AA"]


async def test_auto_both_fail_does_not_retry_either_side():
    """Exactly one attempt per side — no internal retry loop here (that
    would be a second, hidden failure mode on top of FineCheckService's own
    single-catch error handling, see module docstring)."""
    local = _StubProvider(raise_exc=FineProviderError("local down"))
    rednet = _StubProvider(raise_exc=PoliceGeUnavailableError("rednet circuit open too"))
    auto = AutoFailoverCheckNowProvider(local, rednet)

    with pytest.raises(FineProviderError):
        await auto.search_by_plate("AA001AA")

    assert local.calls == ["AA001AA"]
    assert rednet.calls == ["AA001AA"]


async def test_auto_is_available_true_if_either_side_available():
    assert AutoFailoverCheckNowProvider(
        _StubProvider(available=False), _StubProvider(available=True),
    ).is_available() is True
    assert AutoFailoverCheckNowProvider(
        _StubProvider(available=True), _StubProvider(available=False),
    ).is_available() is True
    assert AutoFailoverCheckNowProvider(
        _StubProvider(available=False), _StubProvider(available=False),
    ).is_available() is False
