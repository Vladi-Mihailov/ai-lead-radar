"""Bootstrap-процесс @GEShtrafbot — ОТДЕЛЬНЫЙ от reader/main.py (blast-radius
изоляция публичной поверхности, см. design report). Запускается только
вручную: `python -m reader.public_bot.main`, ни один существующий процесс
его не импортирует и не запускает автоматически.

Stage 2: сам бот + полный Add Car flow + "Мои авто". Никакого ClientFineJob,
delivery poller или фоновых клиентских уведомлений здесь нет (см. Stage 2
report) — только на запрос пользователя (Add Car flow) и immediate check
сразу после него, тем же самым FineCheckService, что и у операторского
Fine Monitor.
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
from reader.public_bot.handlers import register  # noqa: E402
from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import SubscriptionService  # noqa: E402
from reader.settings import ConfigError, load_settings  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_BOT_USERNAME = "GEShtrafbot"

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
        police_ge_session = PoliceGeSession(
            http_client,
            page_url=settings.fine_monitor.source_url,
            request_timeout=settings.fine_monitor.request_timeout,
        )
        fine_provider = PoliceGeProvider(police_ge_session)
        check_service = FineCheckService(fine_provider, task_repository, detected_fine_repository)

        # client конструируется ДО SubscriptionService — тот же самый бот-
        # клиент передаётся туда как owner_resolver_client (см.
        # reader/public_bot/owner_resolution.py): резолв @username
        # trusted-operator delegated flow использует бота, а не отдельное
        # подключение. client.start(bot_token=...) вызывается позже — на
        # момент конструирования достаточно самого объекта (тот же приём,
        # что и register(client, ...) ниже, которое тоже происходит до start()).
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

        # token передаётся ЗДЕСЬ и только здесь.
        await client.start(bot_token=token)
        logger.info("✔ @GEShtrafbot подключён")

        await client.run_until_disconnected()
    finally:
        await http_client.aclose()
        task_repository.close()
        detected_fine_repository.close()
        user_repository.close()
        subscription_repository.close()
        conversation_state_repository.close()
        known_users_repository.close()


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
