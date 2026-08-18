import asyncio
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

from reader.checkout.lock_repository import CheckoutLockRepository  # noqa: E402
from reader.checkout.payment_gateway import (  # noqa: E402
    CardSecrets,
    CardSecretsError,
    PlaywrightBankGatewayClient,
    PlaywrightBrowserLauncher,
)
from reader.checkout.personal_info import OcrPersonalInfoProvider  # noqa: E402
from reader.checkout.reference_data import TplReferenceDataClient  # noqa: E402
from reader.checkout.service import CheckoutService  # noqa: E402
from reader.checkout.telegram_integration import CheckoutReplyHandler  # noqa: E402
from reader.checkout.tpl_client import TplGeClient  # noqa: E402
from reader.commands.album_collector import AlbumCollector  # noqa: E402
from reader.commands.dispatcher import CommandDispatcher  # noqa: E402
from reader.commands.fine import FineCommand  # noqa: E402
from reader.commands.insurance_ocr import InsuranceOcrCommand  # noqa: E402
from reader.core.engine import MatchEngine  # noqa: E402
from reader.core.pipeline import Pipeline  # noqa: E402
from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.notification_coordinator import FineNotificationCoordinator  # noqa: E402
from reader.fines.police_ge_provider import PoliceGeProvider  # noqa: E402
from reader.fines.police_ge_session import PoliceGeSession  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.groups import GroupLoadError, load_groups  # noqa: E402
from reader.jobs.archive_fine_job import ArchiveFineJob  # noqa: E402
from reader.jobs.fine_job import FineJob  # noqa: E402
from reader.jobs.scheduler import Scheduler  # noqa: E402
from reader.lead_ai.service import LeadAiService  # noqa: E402
from reader.logging_setup import setup_logging  # noqa: E402
from reader.notifications.telegram_notification_service import (  # noqa: E402
    TelegramNotificationService,
)
from reader.ocr.service import OcrService  # noqa: E402
from reader.scenarios import KeywordMatcher, ScenarioLoadError, load_scenarios  # noqa: E402
from reader.settings import ConfigError, Settings, load_settings  # noqa: E402
from reader.sinks.console_sink import ConsoleSink  # noqa: E402
from reader.sinks.file_sink import FileSink  # noqa: E402
from reader.sinks.lead_ai_sink import LeadAiSink  # noqa: E402
from reader.sinks.telegram_sink import TelegramSink  # noqa: E402
from reader.sources.telegram_source import TelegramSource  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

logger = logging.getLogger(__name__)

_SCHEDULER_POLL_INTERVAL_SECONDS = 30.0
_POLICE_GE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def resolve_notification_chat_ids(settings: Settings) -> list[int | str]:
    """fine_monitor.notification_chat_ids, а если пусто — тот же чат(ы), куда
    уже уходят лиды (app.lead_forward_to / LEAD_FORWARD_TO)."""
    return settings.fine_monitor.notification_chat_ids or settings.app.lead_forward_to


def resolve_allowed_user_ids(settings: Settings) -> list[int]:
    """fine_monitor.allowed_user_ids как есть, без автофоллбэка: пустой
    список — штатное значение этой функции, отклонять его — забота
    validate_fine_monitor_config(), а не этой функции (см. её докстрок)."""
    return settings.fine_monitor.allowed_user_ids


