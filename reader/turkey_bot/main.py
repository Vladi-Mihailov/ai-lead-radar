"""Bootstrap-процесс production Turkey-бота (@ProtocolTRbot) — ПОЛНОСТЬЮ
ОТДЕЛЬНЫЙ standalone-процесс от reader/main.py и от reader/public_bot/
main.py (не изменилось). Запускается только вручную/через systemd (см.
deploy/ai-lead-radar-turkeybot.service):

    python -m reader.turkey_bot.main

ПЕРЕНЕСЕНО из reader/turkey_bot_test/main.py (см. задачу "Перенос Unified
Turkey функционала в production", READ-ONLY аудит) — UNIFIED UX: USER ->
CAR -> UNIFIED CHECK -> MONITORING, calendar-based scheduler СТРОГО 13:00
и 21:00 Europe/Istanbul, работает ВНУТРИ этого же процесса/systemd-юнита
(ai-lead-radar-turkeybot.service) — отдельный systemd unit НЕ заводится.
Планировщик — reader.jobs.{Job,Scheduler} (bot-agnostic инфраструктура,
уже используется reader/main.py, НЕ изменена ни строкой).

ОТЛИЧИЯ от reader/turkey_bot_test/main.py (production identity, см. задачу
п.2 — НИКОГДА не смешивается с test-ботом):
  - token — ТОЛЬКО TURKEYBOT_TOKEN (см. read_bot_token());
  - Telethon session — ОТДЕЛЬНЫЙ файл (data/sessions/turkeybot), тот же,
    что и раньше — НЕ создаётся заново, старая сессия продолжает работать;
  - SQLite — settings.app.users_db_file (ТОТ ЖЕ физический файл, что и у
    Георгия-бота, см. design report исходного Turkey-бота: "Единственная
    разделяемая с Георгией-ботом вещь — тот же физический SQLite-файл, но
    ТОЛЬКО Turkey-специфичные таблицы") — НЕ отдельный data/turkey_bot_test.db,
    это критично для production migration (см. scripts/migrate_turkey_unified.py):
    новые unified/monitoring-таблицы обязаны жить в ТОМ ЖЕ файле, что и
    существующие turkey_bot_user_cars/turkey_bot_known_users/
    turkey_fine_checks/turkey_toll_checks, иначе backfill не увидит
    исторические данные;
  - GİB/Avrasya/KGM providers — reader.turkey_bot.{gib,avrasya,kgm}.*
    (НЕ reader.turkey_bot_test.*, см. reader/turkey_bot/unified/
    check_service.py — эти модули byte-identical своим test-аналогам, см.
    READ-ONLY аудит, поэтому НЕ дублируются повторно);
  - GIB Russian translation (TurkeyFineTranslationService) — ПОДКЛЮЧЕНА
    (см. задачу п.4: READ-ONLY аудит обнаружил, что test unified-flow эту
    существовавшую в production фичу потерял; здесь она передаётся в
    UnifiedTurkeyCheckService как gib_translator, см.
    reader/turkey_bot/unified/check_service.py) — test-бот сам НЕ
    затрагивается, его main.py не переносит и не подключает translator;
  - trusted_operator_user_ids/payment_help_contact_username/tz — ТЕ ЖЕ
    production-настройки, что были у старого reader/turkey_bot/main.py
    (settings.public_bot.*, settings.fine_monitor.timezone) — НЕ менялись.

НИЧЕГО из reader/turkey_bot_test/* не импортируется (см. задачу п.2/п.12)."""

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
from reader.turkey_bot.conversation import ConversationController
from reader.turkey_bot.conversation_state_repository import (
    TurkeyConversationStateRepository,
)
from reader.turkey_bot.gib.translation import TurkeyFineTranslationService
from reader.turkey_bot.handlers import register
from reader.turkey_bot.known_users_repository import (
    TurkeyBotKnownUsersRepository,
)
from reader.turkey_bot.monitoring.monitoring_service import (
    TurkeyMonitoringService,
)
from reader.turkey_bot.monitoring.scheduler_job import (
    TurkeyMonitoringJob,
    TurkeyMonitoringRetryJob,
)
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.statistics_service import TurkeyStatisticsService
from reader.turkey_bot.unified.check_service import (
    UnifiedTurkeyCheckService,
    default_avrasya_check_factory,
    default_gib_check_factory,
    default_kgm_check_factory,
)
from reader.turkey_bot.unified.run_repository import (
    TurkeyCheckRunRepository,
)
from reader.turkey_bot.user_cars_repository import (
    TurkeyUserCarsRepository,
)

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
# Отдельный .session-файл — та же production-сессия, что и раньше (см.
# deploy/ai-lead-radar-turkeybot.service) — НЕ переносится/не заменяется.
_SESSION_PATH = PROJECT_ROOT / "data" / "sessions" / "turkeybot"

