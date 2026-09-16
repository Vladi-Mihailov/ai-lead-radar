"""Bootstrap-процесс Turkey-бота — ПОЛНОСТЬЮ ОТДЕЛЬНЫЙ standalone-процесс от
reader/main.py И от reader/public_bot/main.py (см. design report Stage 1:
"новый, отдельный Telegram-бот, не расширение существующего Георгия-бота").
Запускается только вручную/через systemd (см.
deploy/ai-lead-radar-turkeybot.service):

    python -m reader.turkey_bot.main

Turkey — ОДНОРАЗОВАЯ проверка (см. design report): здесь нет и не должно
быть ни FineMonitoringTaskRepository/FineSubscriptionRepository/
DetectedFineRepository, ни какого-либо scheduler/job — ничего из
reader/fines/* или reader/public_bot/* сюда не импортируется. Единственная
разделяемая с Георгией-ботом вещь — тот же физический SQLite-файл
(settings.app.users_db_file), но ТОЛЬКО Turkey-специфичные таблицы (см.
reader/turkey_bot/conversation_state_repository.py,
known_users_repository.py, check_repository.py) — ни одна Георгия-таблица
здесь не читается и не пишется.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402
from telethon import TelegramClient  # noqa: E402

from reader.logging_setup import setup_logging  # noqa: E402
from reader.settings import ConfigError, load_settings  # noqa: E402
from reader.turkey_bot.avrasya.live_session_registry import (  # noqa: E402
    LiveAvrasyaSessionRegistry,
)
from reader.turkey_bot.check_repository import TurkeyCheckRepository  # noqa: E402
from reader.turkey_bot.conversation import ConversationController  # noqa: E402
from reader.turkey_bot.conversation_state_repository import (  # noqa: E402
    TurkeyConversationStateRepository,
)
from reader.turkey_bot.gib.translation import TurkeyFineTranslationService  # noqa: E402
from reader.turkey_bot.handlers import register  # noqa: E402
from reader.turkey_bot.known_users_repository import (
    TurkeyBotKnownUsersRepository,  # noqa: E402
)
from reader.turkey_bot.live_session_registry import LiveGibSessionRegistry  # noqa: E402
from reader.turkey_bot.statistics_service import TurkeyStatisticsService  # noqa: E402
from reader.turkey_bot.toll_check_repository import (
    TurkeyTollCheckRepository,  # noqa: E402
)
from reader.turkey_bot.user_cars_repository import (
    TurkeyUserCarsRepository,  # noqa: E402
)

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
# Отдельный .session-файл — своя, независимая Telethon-сессия (см.
# reader/public_bot/main.py про тот же приём) — ни с одним другим процессом
# проекта не делится ни credential, ни файл сессии.
_SESSION_PATH = PROJECT_ROOT / "data" / "sessions" / "turkeybot"

logger = logging.getLogger(__name__)


def read_bot_token() -> str:
    """ТОЛЬКО из окружения (TURKEYBOT_TOKEN, см. .env.example) — никогда из
    config.yaml, никогда не логируется (тот же приём, что и
    reader/public_bot/main.py::read_bot_token)."""
    load_dotenv()
    token = os.getenv("TURKEYBOT_TOKEN")
    if not token:
        raise ConfigError(
            "TURKEYBOT_TOKEN не задан в .env — Turkey-бот не может быть запущен. "
            "См. .env.example."
        )
    return token


async def run() -> None:
    settings = load_settings(CONFIG_PATH)
    setup_logging(settings.app.log_level)

    token = read_bot_token()

    # api_id/api_hash — то же самое Telegram-приложение, что и у остального
    # Reader (см. .env) — второе приложение не регистрируется, только
    # третий bot-mode client с собственным .session-файлом (см. выше).
    missing = [name for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH") if not os.getenv(name)]
    if missing:
        raise ConfigError(
            "Не заданы переменные окружения: " + ", ".join(missing) + ". Заполните .env."
        )
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]

    _SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)

    db_path = settings.app.users_db_file
    conversation_state_repository = TurkeyConversationStateRepository(db_path)
    known_users_repository = TurkeyBotKnownUsersRepository(db_path)
    check_repository = TurkeyCheckRepository(db_path)
    # "Гараж" (см. design report: НЕ мониторинг/подписка — только чтобы не
    # заставлять пользователя вводить номер заново) — та же схема
    # additive-таблицы, что и у остальных Turkey-репозиториев.
    garage_repository = TurkeyUserCarsRepository(db_path)
    statistics_service = TurkeyStatisticsService(known_users_repository, check_repository)
    # Avrasya Tüneli (см. design report Stage 2B) — ОТДЕЛЬНАЯ, additive
    # таблица (НЕ trukey_fine_checks) и ОТДЕЛЬНЫЙ, но структурно
    # идентичный in-memory реестр живых сессий (см.
    # reader/turkey_bot/avrasya/live_session_registry.py) — тот же принцип
    # "живая HTTP-сессия никогда не сериализуется в SQLite", что и у GIB.
    toll_check_repository = TurkeyTollCheckRepository(db_path)
    # Один процесс - один реестр живых GIB-сессий, полностью in-memory (см.
    # reader/turkey_bot/live_session_registry.py и design report Stage 3:
    # "do not pretend that an in-memory client can be reconstructed from a
    # DB session_token") - создаётся здесь и закрывается здесь же (finally
    # ниже), ConversationController им не владеет.
    session_registry = LiveGibSessionRegistry()
    avrasya_session_registry = LiveAvrasyaSessionRegistry()

    try:
        # Тот же общий OPENAI_API_KEY (settings.ocr.openai_api_key) и
        # translation_model, что и у Георгии (reader/fines/translation.py) -
        # второй ключ/секрет не заводится (см. задачу). None - как и там -
        # означает "перевод недоступен" (нет ключа), не ошибку запуска:
        # клиент увидит оригинальный турецкий текст (см. conversation.py::
        # _translate_fines).
        translator = (
            TurkeyFineTranslationService(
                api_key=settings.ocr.openai_api_key, model=settings.fine_monitor.translation_model,
            )
            if settings.ocr.openai_api_key
            else None
        )
        # НЕ содержит и не может содержать сам ключ (см. выше) - только
        # факт "сконструирован/нет" и имя модели (не секрет) - единственный
        # способ подтвердить в логе, что перевод реально включён, без
        # тестового запроса к OpenAI (см. задачу: "do not make a
        # standalone test OpenAI request").
        if translator is not None:
            logger.info(f"✔ Turkey fine translator включён (model={settings.fine_monitor.translation_model})")
        else:
            logger.info("Turkey fine translator выключен (OPENAI_API_KEY не задан)")
        controller = ConversationController(
            conversation_state_repository, check_repository, session_registry,
            garage_repository, statistics_service,
            avrasya_session_registry, toll_check_repository,
            translator=translator,
            # ТА ЖЕ настройка, что и у @ProtocolGEbot (см. design report:
            # "reuse the same trusted manager IDs/configuration... do not
            # duplicate/hardcode a second manager list") — trusted даёт
            # доступ ТОЛЬКО к статистике (см. conversation.py::_is_trusted),
            # НЕ к чужому гаражу.
            trusted_operator_user_ids=frozenset(settings.public_bot.trusted_operator_user_ids),
            # ТА ЖЕ business-timezone, что и у Георгии-статистики
            # (settings.fine_monitor.timezone) — общий конфиг, не
            # Turkey-специфичный (см. design report: не вводить отдельную
            # настройку там, где общая уже есть).
            tz=ZoneInfo(settings.fine_monitor.timezone),
            # ТА ЖЕ destination коммерческих CTA-кнопок после Avrasya
            # has_debt (см. design report: "reuse those exact production
            # values"), что и у @ProtocolGEbot (settings.public_bot.
            # payment_help_contact_username) — не отдельная настройка.
            payment_help_contact_username=settings.public_bot.payment_help_contact_username,
        )

        client = TelegramClient(str(_SESSION_PATH), api_id, api_hash)
        register(client, controller, known_users_repository)

        # token передаётся ЗДЕСЬ и только здесь.
        await client.start(bot_token=token)
        # Реальный @username берётся у Telegram ПОСЛЕ подключения (не
        # hardcoded) — единственный надёжный способ подтвердить в логе,
        # что процесс поднялся именно под ожидаемым ботом, а не спутал
        # токен с чужим (см. design report: "verify which bot is connected").
        me = await client.get_me()
        logger.info(f"✔ @{me.username} подключён (id={me.id})")

        await client.run_until_disconnected()
    finally:
        # Штатное завершение процесса (Ctrl+C/systemd stop/необработанное
        # исключение выше) - закрыть ВСЕ ещё открытые httpx.AsyncClient
        # живых проверок (см. задачу: "graceful shutdown of all live
        # sessions"), иначе они остались бы висящими TCP-соединениями.
        await session_registry.close_all()
        await avrasya_session_registry.close_all()
        conversation_state_repository.close()
        known_users_repository.close()
        check_repository.close()
        garage_repository.close()
        toll_check_repository.close()


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
