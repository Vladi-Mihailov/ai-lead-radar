"""Процесс MAX-бота Турции — ОТДЕЛЬНЫЙ от Telegram-ботов и ai-lead-radar:

    python -m reader.max_bot.turkey.main

Ни один существующий процесс его не запускает и не импортирует. Проверки —
тот же UnifiedTurkeyCheckService, что у Telegram-бота Турции (GİB, Avrasya,
KGM; KGM через relay, если он настроен в окружении — тот же
kgm_check_factory_from_env, поэтому юниту нужен тот же drop-in с переменными
relay, что у turkeybot). Плановый мониторинг MAX-подписчиков — 22:30
Europe/Istanbul, после прогона Telegram-бота, без повторного сбора номеров,
которые Telegram уже проверил (см. reader/max_bot/turkey/monitoring.py).
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from reader.jobs.scheduler import Scheduler
from reader.logging_setup import setup_logging
from reader.max_bot.api import MaxApi
from reader.max_bot.linking import MaxTelegramLinkService
from reader.max_bot.runtime import (
    MaxDispatcher,
    MaxNotificationSender,
    MaxResponder,
    build_webhook_app,
    run_polling_forever,
    run_webhook_forever,
)
from reader.max_bot.settings import MaxConfigError, load_max_bot_config
from reader.max_bot.storage import MaxBotStorage
from reader.max_bot.telegram_readonly import TelegramReadOnly
from reader.max_bot.turkey import keyboards as tr_keyboards
from reader.max_bot.turkey.conversation import MaxTurkeyBot
from reader.max_bot.turkey.monitoring import MaxTurkeyMonitoring, MaxTurkeyMonitoringJob
from reader.max_bot.turkey.repository import MaxTurkeyRepository
from reader.max_bot.turkey.service import MaxTurkeyService
from reader.settings import ConfigError, load_settings
from reader.turkey_bot.gib.translation import TurkeyFineTranslationService
from reader.turkey_bot.kgm.route import kgm_check_factory_from_env
from reader.turkey_bot.unified.check_service import (
    UnifiedTurkeyCheckService,
    default_avrasya_check_factory,
    default_gib_check_factory,
    default_kgm_check_factory,
)
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
_SCHEDULER_POLL_INTERVAL_SECONDS = 30.0

logger = logging.getLogger(__name__)


async def _run_concurrently(coroutines: list) -> None:
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
    # httpx пишет URL запросов в INFO — там user_id получателей MAX.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv()
    config = load_max_bot_config("tr", os.environ)
    db_path = settings.app.users_db_file

    storage = MaxBotStorage(db_path, bot="tr")
    repository = MaxTurkeyRepository(db_path)
    run_repository = TurkeyCheckRunRepository(db_path)
    telegram_readonly = TelegramReadOnly(db_path)

    translator = (
        TurkeyFineTranslationService(
            api_key=settings.ocr.openai_api_key, model=settings.fine_monitor.translation_model,
        )
        if settings.ocr.openai_api_key
        else None
    )
    check_service = UnifiedTurkeyCheckService(
        default_gib_check_factory, default_avrasya_check_factory,
        kgm_check_factory_from_env(os.environ, direct_factory=default_kgm_check_factory),
        gib_translator=translator,
    )
    api = MaxApi(config.token, base_url=config.api_base_url, verify=config.tls_verify)
    try:
        service = MaxTurkeyService(repository, run_repository, check_service)
        responder = MaxResponder(api, error_keyboard=tr_keyboards.back_to_menu())
        bot = MaxTurkeyBot(
            service, storage, manager_user_ids=config.manager_user_ids,
            payment_help_url=f"https://t.me/{settings.public_bot.payment_help_contact_username}",
            telegram_bot_username=config.telegram_bot_username,
            link_service=MaxTelegramLinkService(storage),
            telegram_cars_lookup=telegram_readonly.turkey_cars,
            telegram_subscriber_count=telegram_readonly.turkey_subscriber_count,
        )
        dispatcher = MaxDispatcher(storage, bot, responder)
        monitoring = MaxTurkeyMonitoring(
            repository, run_repository, check_service, MaxNotificationSender(responder, storage),
        )
        scheduler = Scheduler(
            [MaxTurkeyMonitoringJob(
                monitoring, telegram_last_finished_at=repository.last_telegram_monitoring_finished_at,
            )],
            poll_interval_seconds=_SCHEDULER_POLL_INTERVAL_SECONDS,
        )

        me = await api.get_me()
        logger.info("✔ MAX TR бот подключён: @%s (id=%s), режим %s", me.get("username"), me.get("user_id"), config.mode)
        logger.info("✔ MAX TR monitoring: 22:30 Europe/Istanbul")
        transport = (
            run_webhook_forever(
                build_webhook_app(dispatcher, secret=config.webhook_secret, path=config.webhook_path),
                host=config.webhook_host, port=config.webhook_port,
            )
            if config.mode == "webhook"
            else run_polling_forever(api, dispatcher)
        )
        await _run_concurrently([transport, dispatcher.run_forever(), scheduler.run_forever()])
    finally:
        await api.close()
        telegram_readonly.close()
        storage.close()
        repository.close()
        run_repository.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (ConfigError, MaxConfigError) as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)


if __name__ == "__main__":
    main()
