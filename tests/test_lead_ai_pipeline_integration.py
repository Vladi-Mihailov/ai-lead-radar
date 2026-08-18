"""Интеграционный тест: TelegramSink (доставка @ali_na_l_i/@alenaogir БЕЗ
какого-либо AI) + LeadAiSink (AI-отфильтрованная доставка ТОЛЬКО для
@alena_ogi) в одном Pipeline — как они реально собираются в
reader/main.py::run() (см. resolve_telegram_sink_recipients — recipient
lead_ai исключается из forward_to, который получает обычный TelegramSink).

Проверяет ключевые архитектурные требования задачи:
- @ali_na_l_i/@alenaogir получают исходного кандидата НЕЗАВИСИМО от
  результата/ошибки/скорости OpenAI — TelegramSink их вообще не знает про
  lead_ai;
- @alena_ogi НЕ получает кандидата, пока AI не примет решение, и получает
  оригинал+follow-up ТОЛЬКО при relevant=True (fail-closed иначе);
- медленный OpenAI не задерживает ни доставку другим получателям, ни
  Pipeline._process.

TelegramClient — единственный фейковый (по аналогии с
test_telegram_sink.py), OpenAI не используется вовсе (LeadAiService
подменяется фейком)."""

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

from reader.core.engine import MatchEngine  # noqa: E402
from reader.core.models import Message  # noqa: E402
from reader.core.pipeline import Pipeline  # noqa: E402
from reader.lead_ai.models import LeadAiAnalysis  # noqa: E402
from reader.lead_ai.service import LeadAiServiceError  # noqa: E402
from reader.main import resolve_telegram_sink_recipients  # noqa: E402
from reader.scenarios import KeywordMatcher, Scenario  # noqa: E402
from reader.sinks.lead_ai_sink import LeadAiSink  # noqa: E402
from reader.sinks.telegram_sink import TelegramSink  # noqa: E402
from reader.sources.base import BaseSource  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_SCENARIOS = [Scenario(name="transfer", enabled=True, keywords=("перевести",))]
_ALL_CONFIGURED_RECIPIENTS = ["ali_na_l_i", "alena_ogi", "alenaogir"]
_LEAD_AI_RECIPIENT = "alena_ogi"
_OTHER_RECIPIENTS = ["ali_na_l_i", "alenaogir"]


@dataclass
class _FakeEntity:
    id: int
    source: Any


class _FakeTelegramClient:
    def __init__(self, *, forward_errors=None):
        self.forward_calls: list = []
        self.send_message_calls: list = []
        self._forward_errors = forward_errors or {}
        self._forwarded_counter = 1000

    async def get_entity(self, target):
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


class _FakeLeadAiService:
    def __init__(self, result: LeadAiAnalysis = None, *, error=None, hang: bool = False):
        self._result = result
        self._error = error
        self._hang = hang
        self.analyze_calls: list = []

    async def analyze(self, message_text: str) -> LeadAiAnalysis:
        self.analyze_calls.append(message_text)
        if self._hang:
            await asyncio.sleep(999)  # умышленно "виснет" — имитация медленного OpenAI
        if self._error is not None:
            raise self._error
        return self._result


class _FakeSource(BaseSource):
    def __init__(self, messages):
        self._messages_list = messages

    async def start(self):
        return

    async def messages(self):
        for message in self._messages_list:
            yield message

    async def stop(self):
        return


def _message() -> Message:
    return Message(
        id=1, chat_id=-100999, chat_title="Test group", sender_id=111,
        sender_username="ivan", sender_name=None, text="хочу перевести деньги в Грузию",
        date=datetime(2026, 1, 1, tzinfo=timezone.utc), link="https://t.me/testgroup/1",
    )


def _engine() -> MatchEngine:
    return MatchEngine(KeywordMatcher(_SCENARIOS))


def _build_sinks(client, lead_ai_service):
    """Отражает reader/main.py::run(): TelegramSink получает
    app.lead_forward_to БЕЗ recipient'а lead_ai (см.
    resolve_telegram_sink_recipients), LeadAiSink обслуживает этого
    recipient'а отдельно."""
    telegram_forward_to = resolve_telegram_sink_recipients(
        _ALL_CONFIGURED_RECIPIENTS, _LEAD_AI_RECIPIENT,
    )
    telegram_sink = TelegramSink(client, telegram_forward_to)
    lead_ai_sink = LeadAiSink(client, _LEAD_AI_RECIPIENT, lead_ai_service)
    return telegram_sink, lead_ai_sink


