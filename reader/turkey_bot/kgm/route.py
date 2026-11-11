"""Маршрут KGM для Turkey-бота: прямой запрос к KGM (как раньше) или через
relay на грузинском сервере (см. docs/kgm-relay.md). Читается ОДИН раз при
старте reader/turkey_bot/main.py, после load_dotenv():

  - TURKEY_KGM_ROUTE — direct (по умолчанию) | relay;
  - TURKEY_KGM_RELAY_URL — по умолчанию http://10.50.0.2:8767 (WireGuard);
  - TURKEY_KGM_RELAY_HMAC_SECRET — не короче 32 символов, тот же, что
    KGM_RELAY_HMAC_SECRET на грузинском сервере.

Ошибка конфигурации (неизвестный маршрут, плохой URL, нет или короткий
секрет) — одна ERROR-строка при старте, после чего каждая проверка KGM
получает KgmTransportError на start() (-> ERROR "transport_error"), а GİB/
Avrasya работают как обычно. Автоматического перехода на direct НЕТ — ни при
ошибке конфигурации, ни при недоступном relay: немецкий адрес KGM сейчас не
отвечает, а молчаливая смена маршрута только спрятала бы проблему.

Проверка связи production -> relay без обращения к KGM (тот же разбор
настроек, что у бота):

    python -m reader.turkey_bot.kgm.route --env-file /etc/ai-lead-radar/turkeybot-kgm-relay.env"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from reader.turkey_bot.kgm.models import KgmCaptchaChallenge
from reader.turkey_bot.kgm.provider import KgmProvider
from reader.turkey_bot.kgm.relay_auth import MIN_SECRET_LENGTH, secret_problem
from reader.turkey_bot.kgm.relay_client import RelayKgmSession
from reader.turkey_bot.kgm.session import KgmTransportError

logger = logging.getLogger(__name__)

ROUTE_ENV = "TURKEY_KGM_ROUTE"
RELAY_URL_ENV = "TURKEY_KGM_RELAY_URL"
RELAY_SECRET_ENV = "TURKEY_KGM_RELAY_HMAC_SECRET"
DEFAULT_RELAY_URL = "http://10.50.0.2:8767"

_HOST_RE = re.compile(r"[A-Za-z0-9.:-]+")


class _AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


# Та же форма, что unified/check_service.py::KgmCheckFactory (не
# импортируется оттуда: check_service тянет OCR).
KgmCheckFactory = Callable[[], tuple[_AsyncCloseable, KgmProvider]]


@dataclass(frozen=True)
class KgmRoute:
    """name: "direct" | "relay" | "misconfigured"; problem — только для
    "misconfigured", без значений переменных (в них может оказаться секрет)."""

    name: str
    relay_url: str = ""
    relay_secret: str = field(default="", repr=False)
    problem: str = ""


def _relay_url_problem(url: str) -> str | None:
    """Разбор тем же httpx.URL, которым потом пользуется RelayKgmSession."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return "invalid URL"
    if parsed.scheme not in ("http", "https") or not parsed.host or not _HOST_RE.fullmatch(parsed.host):
        return "expected an http(s) URL with a host"
    if parsed.port is not None and not 0 < parsed.port < 65536:
        return "invalid port"
    if parsed.userinfo or parsed.query or parsed.fragment or parsed.path != "/":
        return "path, query, fragment and credentials are not allowed"
    return None


def resolve_kgm_route(env: Mapping[str, str]) -> KgmRoute:
    route = (env.get(ROUTE_ENV) or "direct").strip().lower()
    if route == "direct":
        return KgmRoute(name="direct")
    if route != "relay":
        return KgmRoute(name="misconfigured", problem=f"{ROUTE_ENV}: unknown value (expected direct or relay)")
    url = (env.get(RELAY_URL_ENV) or DEFAULT_RELAY_URL).strip()
    url_problem = _relay_url_problem(url)
    if url_problem is not None:
        return KgmRoute(name="misconfigured", problem=f"{RELAY_URL_ENV}: {url_problem}")
    secret = env.get(RELAY_SECRET_ENV)
    problem = secret_problem(secret)
    if problem is not None:
        return KgmRoute(
            name="misconfigured",
            problem=f"{RELAY_SECRET_ENV}: {problem} (at least {MIN_SECRET_LENGTH} characters required)",
        )
    return KgmRoute(name="relay", relay_url=url, relay_secret=secret)


def _display_url(url: str) -> str:
    parsed = httpx.URL(url)
    return f"{parsed.scheme}://{parsed.netloc.decode('ascii')}"


class _MisconfiguredKgmSession:
    """Заглушка на случай неверной конфигурации relay: любая операция с KGM
    — KgmTransportError, как недоступный сайт; закрывать нечего."""

    last_submit_seconds: float | None = None

    async def start(self) -> KgmCaptchaChallenge:
        raise KgmTransportError("KGM relay route is misconfigured (see the startup log)")

    async def refresh_captcha(self) -> KgmCaptchaChallenge:
        raise KgmTransportError("KGM relay route is misconfigured (see the startup log)")

    async def submit(self, *, plate: str, captcha_code: str) -> str:
        raise KgmTransportError("KGM relay route is misconfigured (see the startup log)")

    async def aclose(self) -> None:
        return None


def _misconfigured_factory() -> tuple[_MisconfiguredKgmSession, KgmProvider]:
    session = _MisconfiguredKgmSession()
    return session, KgmProvider(session)


def kgm_check_factory_from_env(env: Mapping[str, str], *, direct_factory: KgmCheckFactory) -> KgmCheckFactory:
    """direct_factory — unified/check_service.py::default_kgm_check_factory
    (передаёт main.py): для direct возвращается именно он, без обёрток."""
    route = resolve_kgm_route(env)
    if route.name == "direct":
        logger.info("KGM: маршрут direct (прямой запрос к KGM)")
        return direct_factory
    if route.name == "misconfigured":
        logger.error(
            "KGM: маршрут relay не настроен — %s. Проверки KGM будут завершаться transport_error, "
            "автоматического перехода на direct нет.",
            route.problem,
        )
        return _misconfigured_factory

    logger.info("KGM: маршрут relay через %s", _display_url(route.relay_url))
    relay_url, relay_secret = route.relay_url, route.relay_secret

    def relay_factory() -> tuple[RelayKgmSession, KgmProvider]:
        session = RelayKgmSession(relay_url, relay_secret)
        return session, KgmProvider(session)

    return relay_factory


async def _check_relay(env: Mapping[str, str]) -> int:
    route = resolve_kgm_route(env)
    if route.name == "direct":
        print("KGM route: direct — relay is not used")
        return 0
    if route.name == "misconfigured":
        print(f"KGM route: misconfigured — {route.problem}")
        return 1
    print(f"KGM route: relay via {_display_url(route.relay_url)}")
    session = RelayKgmSession(route.relay_url, route.relay_secret)
    try:
        health = await session.health()
    except KgmTransportError:
        print("FAIL: relay is not reachable or rejected the request (see the warning above)")
        return 1
    finally:
        await session.aclose()
    print("OK: " + json.dumps(health, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m reader.turkey_bot.kgm.route",
        description="Show the KGM route the Turkey bot would use and check the relay /health (KGM is not contacted).",
    )
    parser.add_argument("--env-file", help="KEY=value file with TURKEY_KGM_* (e.g. the systemd drop-in env file)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    env = dict(os.environ)
    if args.env_file:
        from dotenv import dotenv_values

        env.update({key: value for key, value in dotenv_values(args.env_file).items() if value is not None})
    return asyncio.run(_check_relay(env))


if __name__ == "__main__":
    sys.exit(main())
