from abc import ABC, abstractmethod
from datetime import datetime


class Job(ABC):
    """Общий интерфейс фоновой задачи для Scheduler. Scheduler не знает
    ничего о конкретных реализациях (FineJob и будущих) — только этот
    контракт."""

    name: str

    @abstractmethod
    async def should_run(self, now: datetime) -> bool:
        """Пора ли запускать эту задачу прямо сейчас."""

    @abstractmethod
    async def run(self) -> None:
        """Выполнить задачу."""
