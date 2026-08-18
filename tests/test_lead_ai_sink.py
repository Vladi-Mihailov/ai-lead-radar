"""Тесты reader/sinks/lead_ai_sink.py::LeadAiSink — TelegramClient и
LeadAiService фейковые, ни один тест не обращается к настоящему Telegram
или OpenAI API.

Покрывает ключевое архитектурное требование (см. задачу): recipient
(@alena_ogi) НЕ должен увидеть кандидата до того, как AI примет решение —
handle() классифицирует лид ПЕРВЫМ, и только при relevant=True доставляет
оригинал (форвард + контекст, переиспользуя TelegramLeadDelivery — тот же
формат/fallback, что и у TelegramSink) и следом AI follow-up. Для
relevant=False, timeout, ошибки OpenAI или неожиданного исключения —
recipient не получает ВООБЩЕ НИЧЕГО (fail-closed) — ни оригинал, ни
follow-up, только warning/error в лог. Также покрывает fire-and-forget
семантику handle() и её жизненный цикл — сохранение фоновых задач от GC,
stop() при shutdown, ограничение concurrency."""

import asyncio
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
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
    """forward_messages/send_message — то же, что реально использует
    TelegramLeadDelivery (см. tests/test_telegram_sink.py — тот же фейк по
    духу): forward_error позволяет проверить fallback на текстовую копию."""

    def __init__(self, *, forward_error=None):
        self.get_entity_calls: list = []
        self.forward_calls: list = []
        self.send_message_calls: list = []
        self._forward_error = forward_error
        self._forwarded_counter = 1000

    async def get_entity(self, target):
        self.get_entity_calls.append(target)
        return _FakeEntity(id=hash(str(target)) % 10_000_000, source=target)

    async def forward_messages(self, entity, *, messages, from_peer):
        self.forward_calls.append(entity.source)
        if self._forward_error is not None:
            raise self._forward_error
        self._forwarded_counter += 1
        return SimpleNamespace(id=self._forwarded_counter)

    async def send_message(self, entity, text, *, parse_mode=None, link_preview=None, reply_to=None):
        self.send_message_calls.append((entity.source, text, reply_to))


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


def _follow_up_texts(client) -> list[str]:
    return [text for _r, text, _reply_to in client.send_message_calls if "🤖 AI-анализ" in text]


# ---- relevant=True: оригинал (форвард+контекст) + ОДИН AI follow-up ----


async def test_relevant_result_delivers_original_then_sends_one_follow_up():
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="money_transfer_ru_ge",
        reason="человек ищет способ перевести деньги из России на грузинскую карту",
        suggested_reply="Можем помочь с переводом из России в Грузию. Подскажите сумму и в какой валюте хотите получить?",
    )
    service = _FakeLeadAiService(result=analysis)
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())

    # Оригинал доставлен ровно один раз (форвард + контекст, reply_to задан).
    assert client.forward_calls == ["alena_ogi"]
    context_messages = [
        (r, text, reply_to) for r, text, reply_to in client.send_message_calls
        if reply_to is not None
    ]
    assert len(context_messages) == 1
    assert context_messages[0][0] == "alena_ogi"

    # Плюс ровно один AI follow-up, отдельным сообщением (не reply, не
    # дублирует контекст).
    follow_ups = _follow_up_texts(client)
    assert len(follow_ups) == 1
    text = follow_ups[0]
    assert "✅ Потенциальный лид" in text
    assert "Тип: money_transfer_ru_ge" in text
    assert "Предлагаемый ответ:" in text
    assert analysis.suggested_reply in text

    # Итого — ровно 2 сообщения этому получателю (контекст + follow-up),
    # оригинал НЕ продублирован.
    assert len(client.send_message_calls) == 2


