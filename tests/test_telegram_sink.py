"""
Тесты reader/sinks/telegram_sink.py — доставка одного найденного лида
нескольким независимым Telegram-получателям (см. задачу про расширение
LEAD_FORWARD_TO до трёх получателей: @ali_na_l_i, @alena_ogi,
@vladimihailov). TelegramClient — фейковый (только то, что реально
использует TelegramSink: get_entity/forward_messages/send_message), без
единого реального сетевого вызова.
"""

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.core.models import LeadEvent, Message, ScenarioMatch  # noqa: E402
from reader.sinks.telegram_sink import TelegramSink  # noqa: E402


@dataclass
class _FakeEntity:
    id: int
    source: Any


class _FakeTelegramClient:
    """Ровно то, что использует TelegramSink — get_entity()/
    forward_messages()/send_message(). forward_errors/context_errors/
    text_errors — {исходное значение из forward_to: Exception} — какой
    именно из трёх сетевых вызовов должен упасть для конкретного
    получателя (context — send_message ПОСЛЕ успешного forward,
    reply_to задан; text — fallback send_message ПОСЛЕ неудачного forward,
    reply_to не задан)."""

    def __init__(self, *, forward_errors=None, context_errors=None, text_errors=None):
        self._forward_errors = forward_errors or {}
        self._context_errors = context_errors or {}
        self._text_errors = text_errors or {}
        self.get_entity_calls: list = []
        self.forward_calls: list = []
        self.send_message_calls: list = []
        self._forwarded_counter = 1000

    async def get_entity(self, target):
        self.get_entity_calls.append(target)
        # Регистр и "@"/без "@" — один и тот же аккаунт, как и в реальном
        # Telegram (см. TelegramSink.start() — дедупликация по entity.id).
        key = str(target).lstrip("@").lower()
        return _FakeEntity(id=hash(key) % 10_000_000, source=target)

    async def forward_messages(self, entity, *, messages, from_peer):
        self.forward_calls.append(entity.source)
        if entity.source in self._forward_errors:
            raise self._forward_errors[entity.source]
        self._forwarded_counter += 1
        return SimpleNamespace(id=self._forwarded_counter)

    async def send_message(self, entity, text, *, parse_mode=None, link_preview=None, reply_to=None):
        self.send_message_calls.append((entity.source, text, reply_to))
        errors = self._context_errors if reply_to is not None else self._text_errors
        if entity.source in errors:
            raise errors[entity.source]


def _event(text="нужна страховка") -> LeadEvent:
    message = Message(
        id=1,
        chat_id=-100999,
        chat_title="Test group",
        sender_id=111,
        sender_username="ivan",
        sender_name=None,
        text=text,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        link="https://t.me/testgroup/1",
    )
    return LeadEvent(message=message, matches=[ScenarioMatch(scenario_name="osago", matched_keywords=["страховка"])])


async def _sink(client, forward_to) -> TelegramSink:
    sink = TelegramSink(client, forward_to)
    await sink.start()
    return sink


# ---- 1 лид -> N независимых получателей ----


async def test_handle_delivers_lead_to_all_three_recipients_independently():
    client = _FakeTelegramClient()
    sink = await _sink(client, ["ali_na_l_i", "alena_ogi", "vladimihailov"])

    await sink.handle(_event())

    assert client.forward_calls == ["ali_na_l_i", "alena_ogi", "vladimihailov"]
    # Форвард успешен для всех -> все получают короткий контекст (reply_to задан).
    assert [c[0] for c in client.send_message_calls] == ["ali_na_l_i", "alena_ogi", "vladimihailov"]
    assert all(reply_to is not None for _, _, reply_to in client.send_message_calls)


# ---- failure isolation ----


async def test_middle_recipient_failure_does_not_block_the_other_two(caplog):
    """Требуемый сценарий: A -> success, B -> исключение, C -> success."""
    client = _FakeTelegramClient(
        forward_errors={"alena_ogi": RuntimeError("Telegram недоступен")},
    )
    sink = await _sink(client, ["ali_na_l_i", "alena_ogi", "vladimihailov"])

    # Доставка (форвард+контекст/fallback) теперь реализована в
    # reader/sinks/telegram_lead_delivery.py (см. рефакторинг — вынесено,
    # чтобы reader/sinks/lead_ai_sink.py мог переиспользовать тот же
    # формат/fallback, а не дублировать TelegramSink) — оттуда и логи.
    with caplog.at_level("INFO", logger="reader.sinks.telegram_lead_delivery"):
        await sink.handle(_event())

    # Все три попытки форварда произошли — ошибка одного не остановила цикл.
    assert client.forward_calls == ["ali_na_l_i", "alena_ogi", "vladimihailov"]

    calls_by_source = {c[0]: c for c in client.send_message_calls}
    # ali_na_l_i и vladimihailov — форвард успешен -> контекст (reply_to задан).
    assert calls_by_source["ali_na_l_i"][2] is not None
    assert calls_by_source["vladimihailov"][2] is not None
    # alena_ogi — форвард упал -> fallback текстовая копия (reply_to не задан).
    assert calls_by_source["alena_ogi"][2] is None

    # Ошибка B залогирована с его username.
    assert "alena_ogi" in caplog.text
    assert "Не удалось переслать оригинал сообщения" in caplog.text
    # Успехи A и C понятны из лога.
    assert "✔ Лид доставлен в @ali_na_l_i" in caplog.text
    assert "✔ Лид доставлен в @vladimihailov" in caplog.text


