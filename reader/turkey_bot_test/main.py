"""Bootstrap-процесс ЭКСПЕРИМЕНТАЛЬНОГО test-clone Turkey-бота (см. design
report "изолированный experimental clone Turkey bot" и "Перестроить UX
Turkey test bot") — @Get_8_Status_bot, ПОЛНОСТЬЮ ОТДЕЛЬНЫЙ процесс от
production Turkey-бота (@ProtocolTRbot, reader/turkey_bot/*,
ai-lead-radar-turkeybot.service), от reader/main.py и от
reader/public_bot/main.py.

UNIFIED UX (см. design report): USER -> CAR -> UNIFIED CHECK -> MONITORING.
GİB/Avrasya/KGM — внутренние providers (см.
reader/turkey_bot_test/unified/check_service.py), пользователь их больше
не выбирает. Мониторинг — calendar-based, СТРОГО 13:00 и 21:00
Europe/Istanbul (см. reader/turkey_bot_test/monitoring/scheduler_job.py) —
работает ВНУТРИ этого же процесса/systemd-юнита
(ai-lead-radar-turkeybot-test.service), отдельный systemd unit НЕ заводится
(см. design report решение п.2). Планировщик — reader.jobs.{Job,Scheduler}
(см. design report решение п.1 "Вариант A") — ТОЛЬКО импорт, reader/jobs/*
не изменён ни строкой.

Изоляция (см. design report п.16):
  - Telegram token — ТОЛЬКО TURKEYBOT_TEST_TOKEN (см. read_bot_token());
  - Telethon session — ОТДЕЛЬНЫЙ файл (data/sessions/turkey_bot_test);
  - SQLite — ОТДЕЛЬНЫЙ файл (data/turkey_bot_test.db), НЕ production
    data/users.db;
  - НИЧЕГО из reader/turkey_bot/*, reader/public_bot/*, reader/fines/* не
    импортируется (кроме reader/jobs/*, см. выше — bot-agnostic
    инфраструктура, явно разрешённая design report решением п.1/п.8).

Запускается только вручную/через systemd:
    python -m reader.turkey_bot_test.main"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from telethon import TelegramClient

from reader.jobs.scheduler import Scheduler
from reader.logging_setup import setup_logging
from reader.settings import ConfigError, load_settings
from reader.turkey_bot_test.conversation import ConversationController
from reader.turkey_bot_test.conversation_state_repository import (
    TurkeyConversationStateRepository,
)
from reader.turkey_bot_test.handlers import register
from reader.turkey_bot_test.known_users_repository import (
    TurkeyBotKnownUsersRepository,
)
from reader.turkey_bot_test.monitoring.monitoring_service import (
    TurkeyMonitoringService,
)
from reader.turkey_bot_test.monitoring.scheduler_job import (
    TurkeyMonitoringJob,
)
from reader.turkey_bot_test.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot_test.statistics_service import (
    TurkeyStatisticsService,
)
from reader.turkey_bot_test.unified.check_service import (
    UnifiedTurkeyCheckService,
    default_avrasya_check_factory,
    default_gib_check_factory,
    default_kgm_check_factory,
)
from reader.turkey_bot_test.unified.run_repository import (
    TurkeyCheckRunRepository,
)
from reader.turkey_bot_test.user_cars_repository import (
    TurkeyUserCarsRepository,
)

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
_SESSION_PATH = PROJECT_ROOT / "data" / "sessions" / "turkey_bot_test"
_DB_PATH = PROJECT_ROOT / "data" / "turkey_bot_test.db"

# Планировщик тикает раз в 30с (см. reader/jobs/scheduler.py) — тот же
# poll_interval, что и у reader/main.py (не Turkey-специфичная настройка).
_SCHEDULER_POLL_INTERVAL_SECONDS = 30.0

logger = logging.getLogger(__name__)


def read_bot_token() -> str:
    """ТОЛЬКО из окружения (TURKEYBOT_TEST_TOKEN) — НИКОГДА не читает и не
    fallback'ится на production TURKEYBOT_TOKEN."""
    load_dotenv()
    token = os.getenv("TURKEYBOT_TEST_TOKEN")
    if not token:
        raise ConfigError(
            "TURKEYBOT_TEST_TOKEN не задан — экспериментальный test-clone "
            "Turkey-бота (@Get_8_Status_bot) не может быть запущен. Задайте "
            "TURKEYBOT_TEST_TOKEN (например, в отдельном EnvironmentFile "
            "systemd-юнита ai-lead-radar-turkeybot-test.service) — "
            "production TURKEYBOT_TOKEN сюда не подходит и не используется."
        )
    return token


