"""
Тесты reader/turkey_bot/live_session_registry.py::LiveGibSessionRegistry —
in-memory реестр живых GIB-проверок (см. design report Stage 3: "do not
pretend that an in-memory client can be reconstructed from a DB
session_token"). client здесь — лёгкий фейк с async aclose(), реальная
сеть не используется вовсе.
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.live_session_registry import (  # noqa: E402
    LiveGibCheck,
    LiveGibSessionRegistry,
)


class _FakeClient:
    def __init__(self):
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _make_check(*, plate: str = "34ABC123", image_id: str = "cid-1", created_at: float | None = None) -> LiveGibCheck:
    kwargs = {}
    if created_at is not None:
        kwargs["created_at"] = created_at
    return LiveGibCheck(client=_FakeClient(), provider=object(), plate=plate, image_id=image_id, **kwargs)


async def test_get_returns_none_when_nothing_registered():
    registry = LiveGibSessionRegistry()

    assert await registry.get(111) is None


async def test_put_then_get_returns_the_same_check():
    registry = LiveGibSessionRegistry()
    check = _make_check()

    await registry.put(111, check)

    assert await registry.get(111) is check


async def test_put_closes_previous_check_for_same_chat():
    registry = LiveGibSessionRegistry()
    old_check = _make_check(image_id="old")
    new_check = _make_check(image_id="new")

    await registry.put(111, old_check)
    await registry.put(111, new_check)

    assert old_check.client.closed is True
    assert await registry.get(111) is new_check


async def test_pop_and_close_removes_and_closes():
    registry = LiveGibSessionRegistry()
    check = _make_check()
    await registry.put(111, check)

    await registry.pop_and_close(111)

    assert await registry.get(111) is None
    assert check.client.closed is True


async def test_pop_and_close_is_safe_when_nothing_registered():
    registry = LiveGibSessionRegistry()

    await registry.pop_and_close(999)  # не должно бросать


async def test_idle_ttl_evicts_and_closes_stale_check():
    registry = LiveGibSessionRegistry(idle_ttl_seconds=0.05)
    check = _make_check()
    await registry.put(111, check)

    await asyncio.sleep(0.1)

    assert await registry.get(111) is None
    assert check.client.closed is True


async def test_close_all_closes_every_active_check():
    registry = LiveGibSessionRegistry()
    check_a = _make_check(plate="34ABC123")
    check_b = _make_check(plate="06XYZ999")
    await registry.put(1, check_a)
    await registry.put(2, check_b)

    await registry.close_all()

    assert check_a.client.closed is True
    assert check_b.client.closed is True
    assert await registry.get(1) is None
    assert await registry.get(2) is None
    assert registry.count_active() == 0


async def test_close_all_is_safe_on_empty_registry():
    registry = LiveGibSessionRegistry()

    await registry.close_all()  # не должно бросать


async def test_lock_for_returns_same_lock_object_for_same_chat_id():
    registry = LiveGibSessionRegistry()

    assert registry.lock_for(111) is registry.lock_for(111)


async def test_lock_for_returns_independent_locks_for_different_chats():
    registry = LiveGibSessionRegistry()

    assert registry.lock_for(1) is not registry.lock_for(2)


async def test_lock_for_actually_serializes_concurrent_access():
    """Регрессия на "per-chat locking" (см. задачу): без lock'а два
    конкурентных обработчика одного chat_id могли бы интерливиться вокруг
    await-точки; с lock'ом — строго по очереди."""
    registry = LiveGibSessionRegistry()
    order: list[str] = []

    async def worker(name: str) -> None:
        async with registry.lock_for(111):
            order.append(f"{name}-start")
            await asyncio.sleep(0.01)
            order.append(f"{name}-end")

    await asyncio.gather(worker("a"), worker("b"))

    # Один воркер должен полностью завершиться до старта другого - никакого
    # чередования "a-start, b-start, a-end, b-end".
    assert order in (
        ["a-start", "a-end", "b-start", "b-end"],
        ["b-start", "b-end", "a-start", "a-end"],
    )


async def test_count_active_reflects_registered_checks():
    registry = LiveGibSessionRegistry()
    assert registry.count_active() == 0

    await registry.put(1, _make_check())
    assert registry.count_active() == 1

    await registry.put(2, _make_check())
    assert registry.count_active() == 2

    await registry.pop_and_close(1)
    assert registry.count_active() == 1
