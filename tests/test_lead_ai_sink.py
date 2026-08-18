"""Тесты reader/sinks/lead_ai_sink.py::LeadAiSink — TelegramClient и
LeadAiService фейковые, ни один тест не обращается к настоящему Telegram
или OpenAI API.

Покрывает: follow-up отправляется отдельным сообщением, raw JSON не
показывается, AI timeout/ошибка не пробрасываются наружу (fail-open — сам
sink ничего не бросает, см. reader/core/pipeline.py, который и так оборачивает
sink.handle в try/except, но LeadAiSink не должен полагаться только на это),
а также fire-and-forget семантику handle() (см. задачу: AI-анализ не должен
задерживать Pipeline._process) и её жизненный цикл — сохранение фоновых
задач от GC, stop() при shutdown, ограничение concurrency."""

import asyncio
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.core.models import LeadEvent, Message, ScenarioMatch  # noqa: E402
from reader.lead_ai.models import LeadAiAnalysis  # noqa: E402
from reader.lead_ai.service import LeadAiServiceError  # noqa: E402
from reader.sinks.lead_ai_sink import LeadAiSink  # noqa: E402


@dataclass
class _FakeEntity:
    id: int
    source: Any


class _FakeTelegramClient:
    def __init__(self):
        self.get_entity_calls: list = []
        self.send_message_calls: list = []

    async def get_entity(self, target):
        self.get_entity_calls.append(target)
        return _FakeEntity(id=hash(str(target)) % 10_000_000, source=target)

    async def send_message(self, entity, text, *, link_preview=None):
        self.send_message_calls.append((entity.source, text))


class _FakeLeadAiService:
    def __init__(self, *, result=None, error=None, hang=False, delay=0.0):
        self._result = result
        self._error = error
        self._hang = hang
        self._delay = delay
        self.analyze_calls: list = []

    async def analyze(self, message_text: str) -> LeadAiAnalysis:
        self.analyze_calls.append(message_text)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._hang:
            await asyncio.sleep(999)
        if self._error is not None:
            raise self._error
        return self._result


def _event(text="нужна страховка в Грузии") -> LeadEvent:
    message = Message(
        id=1, chat_id=-100999, chat_title="Test group", sender_id=111,
        sender_username="ivan", sender_name=None, text=text,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc), link="https://t.me/testgroup/1",
    )
    return LeadEvent(
        message=message,
        matches=[ScenarioMatch(scenario_name="osago", matched_keywords=["страховка"])],
    )


async def _sink(client, service, recipient="alena_ogi") -> LeadAiSink:
    sink = LeadAiSink(client, recipient, service)
    await sink.start()
    return sink


async def _handle_and_wait(sink: LeadAiSink, event: LeadEvent) -> None:
    """handle() теперь fire-and-forget (см. задачу) — для тестов, которым
    важен РЕЗУЛЬТАТ анализа (не сам факт, что handle() не блокирует),
    нужно явно дождаться фоновой задачи, которую handle() запустил."""
    await sink.handle(event)
    tasks = list(sink._background_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ---- follow-up отдельным сообщением, relevant=true ----


async def test_relevant_result_sends_lead_follow_up_message():
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="money_transfer_ru_ge",
        reason="человек ищет способ перевести деньги из России на грузинскую карту",
        suggested_reply="Можем помочь с переводом из России в Грузию. Подскажите сумму и в какой валюте хотите получить?",
    )
    service = _FakeLeadAiService(result=analysis)
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())

    assert len(client.send_message_calls) == 1
    recipient, text = client.send_message_calls[0]
    assert recipient == "alena_ogi"
    assert "🤖 AI-анализ" in text
    assert "✅ Потенциальный лид" in text
    assert "Тип: money_transfer_ru_ge" in text
    assert "Предлагаемый ответ:" in text
    assert analysis.suggested_reply in text


async def test_irrelevant_result_sends_not_a_lead_follow_up_message():
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(
        relevant=False, lead_type="irrelevant",
        reason="человек спрашивает только про обмен наличных в обменнике",
        suggested_reply="",
    )
    service = _FakeLeadAiService(result=analysis)
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())

    assert len(client.send_message_calls) == 1
    _recipient, text = client.send_message_calls[0]
    assert "❌ Не лид" in text
    assert "Тип: irrelevant" in text
    assert "Предлагаемый ответ" not in text


