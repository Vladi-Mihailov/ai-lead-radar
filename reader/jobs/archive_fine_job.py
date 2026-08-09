"""ArchiveFineJob — второй, независимый от FineJob режим мониторинга
штрафов: для автомобилей, у которых закончился обычный (несколько раз в
сутки) период, но которые всё ещё стоит изредка перепроверять, а не
переставать наблюдать совсем.

Никакой отдельной бизнес-логики проверки здесь нет — ArchiveFineJob вызывает
ту же самую FineCheckService.check_task(), что и FineJob (по расписанию) и
FineCommand (fine check/update-all), и тот же FineNotificationCoordinator для
доставки. Единственное, что у этого job'а своё — выборка задач
(list_due_for_archive_check(), а не list_active()) и то, что происходит с
задачей ПОСЛЕ проверки (см. run()).

FineMonitoringTaskRepository остаётся простым хранилищем — вся логика
"что дальше" (перенести на +interval_days, вернуть в частый мониторинг,
оставить due при ошибке) находится здесь, а не в репозитории — тот же
принцип, что и у FineJob/fine_monitoring_tasks.status.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from reader.fines.check_service import FineCheckService
from reader.fines.models import FineMonitoringTask
from reader.fines.notification_coordinator import FineNotificationCoordinator
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.jobs.base import Job

logger = logging.getLogger(__name__)


@dataclass
class ArchiveFineJobStatus:
    """Статистика запусков — только для наблюдаемости (аналог
    reader.jobs.fine_job.FineJobStatus), не влияет на логику. Мутируется
    исключительно из run() этого же объекта."""

    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    error_count: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None


class ArchiveFineJob(Job):
    name = "fine_archive_monitoring"

    def __init__(
        self,
        task_repository: FineMonitoringTaskRepository,
        check_service: FineCheckService,
        notification_coordinator: FineNotificationCoordinator,
        *,
        enabled: bool,
        hour: int,
        interval_days: int,
        daily_limit: int,
        tz: ZoneInfo,
    ):
        self._task_repository = task_repository
        self._check_service = check_service
        self._notification_coordinator = notification_coordinator
        self._enabled = enabled
        self._hour = hour
        self._interval_days = interval_days
        self._daily_limit = daily_limit
        self._tz = tz
        self._last_run_date: date | None = None
        self.status = ArchiveFineJobStatus()

    async def should_run(self, now: datetime) -> bool:
        """Ровно один раз в сутки, в self._hour:00 по self._tz. enabled=False
        (см. settings.fine_monitor.archive_check_enabled) отключает job
        полностью — Scheduler продолжает его опрашивать, но should_run()
        всегда возвращает False, ни одна due-задача не читается."""
        if not self._enabled:
            return False

        local_now = now.astimezone(self._tz)
        if local_now.hour != self._hour or local_now.minute != 0:
            return False

        if local_now.date() == self._last_run_date:
            # Уже отработали сегодняшний слот — не запускать снова при
            # следующем опросе Scheduler'а в течение той же минуты.
            return False

        self._last_run_date = local_now.date()
        return True

    async def run(self, now: datetime | None = None) -> None:
        run_at = now or datetime.now(timezone.utc)
        self.status.last_run_at = run_at

        # limit — safety limit на один проход (см. settings.fine_monitor.
        # archive_daily_limit): если downtime/backlog накопил больше задач,
        # чем лимит, остаток остаётся due (next_archive_check_at не тронут)
        # и будет подобран следующим запуском — ничего не теряется, но и
        # неконтролируемый backlog не обрабатывается весь разом.
        due_tasks = self._task_repository.list_due_for_archive_check(
            run_at, limit=self._daily_limit
        )
        logger.info("ArchiveFineJob started: due=%d", len(due_tasks))

        checked = 0
        new_fines_total = 0
        errors = 0

        for index, task in enumerate(due_tasks, start=1):
            try:
                result = await self._check_service.check_task(task)
            except Exception as exc:
                # Ошибка одной задачи не должна останавливать остальные —
                # тот же принцип, что и в FineJob.run()/fine update-all.
                errors += 1
                self._record_error(exc)
                logger.exception(
                    "Archive check %d/%d: id=%s (%s) завершилась с ошибкой",
                    index, len(due_tasks), task.id, task.car_number,
                )
                # Техническая ошибка — НЕ откладываем на месяц: next_archive_
                # check_at не трогаем (ни один reschedule ниже не вызывается),
                # задача остаётся due и получит новую попытку на следующем
                # запуске (~через сутки — сама суточная периодичность
                # ArchiveFineJob уже исключает тесный цикл повторов).
                continue

            logger.info(
                "Archive check: %d/%d %s — %s",
                index, len(due_tasks), task.car_number, result.status,
            )

            if result.status == "error":
                # FineCheckService.check_task() сам не бросает исключение на
                # ошибку провайдера — попадает сюда, а не в except выше.
                # Тот же принцип "оставить due" — record_check_result уже
                # сохранил last_check_status/last_error, но next_archive_
                # check_at не двигаем.
                errors += 1
                continue

            checked += 1

            if result.new_fines:
                # new_fines — ТОЛЬКО фингерпринты, ни разу не виденные для
                # этой задачи (см. FineCheckService.check_task) — то есть
                # здесь гарантированно новый штраф, а не повторно пришедший
                # уже известный. Возврат в частый мониторинг оправдан.
                new_fines_total += len(result.new_fines)
                today = run_at.astimezone(self._tz).date()
                self._task_repository.return_to_active_monitoring(
                    task.id,
                    start_date=today,
                    end_date=today + timedelta(days=self._interval_days),
                )
                logger.info(
                    "Archive check: %s — найден новый штраф (%d), задача возвращена "
                    "в обычный мониторинг на %d дней",
                    task.car_number, len(result.new_fines), self._interval_days,
                )
            else:
                next_check_at = self._compute_next_check_at(task, run_at)
                self._task_repository.reschedule_next_archive_check(
                    task.id, next_check_at=next_check_at
                )

        flush_failed = False
        try:
            # Тот же самый координатор, что и FineJob/fine check/update-all —
            # один общий retry-механизм на notification_sent_at IS NULL, без
            # второго notification flow.
            await self._notification_coordinator.flush_pending()
        except Exception as exc:
            flush_failed = True
            errors += 1
            self._record_error(exc)
            logger.exception(
                "ArchiveFineJob: отправка накопленных уведомлений завершилась с ошибкой"
            )

        if not flush_failed:
            self.status.last_success_at = run_at

        logger.info(
            "ArchiveFineJob finished: checked=%d, new_fines=%d, errors=%d",
            checked, new_fines_total, errors,
        )

    def _compute_next_check_at(self, task: FineMonitoringTask, run_at: datetime) -> datetime:
        """previous_due_at (task.next_archive_check_at — срок, который
        только что сработал) + interval_days, а НЕ run_at + interval_days —
        иначе накопленная задержка Scheduler'а (тик раз в ~30 сек,
        пропущенные дни, рестарты) постепенно "утягивала" бы расписание
        вперёд с каждым циклом (schedule drift). Если previous_due_at
        отсутствует/некорректен — не полагаемся молча на список из
        list_due_for_archive_check(), используем run_at как разумный
        fallback."""
        previous_due_at = task.next_archive_check_at
        if previous_due_at is None:
            return run_at + timedelta(days=self._interval_days)

        candidate = previous_due_at + timedelta(days=self._interval_days)
        # Если задача настолько отстала (backlog из-за daily_limit), что
        # даже "previous + interval" всё ещё не позже run_at — не оставляем
        # её тут же снова due: минимум run_at гарантирует хотя бы один полный
        # интервал вперёд, без немедленного повторного попадания в тот же
        # (или следующий) прогон.
        return max(candidate, run_at)

    def _record_error(self, exc: Exception) -> None:
        self.status.error_count += 1
        self.status.last_error = str(exc)
        self.status.last_error_at = datetime.now(timezone.utc)
