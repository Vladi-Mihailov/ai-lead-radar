"""
Тесты reader/commands/album_collector.py::AlbumCollector — debounce-сборка
Telegram-альбома по grouped_id, без реального Telegram/сети (только
маленькие реальные asyncio.sleep, по тому же принципу, что и
tests/test_inviter_worker.py — debounce_seconds здесь тоже маленький,
чтобы тесты были быстрыми и не полагались на monkeypatch asyncio.sleep,
который сломал бы саму проверку "новое сообщение продлевает окно").
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.commands.album_collector import AlbumCollector  # noqa: E402

_DEBOUNCE = 0.05


class _FakeEvent:
    def __init__(self, *, grouped_id=None, message_id=1):
        self.grouped_id = grouped_id
        self.id = message_id


class _FakeClient:
    def __init__(self, entity=None, get_entity_error=None):
        self._entity = entity if entity is not None else object()
        self._get_entity_error = get_entity_error
        self.registered_handlers = []

    async def get_entity(self, chat_id):
        if self._get_entity_error is not None:
            raise self._get_entity_error
        return self._entity

    def add_event_handler(self, callback, event_filter):
        self.registered_handlers.append((callback, event_filter))


def _collector(*, debounce_seconds=_DEBOUNCE):
    calls: list[list] = []

    async def on_group_ready(events_batch):
        calls.append(events_batch)

    collector = AlbumCollector(on_group_ready=on_group_ready, debounce_seconds=debounce_seconds)
    return collector, calls


async def test_single_event_without_grouped_id_is_ignored():
    collector, calls = _collector()

    await collector.on_new_message(_FakeEvent(grouped_id=None))
    await asyncio.sleep(_DEBOUNCE * 3)

    assert calls == []


async def test_two_events_same_group_are_collected_together():
    collector, calls = _collector()
    e1 = _FakeEvent(grouped_id=111, message_id=1)
    e2 = _FakeEvent(grouped_id=111, message_id=2)

    await collector.on_new_message(e1)
    await collector.on_new_message(e2)
    await asyncio.sleep(_DEBOUNCE * 3)

    assert len(calls) == 1
    assert calls[0] == [e1, e2]


async def test_group_fires_only_once_after_debounce_settles():
    collector, calls = _collector()
    await collector.on_new_message(_FakeEvent(grouped_id=222, message_id=1))
    await asyncio.sleep(_DEBOUNCE * 3)

    assert len(calls) == 1


async def test_late_arriving_part_extends_the_debounce_window():
    """Часть альбома пришла позже (но до истечения debounce) — должна
    попасть в ТУ ЖЕ группу, а не запустить обработку без неё (см. задачу:
    "album приходит частями")."""
    collector, calls = _collector(debounce_seconds=0.15)
    e1 = _FakeEvent(grouped_id=333, message_id=1)
    e2 = _FakeEvent(grouped_id=333, message_id=2)

    await collector.on_new_message(e1)
    await asyncio.sleep(0.05)  # меньше debounce_seconds — таймер ещё не сработал
    assert calls == []  # пока ничего не собрано

    await collector.on_new_message(e2)
    await asyncio.sleep(0.3)  # ждём полного расчётного debounce после e2

    assert len(calls) == 1
    assert calls[0] == [e1, e2]


async def test_different_groups_are_handled_independently():
    collector, calls = _collector()
    a1 = _FakeEvent(grouped_id=1, message_id=1)
    b1 = _FakeEvent(grouped_id=2, message_id=2)

    await collector.on_new_message(a1)
    await collector.on_new_message(b1)
    await asyncio.sleep(_DEBOUNCE * 3)

    assert len(calls) == 2
    groups = {id(batch[0]) for batch in calls}
    assert groups == {id(a1), id(b1)}


async def test_exception_in_callback_does_not_propagate(caplog):
    async def failing_on_group_ready(events_batch):
        raise RuntimeError("симулированный сбой обработки альбома")

    collector = AlbumCollector(on_group_ready=failing_on_group_ready, debounce_seconds=_DEBOUNCE)

    with caplog.at_level("ERROR", logger="reader.commands.album_collector"):
        await collector.on_new_message(_FakeEvent(grouped_id=444))
        await asyncio.sleep(_DEBOUNCE * 3)

    assert "Не удалось обработать альбом" in caplog.text


async def test_start_resolves_entity_and_registers_handler():
    entity = object()
    client = _FakeClient(entity=entity)
    collector, _calls = _collector()

    await collector.start(client, "@ocr_service_chat")

    assert len(client.registered_handlers) == 1
    callback, _event_filter = client.registered_handlers[0]
    assert callback == collector.on_new_message


async def test_start_raises_runtime_error_when_chat_not_found():
    client = _FakeClient(get_entity_error=ValueError("not found"))
    collector, _calls = _collector()

    try:
        await collector.start(client, "@missing_chat")
        assert False, "ожидалось RuntimeError"
    except RuntimeError as exc:
        assert "@missing_chat" in str(exc)
