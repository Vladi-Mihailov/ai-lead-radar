"""Служебные вызовы MAX Bot API для выпуска MAX-ботов (см. docs/max-bots.md):

    python scripts/max_bot_admin.py ge me
    python scripts/max_bot_admin.py ge subscriptions
    python scripts/max_bot_admin.py ge subscribe https://max.example.com/max/ge/webhook
    python scripts/max_bot_admin.py ge unsubscribe https://max.example.com/max/ge/webhook
    python scripts/max_bot_admin.py ge set-commands

Токен и секрет webhook — из .env (MAX_GE_* / MAX_TR_*), в вывод не попадают.
subscribe/unsubscribe/set-commands меняют настройки бота в MAX — выполнять
только в рамках согласованного выпуска.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

from reader.max_bot.api import MaxApi, MaxApiError  # noqa: E402
from reader.max_bot.settings import MaxConfigError, load_max_bot_config  # noqa: E402
from reader.max_bot.updates import SUPPORTED_UPDATE_TYPES  # noqa: E402

COMMANDS = [
    ("start", "Главное меню"),
    ("help", "Справка"),
    ("cancel", "Отменить текущее действие"),
]


async def _run(args) -> int:
    config = load_max_bot_config(args.bot, os.environ)
    api = MaxApi(config.token, base_url=config.api_base_url, verify=config.tls_verify)
    try:
        if args.action == "me":
            me = await api.get_me()
            print(f"@{me.get('username')} id={me.get('user_id')} name={me.get('first_name')}")
        elif args.action == "subscriptions":
            for subscription in await api.subscriptions():
                print(subscription.get("url"), subscription.get("update_types"))
        elif args.action == "subscribe":
            if not args.url or not args.url.startswith("https://"):
                print("Нужен https:// URL webhook (порт 443).", file=sys.stderr)
                return 2
            if config.webhook_secret is None:
                print(f"MAX_{args.bot.upper()}_WEBHOOK_SECRET не задан.", file=sys.stderr)
                return 2
            await api.subscribe(args.url, secret=config.webhook_secret, update_types=SUPPORTED_UPDATE_TYPES)
            print("Подписка webhook создана.")
        elif args.action == "unsubscribe":
            await api.unsubscribe(args.url)
            print("Подписка webhook удалена.")
        elif args.action == "set-commands":
            await api.set_commands(COMMANDS)
            print("Команды бота обновлены.")
    except MaxApiError as exc:
        print(f"MAX API: status={exc.status} code={exc.code} message={exc.message}", file=sys.stderr)
        return 1
    finally:
        await api.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bot", choices=["ge", "tr"])
    parser.add_argument("action", choices=["me", "subscriptions", "subscribe", "unsubscribe", "set-commands"])
    parser.add_argument("url", nargs="?")
    args = parser.parse_args()
    load_dotenv()
    try:
        sys.exit(asyncio.run(_run(args)))
    except MaxConfigError as exc:
        print(f"Ошибка настройки: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
