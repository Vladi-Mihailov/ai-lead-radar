"""
Тесты TelegramNotificationService. TelegramClient подменяется лёгким
фейком (без сети) — как и в test_command_dispatcher.py.
"""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.models import NewFineEvent  # noqa: E402
from reader.notifications.base import NotificationResult  # noqa: E402
from reader.notifications.telegram_notification_service import (  # noqa: E402
    TelegramNotificationService,
)

_SOURCE_URL = "https://police.ge/protocol/index.php?lang=en"


class _FakeClient:
    def __init__(self, entities: dict | None = None, get_entity_error: Exception | None = None):
        self._entities = entities or {}
        self._get_entity_error = get_entity_error
        self.sent_messages: list[tuple] = []
        self.send_message_error_for: set = set()

    async def get_entity(self, chat_id):
        if self._get_entity_error is not None:
            raise self._get_entity_error
        return self._entities.get(chat_id, chat_id)

    async def send_message(self, entity, text, **kwargs):
        if entity in self.send_message_error_for:
            raise RuntimeError("не удалось отправить")
        self.sent_messages.append((entity, text, kwargs))


def _event(
    car_number="B957MA09",
    label=None,
    external_fine_id="AB123456",
    penalty_date=date(2026, 8, 6),
    due_date=date(2026, 8, 20),
    delivered_status="Не вручено",
    task_id=1,
    detected_fine_id=1,
    car_owner_display=None,
) -> NewFineEvent:
    return NewFineEvent(
        detected_fine_id=detected_fine_id,
        task_id=task_id,
        car_number=car_number,
        label=label,
        external_fine_id=external_fine_id,
        penalty_date=penalty_date,
        due_date=due_date,
        delivered_status=delivered_status,
        car_owner_display=car_owner_display,
    )


async def _started_service(client=None, chat_ids=None) -> TelegramNotificationService:
    client = client or _FakeClient()
    service = TelegramNotificationService(client, chat_ids or ["@operator_chat"], _SOURCE_URL)
    await service.start()
    return service


async def test_notify_single_event_produces_expected_message_format():
    client = _FakeClient()
    service = await _started_service(client)

    await service.notify([_event()])

    assert len(client.sent_messages) == 1
    _entity, text, kwargs = client.sent_messages[0]
    assert text == (
        "🚨 Обнаружен новый опубликованный штраф\n"
        "\n"
        "Автомобиль: B957MA09\n"
        "Дата штрафа: 06.08.2026\n"
        "Срок оплаты: 20.08.2026\n"
        "Статус вручения: Не вручено\n"
        "\n"
        f"🔗 [Открыть источник]({_SOURCE_URL})"
    )
    assert kwargs["parse_mode"] == "md"


async def test_notify_operator_message_unaffected_by_client_bot_cta_block():
    """Регрессия: коммерческий CTA-блок ("💬 Помощь с оплатой"/
    "🛡 Оформить страховку", см. reader/public_bot/delivery_texts.py::
    CTA_TEXT_BLOCK) существует ТОЛЬКО в owner-уведомлении @GEShtrafbot —
    существующее уведомление в операторский чат
    (TelegramNotificationService/FineNotificationCoordinator) им не
    затрагивается и продолжает формироваться ровно как раньше."""
    from reader.public_bot.delivery_texts import CTA_TEXT_BLOCK

    client = _FakeClient()
    service = await _started_service(client)

    await service.notify([_event()])

    assert len(client.sent_messages) == 1
    _, text, kwargs = client.sent_messages[0]
    assert CTA_TEXT_BLOCK not in text
    assert "💳" not in text
    # Тот же самый эталонный текст, что и в
    # test_notify_single_event_produces_expected_message_format — ничего
    # не добавилось и не изменилось.
    assert text == (
        "🚨 Обнаружен новый опубликованный штраф\n"
        "\n"
        "Автомобиль: B957MA09\n"
        "Дата штрафа: 06.08.2026\n"
        "Срок оплаты: 20.08.2026\n"
        "Статус вручения: Не вручено\n"
        "\n"
        f"🔗 [Открыть источник]({_SOURCE_URL})"
    )
    assert kwargs["parse_mode"] == "md"


async def test_notify_omits_missing_optional_fields():
    client = _FakeClient()
    service = await _started_service(client)

    await service.notify([_event(due_date=None)])

    _, text, _ = client.sent_messages[0]
    assert "Срок оплаты" not in text
    assert "Дата штрафа: 06.08.2026" in text


async def test_notify_includes_telegram_line_right_after_car_number():
    client = _FakeClient()
    service = await _started_service(client)

    await service.notify([_event(car_owner_display="@ivan_petrov")])

    _, text, _ = client.sent_messages[0]
    assert text == (
        "🚨 Обнаружен новый опубликованный штраф\n"
        "\n"
        "Автомобиль: B957MA09\n"
        "Telegram: @ivan_petrov\n"
        "Дата штрафа: 06.08.2026\n"
        "Срок оплаты: 20.08.2026\n"
        "Статус вручения: Не вручено\n"
        "\n"
        f"🔗 [Открыть источник]({_SOURCE_URL})"
    )


