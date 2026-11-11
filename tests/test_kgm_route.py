"""Тесты reader/turkey_bot/kgm/route.py — выбор маршрута KGM: direct по
умолчанию, relay по TURKEY_KGM_ROUTE=relay, а неверная конфигурация relay —
одна ERROR-строка при старте и transport_error на каждой проверке KGM, БЕЗ
перехода на direct."""

import logging
import socket

import pytest
from aiohttp.test_utils import TestServer

from reader.turkey_bot.kgm.provider import KgmProvider
from reader.turkey_bot.kgm.relay_client import RelayKgmSession
from reader.turkey_bot.kgm.relay_server import build_kgm_relay_app
from reader.turkey_bot.kgm.route import _check_relay, kgm_check_factory_from_env, main, resolve_kgm_route
from reader.turkey_bot.kgm.session import KgmTransportError
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService, default_kgm_check_factory
from reader.turkey_bot.unified.models import OverallStatus, ProviderStatus

from _kgm_relay_fakes import (
    ACCEPTED_CODE,
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


@pytest.fixture
def _restore_httpx_log_level():
    logger = logging.getLogger("httpx")
    level = logger.level
    yield
    logger.setLevel(level)


def _relay_env(**overrides) -> dict[str, str]:
    return {"TURKEY_KGM_ROUTE": "relay", "TURKEY_KGM_RELAY_HMAC_SECRET": SECRET} | overrides


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER and r.levelno == logging.ERROR]


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


def test_relay_url_defaults_to_the_wireguard_address():
    route = resolve_kgm_route(_relay_env())

    assert route.name == "relay"
    assert route.relay_url == "http://10.50.0.2:8767"
    assert SECRET not in repr(route)


_MISCONFIGURED = [
    pytest.param({"TURKEY_KGM_ROUTE": "relay"}, "TURKEY_KGM_RELAY_HMAC_SECRET: missing", id="no-secret"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_HMAC_SECRET="s" * 31), "TURKEY_KGM_RELAY_HMAC_SECRET: too_short", id="short"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="ftp://10.50.0.2:8767"), "TURKEY_KGM_RELAY_URL", id="ftp"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://10.50.0.2:8767/x"), "TURKEY_KGM_RELAY_URL", id="path"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://u:pw@10.50.0.2:8767"), "TURKEY_KGM_RELAY_URL", id="userinfo"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://10.50.0.2:8767?a=1"), "TURKEY_KGM_RELAY_URL", id="query"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://10.50.0.2:abc"), "TURKEY_KGM_RELAY_URL", id="bad-port"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://10.50.0.2:70000"), "TURKEY_KGM_RELAY_URL", id="port-range"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="10.50.0.2:8767"), "TURKEY_KGM_RELAY_URL", id="no-scheme"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://"), "TURKEY_KGM_RELAY_URL", id="no-host"),
    pytest.param(_relay_env(TURKEY_KGM_RELAY_URL="http://exa mple:8767"), "TURKEY_KGM_RELAY_URL", id="bad-host"),
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


# ---- python -m reader.turkey_bot.kgm.route (проверка связи без KGM) ----

def test_cli_reports_the_direct_route(monkeypatch, capsys, _restore_httpx_log_level):
    monkeypatch.delenv("TURKEY_KGM_ROUTE", raising=False)

    assert main([]) == 0
    assert "KGM route: direct" in capsys.readouterr().out


def test_cli_reads_the_env_file_and_reports_misconfiguration_without_values(
    tmp_path, monkeypatch, capsys, _restore_httpx_log_level,
):
    monkeypatch.delenv("TURKEY_KGM_ROUTE", raising=False)
    env_file = tmp_path / "turkeybot-kgm-relay.env"
    env_file.write_text("TURKEY_KGM_ROUTE=relay\nTURKEY_KGM_RELAY_HMAC_SECRET=short-secret-value\n", encoding="utf-8")

    assert main(["--env-file", str(env_file)]) == 1
    out = capsys.readouterr().out
    assert "TURKEY_KGM_RELAY_HMAC_SECRET: too_short" in out
    assert "short-secret-value" not in out


async def test_cli_check_reports_relay_health_without_contacting_kgm(capsys):
    fake = FakeKgm()
    async with TestServer(build_kgm_relay_app(hmac_secret=SECRET, session_factory=fake.session_factory)) as server:
        url = f"http://{server.host}:{server.port}"
        code = await _check_relay(_relay_env(TURKEY_KGM_RELAY_URL=url))

    out = capsys.readouterr().out
    assert code == 0
    assert f"KGM route: relay via {url}" in out
    assert 'OK: {"ok": true, "open_sessions": 0, "sessions_today": 0, "version": "1"}' in out
    assert SECRET not in out
    assert fake.requests == []


async def test_cli_check_fails_when_the_relay_is_down(capsys):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    code = await _check_relay(_relay_env(TURKEY_KGM_RELAY_URL=f"http://127.0.0.1:{port}"))

    assert code == 1
    assert "FAIL" in capsys.readouterr().out