def _follow_up_texts(client) -> list[str]:
    return [text for _r, text, _rt in client.send_message_calls if "🤖 AI-анализ" in text]


def _messages_to(client, recipient) -> list[tuple[str, object]]:
    return [(text, reply_to) for r, text, reply_to in client.send_message_calls if r == recipient]


# ---- @ali_na_l_i/@alenaogir получают исходного кандидата НЕЗАВИСИМО от AI ----


async def test_other_recipients_receive_original_when_ai_says_relevant(tmp_path):
    client = _FakeTelegramClient()
    lead_ai_service = _FakeLeadAiService(
        LeadAiAnalysis(
            relevant=True, lead_type="money_transfer_ru_ge",
            reason="хочет перевести деньги из России в Грузию",
            suggested_reply="Подскажите сумму и в какой валюте хотите получить?",
        )
    )
    telegram_sink, lead_ai_sink = _build_sinks(client, lead_ai_service)

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        source = _FakeSource([_message()])
        pipeline = Pipeline(source, _engine(), [telegram_sink, lead_ai_sink], user_repository)
        await pipeline.run()

        assert set(client.forward_calls) >= set(_OTHER_RECIPIENTS)
        for recipient in _OTHER_RECIPIENTS:
            assert len(_messages_to(client, recipient)) == 1
    finally:
        user_repository.close()


async def test_other_recipients_receive_original_when_ai_says_irrelevant(tmp_path):
    client = _FakeTelegramClient()
    lead_ai_service = _FakeLeadAiService(
        LeadAiAnalysis(relevant=False, lead_type="irrelevant", reason="r", suggested_reply="")
    )
    telegram_sink, lead_ai_sink = _build_sinks(client, lead_ai_service)

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        source = _FakeSource([_message()])
        pipeline = Pipeline(source, _engine(), [telegram_sink, lead_ai_sink], user_repository)
        await pipeline.run()

        assert set(client.forward_calls) >= set(_OTHER_RECIPIENTS)
        for recipient in _OTHER_RECIPIENTS:
            assert len(_messages_to(client, recipient)) == 1
    finally:
        user_repository.close()


async def test_other_recipients_receive_original_when_ai_errors_out(tmp_path):
    client = _FakeTelegramClient()
    lead_ai_service = _FakeLeadAiService(error=LeadAiServiceError("boom"))
    telegram_sink, lead_ai_sink = _build_sinks(client, lead_ai_service)

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        source = _FakeSource([_message()])
        pipeline = Pipeline(source, _engine(), [telegram_sink, lead_ai_sink], user_repository)
        await pipeline.run()

        assert set(client.forward_calls) >= set(_OTHER_RECIPIENTS)
        for recipient in _OTHER_RECIPIENTS:
            assert len(_messages_to(client, recipient)) == 1
        # alena_ogi (AI ошибся) не получил вообще ничего — fail-closed.
        assert _messages_to(client, "alena_ogi") == []
        assert "alena_ogi" not in client.forward_calls
    finally:
        user_repository.close()


# ---- @alena_ogi: relevant=True -> оригинал + один follow-up; иначе ничего ----


async def test_ai_filtered_recipient_gets_original_and_one_follow_up_when_relevant(tmp_path):
    client = _FakeTelegramClient()
    lead_ai_service = _FakeLeadAiService(
        LeadAiAnalysis(
            relevant=True, lead_type="money_transfer_ru_ge",
            reason="хочет перевести деньги из России в Грузию",
            suggested_reply="Подскажите сумму и в какой валюте хотите получить?",
        )
    )
    telegram_sink, lead_ai_sink = _build_sinks(client, lead_ai_service)

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        source = _FakeSource([_message()])
        pipeline = Pipeline(source, _engine(), [telegram_sink, lead_ai_sink], user_repository)
        await pipeline.run()

        assert "alena_ogi" in client.forward_calls
        alena_messages = _messages_to(client, "alena_ogi")
        assert len(alena_messages) == 2  # контекст (reply) + follow-up
        follow_ups = [text for text, _rt in alena_messages if "🤖 AI-анализ" in text]
        assert len(follow_ups) == 1
        assert "✅ Потенциальный лид" in follow_ups[0]
        assert "money_transfer_ru_ge" in follow_ups[0]
    finally:
        user_repository.close()


