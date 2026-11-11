"""Тесты reader/turkey_bot/kgm/relay_main.py — точка входа relay на
грузинском сервере: настройки из окружения (секрет обязателен, только
конкретный IP, никаких "все интерфейсы"), запуск/штатная остановка на
настоящем TCP-порту 127.0.0.1, лёгкое дерево импортов и согласованность
deploy/kgm-relay.service и requirements-kgm-relay.txt с кодом."""

import asyncio
import logging
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from reader.turkey_bot.kgm import relay_main
from reader.turkey_bot.kgm.relay_client import RelayKgmSession
from reader.turkey_bot.kgm.relay_main import RelayConfigError, load_config, serve

from _kgm_relay_fakes import SECRET

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def _restore_log_levels():
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    levels = [logger.level for logger in loggers]
    yield
    for logger, level in zip(loggers, levels, strict=True):
        logger.setLevel(level)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_defaults_are_the_wireguard_address_and_port_8767():
    config = load_config({"KGM_RELAY_HMAC_SECRET": SECRET})

    assert (config.host, config.port) == ("10.50.0.2", 8767)
    assert config.hmac_secret == SECRET
    assert SECRET not in repr(config)


@pytest.mark.parametrize("env", [{}, {"KGM_RELAY_HMAC_SECRET": ""}, {"KGM_RELAY_HMAC_SECRET": "short-secret-31-characters-xxxx"}])
def test_secret_is_required_and_at_least_32_characters(env):
    with pytest.raises(RelayConfigError) as excinfo:
        load_config(env)

    assert "KGM_RELAY_HMAC_SECRET" in str(excinfo.value)
    assert "short-secret-31-characters-xxxx" not in str(excinfo.value)


@pytest.mark.parametrize("host", ["", "   ", "0.0.0.0", "::", "[::]", "0:0:0:0:0:0:0:0", "localhost", "10.50.0.256"])
def test_wildcard_empty_or_non_ip_host_is_refused(host):
    with pytest.raises(RelayConfigError):
        load_config({"KGM_RELAY_HMAC_SECRET": SECRET, "KGM_RELAY_HOST": host})


@pytest.mark.parametrize("host, expected", [("10.50.0.2", "10.50.0.2"), (" 127.0.0.1 ", "127.0.0.1"), ("[fd00::2]", "fd00::2")])
def test_specific_ip_host_is_accepted(host, expected):
    assert load_config({"KGM_RELAY_HMAC_SECRET": SECRET, "KGM_RELAY_HOST": host}).host == expected


@pytest.mark.parametrize("port", ["", "abc", "0", "65536", "-1", "8767.0"])
def test_invalid_port_is_refused(port):
    with pytest.raises(RelayConfigError):
        load_config({"KGM_RELAY_HMAC_SECRET": SECRET, "KGM_RELAY_PORT": port})


def test_main_refuses_to_start_without_a_secret(monkeypatch, caplog, _restore_log_levels):
    monkeypatch.delenv("KGM_RELAY_HMAC_SECRET", raising=False)

    assert relay_main.main() == 2
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == ["KGM relay не запущен: KGM_RELAY_HMAC_SECRET: missing (нужно не меньше 32 символов)"]


def test_main_refuses_a_wildcard_host_without_logging_the_secret(monkeypatch, caplog, _restore_log_levels):
    monkeypatch.setenv("KGM_RELAY_HMAC_SECRET", SECRET)
    monkeypatch.setenv("KGM_RELAY_HOST", "0.0.0.0")

    assert relay_main.main() == 2
    assert "KGM_RELAY_HOST" in caplog.text
    assert SECRET not in caplog.text


def test_main_reports_a_bind_failure_with_exit_code_1(monkeypatch, caplog, _restore_log_levels):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        monkeypatch.setenv("KGM_RELAY_HMAC_SECRET", SECRET)
        monkeypatch.setenv("KGM_RELAY_HOST", "127.0.0.1")
        monkeypatch.setenv("KGM_RELAY_PORT", str(port))

        assert relay_main.main() == 1

    assert f"KGM relay: не удалось слушать 127.0.0.1:{port}" in caplog.text
    assert SECRET not in caplog.text


async def test_serve_listens_on_the_given_ip_and_stops_cleanly():
    port = _free_port()
    config = load_config({"KGM_RELAY_HMAC_SECRET": SECRET, "KGM_RELAY_HOST": "127.0.0.1", "KGM_RELAY_PORT": str(port)})
    stop = asyncio.Event()
    server = asyncio.create_task(serve(config, stop))
    try:
        for _ in range(200):
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.01)
                continue
            writer.close()
            await writer.wait_closed()
            break
        session = RelayKgmSession(f"http://127.0.0.1:{port}", SECRET)
        try:
            health = await session.health()
        finally:
            await session.aclose()
        assert health == {"ok": True, "open_sessions": 0, "sessions_today": 0, "version": "1"}
    finally:
        stop.set()
        await asyncio.wait_for(server, timeout=10)

    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


def test_relay_main_imports_no_ocr_check_service_telegram_or_dotenv():
    heavy = {"cv2", "numpy", "rapidocr", "rapidocr_onnxruntime", "onnxruntime", "pytesseract", "telethon", "openai", "dotenv"}
    code = (
        "import sys, reader.turkey_bot.kgm.relay_main\n"
        f"heavy = {sorted(heavy)!r}\n"
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('reader') or m.split('.')[0] in heavy)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    )

    assert set(result.stdout.split()) == {
        "reader",
        "reader.turkey_bot",
        "reader.turkey_bot.kgm",
        "reader.turkey_bot.kgm.models",
        "reader.turkey_bot.kgm.parser",
        "reader.turkey_bot.kgm.relay_auth",
        "reader.turkey_bot.kgm.relay_main",
        "reader.turkey_bot.kgm.relay_server",
        "reader.turkey_bot.kgm.session",
    }


def test_systemd_unit_runs_the_relay_module_with_limits_and_hardening():
    lines = (PROJECT_ROOT / "deploy" / "kgm-relay.service").read_text(encoding="utf-8").splitlines()

    for expected in [
        "User=kgmrelay",
        "WorkingDirectory=/opt/kgm-relay",
        "EnvironmentFile=/etc/kgm-relay/kgm-relay.env",
        "ExecStart=/opt/kgm-relay/.venv/bin/python -m reader.turkey_bot.kgm.relay_main",
        "After=network-online.target wg-quick@wg0.service",
        "Wants=network-online.target wg-quick@wg0.service",
        "Restart=on-failure",
        "RestartSec=10",
        "MemoryMax=192M",
        "CPUQuota=50%",
        "TasksMax=64",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
    ]:
        assert expected in lines, expected


def test_minimal_requirements_cover_the_relay_and_nothing_heavy():
    requirements = [
        line.split(">=")[0].strip().lower()
        for line in (PROJECT_ROOT / "requirements-kgm-relay.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert sorted(requirements) == ["aiohttp", "beautifulsoup4", "httpx"]
