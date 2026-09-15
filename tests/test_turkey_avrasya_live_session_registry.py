"""
Тесты reader/turkey_bot/avrasya/live_session_registry.py::
LiveAvrasyaSessionRegistry — in-memory реестр живых Avrasya-проверок,
структурная копия test_turkey_live_session_registry.py (GIB), КРОМЕ
lock_for() — единый per-chat lock для ОБОИХ провайдеров обеспечивает
LiveGibSessionRegistry.lock_for() (см. design report Stage 2B: "Use one
common per-chat lock across GİB and Avrasya"), у Avrasya-реестра
собственного lock_for() намеренно нет.
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.avrasya.live_session_registry import (  # noqa: E402
    LiveAvrasyaCheck,
    LiveAvrasyaSessionRegistry,
)


class _FakeClient:
    def __init__(self):
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _make_check(*, plate: str = "A123AA123", created_at: float | None = None) -> LiveAvrasyaCheck:
    kwargs = {}
    if created_at is not None:
        kwargs["created_at"] = created_at
    return LiveAvrasyaCheck(client=_FakeClient(), provider=object(), plate=plate, **kwargs)


async def test_has_no_own_lock_for_method():
    """См. модуль docstring — намеренное архитектурное отличие от
    LiveGibSessionRegistry."""
    registry = LiveAvrasyaSessionRegistry()

    assert not hasattr(registry, "lock_for")


async def test_get_returns_none_when_nothing_registered():
    registry = LiveAvrasyaSessionRegistry()

    assert await registry.get(111) is None


async def test_put_then_get_returns_the_same_check():
    registry = LiveAvrasyaSessionRegistry()
    check = _make_check()

    await registry.put(111, check)

    assert await registry.get(111) is check


async def test_put_closes_previous_check_for_same_chat():
    registry = LiveAvrasyaSessionRegistry()
    old_check = _make_check(plate="A123AA123")
    new_check = _make_check(plate="A777AA777")

    await registry.put(111, old_check)
    await registry.put(111, new_check)

    assert old_check.client.closed is True
    assert await registry.get(111) is new_check


async def test_pop_and_close_removes_and_closes():
    registry = LiveAvrasyaSessionRegistry()
    check = _make_check()
    await registry.put(111, check)

    await registry.pop_and_close(111)

    assert await registry.get(111) is None
    assert check.client.closed is True


async def test_pop_and_close_is_safe_when_nothing_registered():
    registry = LiveAvrasyaSessionRegistry()

    await registry.pop_and_close(999)  # не должно бросать


async def test_idle_ttl_evicts_and_closes_stale_check():
    registry = LiveAvrasyaSessionRegistry(idle_ttl_seconds=0.05)
    check = _make_check()
    await registry.put(111, check)

    await asyncio.sleep(0.1)

    assert await registry.get(111) is None
    assert check.client.closed is True


async def test_close_all_closes_every_active_check():
    registry = LiveAvrasyaSessionRegistry()
    check_a = _make_check(plate="A123AA123")
    check_b = _make_check(plate="A777AA777")
    await registry.put(1, check_a)
    await registry.put(2, check_b)

    await registry.close_all()

    assert check_a.client.closed is True
    assert check_b.client.closed is True
    assert await registry.get(1) is None
    assert await registry.get(2) is None
    assert registry.count_active() == 0


async def test_close_all_is_safe_on_empty_registry():
    registry = LiveAvrasyaSessionRegistry()

    await registry.close_all()  # не должно бросать


async def test_count_active_reflects_registered_checks():
    registry = LiveAvrasyaSessionRegistry()
    assert registry.count_active() == 0

    await registry.put(1, _make_check())
    assert registry.count_active() == 1

    await registry.put(2, _make_check())
    assert registry.count_active() == 2

    await registry.pop_and_close(1)
    assert registry.count_active() == 1
