"""FineJob — единственная сегодня реализация Job для мониторинга штрафов.
Никакой логики проверки штрафов здесь нет (это FineCheckService), никакой
логики доставки уведомлений (это FineNotificationCoordinator) и никакого
знания о Telegram — только: получить активные задачи, для каждой решить
(по start_date/end_date в настроенном timezone) — пропустить, проверить или
завершить, вызвать FineCheckService там, где нужно, затем одним вызовом
координатора доставить всё, что ещё не доставлено оператору.
FineMonitoringTaskRepository остаётся простым хранилищем — сравнение дат
делает сам FineJob, не репозиторий.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from reader.fines.archive_scheduling import local_time_to_utc
from reader.fines.check_service import FineCheckService
from reader.fines.notification_coordinator import FineNotificationCoordinator
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.jobs.base import Job

logger = logging.getLogger(__name__)


@dataclass
class FineJobStatus:
    """Статистика запусков — только для отображения в "fine status", не
    влияет на логику. Мутируется исключительно из run() этого же объекта."""

    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    error_count: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None


class FineJob(Job):
    name = "fine_monitoring"

    def __init__(
        self,
        task_repository: FineMonitoringTaskRepository,
        check_service: FineCheckService,
        notification_coordinator: FineNotificationCoordinator,
        *,
        run_times: list[dt_time],
        tz: ZoneInfo,
        archive_check_enabled: bool = False,
        archive_check_hour: int = 4,
        archive_interval_days: int = 30,
    ):
        self._task_repository = task_repository
        self._check_service = check_service
        self._notification_coordinator = notification_coordinator
        self._run_times = run_times
        self._tz = tz
        # Архивный режим (см. reader/jobs/archive_fine_job.py) выключен по
        # умолчанию — конструктор без этих аргументов (как во всех
        # существующих вызовах/тестах) ведёт себя БИТ В БИТ как раньше:
        # завершение задачи — это только set_status(..., "completed").
        self._archive_check_enabled = archive_check_enabled
        self._archive_check_hour = archive_check_hour
        self._archive_interval_days = archive_interval_days
        self._last_run_slot: tuple[date, dt_time] | None = None
        self.status = FineJobStatus()

    async def should_run(self, now: datetime) -> bool:
        """Срабатывает при точном совпадении часа и минуты с одним из
        run_times (в self._tz). Осознанно принятое, простое поведение,
        без persistent state и без catch-up:

        - `_last_run_slot` — только в памяти этого объекта. После рестарта
          процесса создаётся новый FineJob с `_last_run_slot=None`, поэтому
          если рестарт пришёлся ровно на минуту одного из run_times — job
          запустится снова (не "помнит" о работе до рестарта).
        - Если процесс не опрашивал should_run() ни разу в течение нужной
          минуты (был выключен/завис) — этот запуск просто пропускается
          навсегда, никакого автоматического докатывания ("catch-up") нет.
          При текущем масштабе (3 фиксированных времени в сутки) это
          осознанный компромисс простоты, а не недосмотр.
        """
        local_now = now.astimezone(self._tz)

        matched_time = next(
            (
                t
                for t in self._run_times
                if (t.hour, t.minute) == (local_now.hour, local_now.minute)
            ),
            None,
        )
        if matched_time is None:
            return False

        slot = (local_now.date(), matched_time)
        if slot == self._last_run_slot:
            # Уже отработали именно этот слот сегодня — не запускать снова
            # при следующем опросе Scheduler'а в течение той же минуты.
            return False

        self._last_run_slot = slot
        return True

    async def run(self, now: datetime | None = None) -> None:
        run_at = now or datetime.now(timezone.utc)
        self.status.last_run_at = run_at
        today = run_at.astimezone(self._tz).date()

        tasks = self._task_repository.list_active()

        for task in tasks:
            try:
                if today < task.start_date:
                    # Период мониторинга ещё не начался — не проверяем.
                    continue

                if today > task.end_date:
                    # Период закончился — завершаем задачу, к FineCheckService
                    # не обращаемся вовсе.
                    self._task_repository.set_status(task.id, "completed")
                    if self._archive_check_enabled:
                        # Задача только что завершилась ЕСТЕСТВЕННО, под
                        # надзором самого FineJob — источник события
                        # однозначен, поэтому (в отличие от уже существующих
                        # исторических completed-задач, см.
                        # reader/fines/archive_enrollment.py) можно безопасно
                        # поставить первую архивную проверку автоматически,
                        # без отдельного enrollment-шага.
                        self._task_repository.schedule_first_archive_check(
                            task.id, next_check_at=self._first_archive_check_at(run_at)
                        )
                    continue

                # start_date <= today <= end_date — границы включительно.
                await self._check_service.check_task(task)
            except Exception as exc:
                # Ошибка на одной задаче (проверка или завершение) не должна
                # прерывать обработку остальных активных задач в этом же
                # проходе.
                self._record_error(exc)
                logger.exception(
                    "Обработка задачи мониторинга id=%s (%s) завершилась с ошибкой",
                    task.id,
                    task.car_number,
                )
                continue

        try:
            # Уведомляем отдельно от самой проверки: сюда попадают и штрафы,
            # только что созданные выше (notification_sent_at ещё NULL), и
            # оставшиеся недоставленными с прошлых проходов — один и тот же
            # признак, поэтому "новое" и "повтор" не различаются искусственно.
            await self._notification_coordinator.flush_pending()
        except Exception as exc:
            self._record_error(exc)
            logger.exception("Отправка накопленных уведомлений завершилась с ошибкой")
            return

        self.status.last_success_at = run_at

    def _first_archive_check_at(self, run_at: datetime) -> datetime:
        """~archive_interval_days от сегодня (по self._tz), время —
        archive_check_hour:00 — тот же формат "день + фиксированный час",
        что и у массового enrollment (см. build_archive_schedule), чтобы
        обе точки постановки в архивный режим были согласованы."""
        target_day = run_at.astimezone(self._tz).date() + timedelta(
            days=self._archive_interval_days
        )
        return local_time_to_utc(target_day, self._archive_check_hour, self._tz)

    def _record_error(self, exc: Exception) -> None:
        self.status.error_count += 1
        self.status.last_error = str(exc)
        self.status.last_error_at = datetime.now(timezone.utc)