async def test_first_recipient_failure_does_not_block_the_rest():
    client = _FakeTelegramClient(forward_errors={"ali_na_l_i": RuntimeError("boom")})
    sink = await _sink(client, ["ali_na_l_i", "alena_ogi", "vladimihailov"])

    await sink.handle(_event())

    assert client.forward_calls == ["ali_na_l_i", "alena_ogi", "vladimihailov"]
    calls_by_source = {c[0]: c for c in client.send_message_calls}
    assert calls_by_source["ali_na_l_i"][2] is None  # fallback (форвард упал)
    assert calls_by_source["alena_ogi"][2] is not None
    assert calls_by_source["vladimihailov"][2] is not None


async def test_last_recipient_failure_does_not_block_the_rest():
    client = _FakeTelegramClient(forward_errors={"vladimihailov": RuntimeError("boom")})
    sink = await _sink(client, ["ali_na_l_i", "alena_ogi", "vladimihailov"])

    await sink.handle(_event())

    assert client.forward_calls == ["ali_na_l_i", "alena_ogi", "vladimihailov"]
    calls_by_source = {c[0]: c for c in client.send_message_calls}
    assert calls_by_source["ali_na_l_i"][2] is not None
    assert calls_by_source["alena_ogi"][2] is not None
    assert calls_by_source["vladimihailov"][2] is None  # fallback (форвард упал)


async def test_all_three_recipients_succeed():
    client = _FakeTelegramClient()
    sink = await _sink(client, ["ali_na_l_i", "alena_ogi", "vladimihailov"])

    await sink.handle(_event())

    assert len(client.forward_calls) == 3
    assert all(reply_to is not None for _, _, reply_to in client.send_message_calls)


# ---- дедупликация ----


async def test_duplicate_username_in_config_does_not_send_twice(caplog):
    """Дубликат — в любом виде (регистр/"@"/без) — не должен приводить к
    повторной резолюции/отправке (см. TelegramSink.start(), дедупликация
    по entity.id)."""
    client = _FakeTelegramClient()

    with caplog.at_level("INFO", logger="reader.sinks.telegram_sink"):
        sink = await _sink(client, ["ali_na_l_i", "ALI_NA_L_I", "alena_ogi"])

    assert "дубликат" in caplog.text

    await sink.handle(_event())

    # Только 2 УНИКАЛЬНЫХ получателя — ALI_NA_L_I (дубликат ali_na_l_i)
    # исключён, его сообщение не отправлено вовсе.
    assert client.forward_calls == ["ali_na_l_i", "alena_ogi"]
    assert len(client.send_message_calls) == 2


async def test_duplicate_numeric_and_username_form_does_not_send_twice():
    """Дубликат может быть представлен по-разному (не только регистром) —
    дедупликация по entity.id (после резолва), а не по сырой строке
    forward_to, ловит и такие случаи."""
    client = _FakeTelegramClient()
    # get_entity() у фейка нормализует str(target).lstrip("@").lower() —
    # "alena_ogi" дважды в разных формах всё равно резолвится в одну и ту
    # же entity.id.
    sink = await _sink(client, ["alena_ogi", "@alena_ogi", "ALENA_OGI"])

    await sink.handle(_event())

    assert client.forward_calls == ["alena_ogi"]


# ---- пустой список получателей ----


async def test_empty_recipients_list_is_a_safe_noop():
    client = _FakeTelegramClient()
    sink = await _sink(client, [])

    await sink.handle(_event())

    assert client.get_entity_calls == []
    assert client.forward_calls == []
    assert client.send_message_calls == []


# ---- порядок получателей не влияет на результат ----


async def test_recipient_order_does_not_affect_outcome():
    forward_errors = {"alena_ogi": RuntimeError("boom")}

    client_order_a = _FakeTelegramClient(forward_errors=forward_errors)
    sink_a = await _sink(client_order_a, ["ali_na_l_i", "alena_ogi", "vladimihailov"])
    await sink_a.handle(_event())

    client_order_b = _FakeTelegramClient(forward_errors=forward_errors)
    sink_b = await _sink(client_order_b, ["vladimihailov", "alena_ogi", "ali_na_l_i"])
    await sink_b.handle(_event())

    def _delivered_via_forward(client) -> set:
        return {source for source, _, reply_to in client.send_message_calls if reply_to is not None}

    # Независимо от порядка в конфиге — тот же набор успешно доставленных
    # (форвардом) получателей, alena_ogi неизменно проваливается сам по себе.
    assert _delivered_via_forward(client_order_a) == {"ali_na_l_i", "vladimihailov"}
    assert _delivered_via_forward(client_order_b) == {"ali_na_l_i", "vladimihailov"}
