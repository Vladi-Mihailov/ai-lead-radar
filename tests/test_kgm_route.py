"""Тесты reader/turkey_bot/kgm/route.py — выбор маршрута KGM: direct по
умолчанию, relay по TURKEY_KGM_ROUTE=relay (только http://<IP в сети
WireGuard>:<порт>), а неверная конфигурация relay — одна ERROR-строка при
старте и transport_error на каждой проверке KGM, БЕЗ перехода на direct.
Плюс CLI проверки связи (python -m reader.turkey_bot.kgm.route) и паритет
HTTP-клиента KGM на relay с прямым маршрутом."""

import logging
import socket

import pytest
from aiohttp.test_utils import TestServer

from reader.turkey_bot.kgm import route
from reader.turkey_bot.kgm.provider import KgmProvider
from reader.turkey_bot.kgm.relay_client import RelayKgmSession
from reader.turkey_bot.kgm.relay_server import build_kgm_relay_app, default_session_factory
from reader.turkey_bot.kgm.relay_version import kgm_code_fingerprint
from reader.turkey_bot.kgm.route import _check_relay, kgm_check_factory_from_env, main, resolve_kgm_route
from reader.turkey_bot.kgm.session import KgmSession, KgmTransportError
from reader.turkey_bot.unified import check_service
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService, default_kgm_check_factory
from reader.turkey_bot.unified.models import OverallStatus, ProviderStatus

from _kgm_relay_fakes import (
    ACCEPTED_CODE,
    LOOPBACK,
    SECRET,
    TEST_PLATE,
    FakeAvrasyaProvider,
    FakeCloseable,
    FakeGibProvider,
    FakeKgm,
    FixedCaptchaResolver,
    gib_has_debt_outcome,
)

_LOGGER = "reader.turkey_bot.kgm.route"


class _DirectFactorySpy:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise AssertionError("the direct KGM route must not be used")


class _LoadDotenvSpy:
    def __init__(self):
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return False


@pytest.fixture
def _restore_httpx_log_level():
    logger = logging.getLogger("httpx")
    level = logger.level
    yield
    logger.setLevel(level)


def _relay_env(**overrides) -> dict[str, str]:
    return {"TURKEY_KGM_ROUTE": "relay", "TURKEY_KGM_RELAY_HMAC_SECRET": SECRET} | overrides


def _loopback_env(url: str) -> dict[str, str]:
    return _relay_env(TURKEY_KGM_RELAY_URL=url, TURKEY_KGM_RELAY_ALLOWED_NETWORK="127.0.0.0/8")


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER and r.levelno == logging.ERROR]


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_default_route_is_direct_and_returns_the_given_factory_unchanged():
    assert kgm_check_factory_from_env({}, direct_factory=default_kgm_check_factory) is default_kgm_check_factory


@pytest.mark.parametrize("value", ["direct", " DIRECT ", ""])
def test_explicit_direct_route(value):
    spy = _DirectFactorySpy()

    assert kgm_check_factory_from_env({"TURKEY_KGM_ROUTE": value}, direct_factory=spy) is spy
    assert spy.calls == 0


def test_relay_settings_are_ignored_on_the_direct_route():
    env = {"TURKEY_KGM_RELAY_HMAC_SECRET": "x", "TURKEY_KGM_RELAY_URL": "garbage"}

    assert kgm_check_factory_from_env(env, direct_factory=default_kgm_check_factory) is default_kgm_check_factory