async def test_relevant_result_falls_back_to_text_copy_when_forward_fails():
    """Тот же fallback, что и у TelegramSink (см. TelegramLeadDelivery) —
    переиспользуется, а не теряется при рефакторинге."""
    client = _FakeTelegramClient(forward_error=RuntimeError("boom"))
    analysis = LeadAiAnalysis(relevant=True, lead_type="fine_payment", reason="r", suggested_reply="s")
    service = _FakeLeadAiService(result=analysis)
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())

    assert client.forward_calls == ["alena_ogi"]
    non_follow_up = [
        (r, text, reply_to) for r, text, reply_to in client.send_message_calls
        if "🤖 AI-анализ" not in text
    ]
    assert len(non_follow_up) == 1
    assert non_follow_up[0][2] is None  # fallback — не reply, форвард не удался
    assert len(_follow_up_texts(client)) == 1


async def test_raw_json_is_never_shown_to_manager():
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="fine_payment", reason="r", suggested_reply="s",
    )
    service = _FakeLeadAiService(result=analysis)
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())

    follow_up = _follow_up_texts(client)[0]
    assert "{" not in follow_up
    assert '"relevant"' not in follow_up


# ---- fail-closed: relevant=False/timeout/ошибка/неожиданное исключение
# -> получателю НИЧЕГО не уходит (ни оригинал, ни follow-up) ----


async def test_relevant_false_sends_nothing_at_all():
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(
        relevant=False, lead_type="irrelevant",
        reason="человек спрашивает только про обмен наличных в обменнике",
        suggested_reply="",
    )
    service = _FakeLeadAiService(result=analysis)
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())

    # Ни оригинал (форвард), ни контекст, ни follow-up — recipient не
    # должен увидеть кандидата вовсе (см. задачу).
    assert client.forward_calls == []
    assert client.send_message_calls == []


async def test_lead_type_irrelevant_sends_nothing_even_with_other_fields_set():
    """lead_type="irrelevant" — отдельная проверка (см. задачу), не только
    как побочный эффект relevant=False: даже если бы reason/suggested_reply
    были непустыми, ничего не должно уйти менеджеру."""
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(
        relevant=False, lead_type="irrelevant",
        reason="есть текст причины", suggested_reply="есть предложенный ответ",
    )
    service = _FakeLeadAiService(result=analysis)
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())

    assert client.forward_calls == []
    assert client.send_message_calls == []


async def test_service_error_sends_nothing_at_all():
    client = _FakeTelegramClient()
    service = _FakeLeadAiService(error=LeadAiServiceError("boom"))
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())  # не должно бросить исключение

    assert client.forward_calls == []
    assert client.send_message_calls == []


async def test_timeout_sends_nothing_at_all(monkeypatch):
    client = _FakeTelegramClient()
    service = _FakeLeadAiService(hang=True)
    monkeypatch.setattr("reader.sinks.lead_ai_sink._ANALYSIS_TIMEOUT_SECONDS", 0.01)
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())  # не должно бросить исключение / не должно зависнуть

    assert client.forward_calls == []
    assert client.send_message_calls == []


async def test_unexpected_analyze_exception_sends_nothing_and_is_swallowed():
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
    assert client.forward_calls == []
    assert client.send_message_calls == []


async def test_send_message_failure_after_successful_analysis_does_not_raise():
    class _FailingSendClient(_FakeTelegramClient):
        async def send_message(self, entity, text, *, parse_mode=None, link_preview=None, reply_to=None):
            raise RuntimeError("telegram недоступен")

    client = _FailingSendClient()
    analysis = LeadAiAnalysis(relevant=True, lead_type="fine_payment", reason="r", suggested_reply="s")
    service = _FakeLeadAiService(result=analysis)
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())  # не должно бросить исключение


# ---- ключевое требование: recipient не видит кандидата ДО классификации ----


