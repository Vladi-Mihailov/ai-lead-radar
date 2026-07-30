import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient, events, utils

from reader.core.models import Message
from reader.groups import Group
from reader.settings import TelegramSettings
from reader.sources.base import BaseSource
from reader.users.models import TelegramUserInfo
from reader.users.repository import UserRepository

logger = logging.getLogger(__name__)


@dataclass
class _ResolvedGroup:
    entity: Any
    title: str


class TelegramSource(BaseSource):
    def __init__(
        self,
        telegram_settings: TelegramSettings,
        groups: list[Group],
        user_repository: UserRepository,
        *,
        debug_events: bool = False,
    ):
        self._settings = telegram_settings
        self._groups = groups
        self._user_repository = user_repository
        # Временная диагностика доставки сообщений (TRACKED GROUPS/RAW EVENT/
        # FILTERED EVENT/QUEUE PUT) — включается DEBUG_TELEGRAM_EVENTS в .env.
        self._debug_events = debug_events
        self._client = TelegramClient(
            str(telegram_settings.session_path_live),
            telegram_settings.api_id,
            telegram_settings.api_hash,
        )

        self._queue: asyncio.Queue[Message] = asyncio.Queue()
        self._resolved: dict[int, _ResolvedGroup] = {}

    @property
    def client(self) -> TelegramClient:
        return self._client

    async def start(self) -> None:
        self._settings.session_path_live.parent.mkdir(parents=True, exist_ok=True)

        await self._client.start(phone=self._settings.phone)
        logger.info("Авторизация в Telegram выполнена")

        await self._client.get_dialogs()
        await self._resolve_groups()

        if self._debug_events:
            # ---- ВРЕМЕННАЯ ДИАГНОСТИКА: список того, что реально попало в self._resolved ----
            for chat_id, resolved_group in self._resolved.items():
                logger.warning(
                    "TRACKED GROUPS\nchat_id=%s\ntitle=%s",
                    chat_id,
                    resolved_group.title,
                )
            # -------------------------------------------------------------------------------

            # ---- ВРЕМЕННАЯ ДИАГНОСТИКА: события приходят только из первой группы ----
            # Обработчик без chats= — ловит вообще все чаты, куда есть доступ у
            # аккаунта, минуя наш фильтр. Если тут для группы нет RAW EVENT —
            # проблема на стороне Telegram/членства, а не в нашем коде/фильтре.
            self._client.add_event_handler(
                self._log_raw_event,
                events.NewMessage(),
            )
            # ---------------------------------------------------------------------

            # Те же ключи, что и в self._resolved (см. TRACKED GROUPS выше) —
            # именно они пойдут в chats= ниже.
            filter_chat_ids = list(self._resolved.keys())

            # ---- ВРЕМЕННАЯ ДИАГНОСТИКА: что именно передаём в chats= ----
            logger.warning(
                "Registering NewMessage handler for %d chats\n%s",
                len(filter_chat_ids),
                "\n".join(f"chat_id={cid}" for cid in filter_chat_ids),
            )
            # ---------------------------------------------------------------

        self._client.add_event_handler(
            self.handle_new_message,
            events.NewMessage(chats=[g.entity for g in self._resolved.values()]),
        )

        logger.info("Отслеживается групп: %d", len(self._resolved))

    # ---- ВРЕМЕННАЯ ДИАГНОСТИКА ----
    async def _log_raw_event(self, event: events.NewMessage.Event) -> None:
        """Только логирует, ничего больше не делает — не часть бизнес-логики."""
        chat_id = event.chat_id
        title = getattr(event.chat, "title", None) or getattr(event.chat, "username", None)
        tracked = chat_id in self._resolved
        text = (event.raw_text or "")[:100]

        logger.warning(
            "RAW EVENT\nevent_id=%s\nchat_id=%s\ntitle=%s\ntracked=%s\ndate=%s\ntext=%r",
            event.id,
            chat_id,
            title,
            "YES" if tracked else "NO",
            event.date,
            text,
        )
    # --------------------------------

    async def _resolve_groups(self) -> None:
        for group in self._groups:
            try:
                entity = await self._client.get_entity(group.identifier)
            except Exception as exc:
                logger.error("✖ Группа '%s' не найдена", group.identifier)
                raise RuntimeError(
                    f"Не удалось найти группу '{group.identifier}'. "
                    f"Убедитесь, что аккаунт состоит в этой группе. "
                    f"Причина: {exc}"
                ) from exc

            title = (
                group.title
                or getattr(entity, "title", None)
                or getattr(entity, "username", None)
                or str(entity.id)
            )

            self._resolved[utils.get_peer_id(entity)] = _ResolvedGroup(
                entity=entity,
                title=title,
            )

            logger.info("✔ Подключена группа %s", title)

    async def _fetch_sender_info(
        self,
        event: events.NewMessage.Event,
    ) -> TelegramUserInfo | None:
        sender_id = event.sender_id
        if not sender_id:
            return None

        sender = None
        try:
            sender = await event.get_sender()
        except Exception:
            logger.debug("Не удалось получить отправителя %s из события", sender_id)

        if sender is None:
            try:
                sender = await self._client.get_entity(sender_id)
            except Exception:
                logger.debug("Не удалось получить отправителя %s через get_entity", sender_id)

        if sender is None:
            return None

        return TelegramUserInfo(
            user_id=sender_id,
            username=getattr(sender, "username", None),
            first_name=getattr(sender, "first_name", None),
            last_name=getattr(sender, "last_name", None),
            is_bot=bool(getattr(sender, "bot", False)),
        )

    async def _resolve_sender(
        self,
        event: events.NewMessage.Event,
    ) -> tuple[int | None, str | None, str | None]:
        sender_id = event.sender_id
        info = await self._fetch_sender_info(event)

        # Даже если пользователь уже был в базе — обновляем свежими данными.
        # Сбой локального кэша не должен приводить к потере сообщения.
        if info is not None:
            try:
                self._user_repository.upsert(info)
            except Exception:
                logger.warning("Не удалось обновить локальный кэш пользователя %s", sender_id)

        username = info.username if info else None
        display_name = info.full_name if info else None

        # Telegram не отдал username — пробуем локальный кэш по sender_id
        if not username and sender_id:
            try:
                cached = self._user_repository.get(sender_id)
            except Exception:
                logger.warning("Не удалось прочитать локальный кэш пользователя %s", sender_id)
                cached = None

            if cached:
                username = username or cached.username
                display_name = display_name or cached.full_name

        return sender_id, username, display_name

    async def handle_new_message(self, event: events.NewMessage.Event) -> None:
        """Точка входа для новых сообщений — регистрируется как обработчик у Telethon."""
        if self._debug_events:
            # ---- ВРЕМЕННАЯ ДИАГНОСТИКА ----
            # Если для события есть RAW EVENT, но нет FILTERED EVENT — проблема
            # в фильтре chats=. Лог до любого раннего return, чтобы не пропустить
            # события с пустым текстом.
            resolved_diag = self._resolved.get(event.chat_id)
            logger.warning(
                "FILTERED EVENT\nevent_id=%s\nchat_id=%s\ntitle=%s",
                event.id,
                event.chat_id,
                resolved_diag.title if resolved_diag else None,
            )
            # --------------------------------

        text = event.raw_text

        if not text:
            return

        resolved = self._resolved.get(event.chat_id)

        sender_id, username, display_name = await self._resolve_sender(event)

        message = Message(
            id=event.id,
            chat_id=event.chat_id,
            chat_title=resolved.title if resolved else str(event.chat_id),
            sender_id=sender_id,
            sender_username=username,
            sender_name=display_name,
            text=text,
            date=event.date,
            link=self._build_link(event.chat_id, event.id, resolved),
        )

        if self._debug_events:
            # ---- ВРЕМЕННАЯ ДИАГНОСТИКА ----
            logger.warning(
                "QUEUE PUT\nevent_id=%s\nchat_id=%s\ntitle=%s",
                message.id,
                message.chat_id,
                message.chat_title,
            )
            # --------------------------------

        await self._queue.put(message)

    @staticmethod
    def _build_link(
        chat_id: int,
        message_id: int,
        resolved: _ResolvedGroup | None,
    ) -> str | None:
        if resolved is None:
            return None

        username = getattr(resolved.entity, "username", None)

        if username:
            return f"https://t.me/{username}/{message_id}"

        internal_id = str(chat_id)

        if internal_id.startswith("-100"):
            internal_id = internal_id[4:]

        return f"https://t.me/c/{internal_id}/{message_id}"

    async def messages(self) -> AsyncIterator[Message]:
        while True:
            yield await self._queue.get()

    async def stop(self) -> None:
        await self._client.disconnect()
        logger.info("Отключено от Telegram")