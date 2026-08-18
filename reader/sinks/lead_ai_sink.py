import asyncio
import logging
import time

from telethon import TelegramClient

from reader.core.models import LeadEvent
from reader.lead_ai.greeting import tbilisi_greeting
from reader.lead_ai.models import LeadAiAnalysis
from reader.lead_ai.service import LeadAiService, LeadAiServiceError
from reader.sinks.base import BaseSink
from reader.sinks.telegram_lead_delivery import ResolvedTarget, TelegramLeadDelivery, resolve_label

logger = logging.getLogger(__name__)

# Достаточно, чтобы не ждать OpenAI бесконечно, но не обрывать нормальный
# ответ модели раньше времени — сама LeadAiService уже делает не более
# одного внутреннего ретрая (см. её докстрок). ПРИМЕЧАНИЕ: этот timeout
# больше НЕ блокирует Pipeline._process (см. handle() ниже — fire-and-
# forget) — он ограничивает только фоновую задачу самого анализа.
_ANALYSIS_TIMEOUT_SECONDS = 10.0

# Верхняя граница ОДНОВРЕМЕННЫХ обращений к OpenAI из этого sink'а — лиды
# могут приходить быстрее, чем OpenAI успевает отвечать; без лимита
# количество параллельных фоновых задач/исходящих запросов было бы
# неограниченным (см. задачу: "не создавать бесконтрольное количество
# задач"). Сама постановка задачи в handle() при этом НЕ блокируется —
# семафор захватывается ВНУТРИ фоновой задачи, а не в handle().
_MAX_CONCURRENT_ANALYSES = 5

# Сколько ждать активные фоновые задачи при stop() (см. docstring метода)
# прежде чем отменить недоделанные — с запасом над _ANALYSIS_TIMEOUT_SECONDS,
# чтобы обычное завершение анализа/доставки не обрывалось искусственно.
_SHUTDOWN_WAIT_TIMEOUT_SECONDS = 25.0


