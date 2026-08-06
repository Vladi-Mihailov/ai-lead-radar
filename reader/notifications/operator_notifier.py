"""OperatorNotifier — доставка произвольного текста оператору поверх
отдельного Telegram-подключения, по тому же принципу, что уже применяется в
TelegramNotificationService/TelegramSink: те же получатели (chat_id из
конфигурации приложения), тот же client.get_entity()/client.send_message().

В отличие от TelegramNotificationService (жёстко привязан к NewFineEvent),
это не доменное уведомление, а обёртка для произвольного текста — нужна
reader/inviter, где сообщения оператору — просто статистика хода
приглашений, а не какое-то конкретное доменное событие. Существующие
NotificationService/TelegramNotificationService/TelegramSink не меняются.
"""

import logging
from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResolvedTarget:
    entity: Any
    label: str


def _label(target: int | str) -> str:
    return f"@{target}" if isinstance(target, str) else str(target)


class OperatorNotifier:
    """client — отдельное (не live, не sync) Telegram-подключение, чтобы
    работать независимо от main.py/sync_users.py, не деля с ними один
    .session-файл (см. reader/settings.py: session_path_notifier).

    Любая ошибка — подключения, резолва получателя, отправки — только
    логируется. Сбой уведомления не должен останавливать вызывающий сервис
    (см. reader/inviter/service.py)."""

    def __init__(self, client: TelegramClient, chat_ids: list[int | str]):
        self._client = client
        self._chat_ids = chat_ids
        self._resolved: list[_ResolvedTarget] = []
        self._connected = False

    async def start(self) -> None:
        try:
            await self._client.connect()
            self._connected = True
        except Exception:
            logger.warning("✖ Не удалось подключить уведомления оператора", exc_info=True)
            return

        for chat_id in self._chat_ids:
            label = _label(chat_id)
            try:
                entity = await self._client.get_entity(chat_id)
            except Exception:
                logger.warning("✖ Получатель уведомлений оператора %s не найден", label)
                continue

            self._resolved.append(_ResolvedTarget(entity=entity, label=label))
            logger.info("✔ Получатель уведомлений оператора %s найден", label)

    async def notify_text(self, text: str) -> bool:
        """Отправляет text во все резолвнутые чаты (см. start()); возвращает
        True, если доставлено хотя бы в один. Никогда не бросает исключение
        наружу — сбой отправки только логируется."""
        if not self._resolved:
            logger.warning(
                "Нет ни одного получателя уведомлений оператора — уведомление не отправлено"
            )
            return False

        delivered = False
        for target in self._resolved:
            try:
                await self._client.send_message(target.entity, text, link_preview=False)
                delivered = True
            except Exception:
                logger.warning(
                    "Не удалось отправить уведомление оператору в %s", target.label, exc_info=True
                )
        return delivered

    async def close(self) -> None:
        if self._connected:
            await self._client.disconnect()
