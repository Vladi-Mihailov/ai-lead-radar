import argparse
import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon import TelegramClient  # noqa: E402

from reader.groups import GroupLoadError, load_groups  # noqa: E402
from reader.logging_setup import setup_logging  # noqa: E402
from reader.scenarios import KeywordMatcher, ScenarioLoadError, load_scenarios  # noqa: E402
from reader.settings import ConfigError, load_settings  # noqa: E402
from reader.users.history_state_repository import HistorySyncStateRepository  # noqa: E402
from reader.users.history_sync import sync_users_from_history  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402
from reader.users.sync import sync_all_users  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

logger = logging.getLogger(__name__)

_SEPARATOR = "=" * 60


def _log_sync_mode(force: bool) -> None:
    """Явно печатает режим работы один раз в начале — до начала
    синхронизации, — чтобы в журнале было сразу видно, чего ожидать
    (обычный инкрементальный проход или полная переиндексация истории)."""
    if force:
        logger.info(
            "%s\n"
            "Mode: FULL REINDEX\n"
            "Checkpoint: IGNORED\n"
            "Будет полностью переиндексирована история всех групп.\n"
            "Это может занять продолжительное время.\n"
            "%s",
            _SEPARATOR,
            _SEPARATOR,
        )
    else:
        logger.info(
            "%s\n"
            "Mode: INCREMENTAL SYNC\n"
            "Checkpoint: ENABLED\n"
            "Будут обработаны только новые сообщения.\n"
            "%s",
            _SEPARATOR,
            _SEPARATOR,
        )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Синхронизация локальной базы пользователей Telegram."
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help=(
            "Полностью игнорировать checkpoint истории и пройти всю историю "
            "всех групп заново (например, после добавления новых полей — "
            "keywords, access_hash и т.п. — которые не были посчитаны для "
            "групп, уже помеченных как полностью проиндексированные)."
        ),
    )
    return parser.parse_args(argv)


async def run(*, force: bool = False) -> None:
    settings = load_settings(CONFIG_PATH)
    setup_logging(settings.app.log_level)
    _log_sync_mode(force)

    groups = load_groups(settings.app.groups_file)
    scenarios = load_scenarios(settings.app.scenarios_file)
    matcher = KeywordMatcher(scenarios)

    client = TelegramClient(
        str(settings.telegram.session_path_sync),
        settings.telegram.api_id,
        settings.telegram.api_hash,
        # Этому процессу не нужны живые апдейты — только точечные запросы
        # (get_entity/iter_participants/iter_messages). Отключено явно, а не
        # только за счёт отдельной сессии.
        receive_updates=False,
    )
    await client.start(phone=settings.telegram.phone)

    repository = UserRepository(settings.app.users_db_file)
    state_repository = HistorySyncStateRepository(settings.app.users_db_file)
    try:
        await sync_all_users(client, groups, repository)
        await sync_users_from_history(
            client, groups, repository, state_repository, matcher, force=force
        )
    finally:
        state_repository.close()
        repository.close()
        await client.disconnect()


def main() -> None:
    args = _parse_args(sys.argv[1:])
    try:
        asyncio.run(run(force=args.reindex))
    except (ConfigError, GroupLoadError, ScenarioLoadError) as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)


if __name__ == "__main__":
    main()
