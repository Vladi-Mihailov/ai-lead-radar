"""End-to-end: RelayKgmSession (production) <-> настоящий relay_server в
aiohttp TestServer на 127.0.0.1 <-> FakeKgm с реальными фикстурами ответов
KGM -> KgmProvider + parser на стороне production -> UnifiedTurkeyCheckService.
OCR подменён фиксированным кодом, GİB/Avrasya — фейками; сеть за пределы
127.0.0.1 не используется (сеть relay в тестах — явно 127.0.0.0/8)."""

import logging
import socket

import httpx
import pytest
from aiohttp.test_utils import TestServer

import reader.turkey_bot.kgm.session as kgm_session
from reader.turkey_bot.kgm.provider import KgmProvider
from reader.turkey_bot.kgm.relay_client import RelayKgmSession
from reader.turkey_bot.kgm.relay_server import build_kgm_relay_app
from reader.turkey_bot.kgm.route import kgm_check_factory_from_env
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService
from reader.turkey_bot.unified.models import OverallStatus, ProviderStatus

from _kgm_relay_fakes import (
    ACCEPTED_CODE,
    HAS_DEBT_GRAND_TOTAL,
    LOOPBACK,
    NO_DEBT_DELTA,
    OTHER_SECRET,
    SECRET,
    TEST_PLATE,
    FakeAvrasyaProvider,
    FakeCloseable,
    FakeGibProvider,
    FakeKgm,
    FixedCaptchaResolver,
    captcha_image,
    gib_has_debt_outcome,
)

_RELAY_LOGGERS = ("reader.turkey_bot.kgm.relay_client", "reader.turkey_bot.kgm.relay_server", "httpx")


@pytest.fixture(autouse=True)
def _no_throttle_delay(monkeypatch):
    monkeypatch.setattr(kgm_session, "_MIN_REQUEST_INTERVAL_SECONDS", 0.0)


def _relay_server(fake: FakeKgm, **kwargs) -> TestServer:
    return TestServer(build_kgm_relay_app(
        hmac_secret=SECRET, allowed_networks=LOOPBACK, session_factory=fake.session_factory, **kwargs,
    ))


def _url(server: TestServer) -> str:
    return f"http://{server.host}:{server.port}"


def _closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _relay_env(url: str, secret: str = SECRET) -> dict[str, str]:
    return {
        "TURKEY_KGM_ROUTE": "relay",
        "TURKEY_KGM_RELAY_URL": url,
        "TURKEY_KGM_RELAY_ALLOWED_NETWORK": "127.0.0.0/8",
        "TURKEY_KGM_RELAY_HMAC_SECRET": secret,
    }


def _never_direct():
    raise AssertionError("the direct KGM route must never be used as a fallback")


def _service(kgm_factory, *, gib_outcome=None, resolver=None) -> UnifiedTurkeyCheckService:
    gib = FakeGibProvider(gib_outcome)
    avrasya = FakeAvrasyaProvider()
    return UnifiedTurkeyCheckService(
        lambda: (FakeCloseable(), gib),
        lambda: (FakeCloseable(), avrasya),
        kgm_factory,
        captcha_resolver=resolver or FixedCaptchaResolver(),
    )


class _FailingDeleteTransport(httpx.AsyncBaseTransport):
    """Настоящий HTTP к relay, кроме DELETE: тот падает так, как задано."""

    def __init__(self, failure):
        self._inner = httpx.AsyncHTTPTransport()
        self._failure = failure

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            if isinstance(self._failure, BaseException):
                raise self._failure
            return self._failure
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


async def test_provider_through_relay_parses_has_debt_on_the_production_side():
    fake = FakeKgm()
    async with _relay_server(fake) as server:
        session = RelayKgmSession(_url(server), SECRET)
        provider = KgmProvider(session)
        challenge = await provider.start()
        outcome = await provider.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
        await session.aclose()

    assert challenge.image_png == captcha_image(1, 1)
    assert outcome.kind == "has_debt"
    assert outcome.grand_total == HAS_DEBT_GRAND_TOTAL
    assert session.last_submit_seconds is not None
    assert fake.created_clients == fake.closed_clients == 1


async def test_provider_through_relay_parses_no_debt():
    fake = FakeKgm(success_delta=NO_DEBT_DELTA)
    async with _relay_server(fake) as server:
        session = RelayKgmSession(_url(server), SECRET)
        provider = KgmProvider(session)
        await provider.start()
        outcome = await provider.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
        await session.aclose()

    assert outcome.kind == "no_debt"


async def test_rejected_captcha_then_retry_in_the_same_relay_session():
    fake = FakeKgm(reject_first=1)
    async with _relay_server(fake) as server:
        session = RelayKgmSession(_url(server), SECRET)
        provider = KgmProvider(session)
        await provider.start()
        first = await provider.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
        challenge = await provider.refresh_captcha()
        second = await provider.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
        await session.aclose()

    assert first.kind == "rejected"
    assert challenge.image_png == captcha_image(1, 2)
    assert second.kind == "has_debt"
    assert [r["cookie"] for r in fake.submits()] == ["fakeaspnetsession0001"] * 2
    assert [r["form"]["__VIEWSTATE"] for r in fake.submits()] == ["VS-1-0", "VS-1-1"]
    assert [r["form"]["hdnx"] for r in fake.submits()] == ["HX-1-0", "HX-1-1"]


