import asyncio
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient, events, utils
from telethon.tl.types import User, UserEmpty

from reader.core.models import Message
from reader.groups import Group
from reader.settings import TelegramSettings
from reader.sources.base import BaseSource
from reader.users.models import TelegramUserInfo
from reader.users.repository import UserRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResolvedGroup:
    entity: Any
    title: str


class TelegramSource(BaseSource):
    # Telethon иногда доставляет одно и то же обновление повторно (реконнект,
    # gap-recovery и т.п.) — защита от повторной обработки по (chat_id,
    # message_id). Только in-memory, без БД: хранится не более последних
    # _SEEN_MESSAGES_MAXSIZE пар, старые вытесняются по LRU.
    _SEEN_MESSAGES_MAXSIZE = 5000

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
        # Явные исключения из config.yaml (telegram.ignored_sender_ids/
        # ignored_usernames) — проверяются раньше is_bot и раньше очереди.
        self._ignored_sender_ids = set(telegram_settings.ignored_sender_ids)
        self._ignored_usernames = {
            u.lower().lstrip("@") for u in telegram_settings.ignored_usernames
        }
        self._ignored_display_names = set(telegram_settings.ignored_display_names)
        # Диагностика доставки сообщений (TRACKED GROUPS/RAW EVENT/FILTERED
        # EVENT/QUEUE PUT) — постоянная, но выключенная по умолчанию
        # функция; включается DEBUG_TELEGRAM_EVENTS в .env при разборе
        # проблем с доставкой апдейтов.
        self._debug_events = debug_events
        self._client = TelegramClient(
            str(telegram_settings.session_path_live),
            telegram_settings.api_id,
            telegram_settings.api_hash,
        )
        # Устанавливается в start() сразу после client.start() — то есть
        # клиент уже подключён и авторизован, можно слать RPC (get_me(),
        # send_message() и т.п.), даже если резолв групп ниже в start() ещё
        # не завершён. Позволяет другим потребителям client (см.
        # wait_until_ready) дождаться этого момента через Event вместо
        # опроса is_user_authorized().
        self._ready = asyncio.Event()

        self._queue: asyncio.Queue[Message] = asyncio.Queue()
        self._resolved: dict[int, _ResolvedGroup] = {}
        self._seen_messages: OrderedDict[tuple[int, int], None] = OrderedDict()

    @property
    def client(self) -> TelegramClient:
        return self._client

    async def wait_until_ready(self) -> None:
        """Дожидается момента сразу после успешного client.start() внутри
        start() — клиент подключён и авторизован, готов к RPC. Не ждёт
        остального start() (get_dialogs/_resolve_groups), это не нужно
        потребителям, которым важен только authenticated client (например,
        мониторингу штрафов, см. reader/main.py: _run_fine_monitor)."""
        await self._ready.wait()

    async def start(self) -> None:
        self._settings.session_path_live.parent.mkdir(parents=True, exist_ok=True)

        await self._client.start(phone=self._settings.phone)
        logger.info("Авторизация в Telegram выполнена")
        self._ready.set()

        await self._client.get_dialogs()
        await self._resolve_groups()

        if self._debug_events:
            # Список того, что реально попало в self._resolved.
            for chat_id, resolved_group in self._resolved.items():
                logger.warning(
                    "TRACKED GROUPS\nchat_id=%s\ntitle=%s",
                    chat_id,
                    resolved_group.title,
                )

            # Обработчик без chats= — ловит вообще все чаты, куда есть доступ у
            # аккаунта, минуя наш фильтр. Если тут для группы нет RAW EVENT —
            # проблема на стороне Telegram/членства, а не в нашем коде/фильтре.
            self._client.add_event_handler(
                self._log_raw_event,
                events.NewMessage(),
            )

            # Те же ключи, что и в self._resolved (см. TRACKED GROUPS выше) —
            # именно они пойдут в chats= ниже.
            filter_chat_ids = list(self._resolved.keys())

            logger.warning(
                "Registering NewMessage handler for %d chats\n%s",
                len(filter_chat_ids),
                "\n".join(f"chat_id={cid}" for cid in filter_chat_ids),
            )

        self._client.add_event_handler(
            self.handle_new_message,
            events.NewMessage(chats=[g.entity for g in self._resolved.values()]),
        )

        logger.info("Отслеживается групп: %d", len(self._resolved))

    async def _log_raw_event(self, event: events.NewMessage.Event) -> None:
        """Диагностический handler (см. debug_events) — только логирует,
        ничего больше не делает, не часть бизнес-логики."""
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

    async def _resolve_entity_info(
        self,
        entity_id: int | None,
        get_sender: Callable[[], Awaitable[User | UserEmpty | None]],
    ) -> TelegramUserInfo | None:
        """Общая логика резолва User по id с фолбэками через UserEmpty.

        Используется как для прямого отправителя события (event.get_sender),
        так и для оригинального автора пересылки (event.forward.get_sender) —
        у обоих один и тот же класс проблемы: Telegram может вернуть
        UserEmpty, если у читающего аккаунта нет access_hash для этого id
        (см. _fetch_sender_info для истории/деталей этого сценария).
        """
        if not entity_id:
            return None

        sender = None
        try:
            sender = await get_sender()
        except Exception:
            logger.debug("Не удалось получить отправителя %s из события", entity_id)

        # UserEmpty — Telegram знает только id, профиля нет вообще
        # (ни username, ни bot). Для наших целей это равнозначно отсутствию
        # sender, поэтому пробуем дорезолвить через get_entity() — но только
        # в этом случае, не при каждом сообщении.
        if sender is None or isinstance(sender, UserEmpty):
            try:
                resolved = await self._client.get_entity(entity_id)
                if not isinstance(resolved, UserEmpty):
                    sender = resolved
            except Exception:
                logger.debug("Не удалось получить отправителя %s через get_entity", entity_id)

        cached = None
        if sender is None or isinstance(sender, UserEmpty):
            # Резолв по id не дал ничего. Если для этого id уже известен
            # username (из локального кэша) — пробуем резолвнуть по нему: это
            # отдельный, более надёжный путь (ResolveUsername), не зависящий
            # от access_hash/контактов, которым упирается резолв по голому
            # id. Выполняется только в этой ветке, не на каждое сообщение.
            try:
                cached = self._user_repository.get(entity_id)
            except Exception:
                logger.warning("Не удалось прочитать локальный кэш пользователя %s", entity_id)

            if cached and cached.username:
                try:
                    resolved = await self._client.get_entity(cached.username)
                    if not isinstance(resolved, UserEmpty):
                        sender = resolved
                except Exception:
                    logger.debug(
                        "Не удалось получить отправителя %s через username %s",
                        entity_id,
                        cached.username,
                    )

        if sender is None or isinstance(sender, UserEmpty):
            # Последний фолбэк: и username-резолв не помог (или username в
            # кэше не было) — если этот id уже встречался раньше, доверяем
            # тому, что о нём знаем, вместо того чтобы по умолчанию считать
            # его не ботом.
            if cached is not None:
                return TelegramUserInfo(
                    user_id=entity_id,
                    username=cached.username,
                    first_name=cached.first_name,
                    last_name=cached.last_name,
                    is_bot=bool(
                        cached.is_bot or "bot" in (cached.username or "").lower()
                    ),
                )
            return None

        return TelegramUserInfo(
            user_id=entity_id,
            username=getattr(sender, "username", None),
            first_name=getattr(sender, "first_name", None),
            last_name=getattr(sender, "last_name", None),
            is_bot=bool(getattr(sender, "bot", False)),
        )

    async def _fetch_sender_info(
        self,
        event: events.NewMessage.Event,
    ) -> TelegramUserInfo | None:
        return await self._resolve_entity_info(event.sender_id, event.get_sender)

    async def _fetch_forward_origin_info(
        self,
        event: events.NewMessage.Event,
    ) -> TelegramUserInfo | None:
        """Автор ОРИГИНАЛА пересланного сообщения (не тот, кто переслал).

        Telegram/Telethon хранит это отдельно от sender: MessageFwdHeader
        (event.message.fwd_from), обёрнутый Telethon в event.forward
        (custom.forward.Forward). event.sender_id/get_sender() всегда
        относятся к человеку, который нажал "Переслать", поэтому бот,
        репост которого переслал живой человек, не ловился существующим
        is_bot-фильтром — только этой проверкой.

        getattr(..., None) — на случай другой версии Telethon, где forward
        мог бы называться иначе или отсутствовать: тогда просто считаем, что
        сообщение не переслано, вместо падения с AttributeError.
        """
        forward = getattr(event, "forward", None)
        if forward is None:
            return None

        forward_sender_id = getattr(forward, "sender_id", None)
        if not forward_sender_id:
            # Автор пересылки скрыт настройками приватности — Telegram отдаёт
            # только свободный текст (fwd_from.from_name, например "Бот Край
            # Земли"), без from_id и username. Полноценно резолвнуть тут
            # нечего (см. класс-докстрока) — единственный последний фолбэк:
            # если from_name сам содержит "бот"/"bot", считаем это ботом.
            # Это заведомо неточный сигнал (совпадение по подстроке в
            # свободном тексте), поэтому применяется только здесь, в самом
            # конце цепочки, когда никакой другой идентификатор недоступен.
            fwd_from = getattr(event.message, "fwd_from", None)
            from_name = getattr(fwd_from, "from_name", None) if fwd_from else None
            if from_name:
                lowered = from_name.lower()
                if "бот" in lowered or "bot" in lowered:
                    logger.debug(
                        'Skipping forwarded message from hidden bot name: "%s"',
                        from_name,
                    )
                    return TelegramUserInfo(
                        user_id=0,
                        username=None,
                        first_name=None,
                        last_name=None,
                        is_bot=True,
                    )

                logger.debug(
                    "Forward origin without sender_id\nfrom_name=%r",
                    from_name,
                )
            return None

        return await self._resolve_entity_info(forward_sender_id, forward.get_sender)

    async def _resolve_sender(
        self,
        event: events.NewMessage.Event,
    ) -> tuple[int | None, str | None, str | None, bool]:
        sender_id = event.sender_id
        info = await self._fetch_sender_info(event)
        forward_info = await self._fetch_forward_origin_info(event)

        # Даже если пользователь уже был в базе — обновляем свежими данными.
        # Сбой локального кэша не должен приводить к потере сообщения.
        if info is not None:
            try:
                self._user_repository.upsert(info)
            except Exception:
                logger.warning("Не удалось обновить локальный кэш пользователя %s", sender_id)

        # Сначала — окончательные username/display_name (Telegram +, если
        # чего-то не хватает, локальный кэш). is_bot считаем только ПОСЛЕ
        # этого, по итоговому username, а не по промежуточному info.username.
        # Причина: Telegram может отдать sender как "min"-объект — не None и
        # не UserEmpty, а частично заполненный User, где first_name есть, а
        # username — нет (см. комментарий в telethon/tl/custom/sendergetter.py
        # про sender.min). Если считать is_bot по такому неполному info.username,
        # эвристика "bot" in username не увидит username, который donабирается
        # из кэша только несколькими строками ниже.
        username = info.username if info else None
        display_name = info.full_name if info else None

        if not username and sender_id:
            try:
                cached = self._user_repository.get(sender_id)
            except Exception:
                logger.warning("Не удалось прочитать локальный кэш пользователя %s", sender_id)
                cached = None

            if cached:
                username = username or cached.username
                display_name = display_name or cached.full_name

        forward_username = forward_info.username if forward_info else None

        if forward_info is not None and not forward_username and forward_info.user_id:
            try:
                forward_cached = self._user_repository.get(forward_info.user_id)
            except Exception:
                logger.warning(
                    "Не удалось прочитать локальный кэш пользователя %s", forward_info.user_id
                )
                forward_cached = None

            if forward_cached:
                forward_username = forward_cached.username

        # IMPORTANT:
        # username/display_name must already be finalized (including UserRepository
        # fallback) before bot detection. Telegram may return min entities without
        # username, so evaluating is_bot earlier causes bot accounts to bypass
        # filtering.
        #
        # Учитываем ОБА источника: прямого отправителя (кто прислал именно это
        # сообщение) и автора оригинала, если сообщение переслано (кто написал
        # пересланный текст) — бот в любой из этих ролей должен блокироваться
        # одинаково.
        direct_is_bot = bool(
            info is not None
            and (info.is_bot or "bot" in (username or "").lower())
        )
        forward_is_bot = bool(
            forward_info is not None
            and (forward_info.is_bot or "bot" in (forward_username or "").lower())
        )
        is_bot = direct_is_bot or forward_is_bot

        if forward_is_bot and not direct_is_bot:
            logger.debug(
                "Пересланное сообщение: оригинал от бота username=%s",
                forward_username,
            )

        return sender_id, username, display_name, is_bot

    def _is_duplicate_message(self, chat_id: int, message_id: int) -> bool:
        """LRU-проверка (chat_id, message_id) — без БД, только в памяти.

        Telethon иногда доставляет одно и то же обновление повторно
        (реконнект/gap-recovery) — само по себе это не ошибка бизнес-логики,
        поэтому дедуп сделан отдельным, не пересекающимся с остальными
        фильтрами шагом.
        """
        key = (chat_id, message_id)

        if key in self._seen_messages:
            self._seen_messages.move_to_end(key)
            return True

        self._seen_messages[key] = None
        if len(self._seen_messages) > self._SEEN_MESSAGES_MAXSIZE:
            self._seen_messages.popitem(last=False)

        return False

    async def handle_new_message(self, event: events.NewMessage.Event) -> None:
        """Точка входа для новых сообщений — регистрируется как обработчик у Telethon."""
        # Защита от повторной доставки — самая первая проверка, ещё дешевле
        # ignored_sender_ids: если это дубль, остальная обработка (включая
        # диагностику ниже) вообще не нужна.
        if self._is_duplicate_message(event.chat_id, event.id):
            logger.debug(
                "Skipping duplicate message\nchat_id=%s\nmessage_id=%s",
                event.chat_id,
                event.id,
            )
            return

        # Явные исключения (config.yaml: ignored_sender_ids) — самая дешёвая
        # проверка, без единого резолва/обращения к сети, раньше всего
        # остального.
        if event.sender_id in self._ignored_sender_ids:
            logger.debug("Skipped ignored sender_id=%s", event.sender_id)
            return

        if self._debug_events:
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

        text = event.raw_text

        if not text:
            return

        resolved = self._resolved.get(event.chat_id)

        sender_id, username, display_name, is_bot = await self._resolve_sender(event)

        if username and username.lower().lstrip("@") in self._ignored_usernames:
            logger.debug("Skipped ignored username=%s", username)
            return

        if display_name and display_name in self._ignored_display_names:
            logger.debug('Skipping ignored display name: "%s"', display_name)
            return

        if is_bot:
            logger.debug("Skipping Telegram bot account | username=%s", username)
            return

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
            logger.warning(
                "QUEUE PUT\nevent_id=%s\nchat_id=%s\ntitle=%s",
                message.id,
                message.chat_id,
                message.chat_title,
            )

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

        internal_id = str(chat_id).removeprefix("-100")

        return f"https://t.me/c/{internal_id}/{message_id}"

    async def messages(self) -> AsyncIterator[Message]:
        while True:
            yield await self._queue.get()

    async def stop(self) -> None:
        await self._client.disconnect()
        logger.info("Отключено от Telegram")