import asyncio
import logging
import time

from telethon import TelegramClient

from reader.core.models import LeadEvent
from reader.lead_ai.models import LeadAiAnalysis
from reader.lead_ai.service import LeadAiService, LeadAiServiceError
from reader.sinks.base import BaseSink

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
# чтобы обычное завершение анализа/отправки не обрывалось искусственно.
_SHUTDOWN_WAIT_TIMEOUT_SECONDS = 25.0

_UNAVAILABLE_TEXT = "🤖 AI-анализ\n\nAI-анализ временно недоступен."


class LeadAiSink(BaseSink):
    """Follow-up AI-анализ лида — ТОЛЬКО для одного получателя (см.
    settings.lead_ai.recipient), независимо от того, сколько получателей
    настроено в app.lead_forward_to. Оригинальный лид этому получателю уже
    доставлен другим sink'ом (TelegramSink, см. reader/main.py — порядок
    sinks в списке) — этот sink НИЧЕГО не пересылает сам, только
    отправляет отдельное follow-up сообщение с результатом анализа.

    handle() — fire-and-forget: запускает анализ в фоновой asyncio.Task и
    немедленно возвращается, НЕ дожидаясь ответа OpenAI. Pipeline._process
    вызывает sink'и последовательно (см. reader/core/pipeline.py) — если бы
    handle() ждал OpenAI (до _ANALYSIS_TIMEOUT_SECONDS), это задерживало бы
    обработку ВСЕХ следующих сообщений в очереди, а не только AI-анализ
    текущего лида. Фоновые задачи хранятся в self._background_tasks (иначе
    единственная ссылка на Task существовала бы только в event loop'е, и
    сборщик мусора мог бы забрать её до завершения — см. предупреждение в
    документации asyncio.create_task) и удаляются оттуда по завершении.

    AI недоступен/упал/не успел за timeout -> follow-up либо не
    отправляется, либо заменяется коротким "временно недоступен" (см.
    _send_unavailable) — сама доставка лида к этому моменту уже завершена
    предыдущими sink'ами и не откатывается: любое исключение фоновой
    задачи только логируется, наружу (в Pipeline) никогда не пробрасывается
    — оно и не может, т.к. создание задачи уже никак не связано со стеком
    вызова handle()."""

    def __init__(self, client: TelegramClient, recipient: int | str, service: LeadAiService):
        self._client = client
        self._recipient = recipient
        self._service = service
        self._entity = None
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_ANALYSES)
        self._background_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._entity = await self._client.get_entity(self._recipient)

    async def handle(self, event: LeadEvent) -> None:
        task = asyncio.create_task(self._analyze_and_notify(event))
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

    async def _analyze_and_notify(self, event: LeadEvent) -> None:
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
                logger.warning(
                    "lead_ai analysis timed out after %.1fs", _ANALYSIS_TIMEOUT_SECONDS,
                )
                await self._send_unavailable()
                return
            except LeadAiServiceError:
                logger.warning("lead_ai analysis failed")
                await self._send_unavailable()
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

            await self._send(_format(analysis))

    async def _send_unavailable(self) -> None:
        await self._send(_UNAVAILABLE_TEXT)

    async def _send(self, text: str) -> None:
        try:
            await self._client.send_message(self._entity, text, link_preview=False)
        except Exception:
            logger.exception("Не удалось отправить AI-анализ получателю lead_ai")


def _format(analysis: LeadAiAnalysis) -> str:
    lines = ["🤖 AI-анализ", ""]
    if analysis.relevant:
        lines.append("✅ Потенциальный лид")
        lines.append(f"Тип: {analysis.lead_type}")
        lines.append(f"Причина: {analysis.reason}")
        lines.append("")
        lines.append("Предлагаемый ответ:")
        lines.append(analysis.suggested_reply)
    else:
        lines.append("❌ Не лид")
        lines.append(f"Тип: {analysis.lead_type}")
        lines.append(f"Причина: {analysis.reason}")
    return "\n".join(lines)
