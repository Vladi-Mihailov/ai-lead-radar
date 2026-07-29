from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from reader.core.models import Message


class BaseSource(ABC):
    @abstractmethod
    async def start(self) -> None:
        """Подключиться к источнику и начать приём сообщений."""

    @abstractmethod
    def messages(self) -> AsyncIterator[Message]:
        """Асинхронный поток входящих сообщений."""

    @abstractmethod
    async def stop(self) -> None:
        """Отключиться от источника."""