async def test_recipient_receives_nothing_until_ai_classification_completes():
    """До завершения фоновой AI-задачи (даже если relevant в итоге True)
    ни форвард, ни какое-либо сообщение НЕ должны были уйти получателю —
    решение принимается ДО первой отправки (см. задачу)."""
    client = _FakeTelegramClient()
    release = asyncio.Event()

    class _PausedService:
        async def analyze(self, message_text):
            await release.wait()
            return LeadAiAnalysis(relevant=True, lead_type="fine_payment", reason="r", suggested_reply="s")

    sink = await _sink(client, _PausedService())

    await sink.handle(_event())
    await asyncio.sleep(0)  # даём фоновой задаче стартовать и дойти до release.wait()

    # Классификация ещё не завершена — получатель не увидел вообще ничего.
    assert client.forward_calls == []
    assert client.send_message_calls == []

    release.set()
    await asyncio.gather(*list(sink._background_tasks), return_exceptions=True)

    # После relevant=True — оригинал и follow-up доставлены.
    assert client.forward_calls == ["alena_ogi"]
    assert len(_follow_up_texts(client)) == 1


# ---- один LeadEvent не приводит к двойной отправке оригинала ----


async def test_single_relevant_event_delivers_original_exactly_once():
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(relevant=True, lead_type="fine_payment", reason="r", suggested_reply="s")
    service = _FakeLeadAiService(result=analysis)
    sink = await _sink(client, service)

    await _handle_and_wait(sink, _event())

    assert client.forward_calls == ["alena_ogi"]  # ровно один форвард, не два
    assert len(_follow_up_texts(client)) == 1  # ровно один follow-up, не два


# ---- LeadAiSink работает только с настроенным получателем ----


async def test_sink_only_targets_its_configured_recipient():
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(relevant=True, lead_type="fine_payment", reason="r", suggested_reply="s")
    service = _FakeLeadAiService(result=analysis)
    sink = await _sink(client, service, recipient="alena_ogi")

    await _handle_and_wait(sink, _event())

    assert client.get_entity_calls == ["alena_ogi"]
    assert client.forward_calls == ["alena_ogi"]
    assert all(r == "alena_ogi" for r, _t, _rt in client.send_message_calls)


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
    небольшую задержку analyze() — доставка (оригинал + follow-up)
    происходит уже ПОСЛЕ того, как handle() вернул управление, из фоновой
    задачи."""
    client = _FakeTelegramClient()
    analysis = LeadAiAnalysis(relevant=True, lead_type="fine_payment", reason="r", suggested_reply="s")
    service = _FakeLeadAiService(result=analysis, delay=0.2)
    sink = await _sink(client, service)

    started = time.monotonic()
    await sink.handle(_event())
    elapsed = time.monotonic() - started

    assert elapsed < 0.05
    # Ничего ещё не отправлено — фоновая задача только запущена.
    assert client.forward_calls == []
    assert client.send_message_calls == []
    assert len(sink._background_tasks) == 1

    await asyncio.gather(*list(sink._background_tasks))
    assert client.forward_calls == ["alena_ogi"]
    assert len(_follow_up_texts(client)) == 1


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
    analysis = LeadAiAnalysis(relevant=True, lead_type="fine_payment", reason="r", suggested_reply="s")
    service = _FakeLeadAiService(result=analysis, delay=0.1)
    sink = await _sink(client, service)

    await sink.handle(_event())
    assert client.forward_calls == []  # фоновая задача только запущена
    assert client.send_message_calls == []

    await sink.stop()

    # stop() дождался её завершения — оригинал + follow-up доставлены.
    assert client.forward_calls == ["alena_ogi"]
    assert len(_follow_up_texts(client)) == 1
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
    assert client.send_message_calls == []


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
            return LeadAiAnalysis(relevant=True, lead_type="fine_payment", reason="r", suggested_reply="s")

    sink = await _sink(client, _ConcurrencyTrackingService())

    for _ in range(5):
        await sink.handle(_event())

    # Даём фоновым задачам шанс дойти до analyze() (до release.wait()).
    for _ in range(10):
        await asyncio.sleep(0)

    assert state["max_seen"] <= 2  # не больше лимита semaphore, несмотря на 5 задач

    release.set()
    await asyncio.gather(*list(sink._background_tasks), return_exceptions=True)
    assert len(client.forward_calls) == 5
    assert len(_follow_up_texts(client)) == 5