async def test_raw_json_is_never_shown_to_manager():
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="fine_payment", reason="r", suggested_reply="s",
    )
    service = _FakeLeadAiService(result=analysis)
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())

    _recipient, text = client.send_message_calls[0]
    assert "{" not in text
    assert '"relevant"' not in text


# ---- fail-open: ошибка/timeout AI не ломает доставку (follow-up просто заменяется) ----


async def test_service_error_does_not_raise_and_sends_unavailable_message():
    client = _FakeTelegramClient()
    service = _FakeLeadAiService(error=LeadAiServiceError("boom"))
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())  # не должно бросить исключение

    assert len(client.send_message_calls) == 1
    _recipient, text = client.send_message_calls[0]
    assert "временно недоступен" in text


async def test_timeout_does_not_raise_and_sends_unavailable_message(monkeypatch):
    client = _FakeTelegramClient()
    service = _FakeLeadAiService(hang=True)
    monkeypatch.setattr("reader.sinks.lead_ai_sink._ANALYSIS_TIMEOUT_SECONDS", 0.01)
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())  # не должно бросить исключение / не должно зависнуть

    assert len(client.send_message_calls) == 1
    _recipient, text = client.send_message_calls[0]
    assert "временно недоступен" in text


async def test_send_message_failure_after_successful_analysis_does_not_raise():
    class _FailingSendClient(_FakeTelegramClient):
        async def send_message(self, entity, text, *, link_preview=None):
            raise RuntimeError("telegram недоступен")

    client = _FailingSendClient()
    analysis = LeadAiAnalysis(relevant=False, lead_type="irrelevant", reason="r", suggested_reply="")
    service = _FakeLeadAiService(result=analysis)
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())  # не должно бросить исключение


async def test_unexpected_analyze_exception_does_not_raise_and_is_swallowed():
    """Даже совсем неожиданное исключение из analyze() (не LeadAiServiceError)
    не должно "утечь" как непойманное исключение фоновой задачи (asyncio
    иначе залогировал бы "Task exception was never retrieved")."""
    client = _FakeTelegramClient()
    service = _FakeLeadAiService(error=RuntimeError("совершенно неожиданная ошибка"))
    sink = await _sink(client, service)

    await sink.handle(_event())
    tasks = list(sink._background_tasks)
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(not isinstance(r, Exception) for r in results)
    # Никакого follow-up для неожиданной ошибки не отправляется (в отличие
    # от LeadAiServiceError/timeout, для которых есть "временно недоступен").
    assert client.send_message_calls == []


# ---- LeadAiSink работает только с настроенным получателем ----


async def test_sink_only_targets_its_configured_recipient():
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(relevant=False, lead_type="irrelevant", reason="r", suggested_reply="")
    service = _FakeLeadAiService(result=analysis)
    sink = await _sink(client, service, recipient="alena_ogi")

    await _handle_and_wait(sink, _event())

    assert client.get_entity_calls == ["alena_ogi"]
    assert [c[0] for c in client.send_message_calls] == ["alena_ogi"]


async def test_sink_passes_message_text_to_service():
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(relevant=False, lead_type="irrelevant", reason="r", suggested_reply="")
    service = _FakeLeadAiService(result=analysis)
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event(text="хочу перевести деньги в Грузию"))

    assert service.analyze_calls == ["хочу перевести деньги в Грузию"]


# ---- fire-and-forget: handle() не ждёт AI (см. задачу) ----


async def test_handle_returns_immediately_without_waiting_for_analysis():
    """Ключевое архитектурное требование: handle() не должен ждать даже
    небольшую задержку analyze() — follow-up отправляется уже ПОСЛЕ того,
    как handle() вернул управление, из фоновой задачи."""
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(relevant=False, lead_type="irrelevant", reason="r", suggested_reply="")
    service = _FakeLeadAiService(result=analysis, delay=0.2)
    sink = await _sink(client, service)

    started = time.monotonic()
    await sink.handle(_event())
    elapsed = time.monotonic() - started

    assert elapsed < 0.05
    # follow-up ещё не отправлен — фоновая задача только запущена.
    assert client.send_message_calls == []
    assert len(sink._background_tasks) == 1

    await asyncio.gather(*list(sink._background_tasks))
    assert len(client.send_message_calls) == 1


