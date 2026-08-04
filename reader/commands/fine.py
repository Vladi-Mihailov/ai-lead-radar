"""FineCommand — операторский интерфейс уже готового мониторинга штрафов.

Ничего не решает сам: валидация вынесена в reader/fines/validation.py,
проверка — в FineCheckService (тот же самый объект, что использует
FineJob), доставка уведомлений — в FineNotificationCoordinator (тоже общий
с FineJob). Эта команда — только парсинг ввода/форматирование ответа.
"""

from datetime import date, datetime, timezone
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from reader.commands.base import Command, CommandContext, CommandError, CommandResult
from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import CarFineStats, FineMonitoringTask
from reader.fines.notification_coordinator import FineNotificationCoordinator
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.fines.validation import (
    FineValidationError,
    normalize_car_number,
    resolve_monitoring_period,
    validate_no_overlap,
)
from reader.jobs.fine_job import FineJob
from reader.jobs.scheduler import Scheduler

_DATE_FORMAT = "%d.%m.%Y"

_ADD_USAGE_ERROR = (
    "❌ Неверный формат команды\n\n"
    "Используйте:\n"
    "fine add B957MA09\n"
    "или\n"
    "fine add B957MA09 03.08.2026 13.08.2026"
)
_STOP_USAGE_ERROR = "❌ Неверный формат команды\n\nИспользуйте:\nfine stop <TASK_ID>"
_CHECK_USAGE_ERROR = "❌ Неверный формат команды\n\nИспользуйте:\nfine check <TASK_ID>"
_UNKNOWN_SUBCOMMAND_ERROR = (
    "❌ Неверный формат команды\n\n"
    "Используйте:\n"
    "fine add | fine list | fine stop <TASK_ID> | fine check <TASK_ID> | "
    "fine status | fine stats"
)


def _fmt_date(value: date) -> str:
    return value.strftime(_DATE_FORMAT)


def _fmt_datetime(value: datetime) -> str:
    return value.strftime("%d.%m.%Y %H:%M")


def _format_check_times(run_times: list[dt_time]) -> str:
    formatted = [t.strftime("%H:%M") for t in run_times]
    if len(formatted) <= 1:
        return ", ".join(formatted)
    return ", ".join(formatted[:-1]) + f" и {formatted[-1]}"


