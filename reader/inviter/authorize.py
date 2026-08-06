"""
Разовая интерактивная авторизация Telegram-аккаунта инвайтера — создаёт
.session-файл (session_path, см. reader/inviter/repository.py), которого
InviterService ждёт при запуске (см. reader/inviter/service.py:
_default_session_checker). Больше ничего не делает: ни кампаний, ни
приглашений, ни записи в user_campaign_invites.

Использование:
    python -m reader.inviter.authorize <name>

<name> — TelegramAccount.name из БД (см. reader/inviter/repository.py:
TelegramAccountRepository), например "@vladimihailov" — ровно то же
значение, что показывает лог "Account: ..." при отсутствии сессии.

Телефон, код Telegram и (если включена) пароль двухфакторной аутентификации
запрашиваются самим Telethon интерактивно (client.start() без phone=...) —
эта команда их не хранит и не подставляет заранее.
"""

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon import TelegramClient  # noqa: E402

from reader.inviter.repository import TelegramAccountRepository  # noqa: E402
from reader.settings import ConfigError, load_settings  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Интерактивная авторизация одного Telegram-аккаунта инвайтера — "
            "создаёт .session-файл, ничего больше."
        )
    )
    parser.add_argument(
        "name",
        help="TelegramAccount.name из БД, например @vladimihailov (см. лог 'Session not found').",
    )
    return parser.parse_args(argv)


async def run(name: str) -> None:
    settings = load_settings(CONFIG_PATH)

    account_repository = TelegramAccountRepository(settings.app.users_db_file)
    try:
        account = next((a for a in account_repository.list() if a.name == name), None)
    finally:
        account_repository.close()

    if account is None:
        print(f"Аккаунт '{name}' не найден в БД (TelegramAccountRepository).", file=sys.stderr)
        sys.exit(1)

    print(f"Авторизация аккаунта: {account.name}")
    print(f"Session будет создана: {account.session_path}.session")
    print()

    client = TelegramClient(
        account.session_path, settings.telegram.api_id, settings.telegram.api_hash,
        receive_updates=False,
    )
    # Без phone=... — Telethon сам запросит номер телефона, код Telegram и
    # (если включена) пароль 2FA через stdin. Больше эта команда ничего не
    # делает — ни резолва target_chat, ни приглашений.
    await client.start()
    await client.disconnect()

    print()
    print(f"✔ Сессия создана: {account.session_path}.session")
    print(f"Теперь можно запускать: python -m reader.inviter --execute")


def main() -> None:
    args = _parse_args(sys.argv[1:])
    try:
        asyncio.run(run(args.name))
    except ConfigError as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)


if __name__ == "__main__":
    main()
