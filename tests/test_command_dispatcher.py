"""
Тесты CommandDispatcher — общей инфраструктуры служебных команд оператора
Реальный Telethon/сеть не используются:
TelegramClient и событие подменяются лёгкими фейками, как и в
test_telegram_source_user_cache.py.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.commands.base import Command, CommandContext, CommandError, CommandResult  # noqa: E402
from reader.commands.dispatcher import CommandDispatcher  # noqa: E402

_OPERATOR_CHAT_ID = -100999
_ALLOWED_USER_ID = 111
_OTHER_USER_ID = 222


class _FakeEvent:
    """Минимальная имитация telethon.events.NewMessage.Event."""

    def __init__(self, *, sender_id, text, chat_id=_OPERATOR_CHAT_ID):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.raw_text = text
        self.sent_replies: list[str] = []

    async def respond(self, text):
        self.sent_replies.append(text)


class _FakeClient:
    """Имитация TelegramClient — только то, что использует CommandDispatcher."""

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


class _EchoCommand(Command):
    name = "echo"

    async def handle(self, ctx: CommandContext) -> CommandResult:
        return CommandResult(text=f"echo:{' '.join(ctx.args)}")


class _StrictCommand(Command):
    name = "strict"

    async def handle(self, ctx: CommandContext) -> CommandResult:
        raise CommandError("❌ Неверный формат команды")


class _BrokenCommand(Command):
    name = "boom"

    async def handle(self, ctx: CommandContext) -> CommandResult:
        raise RuntimeError("что-то пошло не так")


def _dispatcher(**overrides) -> CommandDispatcher:
    client = overrides.pop("client", _FakeClient())
    chat_id = overrides.pop("chat_id", _OPERATOR_CHAT_ID)
    allowed_user_ids = overrides.pop("allowed_user_ids", [_ALLOWED_USER_ID])
    return CommandDispatcher(client, chat_id, allowed_user_ids)


async def test_known_command_from_allowed_user_gets_reply():
    dispatcher = _dispatcher()
    dispatcher.register(_EchoCommand())

    event = _FakeEvent(sender_id=_ALLOWED_USER_ID, text="echo hello world")
    await dispatcher.handle_event(event)

    assert event.sent_replies == ["echo:hello world"]


async def test_command_name_matching_is_case_insensitive():
    dispatcher = _dispatcher()
    dispatcher.register(_EchoCommand())

    event = _FakeEvent(sender_id=_ALLOWED_USER_ID, text="ECHO one two")
    await dispatcher.handle_event(event)

    assert event.sent_replies == ["echo:one two"]


async def test_disallowed_user_is_silently_ignored():
    dispatcher = _dispatcher()
    dispatcher.register(_EchoCommand())

    event = _FakeEvent(sender_id=_OTHER_USER_ID, text="echo hello")
    await dispatcher.handle_event(event)

    assert event.sent_replies == []


async def test_unknown_command_is_silently_ignored():
    dispatcher = _dispatcher()
    dispatcher.register(_EchoCommand())

    event = _FakeEvent(sender_id=_ALLOWED_USER_ID, text="привет, как дела?")
    await dispatcher.handle_event(event)

    assert event.sent_replies == []


async def test_empty_text_is_ignored():
    dispatcher = _dispatcher()
    dispatcher.register(_EchoCommand())

    event = _FakeEvent(sender_id=_ALLOWED_USER_ID, text="")
    await dispatcher.handle_event(event)

    assert event.sent_replies == []


async def test_command_error_message_is_sent_as_reply():
    dispatcher = _dispatcher()
    dispatcher.register(_StrictCommand())

    event = _FakeEvent(sender_id=_ALLOWED_USER_ID, text="strict bad args")
    await dispatcher.handle_event(event)

    assert event.sent_replies == ["❌ Неверный формат команды"]


async def test_unexpected_exception_does_not_propagate_and_sends_generic_reply():
    dispatcher = _dispatcher()
    dispatcher.register(_BrokenCommand())

    event = _FakeEvent(sender_id=_ALLOWED_USER_ID, text="boom")
    # Не должно бросить исключение наружу — иначе упадёт вся обработка
    # апдейтов Telethon, включая существующий поиск лидов.
    await dispatcher.handle_event(event)

    assert event.sent_replies == ["⚠ Внутренняя ошибка при обработке команды"]


def test_register_duplicate_name_raises():
    dispatcher = _dispatcher()
    dispatcher.register(_EchoCommand())

    with pytest.raises(ValueError):
        dispatcher.register(_EchoCommand())


async def test_start_resolves_chat_and_registers_handler():
    entity = object()
    client = _FakeClient(entity=entity)
    dispatcher = _dispatcher(client=client, chat_id="@operator_chat")

    await dispatcher.start()

    assert len(client.registered_handlers) == 1
    callback, event_filter = client.registered_handlers[0]
    assert callback == dispatcher.handle_event
    # chats= хранится как есть до резолва клиентом (см. telethon EventBuilder) —
    # проверяем именно то, что передали, не резолвя реальный Telegram-объект.
    assert event_filter.chats == [entity]


async def test_start_raises_runtime_error_when_chat_not_found():
    client = _FakeClient(get_entity_error=ValueError("not found"))
    dispatcher = _dispatcher(client=client, chat_id="@missing_chat")

    with pytest.raises(RuntimeError):
        await dispatcher.start()
