from abc import ABC, abstractmethod

from reader.core.models import LeadEvent


class BaseSink(ABC):
    async def start(self) -> None:
        """Опциональная инициализация перед началом обработки сообщений."""
        return

    @abstractmethod
    async def handle(self, event: LeadEvent) -> None:
        """Обработать найденное совпадение."""

    async def stop(self) -> None:
        """Опциональная остановка при завершении Pipeline.run() (см.
        reader/core/pipeline.py) — no-op по умолчанию, как и start(). Нужен
        sink'ам, у которых handle() запускает фоновую работу (например,
        reader/sinks/lead_ai_sink.py::LeadAiSink — fire-and-forget
        AI-анализ) и которым нужен шанс аккуратно её дождаться/отменить
        перед остановкой, а не просто быть брошенными вместе с event loop."""
        return
