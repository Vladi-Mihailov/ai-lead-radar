import asyncio
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

from reader.commands.dispatcher import CommandDispatcher  # noqa: E402
from reader.commands.fine import FineCommand  # noqa: E402
from reader.core.engine import MatchEngine  # noqa: E402
from reader.core.pipeline import Pipeline  # noqa: E402
from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.notification_coordinator import FineNotificationCoordinator  # noqa: E402
from reader.fines.police_ge_provider import PoliceGeProvider  # noqa: E402
from reader.fines.police_ge_session import PoliceGeSession  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.groups import GroupLoadError, load_groups  # noqa: E402
from reader.jobs.fine_job import FineJob  # noqa: E402
from reader.jobs.scheduler import Scheduler  # noqa: E402
from reader.logging_setup import setup_logging  # noqa: E402
from reader.notifications.telegram_notification_service import (  # noqa: E402
    TelegramNotificationService,
)
from reader.scenarios import KeywordMatcher, ScenarioLoadError, load_scenarios  # noqa: E402
from reader.settings import ConfigError, Settings, load_settings  # noqa: E402
from reader.sinks.console_sink import ConsoleSink  # noqa: E402
from reader.sinks.file_sink import FileSink  # noqa: E402
from reader.sinks.telegram_sink import TelegramSink  # noqa: E402
from reader.sources.telegram_source import TelegramSource  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

logger = logging.getLogger(__name__)

_CLIENT_READY_POLL_SECONDS = 0.5
_SCHEDULER_POLL_INTERVAL_SECONDS = 30.0
_POLICE_GE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


async def _wait_until_client_ready(client) -> None:
    """TelegramSource.start() (внутри Pipeline.run()) авторизует клиент —
    ждём этого моста без единого изменения в TelegramSource/Pipeline, просто
    опросом уже существующего Telethon-метода."""
    while not await client.is_user_authorized():
        await asyncio.sleep(_CLIENT_READY_POLL_SECONDS)


def resolve_notification_chat_ids(settings: Settings) -> list[int | str]:
    """fine_monitor.notification_chat_ids, а если пусто — тот же чат(ы), куда
    уже уходят лиды (app.lead_forward_to / LEAD_FORWARD_TO)."""
    return settings.fine_monitor.notification_chat_ids or settings.app.lead_forward_to


def build_fine_monitor_components(
    settings: Settings,
    source: TelegramSource,
    task_repository: FineMonitoringTaskRepository,
    detected_fine_repository: DetectedFineRepository,
    http_client: httpx.AsyncClient,
    notification_chat_ids: list[int | str],
) -> tuple[FineJob, Scheduler, TelegramNotificationService, CommandDispatcher, FineCommand]:
    """Чистая сборка зависимостей мониторинга штрафов — без единого await,
    без обращения к сети/Telegram (конструкторы ничего не подключают, только
    сохраняют переданное). Вынесено отдельно от _run_fine_monitor именно
    для того, чтобы это можно было проверить unit/integration-тестом без
    реального HTTP и Telegram (см. test_main_wiring.py)."""
    fine_monitor = settings.fine_monitor
    tz = ZoneInfo(fine_monitor.timezone)

    notification_service = TelegramNotificationService(
        source.client, notification_chat_ids, fine_monitor.source_url
    )
    notification_coordinator = FineNotificationCoordinator(
        detected_fine_repository, task_repository, notification_service
    )

    police_ge_session = PoliceGeSession(
        http_client,
        page_url=fine_monitor.source_url,
        request_timeout=fine_monitor.request_timeout,
    )
    fine_provider = PoliceGeProvider(police_ge_session)
    check_service = FineCheckService(fine_provider, task_repository, detected_fine_repository)

    fine_job = FineJob(
        task_repository=task_repository,
        check_service=check_service,
        notification_coordinator=notification_coordinator,
        run_times=fine_monitor.check_times,
        tz=tz,
    )
    scheduler = Scheduler([fine_job], poll_interval_seconds=_SCHEDULER_POLL_INTERVAL_SECONDS)

    command_dispatcher = CommandDispatcher(
        source.client, notification_chat_ids[0], fine_monitor.allowed_user_ids
    )
    fine_command = FineCommand(
        task_repository=task_repository,
        check_service=check_service,
        notification_coordinator=notification_coordinator,
        scheduler=scheduler,
        fine_job=fine_job,
        run_times=fine_monitor.check_times,
        tz=tz,
    )

    return fine_job, scheduler, notification_service, command_dispatcher, fine_command


