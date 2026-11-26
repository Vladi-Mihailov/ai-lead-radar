"""Процесс MAX-бота Грузии — ОТДЕЛЬНЫЙ от Telegram-ботов и ai-lead-radar:

    python -m reader.max_bot.georgia.main

Ни один существующий процесс его не запускает и не импортирует. Плановый
мониторинг police.ge остаётся в ai-lead-radar (FineJob client_bot видит и
задачи, созданные из MAX); этот процесс делает только то, что делает и
Telegram-бот по запросу клиента: первичную проверку при добавлении,
«🔎 Проверить штрафы» (тот же провайдер: локально/через REDNET) и ручное
«🔄 Уточнить сумму» (Georgian Card, общие лимиты в БД). Автоматических
проверок сумм, ежедневной перепроверки и collector API здесь нет.

Доставка уведомлений MAX-клиентам — поллер раз в _DELIVERY_POLL_INTERVAL
(как у Telegram-бота).
"""

import asyncio
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx
from dotenv import load_dotenv

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.notification_control import GeorgiaNotificationControl
from reader.fines.payable_amount import PayableAmountRepository
from reader.fines.payment_status import FineStatusRepository, FineStatusTracker
from reader.fines.plate_bootstrap_repository import GeorgiaPlateBootstrapRepository
from reader.fines.police_ge_circuit_breaker import PoliceGeCircuitBreaker
from reader.fines.police_ge_session import PoliceGeSession
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.fines.translation import FineTranslationService
from reader.logging_setup import setup_logging
from reader.max_bot.api import MaxApi
from reader.max_bot.georgia import keyboards as ge_keyboards
from reader.max_bot.georgia.conversation import MaxGeorgiaBot
from reader.max_bot.georgia.delivery import MaxGeorgiaDelivery
from reader.max_bot.georgia.repository import MaxGeorgiaRepository
from reader.max_bot.georgia.service import MaxGeorgiaService
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
from reader.public_bot.main import (
    bootstrap_infrastructure_needed,
    build_check_now_provider,
    build_payable_amount_service,
    build_payment_page_markers,
)
from reader.settings import ConfigError, load_settings

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
_DELIVERY_POLL_INTERVAL_SECONDS = 180.0
_POLICE_GE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

logger = logging.getLogger(__name__)


class _NoClientLinkedCars:
    """PayableAmountService спрашивает Telegram-подписки только для
    автопроверок, которых в MAX-процессе нет."""

    def list_by_car_numbers(self, car_numbers):
        return []


async def run_delivery_poller(delivery, *, interval: float = _DELIVERY_POLL_INTERVAL_SECONDS) -> None:
    while True:
        try:
            result = await delivery.run_once()
            if result.delivered or result.failed:
                logger.info("MAX GE delivery tick: delivered=%d, failed=%d", result.delivered, result.failed)
        except Exception:
            logger.exception("MAX GE delivery: тик завершился с ошибкой")
        await asyncio.sleep(interval)


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
    config = load_max_bot_config("ge", os.environ)
    fine_monitor = settings.fine_monitor
    tz = ZoneInfo(fine_monitor.timezone)
    db_path = settings.app.users_db_file

    storage = MaxBotStorage(db_path, bot="ge")
    repository = MaxGeorgiaRepository(db_path)
    task_repository = FineMonitoringTaskRepository(db_path)
    detected_fine_repository = DetectedFineRepository(db_path)
    notification_control = GeorgiaNotificationControl(db_path)
    status_tracker = FineStatusTracker(
        FineStatusRepository(db_path),
        disappearance_min_gap=timedelta(hours=fine_monitor.fine_disappearance_min_gap_hours),
        markers=build_payment_page_markers(fine_monitor),
    )
    payable_repository = PayableAmountRepository(db_path) if fine_monitor.payable_amount_enabled else None
    plate_bootstrap = (
        GeorgiaPlateBootstrapRepository(db_path) if bootstrap_infrastructure_needed(fine_monitor) else None
    )
    telegram_readonly = TelegramReadOnly(db_path)

    police_http = httpx.AsyncClient(
        base_url="https://police.ge/protocol/", headers={"User-Agent": _POLICE_GE_USER_AGENT},
    )
    rednet_http = (
        httpx.AsyncClient(base_url=fine_monitor.collector_checknow_base_url)
        if fine_monitor.collector_api_enabled and fine_monitor.georgia_checknow_routing != "local"
        else None
    )
    api = MaxApi(config.token, base_url=config.api_base_url, verify=config.tls_verify)
    try:
        police_session = PoliceGeSession(
            police_http, page_url=fine_monitor.source_url, request_timeout=fine_monitor.request_timeout,
            circuit_breaker=PoliceGeCircuitBreaker(),
        )
        translator = (
            FineTranslationService(api_key=settings.ocr.openai_api_key, model=fine_monitor.translation_model)
            if settings.ocr.openai_api_key else None
        )
        check_service = FineCheckService(
            build_check_now_provider(fine_monitor, police_session, rednet_client=rednet_http),
            task_repository, detected_fine_repository, translator,
            status_tracker=status_tracker, dedupe_known_receipts=True,
        )
        payable_service = (
            build_payable_amount_service(
                fine_monitor, payable_repository, status_tracker, _NoClientLinkedCars(),
                trusted_user_ids=settings.public_bot.trusted_operator_user_ids,
            )
            if payable_repository is not None else None
        )
        service = MaxGeorgiaService(
            repository, task_repository, check_service, tz=tz, status_tracker=status_tracker,
            payable_amount=payable_service, plate_bootstrap=plate_bootstrap,
        )
        recovered = service.recover_pending_baselines()
        if recovered:
            logger.info("MAX GE: точка отсчёта восстановлена у %d подписок", recovered)

        payment_help_url = f"https://t.me/{settings.public_bot.payment_help_contact_username}"
        responder = MaxResponder(api, error_keyboard=ge_keyboards.back_to_menu())
        bot = MaxGeorgiaBot(
            service, storage, tz=tz, manager_user_ids=config.manager_user_ids,
            payment_help_url=payment_help_url, telegram_bot_username=config.telegram_bot_username,
            link_service=MaxTelegramLinkService(storage),
            telegram_cars_lookup=telegram_readonly.georgia_active_cars,
            telegram_subscriber_count=telegram_readonly.georgia_subscriber_count,
        )
        dispatcher = MaxDispatcher(storage, bot, responder)
        delivery = MaxGeorgiaDelivery(
            repository, detected_fine_repository, MaxNotificationSender(responder, storage),
            tz=tz, payment_help_url=payment_help_url, status_tracker=status_tracker,
            notification_control=notification_control,
            payable_repository=(
                payable_repository if fine_monitor.payable_amount_increase_notifications_enabled else None
            ),
            plate_bootstrap=plate_bootstrap,
        )

        me = await api.get_me()
        logger.info("✔ MAX GE бот подключён: @%s (id=%s), режим %s", me.get("username"), me.get("user_id"), config.mode)
        transport = (
            run_webhook_forever(
                build_webhook_app(dispatcher, secret=config.webhook_secret, path=config.webhook_path),
                host=config.webhook_host, port=config.webhook_port,
            )
            if config.mode == "webhook"
            else run_polling_forever(api, dispatcher)
        )
        await _run_concurrently([transport, dispatcher.run_forever(), run_delivery_poller(delivery)])
    finally:
        await api.close()
        await police_http.aclose()
        if rednet_http is not None:
            await rednet_http.aclose()
        telegram_readonly.close()
        storage.close()
        repository.close()


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
