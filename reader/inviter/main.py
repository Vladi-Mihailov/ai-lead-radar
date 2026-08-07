import argparse
import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon import TelegramClient  # noqa: E402

from reader.inviter.models import TelegramAccount  # noqa: E402
from reader.inviter.repository import (  # noqa: E402
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)
from reader.inviter.service import InviterService, TEST_MODE_MAX_SUCCESSFUL_INVITES  # noqa: E402
from reader.logging_setup import setup_logging  # noqa: E402
from reader.notifications.operator_notifier import OperatorNotifier  # noqa: E402
from reader.settings import ConfigError, Settings, load_settings  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

logger = logging.getLogger(__name__)


def _build_client_factory(settings: Settings):
    """Каждый TelegramAccount — отдельная Telegram-сессия (session_path),
    но одно и то же приложение (api_id/api_hash из settings.telegram) — тот
    же принцип, что и у reader/sync_users.py. Клиент возвращается
    неподключённым — connect()/disconnect() делает вызывающий (см.
    InviterService._dry_run_account/_execute_account)."""

    def factory(account: TelegramAccount) -> TelegramClient:
        return TelegramClient(
            account.session_path,
            settings.telegram.api_id,
            settings.telegram.api_hash,
            receive_updates=False,
        )

    return factory


def _build_operator_notifier(settings: Settings) -> OperatorNotifier:
    """Отдельная сессия (session_path_notifier) — не делит .session-файл ни
    с main.py (session_path_live), ни с sync_users.py (session_path_sync),
    см. reader/settings.py. Получатели — ИМЕННО app.lead_forward_to, тот
    же рабочий чат проекта, куда Reader уже пересылает найденные лиды (см.
    reader/main.py, TelegramSink) — а не fine_monitor.notification_chat_ids
    (отдельный, потенциально другой, чат для алертов об оштрафованных
    авто): отчёты инвайтера — это статистика по проекту, а не персональное
    уведомление оператору, поэтому им место там же, где лиды, независимо
    от того, настроен ли отдельный чат для fine_monitor (см. задачу)."""
    client = TelegramClient(
        settings.telegram.session_path_notifier,
        settings.telegram.api_id,
        settings.telegram.api_hash,
        receive_updates=False,
    )
    return OperatorNotifier(client, settings.app.lead_forward_to)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Инвайтер — отбор кандидатов и приглашение их в target_chat кампаний."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Только подготовка и лог READY/FAILED, без единого изменения в "
            "Telegram (по умолчанию, можно указать явно)."
        ),
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Выполнить реальные приглашения в Telegram (InviteToChannelRequest/AddChatUserRequest).",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help=(
            "Тестовый прогон: остановить весь запуск, как только он выполнит "
            "30 успешных приглашений (status='invited'). Имеет смысл только "
            "вместе с --execute."
        ),
    )
    return parser.parse_args(argv)


async def run(*, execute: bool = False, test: bool = False) -> None:
    """Поднимает инфраструктуру инвайтера (репозитории + миграции БД) и
    делегирует запуск InviterService — отбор кандидатов и, при execute=True,
    реальные приглашения (см. service.py). execute=False (по умолчанию) —
    только dry-run, без единого изменения в Telegram. test=True (--test) —
    имеет смысл только вместе с execute=True: останавливает весь запуск
    после TEST_MODE_MAX_SUCCESSFUL_INVITES успешных приглашений (см.
    InviterService(max_successful_invites=...))."""
    settings = load_settings(CONFIG_PATH)
    setup_logging(settings.app.log_level)
    logger.info(
        "Инвайтер запущен в режиме: %s%s",
        "EXECUTE" if execute else "DRY RUN",
        " (TEST)" if test else "",
    )

    account_repository = TelegramAccountRepository(settings.app.users_db_file)
    campaign_repository = InviteCampaignRepository(settings.app.users_db_file)
    invite_repository = UserCampaignInviteRepository(settings.app.users_db_file)
    # Инвайтер резолвит некоторых кандидатов лично своим же аккаунтом (см.
    # InviterService._resolve_input_peer) — свежий access_hash из этого
    # резолва сохраняется через UserRepository, ту же таблицу users, что
    # ведут sync_users.py/main.py.
    user_repository = UserRepository(settings.app.users_db_file)
    logger.info(
        "Инфраструктура инвайтера готова: аккаунтов=%d, кампаний=%d, приглашений=%d",
        len(account_repository.list()),
        len(campaign_repository.list()),
        len(invite_repository.list()),
    )

    notifier = _build_operator_notifier(settings)
    await notifier.start()

    try:
        service = InviterService(
            account_repository, campaign_repository, invite_repository,
            client_factory=_build_client_factory(settings),
            notifier=notifier,
            user_repository=user_repository,
            max_successful_invites=TEST_MODE_MAX_SUCCESSFUL_INVITES if test else None,
        )
        await service.run(execute=execute)
    finally:
        await notifier.close()
        account_repository.close()
        campaign_repository.close()
        invite_repository.close()
        user_repository.close()


def main() -> None:
    args = _parse_args(sys.argv[1:])
    try:
        asyncio.run(run(execute=args.execute, test=args.test))
    except ConfigError as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)


if __name__ == "__main__":
    main()
