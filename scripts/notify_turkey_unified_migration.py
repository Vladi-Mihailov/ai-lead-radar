#!/usr/bin/env python
"""Одноразовая рассылка существующим пользователям @ProtocolTRbot после
production deployment/migration (см. задачу "Перенос Unified Turkey
функционала в production" п.8) — отдельный, явный шаг от
scripts/migrate_turkey_unified.py (та скрипт Telegram-сообщения не
отправляет вообще).

По умолчанию — dry-run: печатает, скольким пользователям сообщение было
бы отправлено, НИЧЕГО не отправляет и НИЧЕГО не отмечает как
доставленное (см. reader/turkey_bot/migration_notify.py::notify_all).

Только --execute реально шлёт сообщения через РЕАЛЬНЫЙ production
TelegramClient (TURKEYBOT_TOKEN) — ОТДЕЛЬНЫЙ .session-файл (data/sessions/
turkeybot_migration_notify), НЕ тот же, что у основного бот-процесса (см.
deploy/ai-lead-radar-turkeybot.service, data/sessions/turkeybot) —
намеренно, чтобы файл Telethon-сессии не делили два одновременно
работающих процесса (основной бот остаётся запущенным во время рассылки).

Использование:
    python -m scripts.notify_turkey_unified_migration               # dry-run
    python -m scripts.notify_turkey_unified_migration --execute      # реальная отправка
    python -m scripts.notify_turkey_unified_migration --db-path X.db # другой файл (тесты)

Persistent-отметка о доставке — turkey_unified_migration_notifications
(UNIQUE telegram_user_id) — повторный запуск НИКОГДА не отправляет
повторно уже успешно уведомлённым (см. reader/turkey_bot/
migration_notification_repository.py)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from reader.logging_setup import setup_logging
from reader.settings import ConfigError, load_settings
from reader.turkey_bot.known_users_repository import (
    TurkeyBotKnownUsersRepository,
)
from reader.turkey_bot.migration_notification_repository import (
    TurkeyUnifiedMigrationNotificationRepository,
)
from reader.turkey_bot.migration_notify import notify_all

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
# ОТДЕЛЬНЫЙ session-файл от production-бота (data/sessions/turkeybot) — см.
# модуль docstring про то, почему рассылка НЕ переиспользует основную сессию.
_SESSION_PATH = PROJECT_ROOT / "data" / "sessions" / "turkeybot_migration_notify"


class _TelethonSender:
    def __init__(self, client) -> None:
        self._client = client

    async def send_message(self, *, chat_id: int, text: str, buttons) -> None:
        await self._client.send_message(chat_id, text, buttons=buttons)


class _DryRunSender:
    """Никогда не вызывается в dry-run (см. notify_all: dry_run=True
    пропускает вызов sender.send_message целиком) — присутствует только
    как явный, неиспользуемый Protocol-аргумент; AssertionError здесь
    сигнализировал бы о регрессии в notify_all, если бы это когда-нибудь
    изменилось."""

    async def send_message(self, *, chat_id: int, text: str, buttons) -> None:
        raise AssertionError("_DryRunSender.send_message не должен вызываться в dry-run")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="Реально отправить сообщения (default: dry-run, ничего не отправляет).",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Путь к БД (default: settings.app.users_db_file). НИКОГДА не "
             "указывайте здесь data/turkey_bot_test.db.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = load_settings(CONFIG_PATH)
    db_path = Path(args.db_path) if args.db_path else settings.app.users_db_file
    trusted_operator_user_ids = frozenset(settings.public_bot.trusted_operator_user_ids)

    known_users = TurkeyBotKnownUsersRepository(db_path)
    notifications = TurkeyUnifiedMigrationNotificationRepository(db_path)
    try:
        if not args.execute:
            result = await notify_all(
                _DryRunSender(), known_users, notifications,
                trusted_operator_user_ids=trusted_operator_user_ids, dry_run=True,
            )
            print(
                f"DRY-RUN: eligible={result.eligible} "
                f"already_notified_skipped={result.already_notified_skipped}",
            )
            print("Ничего не отправлено. Запустите с --execute для реальной рассылки.")
            return 0

        load_dotenv()
        token = os.getenv("TURKEYBOT_TOKEN")
        if not token:
            print("TURKEYBOT_TOKEN не задан — см. .env.example.", file=sys.stderr)
            return 1
        missing = [name for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH") if not os.getenv(name)]
        if missing:
            print("Не заданы переменные окружения: " + ", ".join(missing), file=sys.stderr)
            return 1
        api_id = int(os.environ["TELEGRAM_API_ID"])
        api_hash = os.environ["TELEGRAM_API_HASH"]

        from telethon import TelegramClient  # локальный импорт — не нужен в dry-run

        _SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(str(_SESSION_PATH), api_id, api_hash)
        await client.start(bot_token=token)
        try:
            result = await notify_all(
                _TelethonSender(client), known_users, notifications,
                trusted_operator_user_ids=trusted_operator_user_ids, dry_run=False,
            )
        finally:
            await client.disconnect()

        print(
            f"sent={result.sent} failed={result.failed} "
            f"already_notified_skipped={result.already_notified_skipped}",
        )
        return 1 if result.failed else 0
    finally:
        known_users.close()
        notifications.close()


def main(argv: list[str] | None = None) -> int:
    setup_logging("INFO")
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except ConfigError as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
