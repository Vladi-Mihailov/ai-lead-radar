import logging

from telethon import TelegramClient, events

from reader.commands.base import Command, CommandContext, CommandError

logger = logging.getLogger(__name__)


class CommandDispatcher:
    """Единая точка входа для служебных команд оператора (fine, ...).

    Регистрируется как ещё один NewMessage-handler на уже подключённом
    Telethon-клиенте (TelegramSource.client) — второе подключение к Telegram
    не создаётся, ровно так же, как TelegramSink переиспользует тот же
    клиент. Чат оператора не входит в config/groups.yaml, поэтому команды
    физически не попадают в handle_new_message/keyword pipeline: Telethon
    рассылает апдейт только тем handler'ам, чей chats= совпал.
    """

    def __init__(
        self,
        client: TelegramClient,
        chat_id: int | str,
        allowed_user_ids: list[int],
    ):
        self._client = client
        self._chat_id = chat_id
        self._allowed_user_ids = set(allowed_user_ids)
        self._commands: dict[str, Command] = {}

    def register(self, command: Command) -> None:
        name = command.name.lower()
        if name in self._commands:
            raise ValueError(f"Команда '{name}' уже зарегистрирована")
        self._commands[name] = command

    async def start(self) -> None:
        try:
            entity = await self._client.get_entity(self._chat_id)
        except Exception as exc:
            logger.error("✖ Чат оператора '%s' не найден", self._chat_id)
            raise RuntimeError(
                f"Не удалось найти чат оператора '{self._chat_id}'. "
                f"Убедитесь, что аккаунт состоит в этом чате. Причина: {exc}"
            ) from exc

        self._client.add_event_handler(
            self.handle_event,
            events.NewMessage(chats=[entity]),
        )

        logger.info("✔ Диспетчер команд подключён к чату оператора")

    async def handle_event(self, event: events.NewMessage.Event) -> None:
        # Явная проверка user_id — самая дешёвая, до любого парсинга.
        # Фильтр chats= в start() уже гарантирует нужный чат; здесь отсекаем
        # по отправителю внутри этого чата (например, если это группа).
        if event.sender_id not in self._allowed_user_ids:
            logger.info(
                "Команда fine проигнорирована:\nsender_id=%s,\nallowed_user_ids=%s",
                event.sender_id,
                sorted(self._allowed_user_ids),
            )
            return

        text = event.raw_text
        if not text or not text.strip():
            return

        parts = text.strip().split()
        command_name = parts[0].lower()
        args = parts[1:]

        command = self._commands.get(command_name)
        if command is None:
            # Не всякое сообщение в этом чате обязано быть командой —
            # молча игнорируем, а не заваливаем чат "неизвестная команда"
            # на каждую обычную реплику.
            return

        ctx = CommandContext(
            chat_id=event.chat_id,
            user_id=event.sender_id,
            args=args,
            raw_text=text,
            event=event,
        )

        try:
            result = await command.handle(ctx)
            reply_text = result.text
        except CommandError as exc:
            reply_text = exc.message
        except Exception:
            logger.exception("Команда '%s' завершилась с ошибкой", command_name)
            reply_text = "⚠ Внутренняя ошибка при обработке команды"

        if reply_text:
            await event.respond(reply_text)
