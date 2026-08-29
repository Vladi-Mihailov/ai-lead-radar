"""FineCommand — операторский интерфейс уже готового мониторинга штрафов.

Ничего не решает сам: валидация вынесена в reader/fines/validation.py,
проверка — в FineCheckService (тот же самый объект, что использует
FineJob), доставка уведомлений — в FineNotificationCoordinator (тоже общий
с FineJob). Эта команда — только парсинг ввода/форматирование ответа.
"""

import logging
from datetime import date, datetime, timezone
from datetime import time as dt_time
from typing import NamedTuple, Protocol
from zoneinfo import ZoneInfo

from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError
from telethon.tl.types import User

from reader.commands.base import Command, CommandContext, CommandError, CommandResult
from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import CarFineStats, FineMonitoringTask
from reader.fines.notification_coordinator import (
    FineNotificationCoordinator,
    format_car_owner_display,
)
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.fines.validation import (
    FineValidationError,
    find_overlapping_task,
    normalize_car_number,
    parse_date,
    resolve_monitoring_period,
    validate_no_overlap,
)
from reader.jobs.fine_job import FineJob
from reader.jobs.scheduler import Scheduler
from reader.time_display import to_tbilisi
from reader.users.models import TelegramUserInfo

logger = logging.getLogger(__name__)

_DATE_FORMAT = "%d.%m.%Y"