async def test_unified_check_through_the_relay_route_has_debt():
    fake = FakeKgm()
    async with _relay_server(fake) as server:
        factory = kgm_check_factory_from_env(_relay_env(_url(server)), direct_factory=_never_direct)
        result = await _service(factory).check(TEST_PLATE, max_attempts=3)

    kgm = result.provider_result("kgm")
    assert kgm.status == ProviderStatus.HAS_DEBT
    assert kgm.total_amount == HAS_DEBT_GRAND_TOTAL
    assert result.provider_result("gib").status == ProviderStatus.NO_DEBT
    assert result.provider_result("avrasya").status == ProviderStatus.NO_DEBT
    assert result.overall_status == OverallStatus.HAS_DEBT
    # check_service закрыл RelayKgmSession -> DELETE -> relay закрыл KGM-клиент.
    assert fake.created_clients == fake.closed_clients == 1


async def test_unified_check_retries_a_rejected_captcha_with_the_refreshed_image():
    fake = FakeKgm(reject_first=1)
    resolver = FixedCaptchaResolver()
    async with _relay_server(fake) as server:
        factory = kgm_check_factory_from_env(_relay_env(_url(server)), direct_factory=_never_direct)
        result = await _service(factory, resolver=resolver).check(TEST_PLATE, max_attempts=3)

    assert result.provider_result("kgm").status == ProviderStatus.HAS_DEBT
    assert resolver.kgm_images == [captcha_image(1, 1), captcha_image(1, 2)]
    assert fake.created_clients == fake.closed_clients == 1


@pytest.mark.parametrize("failure", [
    pytest.param(RuntimeError(f"DELETE failed for {TEST_PLATE}"), id="exception"),
    pytest.param(httpx.Response(500, content=b"[" * 100_000 + b"]" * 100_000), id="garbage-500"),
])
async def test_failed_delete_keeps_the_successful_kgm_result_and_logs_no_plate(failure, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeKgm()
    async with _relay_server(fake) as server:
        url = _url(server)

        def factory():
            session = RelayKgmSession(url, SECRET, transport=_FailingDeleteTransport(failure))
            return session, KgmProvider(session)

        result = await _service(factory).check(TEST_PLATE, max_attempts=3)

    kgm = result.provider_result("kgm")
    assert kgm.status == ProviderStatus.HAS_DEBT
    assert kgm.total_amount == HAS_DEBT_GRAND_TOTAL
    assert result.overall_status == OverallStatus.HAS_DEBT
    relay_lines = [r.getMessage() for r in caplog.records if r.name in _RELAY_LOGGERS]
    assert [line for line in relay_lines if line.startswith("KGM relay close:")]
    assert not [line for line in relay_lines if TEST_PLATE in line]


async def test_relay_down_gives_kgm_transport_error_partial_and_keeps_gib_avrasya_results():
    factory = kgm_check_factory_from_env(_relay_env(_closed_port_url()), direct_factory=_never_direct)

    result = await _service(factory, gib_outcome=gib_has_debt_outcome()).check(TEST_PLATE, max_attempts=3)

    kgm = result.provider_result("kgm")
    assert kgm.status == ProviderStatus.ERROR
    assert kgm.error_type == "transport_error"
    gib = result.provider_result("gib")
    assert gib.status == ProviderStatus.HAS_DEBT
    assert gib.total_amount == 237
    assert result.provider_result("avrasya").status == ProviderStatus.NO_DEBT
    assert result.overall_status == OverallStatus.PARTIAL


async def test_secret_mismatch_gives_kgm_transport_error_and_partial():
    fake = FakeKgm()
    async with _relay_server(fake) as server:
        factory = kgm_check_factory_from_env(_relay_env(_url(server), OTHER_SECRET), direct_factory=_never_direct)
        result = await _service(factory).check(TEST_PLATE)

    assert result.provider_result("kgm").error_type == "transport_error"
    assert result.overall_status == OverallStatus.PARTIAL
    assert fake.requests == []


async def test_kgm_unreachable_from_the_relay_gives_kgm_transport_error_and_partial():
    fake = FakeKgm(page_error=httpx.ConnectTimeout)
    async with _relay_server(fake) as server:
        factory = kgm_check_factory_from_env(_relay_env(_url(server)), direct_factory=_never_direct)
        result = await _service(factory).check(TEST_PLATE, max_attempts=3)

    assert result.provider_result("kgm").error_type == "transport_error"
    assert result.provider_result("gib").status == ProviderStatus.NO_DEBT
    assert result.overall_status == OverallStatus.PARTIAL
    assert [r["kind"] for r in fake.requests] == ["page"]  # transport_error не повторяется
    assert fake.created_clients == fake.closed_clients == 1