async def test_relay_route_builds_a_relay_session_and_provider_per_check(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    spy = _DirectFactorySpy()
    factory = kgm_check_factory_from_env(_relay_env(TURKEY_KGM_RELAY_URL="http://10.50.0.2:8767"), direct_factory=spy)

    first, provider = factory()
    second, _ = factory()
    try:
        assert isinstance(first, RelayKgmSession)
        assert isinstance(provider, KgmProvider)
        assert provider._session is first
        assert first is not second
        assert str(first._client.base_url).rstrip("/") == "http://10.50.0.2:8767"
    finally:
        await first.aclose()
        await second.aclose()
    assert spy.calls == 0
    assert "KGM: маршрут relay через http://10.50.0.2:8767" in caplog.text
    assert SECRET not in caplog.text


def test_relay_url_and_network_default_to_wireguard():
    route_config = resolve_kgm_route(_relay_env())

    assert route_config.name == "relay"
    assert route_config.relay_url == "http://10.50.0.2:8767"
    assert SECRET not in repr(route_config)


@pytest.mark.parametrize("url", ["http://10.50.0.2:8767/", "http://10.50.0.77:9000", " http://10.50.0.2:8767 "])
def test_ip_literal_urls_inside_the_network_are_accepted(url):
    assert resolve_kgm_route(_relay_env(TURKEY_KGM_RELAY_URL=url)).name == "relay"


def test_ipv6_url_inside_a_custom_network_is_accepted():
    env = _relay_env(TURKEY_KGM_RELAY_URL="http://[fd00::2]:8767", TURKEY_KGM_RELAY_ALLOWED_NETWORK="fd00::/64")

    assert resolve_kgm_route(env).name == "relay"


_MISCONFIGURED = [
    pytest.param({"TURKEY_KGM_ROUTE": "relay"}, "TURKEY_KGM_RELAY_HMAC_SECRET: missing", id="no-secret"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_HMAC_SECRET="s" * 31), "TURKEY_KGM_RELAY_HMAC_SECRET: too_short", id="short"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="https://10.50.0.2:8767"), "TURKEY_KGM_RELAY_URL", id="https"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="ftp://10.50.0.2:8767"), "TURKEY_KGM_RELAY_URL", id="ftp"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://relay.internal:8767"), "TURKEY_KGM_RELAY_URL", id="hostname"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://localhost:8767"), "TURKEY_KGM_RELAY_URL", id="localhost"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://10.51.0.2:8767"), "TURKEY_KGM_RELAY_URL", id="outside-net"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://127.0.0.1:8767"), "TURKEY_KGM_RELAY_URL", id="loopback"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://10.50.0.2"), "TURKEY_KGM_RELAY_URL", id="no-port"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://10.50.0.2:8767/x"), "TURKEY_KGM_RELAY_URL", id="path"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://u:pw@10.50.0.2:8767"), "TURKEY_KGM_RELAY_URL", id="userinfo"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://10.50.0.2:8767?a=1"), "TURKEY_KGM_RELAY_URL", id="query"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://10.50.0.2:abc"), "TURKEY_KGM_RELAY_URL", id="bad-port"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://10.50.0.2:70000"), "TURKEY_KGM_RELAY_URL", id="port-range"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="10.50.0.2:8767"), "TURKEY_KGM_RELAY_URL", id="no-scheme"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://"), "TURKEY_KGM_RELAY_URL", id="no-host"),
    pytest.param(
        _relay_env(TURKEY_KGM_RELAY_ALLOWED_NETWORK="10.50.0.0/33"), "TURKEY_KGM_RELAY_ALLOWED_NETWORK", id="bad-net",
    ),
    pytest.param(
        _relay_env(TURKEY_KGM_RELAY_ALLOWED_NETWORK="0.0.0.0/0"), "TURKEY_KGM_RELAY_ALLOWED_NETWORK", id="public-net",
    ),
    pytest.param({"TURKEY_KGM_ROUTE": "proxy"}, "TURKEY_KGM_ROUTE: unknown value", id="unknown-route"),
]