async def test_ai_filtered_recipient_gets_nothing_when_irrelevant(tmp_path):
    client = _FakeTelegramClient()
    lead_ai_service = _FakeLeadAiService(
        LeadAiAnalysis(relevant=False, lead_type="irrelevant", reason="r", suggested_reply="")
    )
    telegram_sink, lead_ai_sink = _build_sinks(client, lead_ai_service)

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        source = _FakeSource([_message()])
        pipeline = Pipeline(source, _engine(), [telegram_sink, lead_ai_sink], user_repository)
        await pipeline.run()

        assert "alena_ogi" not in client.forward_calls
        assert _messages_to(client, "alena_ogi") == []
    finally:
        user_repository.close()


async def test_ai_filtered_recipient_gets_nothing_on_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("reader.sinks.lead_ai_sink._ANALYSIS_TIMEOUT_SECONDS", 0.01)
    client = _FakeTelegramClient()
    lead_ai_service = _FakeLeadAiService(hang=True)
    telegram_sink, lead_ai_sink = _build_sinks(client, lead_ai_service)

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        source = _FakeSource([_message()])
        pipeline = Pipeline(source, _engine(), [telegram_sink, lead_ai_sink], user_repository)
        await pipeline.run()

        assert "alena_ogi" not in client.forward_calls
        assert _messages_to(client, "alena_ogi") == []
        # Остальные получатели при этом лид получили как обычно.
        for recipient in _OTHER_RECIPIENTS:
            assert len(_messages_to(client, recipient)) == 1
    finally:
        user_repository.close()


# ---- @alena_ogi не получает ничего ДО завершения AI-классификации ----


async def test_ai_filtered_recipient_receives_nothing_before_classification_completes(tmp_path):
    client = _FakeTelegramClient()
    release = asyncio.Event()

    class _PausedService:
        analyze_calls: list = []

        async def analyze(self, message_text):
            self.analyze_calls.append(message_text)
            await release.wait()
            return LeadAiAnalysis(relevant=True, lead_type="fine_payment", reason="r", suggested_reply="s")

    telegram_sink, lead_ai_sink = _build_sinks(client, _PausedService())
    # _process() вызывается напрямую (см. ниже) — start() нужно вызвать
    # вручную, run() сам его не вызовет.
    await telegram_sink.start()
    await lead_ai_sink.start()

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        pipeline = Pipeline(
            _FakeSource([]), _engine(), [telegram_sink, lead_ai_sink], user_repository,
        )

        await pipeline._process(_message())
        await asyncio.sleep(0)  # даём фоновой задаче дойти до release.wait()

        # Другие получатели уже получили лид (TelegramSink синхронный)...
        for recipient in _OTHER_RECIPIENTS:
            assert len(_messages_to(client, recipient)) == 1
        # ...а alena_ogi — ещё нет, классификация не завершена.
        assert "alena_ogi" not in client.forward_calls
        assert _messages_to(client, "alena_ogi") == []

        release.set()
        await asyncio.gather(*list(lead_ai_sink._background_tasks), return_exceptions=True)

        assert "alena_ogi" in client.forward_calls
        assert len(_follow_up_texts(client)) == 1
    finally:
        user_repository.close()


# ---- один LeadEvent не приводит к двойной отправке оригинала никому ----


async def test_single_lead_event_delivers_original_exactly_once_to_each_recipient(tmp_path):
    client = _FakeTelegramClient()
    lead_ai_service = _FakeLeadAiService(
        LeadAiAnalysis(relevant=True, lead_type="fine_payment", reason="r", suggested_reply="s")
    )
    telegram_sink, lead_ai_sink = _build_sinks(client, lead_ai_service)

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        source = _FakeSource([_message()])
        pipeline = Pipeline(source, _engine(), [telegram_sink, lead_ai_sink], user_repository)
        await pipeline.run()

        for recipient in _ALL_CONFIGURED_RECIPIENTS:
            assert client.forward_calls.count(recipient) == 1
        assert len(_follow_up_texts(client)) == 1
    finally:
        user_repository.close()


