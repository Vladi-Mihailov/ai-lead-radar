"""Bootstrap-процесс ЭКСПЕРИМЕНТАЛЬНОГО test-clone Turkey-бота (см. design
report "изолированный experimental clone Turkey bot") — @Get_8_Status_bot,
ПОЛНОСТЬЮ ОТДЕЛЬНЫЙ процесс от production Turkey-бота (@ProtocolTRbot,
reader/turkey_bot/*, ai-lead-radar-turkeybot.service), от reader/main.py и
от reader/public_bot/main.py. Функционально — буквальная копия
reader/turkey_bot/* (GİB/Avrasya/KGM/CAPTCHA human-in-the-loop/гараж/меню/
справка НЕ изменены и не упрощены), но с полностью независимым runtime:

  - Telegram token — ТОЛЬКО TURKEYBOT_TEST_TOKEN (см. read_bot_token() —
    НИКОГДА не fallback на production TURKEYBOT_TOKEN, при отсутствии —
    fail fast с понятной ошибкой, production-бот при этом НЕ затрагивается,
    т.к. это полностью отдельный процесс/файл);
  - Telethon session — ОТДЕЛЬНЫЙ файл (см. _SESSION_PATH ниже,
    data/sessions/turkey_bot_test), НЕ production data/sessions/turkeybot;
  - SQLite — ОТДЕЛЬНЫЙ файл (см. _DB_PATH ниже, data/turkey_bot_test.db),
    НЕ settings.app.users_db_file (production, общий с Георгия-ботом) —
    здесь НЕТ ни одной production/пользовательской строки, схема создаётся
    штатным способом каждого репозитория (CREATE TABLE IF NOT EXISTS) при
    первом запуске в чистый файл;
  - live session registries (GIB/Avrasya/KGM — httpx.AsyncClient/cookies/
    CAPTCHA state) — ОТДЕЛЬНЫЕ, process-local экземпляры этого процесса,
    НЕ разделяются с production Turkey-ботом (два разных процесса — два
    разных набора Python-объектов в памяти, разделять их и не могло бы).

Запускается только вручную/через systemd (см.
deploy/ai-lead-radar-turkeybot-test.service — НЕ enabled/started, пока
TURKEYBOT_TEST_TOKEN не задан):

    python -m reader.turkey_bot_test.main

CAPTCHA НЕ решается автоматически и не обходится — тот же human-in-the-loop
(provider -> CAPTCHA -> Telegram user -> human answer -> submit -> result),
что и в reader/turkey_bot/* — этот clone НИЧЕГО не меняет в самой логике,
только изолирует runtime, чтобы будущие эксперименты с CAPTCHA/providers не
могли задеть production @ProtocolTRbot.

Turkey — ОДНОРАЗОВАЯ проверка (см. design report): здесь нет и не должно
быть ни FineMonitoringTaskRepository/FineSubscriptionRepository/
DetectedFineRepository, ни какого-либо scheduler/job — ничего из
reader/fines/* или reader/public_bot/* сюда не импортируется."""

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
from reader.turkey_bot_test.avrasya.live_session_registry import (  # noqa: E402
    LiveAvrasyaSessionRegistry,
)
from reader.turkey_bot_test.check_repository import TurkeyCheckRepository  # noqa: E402
from reader.turkey_bot_test.conversation import ConversationController  # noqa: E402
from reader.turkey_bot_test.conversation_state_repository import (  # noqa: E402
    TurkeyConversationStateRepository,
)
from reader.turkey_bot_test.gib.translation import TurkeyFineTranslationService  # noqa: E402
from reader.turkey_bot_test.handlers import register  # noqa: E402
from reader.turkey_bot_test.kgm.live_session_registry import (
    LiveKgmSessionRegistry,  # noqa: E402
)
from reader.turkey_bot_test.known_users_repository import (
    TurkeyBotKnownUsersRepository,  # noqa: E402
)
from reader.turkey_bot_test.live_session_registry import LiveGibSessionRegistry  # noqa: E402
from reader.turkey_bot_test.statistics_service import TurkeyStatisticsService  # noqa: E402
from reader.turkey_bot_test.toll_check_repository import (
    TurkeyTollCheckRepository,  # noqa: E402
)
from reader.turkey_bot_test.user_cars_repository import (
    TurkeyUserCarsRepository,  # noqa: E402
)