class _TelethonNotifier:
    """NotifierLike (см. reader/turkey_bot_test/monitoring/
    monitoring_service.py) поверх уже подключённого TelegramClient —
    тонкий адаптер, тот же приём, что и _TelethonBotSender в
    reader/public_bot/main.py (не импортируется оттуда — Turkey полностью
    отдельный процесс)."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def send(self, *, telegram_chat_id: int, text: str) -> None:
        await self._client.send_message(telegram_chat_id, text)


async def _run_concurrently(coroutines: list) -> None:
    """Как asyncio.gather, но при ошибке в одной корутине отменяет
    остальные — тот же приём, что и reader/main.py/reader/public_bot/
    main.py::_run_concurrently (не импортируется оттуда напрямую — Turkey
    остаётся полностью отдельным процессом/entrypoint)."""
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    for task in done:
        exc = task.exception()
        if exc is not None:
            raise exc


async def run() -> None:
    settings = load_settings(CONFIG_PATH)
    setup_logging(settings.app.log_level)

    token = read_bot_token()

    missing = [name for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH") if not os.getenv(name)]
    if missing:
        raise ConfigError(
            "Не заданы переменные окружения: " + ", ".join(missing) + ". Заполните .env."
        )
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]

    _SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)

    db_path = _DB_PATH
    conversation_state_repository = TurkeyConversationStateRepository(db_path)
    known_users_repository = TurkeyBotKnownUsersRepository(db_path)
    garage_repository = TurkeyUserCarsRepository(db_path)
    run_repository = TurkeyCheckRunRepository(db_path)
    subscription_repository = TurkeyMonitoringSubscriptionRepository(db_path)
    statistics_service = TurkeyStatisticsService(
        known_users_repository, run_repository, subscription_repository,
    )
    check_service = UnifiedTurkeyCheckService(
        default_gib_check_factory, default_avrasya_check_factory, default_kgm_check_factory,
    )

    try:
        controller = ConversationController(
            conversation_state_repository, garage_repository, run_repository,
            subscription_repository, statistics_service, check_service,
            trusted_operator_user_ids=frozenset(settings.public_bot.trusted_operator_user_ids),
            tz=ZoneInfo(settings.fine_monitor.timezone),
            payment_help_contact_username=settings.public_bot.payment_help_contact_username,
        )

        client = TelegramClient(str(_SESSION_PATH), api_id, api_hash)
        register(client, controller, known_users_repository)

        notifier = _TelethonNotifier(client)
        monitoring_service = TurkeyMonitoringService(
            check_service, run_repository, subscription_repository, notifier,
        )
        monitoring_job = TurkeyMonitoringJob(monitoring_service)
        scheduler = Scheduler([monitoring_job], poll_interval_seconds=_SCHEDULER_POLL_INTERVAL_SECONDS)

        await client.start(bot_token=token)
        me = await client.get_me()
        logger.info(f"✔ @{me.username} подключён (id={me.id})")
        logger.info("✔ Turkey monitoring scheduler запущен (13:00/21:00 Europe/Istanbul)")

        await _run_concurrently([
            client.run_until_disconnected(),
            scheduler.run_forever(),
        ])
    finally:
        conversation_state_repository.close()
        known_users_repository.close()
        garage_repository.close()
        run_repository.close()
        subscription_repository.close()


def main() -> None:
    try:
        asyncio.run(run())
    except ConfigError as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)


if __name__ == "__main__":
    main()
