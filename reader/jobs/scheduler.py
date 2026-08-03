import asyncio
import logging
from datetime import datetime, timezone

from reader.jobs.base import Job

logger = logging.getLogger(__name__)


class Scheduler:
    """Универсальный исполнитель зарегистрированных Job в одном asyncio-цикле.

    Ничего не знает про FineJob/мониторинг штрафов — только про интерфейс
    Job. Один Scheduler обслуживает весь список задач, отдельный цикл на
    каждую задачу не создаётся.
    """

    def __init__(self, jobs: list[Job], *, poll_interval_seconds: float = 30.0):
        self._jobs = jobs
        self._poll_interval_seconds = poll_interval_seconds
        # Только для отображения статуса (например, "fine status") — не
        # влияет на логику опроса.
        self.is_running = False

    async def tick(self, now: datetime) -> None:
        """Одна проверка всех job на момент now. Ошибка одной job не мешает
        остальным — ловится и логируется здесь же."""
        for job in self._jobs:
            try:
                if await job.should_run(now):
                    await job.run()
            except Exception:
                logger.exception("Job '%s' завершилась с ошибкой", job.name)

    async def run_forever(self) -> None:
        self.is_running = True
        try:
            while True:
                await self.tick(datetime.now(timezone.utc))
                await asyncio.sleep(self._poll_interval_seconds)
        finally:
            self.is_running = False