async def test_notify_omits_telegram_line_when_car_owner_display_is_none():
    client = _FakeClient()
    service = await _started_service(client)

    await service.notify([_event(car_owner_display=None)])

    _, text, _ = client.sent_messages[0]
    assert "Telegram:" not in text


async def test_multiple_events_for_same_car_are_grouped_into_one_message():
    client = _FakeClient()
    service = await _started_service(client)

    events = [
        _event(
            detected_fine_id=1, external_fine_id="A1",
            penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
        ),
        _event(
            detected_fine_id=2, external_fine_id="A2",
            penalty_date=date(2026, 7, 1), due_date=date(2026, 7, 20),
        ),
    ]

    result = await service.notify(events)

    assert len(client.sent_messages) == 1
    _, text, _ = client.sent_messages[0]
    assert text.startswith("🚨 Обнаружены новые опубликованные штрафы (2)")
    assert text.count("Автомобиль: B957MA09") == 1
    assert "06.08.2026" in text
    assert "01.07.2026" in text

    # Успешная доставка группового сообщения — оба входящих штрафа доставлены.
    assert sorted(result.delivered_event_ids) == [1, 2]
    assert result.failed_event_ids == []


async def test_multiple_events_for_same_car_show_telegram_line_exactly_once():
    client = _FakeClient()
    service = await _started_service(client)

    events = [
        _event(
            detected_fine_id=1, external_fine_id="A1",
            penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
            car_owner_display="@ivan_petrov",
        ),
        _event(
            detected_fine_id=2, external_fine_id="A2",
            penalty_date=date(2026, 7, 1), due_date=date(2026, 7, 20),
            car_owner_display="@ivan_petrov",
        ),
    ]

    await service.notify(events)

    _, text, _ = client.sent_messages[0]
    assert text.count("Telegram: @ivan_petrov") == 1
    # Строка идёт сразу после "Автомобиль:", один раз на весь автомобиль, а
    # не возле каждого штрафа.
    assert "Автомобиль: B957MA09\nTelegram: @ivan_petrov" in text


async def test_multiple_cars_produce_separate_messages():
    client = _FakeClient()
    service = await _started_service(client)

    events = [
        _event(car_number="AA001AA"),
        _event(car_number="BB002BB"),
    ]

    await service.notify(events)

    assert len(client.sent_messages) == 2
    texts = [msg[1] for msg in client.sent_messages]
    assert any("AA001AA" in t for t in texts)
    assert any("BB002BB" in t for t in texts)


async def test_notify_with_empty_events_sends_nothing():
    client = _FakeClient()
    service = await _started_service(client)

    result = await service.notify([])

    assert client.sent_messages == []
    assert result == NotificationResult(delivered_event_ids=[], failed_event_ids=[])


async def test_notify_broadcasts_to_all_configured_chats():
    client = _FakeClient()
    service = await _started_service(client, chat_ids=["@chat_a", "@chat_b"])

    await service.notify([_event()])

    assert len(client.sent_messages) == 2
    entities = {msg[0] for msg in client.sent_messages}
    assert entities == {"@chat_a", "@chat_b"}


async def test_delivery_to_at_least_one_of_several_chats_counts_as_delivered():
    client = _FakeClient()
    client.send_message_error_for.add("@chat_a")
    service = await _started_service(client, chat_ids=["@chat_a", "@chat_b"])

    result = await service.notify([_event(detected_fine_id=42)])

    # @chat_a упал, но @chat_b получил сообщение — событие всё равно доставлено.
    assert len(client.sent_messages) == 1
    assert client.sent_messages[0][0] == "@chat_b"
    assert result.delivered_event_ids == [42]
    assert result.failed_event_ids == []


async def test_failure_in_all_chats_counts_as_failed():
    client = _FakeClient()
    client.send_message_error_for.update({"@chat_a", "@chat_b"})
    service = await _started_service(client, chat_ids=["@chat_a", "@chat_b"])

    result = await service.notify([_event(detected_fine_id=42)])

    assert client.sent_messages == []
    assert result.delivered_event_ids == []
    assert result.failed_event_ids == [42]


async def test_start_resolves_configured_chats():
    entity_a = object()
    client = _FakeClient(entities={"@chat_a": entity_a})

    service = TelegramNotificationService(client, ["@chat_a"], _SOURCE_URL)
    await service.start()

    await service.notify([_event()])

    assert client.sent_messages[0][0] is entity_a


async def test_start_raises_runtime_error_when_chat_not_found():
    client = _FakeClient(get_entity_error=ValueError("not found"))
    service = TelegramNotificationService(client, ["@missing_chat"], _SOURCE_URL)

    with pytest.raises(RuntimeError):
        await service.start()