@pytest.mark.parametrize("env, problem", _MISCONFIGURED)
async def test_misconfigured_relay_fails_closed_without_direct_fallback(env, problem, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    spy = _DirectFactorySpy()

    factory = kgm_check_factory_from_env(env, direct_factory=spy)

    errors = _errors(caplog)
    assert len(errors) == 1
    assert problem in errors[0]
    for _ in range(3):
        closeable, provider = factory()
        with pytest.raises(KgmTransportError):
            await provider.start()
        with pytest.raises(KgmTransportError):
            await provider.refresh_captcha()
        with pytest.raises(KgmTransportError):
            await provider.submit(plate=TEST_PLATE, captcha_code=ACCEPTED_CODE)
        await closeable.aclose()
    assert spy.calls == 0
    assert len(_errors(caplog)) == 1  # ERROR только при старте, не на каждой проверке
    for value in env.values():
        if value not in ("relay", "proxy"):
            assert value not in caplog.text


async def test_misconfigured_relay_makes_only_kgm_an_error():
    factory = kgm_check_factory_from_env({"TURKEY_KGM_ROUTE": "relay"}, direct_factory=_DirectFactorySpy())
    gib = FakeGibProvider(gib_has_debt_outcome())
    avrasya = FakeAvrasyaProvider()
    service = UnifiedTurkeyCheckService(
        lambda: (FakeCloseable(), gib), lambda: (FakeCloseable(), avrasya), factory,
        captcha_resolver=FixedCaptchaResolver(),
    )

    result = await service.check(TEST_PLATE, max_attempts=3)

    kgm = result.provider_result("kgm")
    assert kgm.status == ProviderStatus.ERROR
    assert kgm.error_type == "transport_error"
    assert result.provider_result("gib").status == ProviderStatus.HAS_DEBT
    assert result.provider_result("avrasya").status == ProviderStatus.NO_DEBT
    assert result.overall_status == OverallStatus.PARTIAL


async def test_relay_kgm_client_matches_the_direct_route_client():
    direct_client = check_service._build_client()
    relay_client, relay_session = default_session_factory()
    try:
        assert relay_client.headers["User-Agent"] == direct_client.headers["User-Agent"]
        assert relay_client.timeout == direct_client.timeout
        assert relay_client.follow_redirects == direct_client.follow_redirects
        assert relay_client._trust_env == direct_client._trust_env
        assert relay_session._timeout == KgmSession(direct_client)._timeout
    finally:
        await relay_client.aclose()
        await direct_client.aclose()


# ---- python -m reader.turkey_bot.kgm.route (проверка связи без KGM) ----

def test_cli_loads_dotenv_like_the_bot_and_reports_the_direct_route(monkeypatch, capsys, _restore_httpx_log_level):
    monkeypatch.delenv("TURKEY_KGM_ROUTE", raising=False)
    spy = _LoadDotenvSpy()
    monkeypatch.setattr(route, "load_dotenv", spy)

    assert main([]) == 0
    assert spy.calls == [((), {})]  # без override: значения от systemd главнее .env
    assert "KGM route: direct" in capsys.readouterr().out


def test_cli_env_file_mode_reports_misconfiguration_without_values(
    tmp_path, monkeypatch, capsys, _restore_httpx_log_level,
):
    monkeypatch.delenv("TURKEY_KGM_ROUTE", raising=False)
    spy = _LoadDotenvSpy()
    monkeypatch.setattr(route, "load_dotenv", spy)
    env_file = tmp_path / "turkeybot-kgm-relay.env"
    env_file.write_text("TURKEY_KGM_ROUTE=relay\nTURKEY_KGM_RELAY_HMAC_SECRET=short-secret-value\n", encoding="utf-8")

    assert main(["--env-file", str(env_file)]) == 1
    out = capsys.readouterr().out
    assert "TURKEY_KGM_RELAY_HMAC_SECRET: too_short" in out
    assert "short-secret-value" not in out
    assert spy.calls == []


async def test_cli_check_reports_relay_health_and_matching_kgm_code_without_contacting_kgm(capsys):
    fake = FakeKgm()
    app = build_kgm_relay_app(hmac_secret=SECRET, allowed_networks=LOOPBACK, session_factory=fake.session_factory)
    async with TestServer(app) as server:
        url = f"http://{server.host}:{server.port}"
        code = await _check_relay(_loopback_env(url))

    out = capsys.readouterr().out
    local = kgm_code_fingerprint()
    assert code == 0
    assert f"KGM route: relay via {url}" in out
    assert f'"kgm_code": "{local}"' in out
    assert f"kgm_code: relay={local} local={local}" in out
    assert "protocol version: relay=1 local=1" in out
    assert out.rstrip().endswith("OK")
    assert "MISMATCH" not in out
    assert SECRET not in out
    assert fake.requests == []


async def test_cli_check_reports_mismatch_when_the_relay_runs_other_kgm_code(monkeypatch, capsys):
    monkeypatch.setattr(route, "kgm_code_fingerprint", lambda: "000000000000")
    app = build_kgm_relay_app(hmac_secret=SECRET, allowed_networks=LOOPBACK, session_factory=FakeKgm().session_factory)
    async with TestServer(app) as server:
        code = await _check_relay(_loopback_env(f"http://{server.host}:{server.port}"))

    out = capsys.readouterr().out
    assert code == 1
    assert f"kgm_code: relay={kgm_code_fingerprint()} local=000000000000" in out
    assert "MISMATCH:" in out


async def test_cli_check_reports_mismatch_on_another_protocol_version(monkeypatch, capsys):
    monkeypatch.setattr(route, "RELAY_PROTOCOL_VERSION", "2")
    app = build_kgm_relay_app(hmac_secret=SECRET, allowed_networks=LOOPBACK, session_factory=FakeKgm().session_factory)
    async with TestServer(app) as server:
        code = await _check_relay(_loopback_env(f"http://{server.host}:{server.port}"))

    out = capsys.readouterr().out
    assert code == 1
    assert "protocol version: relay=1 local=2" in out
    assert "MISMATCH:" in out


async def test_cli_check_fails_when_the_relay_is_down(capsys):
    code = await _check_relay(_loopback_env(f"http://127.0.0.1:{_closed_port()}"))

    assert code == 1
    assert "FAIL" in capsys.readouterr().out
