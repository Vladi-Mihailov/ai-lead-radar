"""Интеграционный тест: TelegramSink (существующая доставка трём
получателям) + LeadAiSink (follow-up ТОЛЬКО для одного из них) в одном
Pipeline — как они реально собираются в reader/main.py::run().

Проверяет ключевое требование задачи: AI-анализ работает только для
@alena_ogi, остальные получатели (@ali_na_l_i, @alenaogir) продолжают
получать лид без какого-либо AI, независимо от порядка sinks/получателей.
TelegramClient — единственный фейковый (по аналогии с test_telegram_sink.py),
OpenAI не используется вовсе (LeadAiService подменяется фейком)."""

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
from reader.scenarios import KeywordMatcher, Scenario  # noqa: E402
from reader.sinks.lead_ai_sink import LeadAiSink  # noqa: E402
from reader.sinks.telegram_sink import TelegramSink  # noqa: E402
from reader.sources.base import BaseSource  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_SCENARIOS = [Scenario(name="transfer", enabled=True, keywords=("перевести",))]
_RECIPIENTS = ["ali_na_l_i", "alena_ogi", "alenaogir"]


@dataclass
class _FakeEntity:
    id: int
    source: Any


class _FakeTelegramClient:
    def __init__(self):
        self.forward_calls: list = []
        self.send_message_calls: list = []
        self._forwarded_counter = 1000

    async def get_entity(self, target):
        key = str(target).lstrip("@").lower()
        return _FakeEntity(id=hash(key) % 10_000_000, source=target)

    async def forward_messages(self, entity, *, messages, from_peer):
        self.forward_calls.append(entity.source)
        self._forwarded_counter += 1
        return SimpleNamespace(id=self._forwarded_counter)

    async def send_message(self, entity, text, *, parse_mode=None, link_preview=None, reply_to=None):
        self.send_message_calls.append((entity.source, text))


class _FakeLeadAiService:
    def __init__(self, result: LeadAiAnalysis = None, *, hang: bool = False):
        self._result = result
        self._hang = hang
        self.analyze_calls: list = []

    async def analyze(self, message_text: str) -> LeadAiAnalysis:
        self.analyze_calls.append(message_text)
        if self._hang:
            await asyncio.sleep(999)  # умышленно "виснет" — имитация медленного OpenAI
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


async def test_ai_follow_up_reaches_only_alena_ogi_others_unaffected(tmp_path):
    # Pipeline.run() вызывает sink.start() само (см. reader/core/pipeline.py) —
    # здесь его вызывать заранее нельзя: TelegramSink.start() не идемпотентен
    # (повторный вызов повторно добавил бы те же цели в self._resolved).
    client = _FakeTelegramClient()
    telegram_sink = TelegramSink(client, _RECIPIENTS)

    lead_ai_service = _FakeLeadAiService(
        LeadAiAnalysis(
            relevant=True, lead_type="money_transfer_ru_ge",
            reason="хочет перевести деньги из России в Грузию",
            suggested_reply="Подскажите сумму и в какой валюте хотите получить?",
        )
    )
    lead_ai_sink = LeadAiSink(client, "alena_ogi", lead_ai_service)

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        source = _FakeSource([_message()])
        pipeline = Pipeline(source, _engine(), [telegram_sink, lead_ai_sink], user_repository)

        await pipeline.run()

        # Существующая доставка — всем трём получателям, как и раньше.
        assert client.forward_calls == _RECIPIENTS

        # AI вызван ровно один раз, с текстом сообщения.
        assert lead_ai_service.analyze_calls == ["хочу перевести деньги в Грузию"]

        # send_message: по одному "контексту" (форвард успешен) на каждого
        # из трёх получателей, ПЛЮС ровно один AI follow-up — и только для
        # alena_ogi.
        ai_follow_ups = [
            (recipient, text) for recipient, text in client.send_message_calls
            if "🤖 AI-анализ" in text
        ]
        assert len(ai_follow_ups) == 1
        assert ai_follow_ups[0][0] == "alena_ogi"

        # ali_na_l_i/alenaogir получили только контекст (реслылка), без
        # какого-либо AI-сообщения.
        other_texts = [
            text for recipient, text in client.send_message_calls
            if recipient in ("ali_na_l_i", "alenaogir")
        ]
        assert all("🤖 AI-анализ" not in text for text in other_texts)
    finally:
        user_repository.close()


async def test_lead_ai_disabled_leaves_pipeline_behavior_unchanged(tmp_path):
    """lead_ai.enabled=false (в реальной сборке — reader/main.py не
    добавляет LeadAiSink вовсе, см. build_lead_ai_sink) — Pipeline с одним
    TelegramSink ведёт себя ровно так же, как до появления lead_ai."""
    client = _FakeTelegramClient()
    telegram_sink = TelegramSink(client, _RECIPIENTS)

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        source = _FakeSource([_message()])
        pipeline = Pipeline(source, _engine(), [telegram_sink], user_repository)

        await pipeline.run()

        assert client.forward_calls == _RECIPIENTS
        assert all("🤖 AI-анализ" not in text for _r, text in client.send_message_calls)
    finally:
        user_repository.close()


# ---- слишком медленный OpenAI не задерживает Pipeline._process (fire-and-forget) ----


async def test_slow_ai_analysis_does_not_delay_pipeline_process(tmp_path):
    """Ключевое архитектурное требование: LeadAiSink.handle() должен быть
    fire-and-forget — Pipeline._process не должен ждать ответа OpenAI
    (тем более полный _ANALYSIS_TIMEOUT_SECONDS, здесь эмулированный
    зависанием analyze() на 999с), иначе медленный AI задерживал бы
    обработку СЛЕДУЮЩИХ лидов в очереди."""
    client = _FakeTelegramClient()
    telegram_sink = TelegramSink(client, _RECIPIENTS)
    await telegram_sink.start()

    lead_ai_service = _FakeLeadAiService(hang=True)
    lead_ai_sink = LeadAiSink(client, "alena_ogi", lead_ai_service)
    await lead_ai_sink.start()

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        # Вызываем Pipeline._process() напрямую (а не run()) — так в
        # измеренное время НЕ попадает shutdown-логика run()'s finally
        # (sink.stop(), который сам по себе умеет ждать/отменять фоновые
        # задачи, см. отдельный тест ниже) — проверяем именно то, что
        # обработка ОДНОГО сообщения не блокируется AI-анализом.
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

        # Оригинальная доставка (TelegramSink) отработала как обычно.
        assert client.forward_calls == _RECIPIENTS
        # AI вызван, но _process не ждал его ответа — "зависший" analyze()
        # (999с) никак не повлиял на время обработки.
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