def build_fine_monitor_components(
    settings: Settings,
    source: TelegramSource,
    task_repository: FineMonitoringTaskRepository,
    detected_fine_repository: DetectedFineRepository,
    http_client: httpx.AsyncClient,
    notification_chat_ids: list[int | str],
    allowed_user_ids: list[int],
    user_repository: UserRepository | None = None,
) -> tuple[FineJob, Scheduler, TelegramNotificationService, CommandDispatcher, FineCommand]:
    """Чистая сборка зависимостей мониторинга штрафов — без единого await,
    без обращения к сети/Telegram (конструкторы ничего не подключают, только
    сохраняют переданное). Вынесено отдельно от _run_fine_monitor именно
    для того, чтобы это можно было проверить unit/integration-тестом без
    реального HTTP и Telegram (см. test_main_wiring.py).

    user_repository — тот же самый UserRepository, что уже открыт в run()
    для Pipeline/TelegramSource (settings.app.users_db_file) — второе
    соединение с users.db не открывается. Нужен только для того, чтобы
    уведомление о новом штрафе показывало Telegram-пользователя, добавившего
    машину в мониторинг (см. FineNotificationCoordinator); None — тоже
    рабочий вариант (уведомление тогда покажет "Telegram: ID N")."""
    fine_monitor = settings.fine_monitor
    tz = ZoneInfo(fine_monitor.timezone)

    notification_service = TelegramNotificationService(
        source.client, notification_chat_ids, fine_monitor.source_url
    )
    notification_coordinator = FineNotificationCoordinator(
        detected_fine_repository, task_repository, notification_service, user_repository
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
        archive_check_enabled=fine_monitor.archive_check_enabled,
        archive_check_hour=fine_monitor.archive_check_hour,
        archive_interval_days=fine_monitor.archive_interval_days,
    )
    # Второй, независимый job — архивные проверки (см. докстрок
    # ArchiveFineJob) для задач, у которых обычный период уже закончился.
    # Тот же task_repository/check_service/notification_coordinator, что и
    # у fine_job — ни второго обращения к police.ge, ни второго notification
    # flow не создаётся.
    archive_fine_job = ArchiveFineJob(
        task_repository=task_repository,
        check_service=check_service,
        notification_coordinator=notification_coordinator,
        enabled=fine_monitor.archive_check_enabled,
        hour=fine_monitor.archive_check_hour,
        interval_days=fine_monitor.archive_interval_days,
        daily_limit=fine_monitor.archive_daily_limit,
        tz=tz,
    )
    scheduler = Scheduler(
        [fine_job, archive_fine_job], poll_interval_seconds=_SCHEDULER_POLL_INTERVAL_SECONDS
    )

    command_dispatcher = CommandDispatcher(
        source.client, notification_chat_ids[0], allowed_user_ids
    )
    fine_command = FineCommand(
        task_repository=task_repository,
        check_service=check_service,
        notification_coordinator=notification_coordinator,
        scheduler=scheduler,
        fine_job=fine_job,
        detected_fine_repository=detected_fine_repository,
        run_times=fine_monitor.check_times,
        tz=tz,
        user_repository=user_repository,
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
            "fine_monitor.allowed_user_ids не настроен.\n"
            "Добавьте Telegram User ID операторов, которым разрешено выполнять команды fine."
        )