def validate_fine_monitor_config(settings: Settings, notification_chat_ids: list[int | str]) -> None:
    """Fail-fast проверки перед запуском мониторинга штрафов — вынесены в
    отдельную синхронную функцию, чтобы их можно было проверить тестом без
    реального Telegram-клиента (см. test_main_wiring.py)."""
    if not notification_chat_ids:
        raise ConfigError(
            "fine_monitor.enabled=true, но не задан ни fine_monitor.notification_chat_ids, "
            "ни app.lead_forward_to (LEAD_FORWARD_TO) — некуда отправлять уведомления и "
            "не в каком чате слушать команды"
        )

    if not settings.fine_monitor.allowed_user_ids:
        raise ConfigError(
            "fine_monitor.enabled=true, но fine_monitor.allowed_user_ids пуст — команды "
            "fine ... были бы доступны любому пользователю в чате, отказываюсь запускаться"
        )


async def _run_fine_monitor(
    settings: Settings,
    source: TelegramSource,
    task_repository: FineMonitoringTaskRepository,
    detected_fine_repository: DetectedFineRepository,
) -> None:
    """Композиция мониторинга штрафов: PoliceGeSession/Provider →
    FineCheckService → FineJob → Scheduler, плюс TelegramNotificationService
    и CommandDispatcher с зарегистрированным FineCommand — на том же
    Telegram-клиенте, что и основной Reader."""
    notification_chat_ids = resolve_notification_chat_ids(settings)
    validate_fine_monitor_config(settings, notification_chat_ids)

    await _wait_until_client_ready(source.client)

    http_client = httpx.AsyncClient(
        base_url="https://police.ge/protocol/",
        headers={"User-Agent": _POLICE_GE_USER_AGENT},
    )
    try:
        _fine_job, scheduler, notification_service, command_dispatcher, fine_command = (
            build_fine_monitor_components(
                settings, source, task_repository, detected_fine_repository,
                http_client, notification_chat_ids,
            )
        )

        command_dispatcher.register(fine_command)

        await notification_service.start()
        await command_dispatcher.start()

        fine_monitor = settings.fine_monitor
        logger.info("Fine monitor enabled")
        logger.info("Scheduler started")
        logger.info(
            "Configured run times: %s",
            ", ".join(t.strftime("%H:%M") for t in fine_monitor.check_times),
        )
        logger.info("Timezone: %s", fine_monitor.timezone)
        logger.info(
            "Notification chats: %s",
            ", ".join(str(chat_id) for chat_id in notification_chat_ids),
        )

        await scheduler.run_forever()
    finally:
        await http_client.aclose()


async def run() -> None:
    settings = load_settings(CONFIG_PATH)
    setup_logging(settings.app.log_level)
    logger.info("Конфигурация загружена из %s", CONFIG_PATH)
    logger.info(
        "Using Telegram session: %s (%s)",
        settings.telegram.session_path_live.name,
        settings.telegram.session_path_live,
    )

    groups = load_groups(settings.app.groups_file)
    scenarios = load_scenarios(settings.app.scenarios_file)
    logger.info("Загружено групп: %d, сценариев: %d", len(groups), len(scenarios))

    matcher = KeywordMatcher(scenarios)
    engine = MatchEngine(matcher)

    user_repository = UserRepository(settings.app.users_db_file)
    source = TelegramSource(
        settings.telegram,
        groups,
        user_repository,
        debug_events=settings.app.debug_telegram_events,
    )
    sinks = [
        ConsoleSink(),
        FileSink(settings.app.leads_output_file),
    ]
    if settings.app.lead_forward_to:
        sinks.append(TelegramSink(source.client, settings.app.lead_forward_to))
        logger.info("Пересылка лидов включена, чатов: %d", len(settings.app.lead_forward_to))

    pipeline = Pipeline(source, engine, sinks)

    # Reader (Pipeline.run() — авторизует и подключает единственный
    # TelegramClient) и мониторинг штрафов работают в одном event loop'е,
    # параллельно, без второго подключения к Telegram.
    background: list = [pipeline.run()]

    fine_task_repository = None
    detected_fine_repository = None
    if settings.fine_monitor.enabled:
        fine_task_repository = FineMonitoringTaskRepository(settings.app.users_db_file)
        detected_fine_repository = DetectedFineRepository(settings.app.users_db_file)
        background.append(
            _run_fine_monitor(settings, source, fine_task_repository, detected_fine_repository)
        )
    else:
        logger.info("Fine monitor disabled (fine_monitor.enabled=false)")

    try:
        await asyncio.gather(*background)
    finally:
        user_repository.close()
        if fine_task_repository is not None:
            fine_task_repository.close()
        if detected_fine_repository is not None:
            detected_fine_repository.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (ConfigError, GroupLoadError, ScenarioLoadError, RuntimeError) as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)


if __name__ == "__main__":
    main()