# config.yaml ОБЩИЙ с production (см. design report: shared, не
# security-sensitive бизнес-настройки — trusted_operator_user_ids/
# payment_help_contact_username/timezone/OPENAI_API_KEY, см. run() ниже —
# ничего production-специфичного здесь нет, изолировать нечего).
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
# ОТДЕЛЬНЫЙ .session-файл (см. design report "изолированный experimental
# clone Turkey bot" п.4) — НЕ production data/sessions/turkeybot — свой,
# независимый Telethon-логин, безопасно запускать одновременно с
# production Turkey-ботом (два разных .session-файла, два разных
# TCP-соединения к Telegram, никакого общего состояния).
_SESSION_PATH = PROJECT_ROOT / "data" / "sessions" / "turkey_bot_test"
# ОТДЕЛЬНЫЙ SQLite-файл (см. design report п.5: "test bot НЕ должен писать
# экспериментальные данные в production data/users.db") — НЕ
# settings.app.users_db_file (production, общий с Георгия-ботом) — чистый
# файл, схема создаётся штатным способом каждого репозитория (CREATE TABLE
# IF NOT EXISTS) при первом запуске, никаких production-миграций.
_DB_PATH = PROJECT_ROOT / "data" / "turkey_bot_test.db"

logger = logging.getLogger(__name__)


def read_bot_token() -> str:
    """ТОЛЬКО из окружения (TURKEYBOT_TEST_TOKEN) — НИКОГДА не читает и не
    fallback'ится на production TURKEYBOT_TOKEN (см. design report п.3:
    "Test bot НИКОГДА не должен fallback'иться на production
    TURKEYBOT_TOKEN") — если TURKEYBOT_TEST_TOKEN не задан, процесс сразу
    завершается понятной ошибкой (fail fast) и НЕ трогает production-бот
    (это полностью отдельный процесс/файл, production TURKEYBOT_TOKEN
    здесь даже не читается). Никогда не логируется (тот же приём, что и
    reader/public_bot/main.py::read_bot_token/reader/turkey_bot/
    main.py::read_bot_token)."""
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

    # ИЗОЛИРОВАННАЯ test-DB (см. _DB_PATH выше) — НЕ settings.app.
    # users_db_file (production). Каждый репозиторий ниже сам создаёт свой
    # родительский каталог/схему (CREATE TABLE IF NOT EXISTS) при первом
    # обращении — отдельный mkdir здесь не нужен (тот же приём, что и в
    # production reader/turkey_bot/main.py).
    db_path = _DB_PATH
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
    # reader/turkey_bot_test/avrasya/live_session_registry.py) — тот же принцип
    # "живая HTTP-сессия никогда не сериализуется в SQLite", что и у GIB.
    toll_check_repository = TurkeyTollCheckRepository(db_path)
    # Один процесс - один реестр живых GIB-сессий, полностью in-memory (см.
    # reader/turkey_bot_test/live_session_registry.py и design report Stage 3:
    # "do not pretend that an in-memory client can be reconstructed from a
    # DB session_token") - создаётся здесь и закрывается здесь же (finally
    # ниже), ConversationController им не владеет.
    session_registry = LiveGibSessionRegistry()
    avrasya_session_registry = LiveAvrasyaSessionRegistry()
    # KGM (webihlaltakip.kgm.gov.tr, см. design report "Реализация KGM
    # provider") — ТРЕТИЙ, структурно идентичный in-memory реестр живых
    # сессий, та же ОБЩАЯ turkey_toll_checks (provider="kgm"), НЕ отдельная
    # таблица (см. reader/turkey_bot_test/kgm/live_session_registry.py).
    kgm_session_registry = LiveKgmSessionRegistry()

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
            avrasya_session_registry, toll_check_repository, kgm_session_registry,
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
        await kgm_session_registry.close_all()
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
