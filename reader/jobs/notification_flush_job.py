"""NotificationFlushJob — на каждом тике Scheduler'а (см.
reader/jobs/scheduler.py, ~30 секунд) доставляет уже накопленные, но ещё
не отправленные оператору штрафы через существующий
FineNotificationCoordinator.flush_pending().

Единственная причина существования этого job'а — избежать того, чтобы
штраф, обнаруженный immediate-check'ом из @GEShtrafbot (Add Car / 🔎
Проверить сейчас, см. reader/public_bot/), ждал следующего планового
запуска FineJob/ClientFineJob (до нескольких часов) прежде чем дойти до
оператора (см. design report Stage 4, раздел "Immediate check").

Работает ИСКЛЮЧИТЕЛЬНО в main-процессе (ai-lead-radar.service), где живёт
FineNotificationCoordinator/операторская Telegram-сессия — бот-процесс
никогда не вызывает flush_pending() напрямую (у него и нет для этого
операторской сессии), поэтому двух конкурирующих отправителей оператору
быть не может (см. design report, раздел "Immediate-check race handling").
Scheduler выполняет job'ы строго последовательно в одном asyncio-цикле
(см. Scheduler.tick()), поэтому этот job и собственный flush_pending() в
конце FineJob.run()/ArchiveFineJob.run() тоже никогда не пересекаются во
времени — гонки нет и внутри одного процесса.
"""

from datetime import datetime

from reader.fines.notification_coordinator import FineNotificationCoordinator
from reader.jobs.base import Job


class NotificationFlushJob(Job):
    name = "fine_notification_flush"

    def __init__(self, notification_coordinator: FineNotificationCoordinator):
        self._notification_coordinator = notification_coordinator

    async def should_run(self, now: datetime) -> bool:
        # Каждый тик Scheduler'а — никакого расписания, флаш дешёв (один
        # SELECT), когда отправлять нечего (см. flush_pending()).
        return True

    async def run(self) -> None:
        await self._notification_coordinator.flush_pending()
