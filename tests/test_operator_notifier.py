"""
Тесты OperatorNotifier (reader/notifications/operator_notifier.py).
TelegramClient подменяется лёгким фейком (без сети) — как и в
test_telegram_notification_service.py/test_command_dispatcher.py.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.notifications.operator_notifier import OperatorNotifier  # noqa: E402


class _FakeClient:
    def __init__(
        self, entities: dict | None = None, *,
        connect_error: Exception | None = None,
        get_entity_error: Exception | None = None,
        authorized: bool = True,
    ):
        self._entities = entities or {}
        self._connect_error = connect_error
        self._get_entity_error = get_entity_error
        # По умолчанию — авторизована, как и было ДО добавления самой
        # проверки (см. задачу): большинству существующих тестов важен
        # только резолв/доставка, не сам факт авторизации. authorized=False
        # — специально для test_start_*_not_authorized*.
        self._authorized = authorized
        self.connected = False
        self.disconnected = False
        self.sent_messages: list[tuple] = []
        self.send_message_error_for: set = set()
        self.get_entity_calls: list = []

    async def connect(self):
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    async def is_user_authorized(self):
        return self._authorized

    async def get_entity(self, chat_id):
        self.get_entity_calls.append(chat_id)
        if self._get_entity_error is not None:
            raise self._get_entity_error
        return self._entities.get(chat_id, chat_id)

    async def send_message(self, entity, text, **kwargs):
        if entity in self.send_message_error_for:
            raise RuntimeError("не удалось отправить")
        self.sent_messages.append((entity, text, kwargs))

    async def disconnect(self):
        self.disconnected = True


async def _started_notifier(client=None, chat_ids=None) -> tuple[OperatorNotifier, "_FakeClient"]:
    client = client or _FakeClient()
    notifier = OperatorNotifier(client, chat_ids or ["@operator_chat"])
    await notifier.start()
    return notifier, client


async def test_start_connects_and_resolves_configured_chats():
    entity = object()
    client = _FakeClient(entities={"@operator_chat": entity})

    notifier, client = await _started_notifier(client)

    assert client.connected is True
    await notifier.notify_text("hello")
    assert client.sent_messages[0][0] is entity


async def test_notify_text_sends_to_all_resolved_chats():
    client = _FakeClient()
    notifier, client = await _started_notifier(client, chat_ids=["@chat_a", "@chat_b"])

    delivered = await notifier.notify_text("статус приглашений")

    assert delivered is True
    assert len(client.sent_messages) == 2
    entities = {msg[0] for msg in client.sent_messages}
    assert entities == {"@chat_a", "@chat_b"}


async def test_notify_text_returns_true_if_at_least_one_chat_succeeds():
    client = _FakeClient()
    client.send_message_error_for.add("@chat_a")
    notifier, client = await _started_notifier(client, chat_ids=["@chat_a", "@chat_b"])

    delivered = await notifier.notify_text("статус приглашений")

    assert delivered is True
    assert len(client.sent_messages) == 1
    assert client.sent_messages[0][0] == "@chat_b"


async def test_notify_text_returns_false_and_does_not_raise_when_all_chats_fail():
    client = _FakeClient()
    client.send_message_error_for.update({"@chat_a", "@chat_b"})
    notifier, client = await _started_notifier(client, chat_ids=["@chat_a", "@chat_b"])

    delivered = await notifier.notify_text("статус приглашений")

    assert delivered is False
    assert client.sent_messages == []


async def test_notify_text_returns_false_when_no_chat_resolved():
    client = _FakeClient(get_entity_error=ValueError("not found"))
    notifier, client = await _started_notifier(client, chat_ids=["@missing_chat"])

    delivered = await notifier.notify_text("статус приглашений")

    assert delivered is False
    assert client.sent_messages == []


async def test_start_does_not_raise_when_connect_fails():
    client = _FakeClient(connect_error=ConnectionError("сессия недействительна"))
    notifier = OperatorNotifier(client, ["@operator_chat"])

    await notifier.start()  # не должно бросить исключение

    delivered = await notifier.notify_text("статус приглашений")
    assert delivered is False
    assert client.sent_messages == []


async def test_start_skips_unresolvable_chat_but_resolves_others():
    entity_b = object()

    class _PartiallyFailingClient(_FakeClient):
        async def get_entity(self, chat_id):
            if chat_id == "@bad_chat":
                raise ValueError("not found")
            return self._entities.get(chat_id, chat_id)

    client = _PartiallyFailingClient(entities={"@good_chat": entity_b})
    notifier, client = await _started_notifier(client, chat_ids=["@bad_chat", "@good_chat"])

    await notifier.notify_text("статус приглашений")

    assert len(client.sent_messages) == 1
    assert client.sent_messages[0][0] is entity_b


async def test_close_disconnects_when_connected():
    client = _FakeClient()
    notifier, client = await _started_notifier(client)

    await notifier.close()

    assert client.disconnected is True


async def test_close_does_not_disconnect_when_connect_failed():
    client = _FakeClient(connect_error=ConnectionError("сессия недействительна"))
    notifier = OperatorNotifier(client, ["@operator_chat"])
    await notifier.start()

    await notifier.close()

    # connect() провалился — disconnect() на никогда не подключавшемся
    # клиенте не вызывается.
    assert client.disconnected is False


# ---- is_user_authorized() — session_path_notifier может подключиться, ----
# ---- но не быть авторизован (см. задачу: "Получатель ... не найден" ------
# ---- на самом деле означал неавторизованную сессию, не отсутствие peer) --


async def test_start_does_not_resolve_chats_when_session_not_authorized(caplog):
    """connect() успешен, но is_user_authorized() -> False — get_entity()
    не должен вызываться вовсе (иначе его ошибка авторизации выглядела бы
    как "получатель не найден", маскируя настоящую причину)."""
    client = _FakeClient(authorized=False)
    notifier = OperatorNotifier(
        client, ["@alenaogir"], session_path="data/sessions/reader_notifier",
    )

    with caplog.at_level("WARNING", logger="reader.notifications.operator_notifier"):
        await notifier.start()

    assert client.get_entity_calls == []
    assert (
        "Сессия уведомлений не авторизована: data/sessions/reader_notifier. "
        "Сначала выполните python -m reader.notifications.authorize_notifier."
    ) in caplog.text

    delivered = await notifier.notify_text("статус приглашений")
    assert delivered is False
    assert client.sent_messages == []


async def test_start_resolves_chats_when_session_is_authorized():
    """Симметричная проверка: is_user_authorized() -> True (по умолчанию,
    как и раньше) — резолв получателей происходит как обычно, поведение
    не изменилось для уже авторизованной сессии."""
    entity = object()
    client = _FakeClient(entities={"@operator_chat": entity}, authorized=True)

    notifier, client = await _started_notifier(client)

    assert client.get_entity_calls == ["@operator_chat"]
    await notifier.notify_text("hello")
    assert client.sent_messages[0][0] is entity


async def test_start_resolves_correctly_once_session_becomes_authorized():
    """До авторизации (is_user_authorized() -> False) — ни одной попытки
    резолва; после того, как сессия авторизована (как после однократного
    python -m reader.notifications.authorize_notifier, см. задачу) — тот
    же получатель резолвится и получает уведомление корректно."""
    entity = object()

    unauthorized_client = _FakeClient(entities={"@alenaogir": entity}, authorized=False)
    unauthorized_notifier = OperatorNotifier(unauthorized_client, ["@alenaogir"])
    await unauthorized_notifier.start()

    assert unauthorized_client.get_entity_calls == []
    assert await unauthorized_notifier.notify_text("статус приглашений") is False

    authorized_client = _FakeClient(entities={"@alenaogir": entity}, authorized=True)
    authorized_notifier = OperatorNotifier(authorized_client, ["@alenaogir"])
    await authorized_notifier.start()

    assert authorized_client.get_entity_calls == ["@alenaogir"]
    delivered = await authorized_notifier.notify_text("статус приглашений")
    assert delivered is True
    assert authorized_client.sent_messages[0][0] is entity
