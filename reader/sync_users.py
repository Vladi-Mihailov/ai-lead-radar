import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon import TelegramClient  # noqa: E402

from reader.groups import GroupLoadError, load_groups  # noqa: E402
from reader.logging_setup import setup_logging  # noqa: E402
from reader.settings import ConfigError, load_settings  # noqa: E402
from reader.users.history_state_repository import HistorySyncStateRepository  # noqa: E402
from reader.users.history_sync import sync_users_from_history  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402
from reader.users.sync import sync_all_users  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


async def run() -> None:
    settings = load_settings(CONFIG_PATH)
    setup_logging(settings.app.log_level)

    groups = load_groups(settings.app.groups_file)

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
        await sync_users_from_history(client, groups, repository, state_repository)
    finally:
        state_repository.close()
        repository.close()
        await client.disconnect()


def main() -> None:
    try:
        asyncio.run(run())
    except (ConfigError, GroupLoadError) as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)


if __name__ == "__main__":
    main()