async def test_handle_does_not_wait_even_for_a_hanging_analysis():
    """То же самое, но analyze() "висит" намного дольше
    _ANALYSIS_TIMEOUT_SECONDS — handle() всё равно должен вернуться
    мгновенно (fire-and-forget не зависит от значения timeout'а вовсе,
    поскольку сам timeout применяется только ВНУТРИ фоновой задачи)."""
    client = _FakeTelegramClient()
    service = _FakeLeadAiService(hang=True)
    sink = await _sink(client, service)

    started = time.monotonic()
    await sink.handle(_event())
    elapsed = time.monotonic() - started

    assert elapsed < 0.05

    for task in list(sink._background_tasks):
        task.cancel()
    await asyncio.gather(*list(sink._background_tasks), return_exceptions=True)


async def test_background_task_is_kept_referenced_until_completion():
    """Задача должна храниться в self._background_tasks, пока не
    завершится (иначе GC мог бы забрать единственную живую ссылку на неё,
    см. предупреждение asyncio.create_task про "fire-and-forget" задачи)."""
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(relevant=False, lead_type="irrelevant", reason="r", suggested_reply="")
    service = _FakeLeadAiService(result=analysis, delay=0.05)
    sink = await _sink(client, service)

    await sink.handle(_event())
    assert len(sink._background_tasks) == 1

    await asyncio.gather(*list(sink._background_tasks))

    # done-callback убирает завершённую задачу из набора.
    assert sink._background_tasks == set()


# ---- stop(): дожидается/отменяет фоновые задачи при shutdown ----


async def test_stop_waits_for_pending_background_task_to_complete():
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(relevant=False, lead_type="irrelevant", reason="r", suggested_reply="")
    service = _FakeLeadAiService(result=analysis, delay=0.1)
    sink = await _sink(client, service)

    await sink.handle(_event())
    assert client.send_message_calls == []  # фоновая задача только запущена

    await sink.stop()

    assert len(client.send_message_calls) == 1  # stop() дождался её завершения
    assert sink._background_tasks == set()


async def test_stop_is_a_noop_when_nothing_is_pending():
    client = _FakeTelegramClient()
    service = _FakeLeadAiService(result=LeadAiAnalysis(
        relevant=False, lead_type="irrelevant", reason="r", suggested_reply="",
    ))
    sink = await _sink(client, service)

    await sink.stop()  # не должно бросить исключение при пустом наборе задач


async def test_stop_cancels_task_that_does_not_finish_within_shutdown_timeout(monkeypatch):
    monkeypatch.setattr("reader.sinks.lead_ai_sink._SHUTDOWN_WAIT_TIMEOUT_SECONDS", 0.05)
    client = _FakeTelegramClient()
    service = _FakeLeadAiService(hang=True)
    sink = await _sink(client, service)

    await sink.handle(_event())
    await sink.stop()  # не должен зависнуть/бросить исключение

    assert sink._background_tasks == set()


# ---- ограничение concurrency (semaphore) ----


async def test_concurrent_analyses_are_limited_by_semaphore(monkeypatch):
    monkeypatch.setattr("reader.sinks.lead_ai_sink._MAX_CONCURRENT_ANALYSES", 2)
    client = _FakeTelegramClient()

    release = asyncio.Event()
    state = {"current": 0, "max_seen": 0}

    class _ConcurrencyTrackingService:
        async def analyze(self, message_text):
            state["current"] += 1
            state["max_seen"] = max(state["max_seen"], state["current"])
            await release.wait()
            state["current"] -= 1
            return LeadAiAnalysis(relevant=False, lead_type="irrelevant", reason="r", suggested_reply="")

    sink = await _sink(client, _ConcurrencyTrackingService())

    for _ in range(5):
        await sink.handle(_event())

    # Даём фоновым задачам шанс дойти до analyze() (до release.wait()).
    for _ in range(10):
        await asyncio.sleep(0)

    assert state["max_seen"] <= 2  # не больше лимита semaphore, несмотря на 5 задач

    release.set()
    await asyncio.gather(*list(sink._background_tasks), return_exceptions=True)
    assert len(client.send_message_calls) == 5