def _parse_task_id(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise CommandError(f'❌ ID задачи должен быть числом, получено: "{raw}"') from None


class FineCommand(Command):
    name = "fine"

    def __init__(
        self,
        task_repository: FineMonitoringTaskRepository,
        check_service: FineCheckService,
        notification_coordinator: FineNotificationCoordinator,
        scheduler: Scheduler,
        fine_job: FineJob,
        detected_fine_repository: DetectedFineRepository,
        *,
        run_times: list[dt_time],
        tz: ZoneInfo,
    ):
        self._task_repository = task_repository
        self._check_service = check_service
        self._notification_coordinator = notification_coordinator
        self._scheduler = scheduler
        self._fine_job = fine_job
        self._detected_fine_repository = detected_fine_repository
        self._run_times = run_times
        self._tz = tz

    async def handle(self, ctx: CommandContext) -> CommandResult:
        if not ctx.args:
            raise CommandError(_UNKNOWN_SUBCOMMAND_ERROR)

        subcommand = ctx.args[0].lower()
        rest = ctx.args[1:]

        try:
            if subcommand == "add":
                return await self._handle_add(ctx, rest)
            if subcommand == "list":
                return self._handle_list()
            if subcommand == "stop":
                return self._handle_stop(rest)
            if subcommand == "check":
                return await self._handle_check(rest)
            if subcommand == "status":
                return self._handle_status()
            if subcommand == "stats":
                return self._handle_stats()
        except FineValidationError as exc:
            raise CommandError(f"❌ {exc.message}") from exc

        raise CommandError(_UNKNOWN_SUBCOMMAND_ERROR)

    async def _handle_add(self, ctx: CommandContext, args: list[str]) -> CommandResult:
        if len(args) not in (1, 3):
            raise CommandError(_ADD_USAGE_ERROR)

        car_number = normalize_car_number(args[0])

        today = datetime.now(timezone.utc).astimezone(self._tz).date()
        start_raw, end_raw = (None, None) if len(args) == 1 else (args[1], args[2])
        start_date, end_date = resolve_monitoring_period(start_raw, end_raw, today=today)

        existing = self._task_repository.get_active_by_car_number(car_number)
        validate_no_overlap(start_date, end_date, existing)

        task = self._task_repository.create(
            car_number=car_number,
            label=None,
            start_date=start_date,
            end_date=end_date,
            telegram_chat_id=ctx.chat_id,
            created_by_user_id=ctx.user_id,
        )

        return CommandResult(
            text=(
                "✅ Мониторинг штрафов добавлен\n\n"
                f"ID: {task.id}\n"
                f"Автомобиль: {task.car_number}\n"
                f"Период: {_fmt_date(start_date)}–{_fmt_date(end_date)}\n"
                f"Проверка: {_format_check_times(self._run_times)} по Тбилиси"
            )
        )

    def _handle_list(self) -> CommandResult:
        tasks = self._task_repository.list_active()
        if not tasks:
            return CommandResult(text="Активных задач мониторинга нет.")

        blocks = [self._format_task_line(task) for task in tasks]
        return CommandResult(text="\n\n".join(blocks))

    @staticmethod
    def _format_task_line(task: FineMonitoringTask) -> str:
        lines = [f"ID {task.id}: {task.car_number}" + (f" ({task.label})" if task.label else "")]
        lines.append(f"Период: {_fmt_date(task.start_date)}–{_fmt_date(task.end_date)}")

        if task.last_checked_at is not None:
            lines.append(
                f"Последняя проверка: {_fmt_datetime(task.last_checked_at)} "
                f"({task.last_check_status})"
            )
        else:
            lines.append("Последняя проверка: ещё не проверялась")

        return "\n".join(lines)

    def _handle_stop(self, args: list[str]) -> CommandResult:
        if len(args) != 1:
            raise CommandError(_STOP_USAGE_ERROR)

        task_id = _parse_task_id(args[0])
        task = self._task_repository.get(task_id)
        if task is None:
            raise CommandError(f"❌ Задача с ID {task_id} не найдена")
        if task.status != "active":
            raise CommandError(
                f"❌ Задача с ID {task_id} уже не активна (статус: {task.status})"
            )

        self._task_repository.set_status(task_id, "stopped")

        return CommandResult(
            text=f"✅ Мониторинг для {task.car_number} (ID {task_id}) остановлен"
        )

    async def _handle_check(self, args: list[str]) -> CommandResult:
        if len(args) != 1:
            raise CommandError(_CHECK_USAGE_ERROR)

        task_id = _parse_task_id(args[0])
        task = self._task_repository.get(task_id)
        if task is None:
            raise CommandError(f"❌ Задача с ID {task_id} не найдена")

        # Тот же FineCheckService, что использует и FineJob по расписанию —
        # никакой отдельной логики проверки здесь нет.
        result = await self._check_service.check_task(task)

        if result.status == "error":
            raise CommandError(f"❌ Ошибка проверки: {result.error_message}")

        # Тот же механизм доставки, что и у FineJob — тем же самым объектом
        # координатора, а не копией логики.
        await self._notification_coordinator.flush_pending()

        return CommandResult(
            text=(
                "✅ Проверка завершена\n\n"
                f"Автомобиль: {task.car_number}\n"
                f"Найдено штрафов: {result.total_fines_found}\n"
                f"Новых: {len(result.new_fines)}\n"
                f"Время: {result.duration_ms} мс"
            )
        )

    def _handle_status(self) -> CommandResult:
        active_count = self._task_repository.count_active()
        scheduler_state = "работает" if self._scheduler.is_running else "не запущен"
        status = self._fine_job.status

        last_run = _fmt_datetime(status.last_run_at) if status.last_run_at else "ещё не запускался"
        last_success = (
            _fmt_datetime(status.last_success_at) if status.last_success_at else "ещё не было"
        )
        last_error = (
            f"{status.last_error} ({_fmt_datetime(status.last_error_at)})"
            if status.last_error
            else "Нет"
        )

        return CommandResult(
            text=(
                "📊 Статус мониторинга штрафов\n\n"
                "Мониторинг: включён\n"
                f"Scheduler: {scheduler_state}\n"
                f"Активных задач: {active_count}\n"
                f"Расписание: {_format_check_times(self._run_times)} ({self._tz})\n"
                f"Последний запуск: {last_run}\n"
                f"Последняя успешная проверка: {last_success}\n"
                f"Ошибок: {status.error_count}\n"
                f"Последняя ошибка: {last_error}"
            )
        )

    def _handle_stats(self) -> CommandResult:
        stats = self._detected_fine_repository.get_stats_by_car()

        if not stats:
            return CommandResult(
                text="📊 Статистика штрафов\n\nПока не найдено ни одного штрафа."
            )

        table = self._format_stats_table(stats)
        total_cars = len(stats)
        total_fines = sum(row.fine_count for row in stats)

        return CommandResult(
            text=(
                "📊 Статистика штрафов\n\n"
                f"{table}\n\n"
                f"Всего автомобилей: {total_cars}\n"
                f"Всего опубликованных штрафов: {total_fines}"
            )
        )

    _STATS_CAR_HEADER = "Автомобиль"
    _STATS_COUNT_HEADER = "Штрафов"
    _STATS_COLUMN_GAP = "  "

    @classmethod
    def _format_stats_table(cls, stats: list[CarFineStats]) -> str:
        car_width = max(len(cls._STATS_CAR_HEADER), *(len(row.car_number) for row in stats))
        count_width = max(
            len(cls._STATS_COUNT_HEADER), *(len(str(row.fine_count)) for row in stats)
        )

        header = (
            f"{cls._STATS_CAR_HEADER.ljust(car_width)}{cls._STATS_COLUMN_GAP}"
            f"{cls._STATS_COUNT_HEADER}"
        )
        separator = f"{'-' * car_width}{cls._STATS_COLUMN_GAP}{'-' * count_width}"
        rows = [
            f"{row.car_number.ljust(car_width)}{cls._STATS_COLUMN_GAP}"
            f"{str(row.fine_count).rjust(count_width)}"
            for row in stats
        ]

        return "\n".join([header, separator, *rows])
