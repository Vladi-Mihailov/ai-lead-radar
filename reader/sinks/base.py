from abc import ABC, abstractmethod

from reader.core.models import LeadEvent


class BaseSink(ABC):
    async def start(self) -> None:
        """Опциональная инициализация перед началом обработки сообщений."""
        return

    @abstractmethod
    async def handle(self, event: LeadEvent) -> None:
        """Обработать найденное совпадение."""