class LeadAiSink(BaseSink):
    """AI-отфильтрованная доставка — ТОЛЬКО для одного получателя (см.
    settings.lead_ai.recipient), независимо от того, сколько получателей
    настроено в app.lead_forward_to. В ОТЛИЧИЕ от TelegramSink, этот sink
    НЕ пересылает оригинал сразу — recipient не должен увидеть кандидата
    ДО того, как AI примет решение (см. задачу): handle() запускает
    fire-and-forget фоновую задачу (_classify_and_maybe_deliver), которая
    сначала классифицирует лид через OpenAI и ТОЛЬКО при relevant=True:
      1) доставляет оригинал/контекст этому получателю, переиспользуя
         TelegramLeadDelivery (reader/sinks/telegram_lead_delivery.py) —
         тот же формат "оригинал + контекст"/fallback на текстовую копию,
         что и у TelegramSink для остальных получателей;
      2) следом отправляет follow-up "🤖 AI-анализ" с типом/причиной/
         suggested_messages (приветствие в начале — см.
         reader/lead_ai/greeting.py, добавляется кодом, не моделью).

    Pipeline._process вызывает sink'и последовательно (см.
    reader/core/pipeline.py) — если бы handle() ждал OpenAI (до
    _ANALYSIS_TIMEOUT_SECONDS), это задерживало бы обработку ВСЕХ
    следующих сообщений в очереди, а не только AI-анализ текущего лида.
    Фоновые задачи хранятся в self._background_tasks (иначе единственная
    ссылка на Task существовала бы только в event loop'е, и сборщик мусора
    мог бы забрать её до завершения — см. предупреждение в документации
    asyncio.create_task) и удаляются оттуда по завершении.

    Fail-closed для ЭТОГО получателя (см. задачу): relevant=False, timeout,
    ошибка OpenAI или любое неожиданное исключение — получатель НЕ видит
    вообще ничего (ни оригинал, ни follow-up) — только warning/error в
    лог. Сама доставка другим получателям (TelegramSink) к этому моменту
    уже завершена независимо и не откатывается."""

    def __init__(self, client: TelegramClient, recipient: int | str, service: LeadAiService):
        self._client = client
        self._recipient = recipient
        self._service = service
        self._resolved_target: ResolvedTarget | None = None
        self._delivery = TelegramLeadDelivery(client)
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_ANALYSES)
        self._background_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        entity = await self._client.get_entity(self._recipient)
        self._resolved_target = ResolvedTarget(entity=entity, label=resolve_label(self._recipient))

    async def handle(self, event: LeadEvent) -> None:
        task = asyncio.create_task(self._classify_and_maybe_deliver(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def stop(self) -> None:
        """Даёт активным фоновым AI-задачам шанс аккуратно завершиться при
        остановке Pipeline (см. reader/core/pipeline.py::Pipeline.run) —
        ждёт их до _SHUTDOWN_WAIT_TIMEOUT_SECONDS, а не бросает состояние
        сразу; то, что не успело завершиться к таймауту, отменяется, чтобы
        задачи не "текли" после остановки sink'а."""
        if not self._background_tasks:
            return

        tasks = list(self._background_tasks)
        _done, pending = await asyncio.wait(tasks, timeout=_SHUTDOWN_WAIT_TIMEOUT_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _classify_and_maybe_deliver(self, event: LeadEvent) -> None:
        async with self._semaphore:
            started = time.monotonic()
            logger.info("lead_ai analysis started")
            try:
                analysis = await asyncio.wait_for(
                    self._service.analyze(event.message.text), timeout=_ANALYSIS_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                # Fail-closed (см. задачу): recipient не получает ничего —
                # ни оригинал, ни follow-up, только лог.
                logger.warning(
                    "lead_ai analysis timed out after %.1fs", _ANALYSIS_TIMEOUT_SECONDS,
                )
                return
            except LeadAiServiceError:
                logger.warning("lead_ai analysis failed")
                return
            except Exception:
                # Фоновая задача — исключение отсюда никогда не попало бы в
                # Pipeline в любом случае (см. handle()), но без явного
                # перехвата asyncio залогировал бы "Task exception was
                # never retrieved" вместо понятной причины.
                logger.exception("lead_ai: неожиданная ошибка фоновой задачи анализа")
                return

            latency = time.monotonic() - started
            logger.info(
                "lead_ai analysis completed relevant=%s lead_type=%s latency=%.2fs",
                analysis.relevant, analysis.lead_type, latency,
            )

            if not analysis.relevant:
                # Fail-closed: нерелевантный кандидат не должен попасть в
                # Telegram этому получателю вообще (см. задачу) — ни
                # оригинал, ни follow-up.
                return

            # relevant=True — ТОЛЬКО теперь доставляем оригинал/контекст
            # (см. докстрок класса) — до этого момента recipient не видел
            # кандидата вовсе.
            await self._delivery.deliver(self._resolved_target, event)
            await self._send_follow_up(analysis)

    async def _send_follow_up(self, analysis: LeadAiAnalysis) -> None:
        try:
            await self._client.send_message(
                self._resolved_target.entity, _format(analysis), link_preview=False,
            )
        except Exception:
            logger.exception("Не удалось отправить AI-анализ получателю lead_ai")


def _format(analysis: LeadAiAnalysis) -> str:
    header = "\n".join(
        [
            "🤖 AI-анализ",
            "",
            "✅ Потенциальный лид",
            f"Тип: {analysis.lead_type}",
            f"Причина: {analysis.reason}",
            "",
            "Предлагаемые сообщения:",
        ]
    )
    # Приветствие — первое "сообщение" в списке, но НЕ от модели (см.
    # reader/lead_ai/greeting.py про то, почему это код, а не suggested_
    # messages) — остальные строки визуально разделены пустой строкой (как
    # менеджер будет отправлять их клиенту по одной), без нумерации.
    messages = [tbilisi_greeting(), *analysis.suggested_messages]
    return header + "\n\n" + "\n\n".join(messages)