_ADD_USAGE_ERROR = (
    "❌ Неверный формат команды\n\n"
    "Используйте:\n"
    "fine add B957MA09\n"
    "или\n"
    "fine add B957MA09 03.08.2026 13.08.2026\n"
    "или, чтобы сразу указать Telegram-владельца автомобиля:\n"
    "fine add B957MA09 @ivan_petrov"
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
_ADD_BULK_COMMAND_USAGE_ERROR = (
    "❌ Неверный формат команды\n\n"
    "Укажите хотя бы один номер автомобиля после fine add-bulk, каждый — "
    "на отдельной строке (также допускаются пробелы/запятые в качестве "
    "разделителя). Например:\n\n"
    "fine add-bulk\n"
    "A111AA111\n"
    "B222BB222\n"
    "C333CC333"
)
_UNKNOWN_SUBCOMMAND_ERROR = (
    "❌ Неверный формат команды\n\n"
    "Используйте:\n"
    "fine add | fine add-bulk | fine list | fine stop <НОМЕР_АВТОМОБИЛЯ> | "
    "fine check <НОМЕР_АВТОМОБИЛЯ> | fine update-all | fine status | fine stats"
)


class FineUserRepositoryLike(Protocol):
    """Ровно то, что нужно FineCommand от UserRepository
    (reader/users/repository.py) — не импортируем сам класс, тот же приём,
    что и UserLookupLike (reader/fines/notification_coordinator.py), но
    надмножество: fine add @username дополнительно должен резолвить
    username -> пользователь и привязывать car_number этому пользователю,
    чего fine check/fine add-bulk (только find_by_car_number) не требуют.

    upsert — нужен, чтобы завести пользователя, которого ещё нет в
    локальной БД, но которого удалось резолвить через Telegram (см.
    _resolve_and_store_new_user/задачу про баг "не найден в базе")."""

    def find_by_car_number(self, car_number: str) -> list[TelegramUserInfo]: ...
    def find_by_username(self, username: str) -> TelegramUserInfo | None: ...
    def add_car_numbers(self, user_id: int, car_numbers: list[str]) -> None: ...
    def upsert(self, user: TelegramUserInfo) -> None: ...


class TelegramUsernameResolverLike(Protocol):
    """Ровно то, что нужно FineCommand от TelegramClient — резолв
    @username через уже авторизованную Telethon-сессию (тот же клиент,
    что и у остального Reader, см. reader/main.py::
    build_fine_monitor_components — второе подключение не создаётся), для
    пользователей, которых ещё нет в локальной БД (см. задачу). None
    (значение по умолчанию) — резолв просто не выполняется, как и раньше:
    @username, которого нет в БД, считается не найденным без сетевого
    обращения (см. _resolve_car_owner_for_add)."""

    async def get_entity(self, entity): ...


class _OwnerLinkResult(NamedTuple):
    """Результат разбора необязательного "@username" в конце fine add —
    только чтение (find_by_username/find_by_car_number), ничего не пишет
    (см. _resolve_car_owner_for_add). Возвращается ТОЛЬКО в случае успеха
    (уже привязан этому же пользователю или ещё ни у кого не записан) —
    любой конфликт (username не найден, номер уже у другого/нескольких
    пользователей) — это CommandError, брошенный ДО task_repository.
    create(), а не часть этого результата: задача мониторинга не должна
    создаваться, если владелец не удалось однозначно определить (см.
    задачу)."""

    telegram_line: str  # "Telegram: ..." в ответе об успешном добавлении
    user_id_to_link: int | None  # кому вызвать add_car_numbers() — None,
    # если car_number уже есть у этого же пользователя (дублировать не нужно)


def _split_bulk_numbers(args: list[str]) -> list[str]:
    """args — уже разбитые диспетчером по ЛЮБЫМ пробельным символам токены
    (см. reader/commands/dispatcher.py: text.strip().split()) — то есть
    номера, каждый на отдельной строке многострочного Telegram-сообщения,
    сюда уже приходят отдельными элементами списка, точно так же, как если
    бы они были разделены пробелами. Дополнительно разбиваем каждый токен
    ещё и по запятым — на случай "A111AA111,B222BB222" в одной строке."""
    numbers: list[str] = []
    for token in args:
        for piece in token.split(","):
            piece = piece.strip()
            if piece:
                numbers.append(piece)
    return numbers


def _fmt_date(value: date) -> str:
    return value.strftime(_DATE_FORMAT)


def _fmt_datetime(value: datetime) -> str:
    """value — aware UTC datetime (см. FineJobStatus.last_run_at/
    last_success_at/last_error_at, все — datetime.now(timezone.utc)), либо
    naive-но-фактически-UTC (FineMonitoringTask.last_checked_at — читается
    из SQLite CURRENT_TIMESTAMP без offset, см. to_tbilisi()) — в обоих
    случаях конвертируется в Asia/Tbilisi перед показом оператору, формат
    отображения (дд.мм.гггг чч:мм) не меняется."""
    return to_tbilisi(value).strftime("%d.%m.%Y %H:%M")


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
        user_repository: FineUserRepositoryLike | None = None,
        telegram_client: TelegramUsernameResolverLike | None = None,
    ):
        self._task_repository = task_repository
        self._check_service = check_service
        self._notification_coordinator = notification_coordinator
        self._scheduler = scheduler
        self._fine_job = fine_job
        self._detected_fine_repository = detected_fine_repository
        self._run_times = run_times
        self._tz = tz
        # None — как и у FineNotificationCoordinator (см. её докстрок) —
        # "Telegram: ..." в результате fine check тогда покажет "не найден".
        self._user_repository = user_repository
        # None — резолв @username, которого нет в локальной БД, через
        # Telegram просто не выполняется (см. TelegramUsernameResolverLike
        # и _resolve_car_owner_for_add) — тот же приём, что и у
        # user_repository=None.
        self._telegram_client = telegram_client

    async def handle(self, ctx: CommandContext) -> CommandResult:
        if not ctx.args:
            raise CommandError(_UNKNOWN_SUBCOMMAND_ERROR)

        subcommand = ctx.args[0].lower()
        rest = ctx.args[1:]

        try:
            if subcommand == "add":
                return await self._handle_add(ctx, rest)
            if subcommand == "add-bulk":
                return await self._handle_add_bulk_command(ctx, rest)
            if subcommand == "list":
                return self._handle_list()
            if subcommand == "stop":
                return self._handle_stop(rest)
            if subcommand == "check":
                return await self._handle_check(rest)
            if subcommand == "update-all":
                return await self._handle_update_all(ctx)
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

        # Необязательный последний аргумент — "@username" Telegram-владельца
        # автомобиля (НЕ путать с created_by_user_id — тем, кто прислал эту
        # команду, см. ctx.user_id ниже, который не меняется). Явный "@" —
        # без него неоднозначность с датами, поэтому "без @" не принимается
        # (см. задачу).
        owner_username: str | None = None
        if args and args[-1].startswith("@"):
            owner_username = args[-1][1:]
            args = args[:-1]
            if not owner_username:
                raise CommandError(_ADD_USAGE_ERROR)

        if len(args) not in (1, 3):
            raise CommandError(_ADD_USAGE_ERROR)

        car_number = normalize_car_number(args[0])

        today = datetime.now(timezone.utc).astimezone(self._tz).date()
        start_raw, end_raw = (None, None) if len(args) == 1 else (args[1], args[2])
        start_date, end_date = resolve_monitoring_period(start_raw, end_raw, today=today)

        existing = self._task_repository.get_active_by_car_number(car_number)

        # Lookup @username ДО создания задачи — если пользователь не
        # найден, задача мониторинга вообще не создаётся (см. задачу: не
        # должно получиться состояния "task создан, а владелец — нет").
        # Намеренно ДО validate_no_overlap(): если номер уже на активном
        # мониторинге (период пересекается) И передан @username — это не
        # ошибка "уже добавлен", а сценарий "обогатить существующую запись
        # владельцем" (см. задачу и ветку ниже) — она должна успеть
        # определить владельца (найти/резолвнуть через Telegram/поймать
        # конфликт) ДО того, как validate_no_overlap возможно бросит
        # исключение "уже есть активная задача".
        owner_result: _OwnerLinkResult | None = None
        if owner_username is not None:
            owner_result = await self._resolve_car_owner_for_add(car_number, owner_username)

        if owner_result is not None and find_overlapping_task(start_date, end_date, existing) is not None:
            # Номер уже в активном мониторинге — вторую (дублирующую)
            # задачу НЕ создаём (см. задачу), существующие даты/настройки
            # этой активной задачи не трогаем вовсе. _resolve_car_owner_
            # for_add() выше уже сама разрешила все конфликтные исходы
            # (не найден/конфликт с другим пользователем — CommandError,
            # брошенный до этой строки) — здесь owner_result гарантированно
            # успешен: новая привязка либо уже существующая (идемпотентно).
            newly_linked = owner_result.user_id_to_link is not None
            if newly_linked:
                self._user_repository.add_car_numbers(owner_result.user_id_to_link, [car_number])
            return CommandResult(
                text=self._format_enrichment_result(car_number, owner_result, newly_linked=newly_linked)
            )

        validate_no_overlap(start_date, end_date, existing)

        task = self._task_repository.create(
            car_number=car_number,
            label=None,
            start_date=start_date,
            end_date=end_date,
            telegram_chat_id=ctx.chat_id,
            created_by_user_id=ctx.user_id,
        )

        if owner_result is not None and owner_result.user_id_to_link is not None:
            # self._user_repository гарантированно не None здесь — иначе
            # _resolve_car_owner_for_add() уже бросил бы CommandError выше.
            self._user_repository.add_car_numbers(owner_result.user_id_to_link, [car_number])

        lines = [
            "✅ Мониторинг штрафов добавлен",
            "",
            f"Автомобиль: {task.car_number}",
        ]
        if owner_result is not None:
            lines.append(f"Telegram: {owner_result.telegram_line}")
        lines.append(f"Период: {_fmt_date(start_date)}–{_fmt_date(end_date)}")
        lines.append(f"Проверка: {_format_check_times(self._run_times)} по Тбилиси")

        return CommandResult(text="\n".join(lines))

    @staticmethod
    def _format_enrichment_result(
        car_number: str, owner_result: _OwnerLinkResult, *, newly_linked: bool,
    ) -> str:
        """Ответ оператору для сценария "номер уже на активном мониторинге,
        владелец обогащён" (см. _handle_add) — БЕЗ повторного создания
        задачи мониторинга и без изменения её дат/настроек (см. задачу)."""
        if newly_linked:
            return (
                f"✔ Автомобиль {car_number} уже был на мониторинге.\n"
                f"Добавлен Telegram-пользователь {owner_result.telegram_line}."
            )
        return (
            f"✔ Автомобиль {car_number} уже на мониторинге и уже связан "
            f"с {owner_result.telegram_line}."
        )

    async def _resolve_car_owner_for_add(self, car_number: str, raw_username: str) -> _OwnerLinkResult:
        """Читает find_by_username/find_by_car_number, и (см. _resolve_and_
        store_new_user) может ЗАПИСАТЬ нового пользователя, если его ещё
        нет в локальной БД, но Telegram успешно резолвит username — тем не
        менее, для car_number/task_repository это по-прежнему только
        чтение: сам мониторинг создаётся ПОСЛЕ, в _handle_add.

        Бросает CommandError (и тогда задача мониторинга НЕ создаётся —
        см. _handle_add) в ЛЮБОМ случае, когда владельца нельзя однозначно
        определить: @username не существует в Telegram (или резолв не
        удался технически), номер уже привязан к ДРУГОМУ пользователю, или
        номер вообще неоднозначен (привязан к нескольким). Единственные
        исходы, при которых задача создаётся — car_number ещё ни у кого не
        записан, либо уже записан именно у запрошенного @username (см.
        задачу: вся pre-validation владельца должна произойти до создания
        task, без частичных состояний).

        Отсутствие @username в НАШЕЙ локальной БД само по себе больше НЕ
        ошибка (см. задачу про баг "не найден в базе") — прежде чем
        признать пользователя не найденным, делается попытка резолва через
        уже авторизованную Telethon-сессию (см. _resolve_and_store_new_user)."""
        owner = self._user_repository.find_by_username(raw_username) if self._user_repository else None

        if owner is None and self._user_repository is not None and self._telegram_client is not None:
            owner = await self._resolve_and_store_new_user(raw_username)

        if owner is None:
            raise CommandError(
                f"❌ Telegram-пользователь @{raw_username} не найден.\n\n"
                "Автомобиль не добавлен."
            )

        existing_owners = self._user_repository.find_by_car_number(car_number)

        if not existing_owners:
            # Ещё ни у кого не записан — привязываем запрошенному пользователю.
            return _OwnerLinkResult(format_car_owner_display([owner]), owner.user_id)

        if len(existing_owners) == 1 and existing_owners[0].user_id == owner.user_id:
            # Уже привязан именно ему — успех без дублей, повторно писать
            # car_numbers не нужно (add_car_numbers и так идемпотентен, но
            # тут даже вызывать незачем).
            return _OwnerLinkResult(format_car_owner_display([owner]), None)

        if len(existing_owners) == 1:
            # Автоматически связь НЕ перезаписываем (см. задачу — потенциально
            # опасный конфликт, независимо от того, это новый номер или уже
            # существующая запись мониторинга, которую пытались обогатить).
            raise CommandError(
                f"⚠️ Автомобиль {car_number} уже связан с "
                f"{format_car_owner_display(existing_owners)}.\n"
                f"Новый пользователь @{raw_username} не установлен."
            )

        # Несколько пользователей уже имеют этот номер в car_numbers —
        # неоднозначно, молча выбирать "победителя" нельзя (см. задачу).
        raise CommandError(
            f"⚠️ Автомобиль {car_number} уже связан с несколькими Telegram-пользователями "
            "— связь неоднозначна и требует ручной проверки.\n\n"
            "Автомобиль не добавлен."
        )

    async def _resolve_and_store_new_user(self, raw_username: str) -> TelegramUserInfo | None:
        """@raw_username отсутствует в локальной БД (см. вызывающий код —
        _resolve_car_owner_for_add) — пробуем резолвить его через уже
        авторизованную Telethon-сессию (self._telegram_client, тот же
        клиент, что и у остального Reader) и, если успешно, сразу завести
        запись в users тем же UserRepository.upsert(), которым уже
        пользуются reader/sources/telegram_source.py, reader/users/sync.py
        и reader/users/history_sync.py — без второй, дублирующей реализации
        маппинга Telegram entity -> TelegramUserInfo (см.
        TelegramUserInfo.from_telethon_user).

        Возвращает None, если Telegram подтверждённо не знает такого
        username (UsernameNotOccupiedError/UsernameInvalidError — синтаксис
        неверный или юзернейм никем не занят) либо резолвнутая entity — не
        обычный пользователь (например, это оказался канал/чат, а не
        User) — тогда вызывающий код сформирует финальное "не найден".

        Любая ДРУГАЯ ошибка (сеть, FloodWait, прочий сбой RPC) — это
        CommandError с отдельным, явно техническим текстом, пойманный ДО
        task_repository.create() (см. _handle_add: lookup происходит до
        создания задачи) — так что частично созданной задачи мониторинга
        без владельца не остаётся ни в одном из исходов."""
        try:
            entity = await self._telegram_client.get_entity(f"@{raw_username}")
        except (UsernameNotOccupiedError, UsernameInvalidError, ValueError):
            return None
        except Exception as exc:
            raise CommandError(
                f"❌ Не удалось проверить Telegram-пользователя @{raw_username} "
                "(техническая ошибка при обращении к Telegram).\n\n"
                "Автомобиль не добавлен."
            ) from exc

        if not isinstance(entity, User):
            return None

        self._user_repository.upsert(TelegramUserInfo.from_telethon_user(entity))

        # Race: между find_by_username() (в _resolve_car_owner_for_add) и
        # этим upsert() тот же пользователь мог уже появиться в БД (другим
        # путём, см. reader/core/pipeline.py/history_sync.py) — upsert()
        # идемпотентен (INSERT ... ON CONFLICT DO UPDATE, см.
        # reader/users/repository.py), дубля не возникает; читаем заново, а
        # не строим _OwnerLinkResult из entity напрямую, чтобы в любом
        # случае вернуть ИМЕННО то, что реально сохранено в БД.
        return self._user_repository.find_by_username(raw_username)

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

    async def _handle_add_bulk_command(self, ctx: CommandContext, args: list[str]) -> CommandResult:
        """fine add-bulk — один номер на строку (см. _split_bulk_numbers для
        точного разбора многострочного/через-запятую сообщения).

        Для каждого номера буквально вызывает self._handle_add(ctx,
        [raw_car_number]) — тот же самый метод, что обрабатывает одиночный
        "fine add NUMBER", без изменений и без параллельной реализации
        валидации/создания задачи. normalize_car_number() вызывается здесь
        ЕЩЁ РАЗ отдельно (это чистая, уже существующая функция из
        reader/fines/validation.py, а не копия логики _handle_add) только
        чтобы отличить "номер некорректен" от "у _handle_add() нет иной
        причины для FineValidationError, кроме пересечения с уже активной
        задачей" — иначе эти два случая неразличимы по одному только типу
        исключения."""
        car_numbers = _split_bulk_numbers(args)
        if not car_numbers:
            raise CommandError(_ADD_BULK_COMMAND_USAGE_ERROR)

        added = 0
        already_tracked = 0
        invalid: list[tuple[str, str]] = []
        errors: list[tuple[str, str]] = []

        for raw_car_number in car_numbers:
            try:
                normalize_car_number(raw_car_number)
            except FineValidationError as exc:
                invalid.append((raw_car_number, exc.message))
                continue

            try:
                await self._handle_add(ctx, [raw_car_number])
            except FineValidationError:
                # normalize_car_number() для этого номера уже прошла выше —
                # единственная причина, по которой _handle_add() всё же
                # бросает FineValidationError для уже нормализуемого
                # номера, это validate_no_overlap() (номер уже в
                # мониторинге с пересекающимся периодом).
                already_tracked += 1
                continue
            except Exception as exc:
                # Один плохой номер (например, сбой БД именно на этой
                # записи) не должен прерывать остальные — импорт
                # продолжается независимо для каждого номера.
                errors.append((raw_car_number, str(exc)))
                logger.exception("fine add-bulk: не удалось добавить номер %s", raw_car_number)
                continue

            added += 1

        return CommandResult(
            text=self._format_add_bulk_command_result(added, already_tracked, invalid, errors)
        )

    @staticmethod
    def _format_add_bulk_command_result(
        added: int,
        already_tracked: int,
        invalid: list[tuple[str, str]],
        errors: list[tuple[str, str]],
    ) -> str:
        lines = [
            f"Добавлено: {added}",
            f"Уже в мониторинге: {already_tracked}",
            f"Некорректных: {len(invalid)}",
            f"Ошибок: {len(errors)}",
        ]

        if invalid:
            lines.append("")
            lines.append("Некорректные номера:")
            lines.extend(f"• {raw} — {message}" for raw, message in invalid)

        if errors:
            lines.append("")
            lines.append("Ошибки:")
            lines.extend(f"• {raw} — {message}" for raw, message in errors)

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

        # car_number -> users.car_numbers -> UserRepository.find_by_car_number() —
        # Telegram-ВЛАДЕЛЕЦ автомобиля, а не тот, кто создал задачу
        # мониторинга (fine_monitoring_tasks.created_by_user_id — другой
        # человек, см. докстрок _car_owner_display и задачу про
        # production-баг). Тот же формат/fallback, что и в уведомлении о
        # новом штрафе (см. FineNotificationCoordinator).
        telegram_display = self._car_owner_display(car_number)

        return CommandResult(
            text=(
                "✅ Проверка завершена\n\n"
                f"Автомобиль: {car_number}\n"
                f"Telegram: {telegram_display}\n"
                f"Найдено штрафов: {total_fines_found}\n"
                f"Новых: {total_new_fines}\n"
                f"Время: {total_duration_ms} мс"
            )
        )

    def _car_owner_display(self, car_number: str) -> str:
        if self._user_repository is None:
            return format_car_owner_display([])

        users = self._user_repository.find_by_car_number(car_number)
        if len(users) > 1:
            logger.warning(
                "По номеру %s найдено несколько Telegram-пользователей: user_id=%s",
                car_number, [user.user_id for user in users],
            )
        return format_car_owner_display(users)

    async def _handle_update_all(self, ctx: CommandContext) -> CommandResult:
        """fine update-all — берёт все активные задачи мониторинга и
        последовательно (без параллелизма, как и FineJob) прогоняет каждую
        через self._check_service.check_task() — тот же самый метод,
        которым для одного автомобиля пользуется _handle_check() и по
        расписанию FineJob.run(); никакой отдельной бизнес-логики проверки
        здесь нет. Ошибка одной задачи не останавливает обработку
        остальных."""
        tasks = self._task_repository.list_active()

        if ctx.event is not None:
            # Единственное промежуточное сообщение — старт. Прогресс по
            # каждому отдельному автомобилю в чат не шлём: на сотни
            # активных задач это были бы сотни сообщений подряд.
            await ctx.event.respond(f"🔄 Запущена проверка {len(tasks)} автомобилей")

        checked = 0
        new_fines_total = 0
        errors: list[tuple[str, str]] = []

        for task in tasks:
            try:
                result = await self._check_service.check_task(task)
            except Exception as exc:
                errors.append((task.car_number, str(exc)))
                logger.exception(
                    "fine update-all: проверка задачи id=%s (%s) завершилась с ошибкой",
                    task.id, task.car_number,
                )
                continue

            if result.status == "error":
                errors.append((task.car_number, result.error_message or "неизвестная ошибка"))
                continue

            checked += 1
            new_fines_total += len(result.new_fines)

        try:
            # Тот же механизм доставки, что и у fine check/FineJob — тем же
            # самым объектом координатора, один раз в конце всего прохода.
            await self._notification_coordinator.flush_pending()
        except Exception as exc:
            errors.append(("(уведомления)", str(exc)))
            logger.exception("fine update-all: отправка накопленных уведомлений завершилась с ошибкой")

        lines = [
            "✅ Массовая проверка завершена",
            f"Всего: {len(tasks)}",
            f"Проверено: {checked}",
            f"Новые штрафы: {new_fines_total}",
            f"Ошибок: {len(errors)}",
        ]
        if errors:
            lines.append("")
            lines.append("Ошибки:")
            lines.extend(f"• {car_number} — {message}" for car_number, message in errors)

        return CommandResult(text="\n".join(lines))

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
