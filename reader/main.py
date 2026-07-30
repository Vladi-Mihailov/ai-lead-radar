import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.core.engine import MatchEngine  # noqa: E402
from reader.core.pipeline import Pipeline  # noqa: E402
from reader.groups import GroupLoadError, load_groups  # noqa: E402
from reader.logging_setup import setup_logging  # noqa: E402
from reader.scenarios import KeywordMatcher, ScenarioLoadError, load_scenarios  # noqa: E402
from reader.settings import ConfigError, load_settings  # noqa: E402
from reader.sinks.console_sink import ConsoleSink  # noqa: E402
from reader.sinks.file_sink import FileSink  # noqa: E402
from reader.sinks.telegram_sink import TelegramSink  # noqa: E402
from reader.sources.telegram_source import TelegramSource  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

logger = logging.getLogger(__name__)


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
    try:
        await pipeline.run()
    finally:
        user_repository.close()


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