# Планировщик тикает раз в 30с (см. reader/jobs/scheduler.py) — тот же
# poll_interval, что и у reader/main.py и у test-бота.
_SCHEDULER_POLL_INTERVAL_SECONDS = 30.0

logger = logging.getLogger(__name__)


def read_bot_token() -> str:
    """ТОЛЬКО из окружения (TURKEYBOT_TOKEN, см. .env.example) — никогда из
    config.yaml, никогда не логируется. НЕ путать с TURKEYBOT_TEST_TOKEN
    (test-clone бота, reader/turkey_bot_test/main.py) — эта функция его не
    читает и не может прочитать (другое имя переменной)."""
    load_dotenv()
    token = os.getenv("TURKEYBOT_TOKEN")
    if not token:
        raise ConfigError(
            "TURKEYBOT_TOKEN не задан в .env — Turkey-бот не может быть запущен. "
            "См. .env.example."
        )
    return token


class _TelethonNotifier:
    """NotifierLike (см. reader/turkey_bot/monitoring/monitoring_service.py)
    поверх уже подключённого TelegramClient — тот же адаптер, что и в
    reader/turkey_bot_test/main.py."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def send(self, *, telegram_chat_id: int, text: str) -> None:
        await self._client.send_message(telegram_chat_id, text)


async def _run_concurrently(coroutines: list) -> None:
    """Как asyncio.gather, но при ошибке в одной корутине отменяет
    остальные — тот же приём, что и в reader/turkey_bot_test/main.py."""
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

    # ТОТ ЖЕ физический файл, что и всегда (settings.app.users_db_file =
    # data/users.db) — см. модуль docstring про то, почему это критично
    # для production migration/backfill.
    db_path = settings.app.users_db_file
    conversation_state_repository = TurkeyConversationStateRepository(db_path)
    known_users_repository = TurkeyBotKnownUsersRepository(db_path)
    garage_repository = TurkeyUserCarsRepository(db_path)
    run_repository = TurkeyCheckRunRepository(db_path)
    subscription_repository = TurkeyMonitoringSubscriptionRepository(db_path)
    statistics_service = TurkeyStatisticsService(
        known_users_repository, run_repository, subscription_repository,
    )

    # Тот же общий OPENAI_API_KEY (settings.ocr.openai_api_key) и
    # translation_model, что и у Георгии/у старого reader/turkey_bot/
    # conversation.py — второй ключ/секрет не заводится. None означает
    # "перевод недоступен" (нет ключа), не ошибку запуска: клиент увидит
    # оригинальный турецкий текст (см. reader/turkey_bot/unified/
    # check_service.py::_translate_gib_fines, тот же fail-open принцип,
    # что был у старого _translate_fines).
    translator = (
        TurkeyFineTranslationService(
            api_key=settings.ocr.openai_api_key, model=settings.fine_monitor.translation_model,
        )
        if settings.ocr.openai_api_key
        else None
    )
    if translator is not None:
        logger.info(f"✔ Turkey fine translator включён (model={settings.fine_monitor.translation_model})")
    else:
        logger.info("Turkey fine translator выключен (OPENAI_API_KEY не задан)")

    check_service = UnifiedTurkeyCheckService(
        default_gib_check_factory, default_avrasya_check_factory, default_kgm_check_factory,
        gib_translator=translator,
    )

    try:
        controller = ConversationController(
            conversation_state_repository, garage_repository, run_repository,
            subscription_repository, statistics_service, check_service,
            # ТА ЖЕ настройка, что и раньше (settings.public_bot.
            # trusted_operator_user_ids) — production manager
            # configuration НЕ менялась.
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
        # См. задачу "Retry orchestration Unified Turkey checks" п.3 —
        # ОТДЕЛЬНЫЙ Job, тикает вместе с monitoring_job на том же
        # Scheduler'е, обрабатывает +5-минутные CAPTCHA-retry, поставленные
        # в очередь TurkeyMonitoringService (см. monitoring_service.py).
        monitoring_retry_job = TurkeyMonitoringRetryJob(monitoring_service)
        scheduler = Scheduler(
            [monitoring_job, monitoring_retry_job], poll_interval_seconds=_SCHEDULER_POLL_INTERVAL_SECONDS,
        )

        # token передаётся ЗДЕСЬ и только здесь.
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
