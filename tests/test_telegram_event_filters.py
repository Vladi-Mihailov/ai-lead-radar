"""Регресс на РЕАЛЬНОЕ поведение Telethon-фильтров (events.NewMessage /
EventBuilder.filter), а не на прямой вызов CommandDispatcher.handle_event()/
AlbumCollector.on_new_message() — см. задачу про production-расследование
"изображения от стороннего участника OCR-чата не доходят до OCR".

Прямой вызов handle_event()/on_new_message() (см. test_command_dispatcher.py/
test_album_collector.py) проверяет только НАШ код внутри метода — он
структурно не может поймать регресс вида "кто-то добавил incoming=True/
from_users=... в конструктор events.NewMessage(...)", потому что такая
проверка происходит ВНУТРИ Telethon, ДО того как наш handle_event/
on_new_message вообще будет вызван. Эти тесты берут РЕАЛЬНО
зарегистрированный events.NewMessage(...) (тот же объект, что и в
CommandDispatcher.start()/AlbumCollector.start()) и вызывают его .filter()
напрямую — тот же код, который использует сам Telethon при диспетчеризации
апдейтов."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.commands.album_collector import AlbumCollector  # noqa: E402
from reader.commands.dispatcher import CommandDispatcher  # noqa: E402

# "Резолвленный" peer id чата — то, во что реальный Telethon превратил бы
# entity/chat_id к моменту первого события (см. EventBuilder._resolve).
# Значение не имеет значения (в тестах это не проходит через настоящий
# _resolve — см. _mark_resolved) — важно только совпадение/несовпадение.
_CHAT_PEER_ID = -100999
_OTHER_CHAT_PEER_ID = -100111


class _FakeClient:
    """Ровно то, что использует CommandDispatcher.start()/
    AlbumCollector.start() — get_entity() + add_event_handler()."""

    def __init__(self, entity=None):
        self._entity = entity if entity is not None else object()
        self.registered_handlers: list = []

    async def get_entity(self, chat_id):
        return self._entity

    def add_event_handler(self, callback, event_filter):
        self.registered_handlers.append((callback, event_filter))


class _FilterMessage:
    """Ровно те атрибуты, которые читает telethon.events.newmessage.NewMessage
    .filter() (event.message.out/sender_id/fwd_from/message) — см. реальный
    исходник Telethon, установленный в этом venv."""

    def __init__(self, *, out=False, sender_id=None, fwd_from=None, text=""):
        self.out = out
        self.sender_id = sender_id
        self.fwd_from = fwd_from
        self.message = text


class _FilterEvent:
    """Минимальный дубль telethon.events.newmessage.NewMessage.Event — ровно
    то, что читает telethon.events.common.EventBuilder.filter()
    (event.chat_id) и NewMessage.filter() (event.message.*)."""

    def __init__(self, *, chat_id, message: _FilterMessage):
        self.chat_id = chat_id
        self.message = message


def _mark_resolved(event_filter, peer_id) -> None:
    """Имитирует то, что реально делает EventBuilder.resolve(client) —
    превращает chats=[entity] в set резолвленных peer id — но синхронно и
    без обращения к сети (наш entity — не настоящий Peer, реальный _resolve
    потребовал бы живой TelegramClient). После этого event_filter.filter(...)
    — ТОТ ЖЕ САМЫЙ код, что использует Telethon при доставке апдейтов."""
    event_filter.chats = {peer_id}
    event_filter.resolved = True


# ---- CommandDispatcher: реально зарегистрированный events.NewMessage(...) ----


async def test_command_dispatcher_registered_filter_has_no_direction_or_sender_restriction():
    """Проверяем сам объект-фильтр, который CommandDispatcher.start() отдаёт
    Telethon — chats=[entity] и БОЛЬШЕ НИЧЕГО (ни incoming=, ни outgoing=,
    ни from_users=). Если бы кто-то по ошибке добавил такой параметр,
    следующие assert'ы стали бы падать без единого изменения в
    handle_event()."""
    client = _FakeClient()
    dispatcher = CommandDispatcher(client, "@service_chat", [111])
    await dispatcher.start()

    _callback, event_filter = client.registered_handlers[0]
    assert event_filter.incoming is None
    assert event_filter.outgoing is None
    assert event_filter.from_users is None


async def test_command_dispatcher_registered_filter_accepts_incoming_from_any_sender():
    client = _FakeClient()
    dispatcher = CommandDispatcher(client, "@service_chat", [111])
    await dispatcher.start()

    _callback, event_filter = client.registered_handlers[0]
    _mark_resolved(event_filter, _CHAT_PEER_ID)

    for sender_id in (111, 999999, None):
        event = _FilterEvent(chat_id=_CHAT_PEER_ID, message=_FilterMessage(out=False, sender_id=sender_id))
        assert event_filter.filter(event) is True


async def test_command_dispatcher_registered_filter_accepts_outgoing_messages_too():
    """Telethon по умолчанию (incoming=outgoing=None) пропускает ОБА
    направления — событие не отсекается на уровне фильтра, даже если
    message.out=True (см. задачу: "не подписана ли сессия только на
    incoming=True" — нет, ни здесь, ни в AlbumCollector)."""
    client = _FakeClient()
    dispatcher = CommandDispatcher(client, "@service_chat", [111])
    await dispatcher.start()

    _callback, event_filter = client.registered_handlers[0]
    _mark_resolved(event_filter, _CHAT_PEER_ID)

    event = _FilterEvent(chat_id=_CHAT_PEER_ID, message=_FilterMessage(out=True, sender_id=111))
    assert event_filter.filter(event) is True


async def test_command_dispatcher_registered_filter_rejects_other_chat():
    client = _FakeClient()
    dispatcher = CommandDispatcher(client, "@service_chat", [111])
    await dispatcher.start()

    _callback, event_filter = client.registered_handlers[0]
    _mark_resolved(event_filter, _CHAT_PEER_ID)

    event = _FilterEvent(chat_id=_OTHER_CHAT_PEER_ID, message=_FilterMessage(out=False, sender_id=111))
    assert not event_filter.filter(event)


# ---- AlbumCollector: реально зарегистрированный events.NewMessage(...) ----


async def test_album_collector_registered_filter_has_no_direction_or_sender_restriction():
    client = _FakeClient()
    collector = AlbumCollector(on_group_ready=_noop_on_group_ready)
    await collector.start(client, "@service_chat")

    _callback, event_filter = client.registered_handlers[0]
    assert event_filter.incoming is None
    assert event_filter.outgoing is None
    assert event_filter.from_users is None


async def test_album_collector_registered_filter_accepts_any_sender():
    client = _FakeClient()
    collector = AlbumCollector(on_group_ready=_noop_on_group_ready)
    await collector.start(client, "@service_chat")

    _callback, event_filter = client.registered_handlers[0]
    _mark_resolved(event_filter, _CHAT_PEER_ID)

    for sender_id in (111, 999999, None):
        event = _FilterEvent(chat_id=_CHAT_PEER_ID, message=_FilterMessage(out=False, sender_id=sender_id))
        assert event_filter.filter(event) is True


async def test_album_collector_registered_filter_rejects_other_chat():
    client = _FakeClient()
    collector = AlbumCollector(on_group_ready=_noop_on_group_ready)
    await collector.start(client, "@service_chat")

    _callback, event_filter = client.registered_handlers[0]
    _mark_resolved(event_filter, _CHAT_PEER_ID)

    event = _FilterEvent(chat_id=_OTHER_CHAT_PEER_ID, message=_FilterMessage(out=False, sender_id=111))
    assert not event_filter.filter(event)


async def _noop_on_group_ready(events_batch):
    return None
