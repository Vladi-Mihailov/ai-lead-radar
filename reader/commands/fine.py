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
    parse_date,
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
_BULK_MAX_CAR_NUMBERS = 100
_BULK_USAGE_ERROR = (
    "❌ Неверный формат команды\n\n"
    "После первой строки укажите хотя бы один номер автомобиля, каждый —"
    " на отдельной строке. Например:\n\n"
    "fine add bulk\n"
    "H663KH702\n"
    "C072H0977\n\n"
    "или с общим периодом для всех номеров:\n\n"
    "fine add bulk 04.08.2026 04.09.2026\n"
    "H663KH702\n"
    "C072H0977"
)
_STOP_USAGE_ERROR = "❌ Неверный формат команды\n\nИспользуйте:\nfine stop <НОМЕР_АВТОМОБИЛЯ>"
_CHECK_USAGE_ERROR = "❌ Неверный формат команды\n\nИспользуйте:\nfine check <НОМЕР_АВТОМОБИЛЯ>"
_UNKNOWN_SUBCOMMAND_ERROR = (
    "❌ Неверный формат команды\n\n"
    "Используйте:\n"
    "fine add | fine list | fine stop <НОМЕР_АВТОМОБИЛЯ> | fine check <НОМЕР_АВТОМОБИЛЯ> | "
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
        if args and args[0].lower() == "bulk":
            return await self._handle_add_bulk(ctx, args[1:])

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
                f"Автомобиль: {task.car_number}\n"
                f"Период: {_fmt_date(start_date)}–{_fmt_date(end_date)}\n"
                f"Проверка: {_format_check_times(self._run_times)} по Тбилиси"
            )
        )

    async def _handle_add_bulk(self, ctx: CommandContext, args: list[str]) -> CommandResult:
        start_raw, end_raw, car_numbers_raw = self._split_bulk_args(args)

        if not car_numbers_raw:
            raise CommandError(_BULK_USAGE_ERROR)

        if len(car_numbers_raw) > _BULK_MAX_CAR_NUMBERS:
            raise CommandError(
                f"❌ Слишком много номеров в одном сообщении: {len(car_numbers_raw)} "
                f"(максимум {_BULK_MAX_CAR_NUMBERS} за одно сообщение)"
            )

        today = datetime.now(timezone.utc).astimezone(self._tz).date()
        start_date, end_date = resolve_monitoring_period(start_raw, end_raw, today=today)

        added = 0
        already_tracked = 0
        errors: list[tuple[str, str]] = []
        seen_car_numbers: set[str] = set()

        for raw_car_number in car_numbers_raw:
            try:
                car_number = normalize_car_number(raw_car_number)
            except FineValidationError as exc:
                errors.append((raw_car_number, exc.message))
                continue

            if car_number in seen_car_numbers:
                # Дубль внутри этого же сообщения — тихо пропускаем, уже
                # обработан (добавлен/учтён как ошибка) при первом появлении.
                continue
            seen_car_numbers.add(car_number)

            existing = self._task_repository.get_active_by_car_number(car_number)
            try:
                validate_no_overlap(start_date, end_date, existing)
            except FineValidationError:
                already_tracked += 1
                continue

            self._task_repository.create(
                car_number=car_number,
                label=None,
                start_date=start_date,
                end_date=end_date,
                telegram_chat_id=ctx.chat_id,
                created_by_user_id=ctx.user_id,
            )
            added += 1

        return CommandResult(text=self._format_bulk_result(added, already_tracked, errors))

    @staticmethod
    def _split_bulk_args(args: list[str]) -> tuple[str | None, str | None, list[str]]:
        """Первая строка "fine add bulk ..." — это args здесь. Если первые
        два токена — обе валидные даты, это общий период (START_DATE
        END_DATE), а всё остальное — номера. Иначе период не задан
        (используется значение по умолчанию), а все токены — номера."""
        if len(args) >= 2:
            try:
                parse_date(args[0])
                parse_date(args[1])
            except FineValidationError:
                pass
            else:
                return args[0], args[1], args[2:]

        return None, None, args

    @staticmethod
    def _format_bulk_result(
        added: int, already_tracked: int, errors: list[tuple[str, str]]
    ) -> str:
        lines = [
            f"✅ Добавлено: {added}",
            f"⚠️ Уже отслеживаются: {already_tracked}",
            f"❌ Ошибок: {len(errors)}",
        ]

        if errors:
            lines.append("")
            lines.append("Ошибки:")
            lines.extend(f"• {raw_car_number} — {message}" for raw_car_number, message in errors)

        return "\n".join(lines)

    def _handle_list(self) -> CommandResult:
        tasks = self._task_repository.list_active()
        if not tasks:
            return CommandResult(text="Активных задач мониторинга нет.")

        blocks = [self._format_task_line(task) for task in tasks]
        return CommandResult(text="\n\n".join(blocks))

    @staticmethod
    def _format_task_line(task: FineMonitoringTask) -> str:
        lines = [task.car_number + (f" ({task.label})" if task.label else "")]
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

        car_number = normalize_car_number(args[0])
        tasks = self._task_repository.get_active_by_car_number(car_number)
        if not tasks:
            raise CommandError(f"❌ Активная задача мониторинга для {car_number} не найдена")

        # validate_no_overlap (fine add) не даёт завести вторую активную
        # задачу с пересекающимся периодом для того же номера, но не
        # исключает две непересекающиеся по времени активные задачи —
        # останавливаем все, а не только первую попавшуюся.
        for task in tasks:
            self._task_repository.set_status(task.id, "stopped")

        return CommandResult(text=f"✅ Мониторинг для {car_number} остановлен")

    async def _handle_check(self, args: list[str]) -> CommandResult:
        if len(args) != 1:
            raise CommandError(_CHECK_USAGE_ERROR)

        car_number = normalize_car_number(args[0])
        tasks = self._task_repository.get_active_by_car_number(car_number)
        if not tasks:
            raise CommandError(f"❌ Активная задача мониторинга для {car_number} не найдена")

        total_fines_found = 0
        total_new_fines = 0
        total_duration_ms = 0

        for task in tasks:
            # Тот же FineCheckService, что использует и FineJob по расписанию —
            # никакой отдельной логики проверки здесь нет. Обычно у номера
            # ровно одна активная задача (см. комментарий в _handle_stop) —
            # цикл на случай, если их всё-таки несколько.
            result = await self._check_service.check_task(task)

            if result.status == "error":
                raise CommandError(f"❌ Ошибка проверки: {result.error_message}")

            total_fines_found += result.total_fines_found
            total_new_fines += len(result.new_fines)
            total_duration_ms += result.duration_ms

        # Тот же механизм доставки, что и у FineJob — тем же самым объектом
        # координатора, а не копией логики.
        await self._notification_coordinator.flush_pending()

        return CommandResult(
            text=(
                "✅ Проверка завершена\n\n"
                f"Автомобиль: {car_number}\n"
                f"Найдено штрафов: {total_fines_found}\n"
                f"Новых: {total_new_fines}\n"
                f"Время: {total_duration_ms} мс"
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
