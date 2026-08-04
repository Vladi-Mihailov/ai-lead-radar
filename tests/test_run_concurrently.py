"""
Тесты reader.main._run_concurrently — обёртки над asyncio, которая при
ошибке в одной фоновой корутине отменяет остальные (см. reader/main.py:
run() — Pipeline.run() и _run_fine_monitor() на одном TelegramClient).

Только простые async-заглушки, без реальных Telegram/HTTP-компонентов.
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.main import _run_concurrently  # noqa: E402


async def _ok(result: str) -> str:
    await asyncio.sleep(0)
    return result


async def _failing(message: str) -> None:
    await asyncio.sleep(0)
    raise RuntimeError(message)


async def _hangs_until_cancelled(cancelled: list) -> None:
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        cancelled.append(True)
        raise


async def test_run_concurrently_succeeds_when_all_coroutines_succeed():
    # Не должно бросить исключение и не должно зависнуть.
    await _run_concurrently([_ok("a"), _ok("b")])


async def test_run_concurrently_cancels_sibling_when_first_coroutine_fails():
    cancelled: list = []

    with pytest.raises(RuntimeError, match="boom-1"):
        await _run_concurrently(
            [_failing("boom-1"), _hangs_until_cancelled(cancelled)]
        )

    assert cancelled == [True]


async def test_run_concurrently_cancels_sibling_when_second_coroutine_fails():
    cancelled: list = []

    with pytest.raises(RuntimeError, match="boom-2"):
        await _run_concurrently(
            [_hangs_until_cancelled(cancelled), _failing("boom-2")]
        )

    assert cancelled == [True]
