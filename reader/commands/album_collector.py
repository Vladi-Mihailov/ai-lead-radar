import asyncio
import logging
from typing import Awaitable, Callable

from telethon import TelegramClient, events

logger = logging.getLogger(__name__)

_DEFAULT_DEBOUNCE_SECONDS = 1.5


class AlbumCollector:
    """Собирает Telegram-альбом (несколько фото одним сообщением пользователя
    — Telegram шлёт их как ОТДЕЛЬНЫЕ NewMessage-события с общим grouped_id;
    подпись/команда обычно прикреплена только к ОДНОМУ из них, остальные
    приходят без текста) в одну группу перед обработкой.

    Подход — небольшой in-memory буфер по grouped_id + debounce-таймер
    (cancel-and-replace: новое сообщение той же группы отменяет предыдущий
    таймер и ставит новый), а НЕ поиск соседних message_id по диапазону —
    Telegram не гарантирует ни точный диапазон id, ни порядок доставки
    сообщений одного альбома, поэтому предположение о смежных id было бы
    ненадёжным (см. задачу). Через debounce_seconds без новых сообщений той
    же группы группа считается полной и передаётся в on_group_ready.

    Регистрируется как ЕЩЁ ОДИН NewMessage-handler на том же чате, что и
    CommandDispatcher (см. reader/commands/insurance_ocr.py и
    reader/main.py) — параллельно, не изменяя сам CommandDispatcher:
    Telethon поддерживает несколько независимых handler'ов с одинаковым
    chats=-фильтром. Сообщения БЕЗ grouped_id (одиночное фото/документ)
    этот класс не трогает вовсе — такие уже целиком обрабатываются обычным
    CommandDispatcher -> Command.handle() синхронно, без ожидания."""

    def __init__(
        self,
        *,
        on_group_ready: Callable[[list], Awaitable[None]],
        debounce_seconds: float = _DEFAULT_DEBOUNCE_SECONDS,
    ):
        self._on_group_ready = on_group_ready
        self._debounce_seconds = debounce_seconds
        self._buffers: dict[int, list] = {}
        self._timers: dict[int, asyncio.Task] = {}

    async def start(self, client: TelegramClient, chat_id: int | str) -> None:
        """Резолвит chat_id и регистрирует on_new_message как отдельный
        NewMessage-handler — тот же паттерн, что и CommandDispatcher.start(),
        специально не переиспользуемый напрямую, чтобы не трогать сам
        CommandDispatcher ради одной команды (см. задачу)."""
        try:
            entity = await client.get_entity(chat_id)
        except Exception as exc:
            logger.error("✖ Чат для сборки альбомов '%s' не найден", chat_id)
            raise RuntimeError(
                f"Не удалось найти чат для сборки альбомов '{chat_id}'. "
                f"Убедитесь, что аккаунт состоит в этом чате. Причина: {exc}"
            ) from exc

        client.add_event_handler(self.on_new_message, events.NewMessage(chats=[entity]))
        logger.info("✔ Сборщик альбомов подключён к чату")

    async def on_new_message(self, event: events.NewMessage.Event) -> None:
        grouped_id = getattr(event, "grouped_id", None)
        if grouped_id is None:
            # Одиночное сообщение — не альбом, этим классом не обрабатывается.
            return

        self._buffers.setdefault(grouped_id, []).append(event)

        existing_timer = self._timers.get(grouped_id)
        if existing_timer is not None:
            existing_timer.cancel()
        self._timers[grouped_id] = asyncio.create_task(self._settle(grouped_id))

    async def _settle(self, grouped_id: int) -> None:
        try:
            await asyncio.sleep(self._debounce_seconds)
        except asyncio.CancelledError:
            # Пришло ещё одно сообщение той же группы (см. on_new_message) —
            # оно уже поставило свой собственный таймер, этот просто выходит.
            return

        events_batch = self._buffers.pop(grouped_id, [])
        self._timers.pop(grouped_id, None)
        if not events_batch:
            return

        try:
            await self._on_group_ready(events_batch)
        except Exception:
            logger.exception("Не удалось обработать альбом (grouped_id=%s)", grouped_id)