# ---- lead_ai выключен: полностью прежнее поведение (все 3 через TelegramSink) ----


async def test_lead_ai_disabled_leaves_pipeline_behavior_unchanged(tmp_path):
    """lead_ai.enabled=false (в реальной сборке — reader/main.py не строит
    LeadAiSink вовсе и передаёт TelegramSink полный forward_to, см.
    resolve_telegram_sink_recipients(forward_to, None)) — Pipeline с одним
    TelegramSink ведёт себя ровно так же, как до появления lead_ai."""
    client = _FakeTelegramClient()
    telegram_forward_to = resolve_telegram_sink_recipients(_ALL_CONFIGURED_RECIPIENTS, None)
    telegram_sink = TelegramSink(client, telegram_forward_to)

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        source = _FakeSource([_message()])
        pipeline = Pipeline(source, _engine(), [telegram_sink], user_repository)

        await pipeline.run()

        assert client.forward_calls == _ALL_CONFIGURED_RECIPIENTS
        assert all("🤖 AI-анализ" not in text for _r, text, _rt in client.send_message_calls)
    finally:
        user_repository.close()


# ---- слишком медленный OpenAI не задерживает Pipeline._process (fire-and-forget) ----


async def test_slow_ai_analysis_does_not_delay_pipeline_process(tmp_path):
    """Ключевое архитектурное требование: LeadAiSink.handle() должен быть
    fire-and-forget — Pipeline._process не должен ждать ответа OpenAI
    (тем более полный _ANALYSIS_TIMEOUT_SECONDS, здесь эмулированный
    зависанием analyze() на 999с), иначе медленный AI задерживал бы
    обработку СЛЕДУЮЩИХ лидов в очереди — и не должен задерживать доставку
    другим получателям (TelegramSink) тоже."""
    client = _FakeTelegramClient()
    lead_ai_service = _FakeLeadAiService(hang=True)
    telegram_sink, lead_ai_sink = _build_sinks(client, lead_ai_service)
    await telegram_sink.start()
    await lead_ai_sink.start()

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        # Вызываем Pipeline._process() напрямую (а не run()) — так в
        # измеренное время НЕ попадает shutdown-логика run()'s finally
        # (sink.stop(), который сам по себе умеет ждать/отменять фоновые
        # задачи, см. отдельный тест в test_lead_ai_sink.py) — проверяем
        # именно то, что обработка ОДНОГО сообщения не блокируется AI.
        pipeline = Pipeline(
            _FakeSource([]), _engine(), [telegram_sink, lead_ai_sink], user_repository,
        )

        started = time.monotonic()
        await pipeline._process(_message())
        elapsed = time.monotonic() - started

        # Даём планировщику шанс запустить только что созданную фоновую
        # задачу до её первой точки await (это не тот же самый ожидание её
        # ЗАВЕРШЕНИЯ — analyze() всё ещё "висит" на 999с) — иначе
        # analyze_calls ниже мог бы оказаться пустым просто потому, что
        # задача ещё не успела получить квант времени от event loop'а.
        await asyncio.sleep(0)

        # Оригинальная доставка (TelegramSink) другим получателям отработала
        # как обычно, несмотря на "зависший" AI.
        for recipient in _OTHER_RECIPIENTS:
            assert len(_messages_to(client, recipient)) == 1
        # alena_ogi ничего не получил — классификация ещё не завершена.
        assert "alena_ogi" not in client.forward_calls

        # AI вызван, но _process не ждал его ответа.
        assert lead_ai_service.analyze_calls == ["хочу перевести деньги в Грузию"]
        assert elapsed < 1.0
        assert len(lead_ai_sink._background_tasks) == 1  # фоновая задача всё ещё выполняется
    finally:
        # "Зависшая" фоновая задача никогда сама не завершится в рамках
        # теста — отменяем явно, не дожидаясь настоящего таймаута.
        for task in list(lead_ai_sink._background_tasks):
            task.cancel()
        await asyncio.gather(*list(lead_ai_sink._background_tasks), return_exceptions=True)
        user_repository.close()
