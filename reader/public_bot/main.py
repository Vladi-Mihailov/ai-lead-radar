"""Bootstrap-процесс @GEShtrafbot — ОТДЕЛЬНЫЙ от reader/main.py (blast-radius
изоляция публичной поверхности, см. design report). Запускается только
вручную: `python -m reader.public_bot.main`, ни один существующий процесс
его не импортирует и не запускает автоматически.

Stage 4: бот + Add Car flow + "Мои авто" + 🔎 Проверить сейчас/⛔ Остановить
мониторинг + client delivery poller (owner/trusted_operator, см.
reader/public_bot/delivery_service.py). Этот процесс НИКОГДА не вызывает
FineNotificationCoordinator.flush_pending() — операторские уведомления
остаются исключительно в ai-lead-radar.service (см.
reader/jobs/notification_flush_job.py и design report Stage 4, раздел
"Immediate-check race handling") — здесь нет и не может быть операторской
Telegram-сессии для этого.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from telethon import TelegramClient  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.police_ge_provider import PoliceGeProvider  # noqa: E402
from reader.fines.police_ge_session import PoliceGeSession  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.logging_setup import setup_logging  # noqa: E402
from reader.public_bot.conversation import ConversationController  # noqa: E402
from reader.public_bot.conversation_state_repository import (  # noqa: E402
    BotConversationStateRepository,
)
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository  # noqa: E402
from reader.public_bot.delivery_service import ClientDeliveryService  # noqa: E402
from reader.public_bot.handlers import register  # noqa: E402
from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import SubscriptionService  # noqa: E402
from reader.settings import ConfigError, load_settings  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_BOT_USERNAME = "GEShtrafbot"
# Как часто client delivery poller проверяет, есть ли что доставить —
# независимо от bounded backoff отдельных доставок (см.
# reader/public_bot/delivery_service.py::RETRY_BACKOFF) — это просто как
# часто вообще опрашивать, не то же самое, что интервал повтора одной
# доставки.
_DELIVERY_POLL_INTERVAL_SECONDS = 180.0

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
# Отдельный .session-файл — своя, третья Telethon-сессия проекта (см.
# data/sessions/reader_live.session, reader_sync.session, reader_notifier.
# session): bot-mode-подключение @GEShtrafbot не делит ни credential, ни
# файл сессии ни с одним из них.
_SESSION_PATH = PROJECT_ROOT / "data" / "sessions" / "geshtrafbot"
_POLICE_GE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

logger = logging.getLogger(__name__)


class _TelethonBotSender:
    """Адаптер reader.public_bot.delivery_service.BotMessageSenderLike
    поверх уже подключённого bot-mode TelegramClient — второе подключение
    не создаётся."""

    def __init__(self, client: TelegramClient):
        self._client = client

    async def send_message(self, chat_id: int, text: str, *, buttons: list[list] | None = None) -> None:
        await self._client.send_message(chat_id, text, buttons=buttons)


def read_bot_token() -> str:
    """ТОЛЬКО из окружения (GESHTRAFBOT_TOKEN, см. .env.example) — никогда
    из config.yaml. Не сохраняется ни в Settings, ни в каком-либо другом
    долгоживущем объекте/атрибуте и никогда не логируется — возвращается
    один раз, как обычная локальная переменная, живущая ровно до передачи
    в TelegramClient.start(bot_token=...) в run()."""
    load_dotenv()
    token = os.getenv("GESHTRAFBOT_TOKEN")
    if not token:
        raise ConfigError(
            "GESHTRAFBOT_TOKEN не задан в .env — @GEShtrafbot не может быть запущен. "
            "См. .env.example."
        )
    return token


async def _run_delivery_poller(delivery_service: ClientDeliveryService) -> None:
    """Бесконечный цикл — тикает раз в _DELIVERY_POLL_INTERVAL_SECONDS,
    независимо от результата предыдущего тика (ошибка одного тика не
    останавливает поллер, см. design report: "failure одного recipient не
    блокирует другого" — то же самое верно и для тика целиком)."""
    while True:
        try:
            result = await delivery_service.run_once()
            if result.delivered or result.failed or result.flood_wait_hit:
                logger.info(
                    "Client delivery tick: delivered=%d, failed=%d, flood_wait=%s",
                    result.delivered, result.failed, result.flood_wait_hit,
                )
        except Exception:
            logger.exception("Client delivery poller: тик завершился с ошибкой")

        await asyncio.sleep(_DELIVERY_POLL_INTERVAL_SECONDS)


async def _run_concurrently(coroutines: list) -> None:
    """Как asyncio.gather, но при ошибке в одной корутине отменяет
    остальные — тот же приём, что и reader/main.py::_run_concurrently (не
    импортируется оттуда напрямую: это отдельный, самостоятельный процесс/
    entrypoint, а не библиотека)."""
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

    # api_id/api_hash — те же учётные данные Telegram-приложения, что и у
    # остального Reader (см. .env) — новых переменных окружения для них не
    # заводим, второе Telegram-приложение не регистрируем: bot-mode
    # Telethon требует api_id/api_hash даже с bot_token, но это не то же
    # самое, что "делить сессию" — .session-файл у бота свой (см. выше).
    missing = [name for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH") if not os.getenv(name)]
    if missing:
        raise ConfigError(
            "Не заданы переменные окружения: " + ", ".join(missing) + ". Заполните .env."
        )
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]

    _SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)

    db_path = settings.app.users_db_file
    task_repository = FineMonitoringTaskRepository(db_path)
    detected_fine_repository = DetectedFineRepository(db_path)
    user_repository = UserRepository(db_path)
    subscription_repository = FineSubscriptionRepository(db_path)
    conversation_state_repository = BotConversationStateRepository(db_path)
    known_users_repository = BotKnownUsersRepository(db_path)
    delivery_repository = ClientFineDeliveryRepository(db_path)

    http_client = httpx.AsyncClient(
        base_url="https://police.ge/protocol/",
        headers={"User-Agent": _POLICE_GE_USER_AGENT},
    )

    try:
        # Тот же PoliceGeSession/PoliceGeProvider/FineCheckService, что и у
        # операторского Fine Monitor (см. reader/main.py::
        # build_fine_monitor_components) — второй независимый провайдер
        # реализация не заводится, второй экземпляр нужен только потому, что
        # это отдельный ПРОЦЕСС, а не потому, что логика проверки другая.
        # Используется и для immediate-check в Add Car flow, и для
        # 🔎 Проверить сейчас — ни то, ни другое не обходит
        # FineCheckService/дедуп (см. design report Stage 4).
        police_ge_session = PoliceGeSession(
            http_client,
            page_url=settings.fine_monitor.source_url,
            request_timeout=settings.fine_monitor.request_timeout,
        )
        fine_provider = PoliceGeProvider(police_ge_session)
        check_service = FineCheckService(fine_provider, task_repository, detected_fine_repository)

        # client конструируется ДО SubscriptionService — тот же самый бот-
        # клиент передаётся туда как owner_resolver_client (см.
        # reader/public_bot/owner_resolution.py) и как отправитель для
        # client delivery poller'а (см. _TelethonBotSender ниже) —
        # client.start(bot_token=...) вызывается позже — на момент
        # конструирования достаточно самого объекта (тот же приём, что и
        # register(client, ...) ниже, тоже до start()).
        client = TelegramClient(str(_SESSION_PATH), api_id, api_hash)

        subscription_service = SubscriptionService(
            task_repository, subscription_repository, user_repository, check_service,
            owner_resolver_client=client, bot_username=_BOT_USERNAME,
        )
        controller = ConversationController(
            conversation_state_repository,
            subscription_service,
            tz=ZoneInfo(settings.fine_monitor.timezone),
            trusted_operator_user_ids=frozenset(settings.public_bot.trusted_operator_user_ids),
        )

        register(client, controller, known_users_repository)

        delivery_service = ClientDeliveryService(
            detected_fine_repository, subscription_repository, delivery_repository,
            _TelethonBotSender(client),
            tz=ZoneInfo(settings.fine_monitor.timezone),
            payment_help_contact_username=settings.public_bot.payment_help_contact_username,
        )

        # token передаётся ЗДЕСЬ и только здесь.
        await client.start(bot_token=token)
        logger.info("✔ @GEShtrafbot подключён")

        # Бот и client delivery poller работают в одном event loop'е,
        # параллельно, без второго Telegram-подключения (poller использует
        # ТОТ ЖЕ уже подключённый client через _TelethonBotSender). Ошибка
        # одного не должна тихо остановить другой незамеченной — тот же
        # принцип, что и в reader/main.py::run().
        await _run_concurrently([
            client.run_until_disconnected(),
            _run_delivery_poller(delivery_service),
        ])
    finally:
        await http_client.aclose()
        task_repository.close()
        detected_fine_repository.close()
        user_repository.close()
        subscription_repository.close()
        conversation_state_repository.close()
        known_users_repository.close()
        delivery_repository.close()


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
