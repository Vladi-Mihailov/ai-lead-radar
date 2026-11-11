"""Точка входа KGM relay на грузинском сервере (REDNET), см. docs/kgm-relay.md
и deploy/kgm-relay.service:

    python -m reader.turkey_bot.kgm.relay_main

Настройки — только из окружения (systemd EnvironmentFile=/etc/kgm-relay/
kgm-relay.env), .env не читается:
  - KGM_RELAY_HMAC_SECRET — обязателен, не короче 32 символов; тот же, что
    TURKEY_KGM_RELAY_HMAC_SECRET на production. НЕ GEORGIA_COLLECTOR_HMAC_SECRET:
    у REDNET-коллектора свой секрет;
  - KGM_RELAY_ALLOWED_NETWORK — сеть WireGuard, по умолчанию 10.50.0.0/24:
    только частная сеть; relay слушает адрес внутри неё и принимает запросы
    только с её адресов (relay_server.build_kgm_relay_app(allowed_networks=));
  - KGM_RELAY_HOST — по умолчанию 10.50.0.2 (адрес wg0). Только IP-адрес
    внутри KGM_RELAY_ALLOWED_NETWORK: пустое значение, имена хостов, 0.0.0.0,
    :: и любые другие "все интерфейсы" отклоняются;
  - KGM_RELAY_PORT — по умолчанию 8767 (8766 занят check-now сервером
    georgia-collector).

Ошибка конфигурации — одна ERROR-строка и код выхода 2 (unit:
RestartPreventExitStatus=2 — systemd не перезапускает заведомо неверную
конфигурацию); сбой bind (например, wg0 ещё не поднят) — код 1, systemd
перезапустит через RestartSec.

Импортирует только relay_server/relay_auth/relay_version и через них
kgm/session.py, parser.py, models.py + aiohttp/httpx/bs4 — никакого
check_service, OCR и Telegram (на сервере стоит только
requirements-kgm-relay.txt)."""

import asyncio
import ipaddress
import logging
import os
import signal
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field

from aiohttp import web

from reader.turkey_bot.kgm import relay_auth
from reader.turkey_bot.kgm.relay_server import IpNetwork, build_kgm_relay_app
from reader.turkey_bot.kgm.relay_version import RELAY_PROTOCOL_VERSION, kgm_code_fingerprint

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_NETWORK = "10.50.0.0/24"
DEFAULT_HOST = "10.50.0.2"
DEFAULT_PORT = 8767
CONFIG_ERROR_EXIT_CODE = 2


class RelayConfigError(Exception):
    """Текст исключения уходит в журнал — секрет в него не попадает."""


@dataclass(frozen=True)
class RelayConfig:
    hmac_secret: str = field(repr=False)
    allowed_network: IpNetwork
    host: str
    port: int


def load_config(env: Mapping[str, str]) -> RelayConfig:
    problem = relay_auth.secret_problem(env.get("KGM_RELAY_HMAC_SECRET"))
    if problem is not None:
        raise RelayConfigError(
            f"KGM_RELAY_HMAC_SECRET: {problem} (нужно не меньше {relay_auth.MIN_SECRET_LENGTH} символов)"
        )

    try:
        network = ipaddress.ip_network(env.get("KGM_RELAY_ALLOWED_NETWORK", DEFAULT_ALLOWED_NETWORK).strip())
    except ValueError:
        raise RelayConfigError("KGM_RELAY_ALLOWED_NETWORK: нужна сеть вида 10.50.0.0/24") from None
    if not network.is_private:
        # 0.0.0.0/0 и любая публичная сеть отключили бы правило "только WireGuard".
        raise RelayConfigError("KGM_RELAY_ALLOWED_NETWORK: допускается только частная сеть (WireGuard)")

    host = env.get("KGM_RELAY_HOST", DEFAULT_HOST).strip()
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        raise RelayConfigError("KGM_RELAY_HOST: нужен конкретный IP-адрес (по умолчанию 10.50.0.2)") from None
    if address.is_unspecified:
        raise RelayConfigError("KGM_RELAY_HOST: адрес 'все интерфейсы' запрещён — только адрес WireGuard")
    if address not in network:
        raise RelayConfigError("KGM_RELAY_HOST: адрес вне KGM_RELAY_ALLOWED_NETWORK")

    port_text = env.get("KGM_RELAY_PORT", str(DEFAULT_PORT)).strip()
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise RelayConfigError("KGM_RELAY_PORT: нужен номер порта 1..65535")

    return RelayConfig(
        hmac_secret=env["KGM_RELAY_HMAC_SECRET"], allowed_network=network, host=str(address), port=int(port_text),
    )


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # httpx пишет INFO-строку на каждый запрос к KGM; relay и так пишет свою
    # строку на каждый запрос, а сбои транспорта логирует KgmSession.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def serve(config: RelayConfig, stop: asyncio.Event) -> None:
    """До stop.set(); затем штатная остановка: runner.cleanup() перестаёт
    принимать соединения, дожидается текущих запросов и через on_cleanup
    закрывает все сессии (relay_server._KgmRelay.on_cleanup)."""
    app = build_kgm_relay_app(hmac_secret=config.hmac_secret, allowed_networks=(config.allowed_network,))
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, config.host, config.port)
        await site.start()
        logger.info(
            "KGM relay слушает %s:%d, клиенты из %s (version %s, kgm_code %s)",
            config.host, config.port, config.allowed_network, RELAY_PROTOCOL_VERSION, kgm_code_fingerprint(),
        )
        await stop.wait()
        logger.info("KGM relay: остановка")
    finally:
        await runner.cleanup()


async def _run(config: RelayConfig) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            # Windows (только локальный запуск) — остановка через Ctrl+C.
            pass
    await serve(config, stop)


def main() -> int:
    _setup_logging()
    try:
        config = load_config(os.environ)
    except RelayConfigError as exc:
        logger.error("KGM relay не запущен: %s", exc)
        return CONFIG_ERROR_EXIT_CODE
    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        # Обычно wg0 ещё не поднят ("Cannot assign requested address"):
        # код 1 -> systemd перезапустит через RestartSec.
        logger.error(
            "KGM relay: не удалось слушать %s:%d (%s errno=%s)",
            config.host, config.port, type(exc).__name__, exc.errno,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