async def _run_fine_monitor(
    settings: Settings,
    source: TelegramSource,
    task_repository: FineMonitoringTaskRepository,
    detected_fine_repository: DetectedFineRepository,
    user_repository: UserRepository | None = None,
) -> None:
    """Композиция мониторинга штрафов: PoliceGeSession/Provider →
    FineCheckService → FineJob → Scheduler, плюс TelegramNotificationService
    и CommandDispatcher с зарегистрированным FineCommand — на том же
    Telegram-клиенте, что и основной Reader."""
    notification_chat_ids = resolve_notification_chat_ids(settings)
    validate_fine_monitor_config(settings, notification_chat_ids)

    await source.wait_until_ready()

    allowed_user_ids = resolve_allowed_user_ids(settings)

    http_client = httpx.AsyncClient(
        base_url="https://police.ge/protocol/",
        headers={"User-Agent": _POLICE_GE_USER_AGENT},
    )
    try:
        _fine_job, scheduler, notification_service, command_dispatcher, fine_command = (
            build_fine_monitor_components(
                settings, source, task_repository, detected_fine_repository,
                http_client, notification_chat_ids, allowed_user_ids,
                user_repository=user_repository,
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
        logger.info(
            "Archive check: %s (hour=%02d:00, interval=%d days, daily_limit=%d)",
            "enabled" if fine_monitor.archive_check_enabled else "disabled",
            fine_monitor.archive_check_hour,
            fine_monitor.archive_interval_days,
            fine_monitor.archive_daily_limit,
        )

        await scheduler.run_forever()
    finally:
        await http_client.aclose()


def build_insurance_ocr_components(
    settings: Settings, source: TelegramSource,
) -> tuple[CommandDispatcher, InsuranceOcrCommand, AlbumCollector]:
    """Чистая сборка зависимостей "insurance ocr" — без единого await, без
    обращения к сети/Telegram (см. build_fine_monitor_components — тот же
    приём, ради тестируемости без реального Telegram/OpenAI, см.
    test_main_wiring.py). Полностью независима от fine_monitor: свой
    CommandDispatcher (свой chat_id), свой OcrService.

    Допуск к самому OCR не ограничен конкретным отправителем (см. задачу:
    "любой участник настроенного OCR-чата") — restrict_to_allowed_users=False
    только для ЭТОГО инстанса CommandDispatcher, поэтому проверка для
    fine-команды (отдельный инстанс, см. build_fine_monitor_components)
    не затрагивается. Допуск к checkout reply/pay/OTP (см.
    build_checkout_components) тоже больше НЕ проверяет ocr.allowed_user_ids
    (production показал, что sender_id вне этого списка молча игнорировался
    даже в правильном чате) — единственное оставшееся применение
    ocr.allowed_user_ids во всём OCR/checkout flow это стартовый sanity-гейт
    ниже (is_checkout_configured/"ocr.allowed_user_ids пуст — команда не
    поднята") и передача в конструктор CommandDispatcher (где она ни на что
    не влияет при restrict_to_allowed_users=False) — оставлены, чтобы
    случайно пустой список не включал сервис молча, а не как авторизация."""
    ocr = settings.ocr
    ocr_service = OcrService(api_key=ocr.openai_api_key, model=ocr.vision_model)
    insurance_command = InsuranceOcrCommand(
        ocr_service,
        default_email=settings.checkout.email,
        default_phone=settings.checkout.phone,
    )
    command_dispatcher = CommandDispatcher(
        source.client, ocr.service_chat_id, ocr.allowed_user_ids, restrict_to_allowed_users=False,
    )
    album_collector = AlbumCollector(
        on_group_ready=insurance_command.handle_album,
        debounce_seconds=ocr.album_debounce_seconds,
    )
    return command_dispatcher, insurance_command, album_collector


def build_lead_ai_sink(settings: Settings, source: TelegramSource) -> LeadAiSink | None:
    """Чистая сборка LeadAiSink — без единого await, без обращения к
    сети/Telegram (тот же приём, что и build_insurance_ocr_components, ради
    тестируемости, см. test_main_wiring.py). None, если lead_ai.enabled=false
    ЛИБО recipient/OPENAI_API_KEY не заданы — тогда run() просто не
    добавляет этот sink в список, и pipeline/остальные sinks (в т.ч.
    TelegramSink для @ali_na_l_i/@alenaogir/@alena_ogi) ведут себя ровно
    так же, как до этой задачи (см. reader/settings.py::LeadAiSettings).

    openai_api_key берётся из settings.ocr (см. LeadAiSettings docstring) —
    второй OPENAI_API_KEY не заводится."""
    lead_ai = settings.lead_ai
    if not lead_ai.enabled:
        return None
    if not lead_ai.recipient or not settings.ocr.openai_api_key:
        logger.warning(
            "lead_ai.enabled=true, но lead_ai.recipient/OPENAI_API_KEY не заданы — "
            "AI-анализ лидов не поднят."
        )
        return None

    service = LeadAiService(api_key=settings.ocr.openai_api_key, model=lead_ai.model)
    return LeadAiSink(source.client, lead_ai.recipient, service)


def is_checkout_configured(settings: Settings) -> bool:
    """checkout.phone и checkout.email оба обязательны (см.
    reader/settings.py::CheckoutSettings) — как и с "insurance ocr", их
    отсутствие означает "функциональность не поднята", а не ошибку запуска.
    payment_bank/policy_period больше НЕ участвуют в этом решении — это
    поля конкретной Telegram-заявки (см. reader/ocr/models.py::OcrResult),
    а не runtime-конфигурация."""
    return bool(settings.checkout.phone and settings.checkout.email)


def build_checkout_components(
    settings: Settings,
    source: TelegramSource,
    http_client: httpx.AsyncClient,
    *,
    card_secrets: CardSecrets | None = None,
) -> CheckoutReplyHandler:
    """Чистая сборка зависимостей checkout tpl.ge (см. reader/checkout/) —
    без единого await, тот же приём, что и build_insurance_ocr_components.
    Работает в ТОМ ЖЕ служебном чате, что и "insurance ocr" (см.
    reader/settings.py::CheckoutSettings) — отдельного chat_id для checkout
    нет. Допуск к reply/pay/OTP — любой участник этого чата (см.
    reader/checkout/telegram_integration.py::CheckoutReplyHandler,
    ocr.allowed_user_ids здесь больше НЕ используется как authorization) —
    ownership конкретного checkout при этом определяется фактическим
    sender_id "pay", см. CheckoutState.operator_user_id.

    card_secrets — если не передан явно, читается из окружения
    (CardSecrets.load(), см. reader/checkout/payment_gateway.py) — параметр
    существует ради тестируемости (см. test_main_wiring.py), в реальном
    запуске (_run_insurance_ocr) не передаётся."""
    checkout = settings.checkout
    tpl_client = TplGeClient(http_client)
    reference_data = TplReferenceDataClient(http_client)
    # phone/email гарантированно заданы здесь (см. reader/settings.py::
    # load_settings — fail-fast, если одно из них задано без другого).
    personal_info_provider = OcrPersonalInfoProvider(
        reference_data=reference_data, phone=checkout.phone, email=checkout.email,
    )
    card_secrets = card_secrets if card_secrets is not None else CardSecrets.load()
    bank_gateway = PlaywrightBankGatewayClient(
        launcher=PlaywrightBrowserLauncher(), card_secrets=card_secrets,
    )
    lock_repository = CheckoutLockRepository(settings.app.users_db_file)
    checkout_service = CheckoutService(
        tpl_client=tpl_client,
        reference_data=reference_data,
        lock_repository=lock_repository,
        personal_info_provider=personal_info_provider,
        bank_gateway=bank_gateway,
    )
    return CheckoutReplyHandler(checkout_service=checkout_service)


async def _run_insurance_ocr(settings: Settings, source: TelegramSource) -> None:
    """"insurance ocr" — свой CommandDispatcher (свой служебный чат, не
    Fine Monitor, см. задачу) плюс AlbumCollector, зарегистрированный ещё
    одним независимым NewMessage-handler'ом на том же чате (см.
    reader/commands/album_collector.py), плюс (если настроен, см.
    is_checkout_configured) CheckoutReplyHandler (см. reader/checkout/) —
    ещё один независимый NewMessage-handler на том же чате, реагирующий на
    reply "pay"/исправленными полями на "Распознано: ..." (см. задачу про
    checkout tpl.ge). На том же Telegram-клиенте, что и основной Reader,
    второе подключение не создаётся.

    Никогда не завершается сама по себе (await asyncio.Event().wait() —
    вся работа происходит в зарегистрированных event handler'ах), как и
    _run_fine_monitor()'s scheduler.run_forever()."""
    await source.wait_until_ready()

    command_dispatcher, insurance_command, album_collector = build_insurance_ocr_components(
        settings, source,
    )
    command_dispatcher.register(insurance_command)

    await command_dispatcher.start()
    await album_collector.start(source.client, settings.ocr.service_chat_id)

    logger.info("Insurance OCR command enabled (chat=%s)", settings.ocr.service_chat_id)

    checkout_http_client: httpx.AsyncClient | None = None
    try:
        if is_checkout_configured(settings):
            try:
                card_secrets = CardSecrets.load()
            except CardSecretsError as exc:
                # Fail-fast, но только для checkout — не роняем весь Reader
                # (тот же приём, что и с пустым ocr.allowed_user_ids). str(exc)
                # безопасен для лога — перечисляет только ИМЕНА переменных
                # окружения, никогда их значения (см.
                # reader/checkout/payment_gateway.py::CardSecrets).
                logger.warning("Checkout tpl.ge disabled: %s", exc)
                card_secrets = None

            if card_secrets is not None:
                checkout_http_client = httpx.AsyncClient()
                checkout_handler = build_checkout_components(
                    settings, source, checkout_http_client, card_secrets=card_secrets,
                )
                await checkout_handler.start(source.client, settings.ocr.service_chat_id)
                # Банк/период — теперь поля конкретной Telegram-заявки (см.
                # reader/ocr/models.py::OcrResult), не runtime-настройки —
                # в этом логе больше нечего показывать про них.
                logger.info("Checkout tpl.ge enabled")
        else:
            logger.info(
                "Checkout tpl.ge disabled (checkout.phone/email не заданы)"
            )

        await asyncio.Event().wait()
    finally:
        if checkout_http_client is not None:
            await checkout_http_client.aclose()


async def _run_concurrently(coroutines: list) -> None:
    """Как asyncio.gather, но при ошибке в одной корутине отменяет
    остальные, а не оставляет их работать дальше (или висеть навсегда:
    например, если Pipeline.run() падает до TelegramSource.start() успевает
    выставить готовность, _run_fine_monitor() иначе завис бы на
    source.wait_until_ready() бесконечно)."""
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

    # Добавлен ПОСЛЕ TelegramSink — к моменту, когда этот sink начинает
    # AI-анализ, оригинальный лид уже доставлен всем настроенным
    # получателям (в т.ч. lead_ai.recipient), см. reader/sinks/lead_ai_sink.py.
    lead_ai_sink = build_lead_ai_sink(settings, source)
    if lead_ai_sink is not None:
        sinks.append(lead_ai_sink)
        logger.info("Lead AI анализ включён (получатель=%s)", settings.lead_ai.recipient)
    else:
        logger.info("Lead AI анализ отключён (lead_ai.enabled=false либо не настроен)")

    pipeline = Pipeline(source, engine, sinks, user_repository)

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
            _run_fine_monitor(
                settings, source, fine_task_repository, detected_fine_repository,
                user_repository,
            )
        )
    else:
        logger.info("Fine monitor disabled (fine_monitor.enabled=false)")

    if settings.ocr.service_chat_id and settings.ocr.openai_api_key:
        if settings.ocr.allowed_user_ids:
            background.append(_run_insurance_ocr(settings, source))
        else:
            logger.warning(
                "Insurance OCR: ocr.service_chat_id/OPENAI_API_KEY заданы, но "
                "ocr.allowed_user_ids пуст — команда не поднята (иначе ей не "
                "смог бы воспользоваться ни один оператор)."
            )
    else:
        logger.info(
            "Insurance OCR disabled (ocr.service_chat_id/OPENAI_API_KEY не заданы)"
        )

    try:
        await _run_concurrently(background)
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
