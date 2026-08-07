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
from pathlib import Path
from typing import Any

from telethon import TelegramClient

logger = logging.getLogger(__name__)

# Команда для одноразовой авторизации session_path_notifier (см.
# reader/notifications/authorize_notifier.py) — упоминается в
# предупреждении start(), когда сессия подключилась (connect() успешен),
# но никогда не проходила логин (is_user_authorized() -> False). Раньше
# в этом случае get_entity() падал с ошибкой авторизации, которая
# неотличимо перехватывалась тем же except Exception, что и "получатель
# не найден" — оператор видел неверный диагноз (см. задачу про
# "Получатель уведомлений оператора ... не найден", хотя тот же получатель
# получает лиды через отдельную, уже авторизованную сессию Reader).
_AUTHORIZE_COMMAND = "python -m reader.notifications.authorize_notifier"


@dataclass(frozen=True)
class _ResolvedTarget:
    entity: Any
    label: str


def _label(target: int | str) -> str:
    return f"@{target}" if isinstance(target, str) else str(target)


class OperatorNotifier:
    """client — отдельное (не live, не sync) Telegram-подключение, чтобы
    работать независимо от main.py/sync_users.py, не деля с ними один
    .session-файл (см. reader/settings.py: session_path_notifier). Эта
    сессия не проходит через client.start()/интерактивный логин
    автоматически — её нужно авторизовать ОДИН раз отдельно (см.
    reader/notifications/authorize_notifier.py), прежде чем start() здесь
    сможет что-либо резолвить.

    Любая ошибка — подключения, резолва получателя, отправки — только
    логируется. Сбой уведомления не должен останавливать вызывающий сервис
    (см. reader/inviter/service.py)."""

    def __init__(
        self,
        client: TelegramClient,
        chat_ids: list[int | str],
        *,
        session_path: str | Path | None = None,
    ):
        self._client = client
        self._chat_ids = chat_ids
        # Только для диагностического сообщения при неавторизованной
        # сессии (см. start()) — сама доставка от него не зависит.
        self._session_path = session_path
        self._resolved: list[_ResolvedTarget] = []
        self._connected = False

    async def start(self) -> None:
        try:
            await self._client.connect()
            self._connected = True
        except Exception:
            logger.warning("✖ Не удалось подключить уведомления оператора", exc_info=True)
            return

        # connect() успешен не означает "авторизован" — это только
        # MTProto-подключение. get_entity() ниже требует авторизованного
        # пользователя; без этой явной проверки ошибка авторизации
        # перехватывалась бы тем же except Exception, что и "получатель не
        # найден", маскируя настоящую причину (см. задачу).
        if not await self._client.is_user_authorized():
            logger.warning(
                "Сессия уведомлений не авторизована: %s. Сначала выполните %s.",
                self._session_path, _AUTHORIZE_COMMAND,
            )
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
