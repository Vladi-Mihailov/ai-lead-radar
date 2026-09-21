"""Точка входа reader/inviter_admin_bot/ — закрытый control-plane бот над
УЖЕ существующим reader/inviter/ (НЕ второй инвайтер, см. design report).
Структура — тот же шаблон, что и у reader/public_bot/main.py: Settings ->
репозитории -> сервис/контроллер -> register(handlers) -> client.start(
bot_token=...) -> run_until_disconnected()."""

import asyncio
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from telethon import TelegramClient

from reader.inviter.repository import (
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)
from reader.inviter.runtime_state_repository import (
    InviterRuntimeStateRepository,
)
from reader.inviter_admin_bot.auth import AccountAuthCoordinator
from reader.inviter_admin_bot.conversation import AdminBotController
from reader.inviter_admin_bot.conversation_state_repository import (
    AdminBotConversationStateRepository,
)
from reader.inviter_admin_bot.handlers import register
from reader.inviter_admin_bot.service import InviterAdminService
from reader.logging_setup import setup_logging
from reader.settings import ConfigError, Settings, load_settings

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
# Своя, отдельная сессия — не .session ни одного из аккаунтов инвайтера
# (те лежат в data/sessions/<slug>, см. reader/inviter/) и не сессия
# другого bot-процесса (тот же принцип, что и у protocolgebot/turkeybot).
_SESSION_PATH = PROJECT_ROOT / "data" / "sessions" / "inviter_admin_bot"
_ACCOUNT_SESSIONS_DIR = PROJECT_ROOT / "data" / "sessions"

logger = logging.getLogger(__name__)


def read_bot_token() -> str:
    """ТОЛЬКО из окружения (INVITER_ADMIN_BOT_TOKEN, см. .env.example) —
    никогда из config.yaml, никогда не логируется (тот же принцип, что и
    reader/public_bot/main.py::read_bot_token)."""
    load_dotenv()
    token = os.getenv("INVITER_ADMIN_BOT_TOKEN")
    if not token:
        raise ConfigError(
            "INVITER_ADMIN_BOT_TOKEN не задан в .env — inviter admin bot не может быть запущен. "
            "См. .env.example."
        )
    return token


def _build_account_client_factory(settings: Settings):
    """Тот же инлайн-приём, что и везде в reader/inviter/ (authorize.py/
    manage.py/main.py каждый строит такую же фабрику самостоятельно, не
    делясь одной реализацией) — используется и для AccountAuthCoordinator
    (новые/переавторизуемые аккаунты), и для sync (уже существующие)."""

    def factory(session_path) -> TelegramClient:
        return TelegramClient(
            session_path, settings.telegram.api_id, settings.telegram.api_hash,
            receive_updates=False,
        )

    return factory


async def run() -> None:
    settings = load_settings(CONFIG_PATH)
    setup_logging(settings.app.log_level)
    token = read_bot_token()

    account_repository = TelegramAccountRepository(settings.app.users_db_file)
    campaign_repository = InviteCampaignRepository(settings.app.users_db_file)
    invite_repository = UserCampaignInviteRepository(settings.app.users_db_file)
    runtime_state_repository = InviterRuntimeStateRepository(settings.app.users_db_file)
    conversation_state_repository = AdminBotConversationStateRepository(settings.app.users_db_file)

    account_client_factory = _build_account_client_factory(settings)

    service = InviterAdminService(
        account_repository, campaign_repository, invite_repository, runtime_state_repository,
        db_path=settings.app.users_db_file,
        trusted_admin_user_ids=frozenset(settings.inviter_admin_bot.trusted_admin_user_ids),
        worker_poll_interval_seconds=settings.inviter.worker.poll_interval_seconds,
    )
    auth_coordinator = AccountAuthCoordinator(
        account_client_factory, account_repository, sessions_dir=_ACCOUNT_SESSIONS_DIR,
    )
    controller = AdminBotController(
        service, auth_coordinator, conversation_state_repository,
        sync_client_factory=lambda account: account_client_factory(account.session_path),
    )

    client = TelegramClient(str(_SESSION_PATH), settings.telegram.api_id, settings.telegram.api_hash)
    register(client, controller)

    try:
        await client.start(bot_token=token)
        logger.info("✔ Inviter admin bot подключён")
        await client.run_until_disconnected()
    finally:
        account_repository.close()
        campaign_repository.close()
        invite_repository.close()
        runtime_state_repository.close()
        conversation_state_repository.close()


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
