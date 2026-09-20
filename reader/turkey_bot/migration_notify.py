"""Post-migration one-off notification: существующие пользователи
@ProtocolTRbot получают ОДНО сообщение о том, что их старые автомобили
уже добавлены в «📋 Мои авто» (см. задачу "Перенос Unified Turkey
функционала в production" п.8).

Migration script (scripts/migrate_turkey_unified.py, reader/turkey_bot/
migration.py) САМ Telegram-сообщения НЕ отправляет (см. задачу: "migration
script НЕ должен отправлять Telegram messages") — это ОТДЕЛЬНЫЙ, явный шаг
(см. scripts/notify_turkey_unified_migration.py), запускаемый ПОСЛЕ
успешного deployment/migration.

Persistent-отметка о доставке — turkey_unified_migration_notifications
(см. reader/turkey_bot/migration_notification_repository.py), UNIQUE
telegram_user_id, пишется ТОЛЬКО ПОСЛЕ успешного send_message (см.
notify_all ниже) — сбой отправки уходит в except и НИКОГДА не доходит до
mark_notified(), поэтому повторный запуск снова попробует отправить
именно этому пользователю, но никогда не отправит повторно уже успешно
уведомлённым. Один упавший пользователь (blocked/deactivated/chat error)
логируется и НЕ останавливает рассылку остальным (см. try/except внутри
цикла)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from reader.turkey_bot.keyboards import main_menu_keyboard
from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.migration_notification_repository import (
    TurkeyUnifiedMigrationNotificationRepository,
)
from reader.turkey_bot.texts import MIGRATION_NOTIFICATION_TEXT

logger = logging.getLogger(__name__)


class MessageSender(Protocol):
    async def send_message(self, *, chat_id: int, text: str, buttons) -> None: ...


@dataclass(frozen=True)
class NotificationRunResult:
    eligible: int
    sent: int
    already_notified_skipped: int
    failed: int


async def notify_all(
    sender: MessageSender,
    known_users: TurkeyBotKnownUsersRepository,
    notifications: TurkeyUnifiedMigrationNotificationRepository,
    *,
    trusted_operator_user_ids: frozenset[int],
    dry_run: bool,
) -> NotificationRunResult:
    """dry_run=True — НИЧЕГО не отправляет и НИЧЕГО не отмечает (см. задачу:
    "dry-run sends nothing") — sender.send_message() ни разу не вызывается
    в этом режиме, `eligible` считает только тех, кто ЕЩЁ не уведомлён."""
    eligible = 0
    sent = 0
    already_skipped = 0
    failed = 0

    for telegram_user_id, telegram_chat_id in known_users.list_all_with_chat_id():
        if notifications.is_notified(telegram_user_id):
            already_skipped += 1
            continue

        eligible += 1
        if dry_run:
            continue

        buttons = main_menu_keyboard(is_trusted=telegram_user_id in trusted_operator_user_ids)
        try:
            await sender.send_message(
                chat_id=telegram_chat_id, text=MIGRATION_NOTIFICATION_TEXT, buttons=buttons,
            )
        except Exception:
            logger.exception(
                "Turkey migration notify: не удалось отправить user_id=%s chat_id=%s",
                telegram_user_id, telegram_chat_id,
            )
            failed += 1
            continue

        notifications.mark_notified(telegram_user_id)
        sent += 1

    return NotificationRunResult(
        eligible=eligible, sent=sent, already_notified_skipped=already_skipped, failed=failed,
    )
