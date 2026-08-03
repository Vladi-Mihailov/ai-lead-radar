import asyncio
import logging

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from reader.groups import Group
from reader.users.history_state_repository import HistorySyncStateRepository
from reader.users.models import TelegramUserInfo
from reader.users.repository import UserRepository

logger = logging.getLogger(__name__)

_BATCH_SIZE = 10_000
_CHECKPOINT_INTERVAL = 500
_MAX_FLOOD_WAIT_RETRIES = 3

_SEPARATOR = "─" * 40


def _log_line(label: str, value) -> str:
    return f"{label:<42}{value:>10}"


def _safe_users_count(repository: UserRepository) -> object:
    """Значение только для отображения в логе — сбой не должен ронять синхронизацию."""
    try:
        return repository.count()
    except Exception:
        return "н/д"


async def sync_users_from_history(
    client: TelegramClient,
    groups: list[Group],
    repository: UserRepository,
    state_repository: HistorySyncStateRepository,
) -> None:
    """Инкрементально проходит историю сообщений групп и сохраняет авторов.

    В отличие от sync_all_users() (список текущих участников), здесь
    источник — реальные авторы сообщений, что может дать значительно больше
    пользователей для групп, где список участников недоступен или урезан
    сервером.

    Прогресс по каждой группе сохраняется в HistorySyncStateRepository по
    message_id (checkpoint), поэтому история никогда не перечитывается
    заново — при повторном запуске обход продолжается с места остановки, а
    группы с полностью пройденной историей пропускаются мгновенно.
    """

    logger.info("Синхронизация истории начата")

    for group in groups:
        try:
            entity = await client.get_entity(group.identifier)
        except Exception:
            logger.warning(
                "✖ Группа '%s' не найдена, синхронизация истории пропущена",
                group.identifier,
                exc_info=True,
            )
            continue

        title = group.title or getattr(entity, "title", None) or str(group.identifier)
        await _sync_group_history(client, entity, title, repository, state_repository)


async def _sync_group_history(
    client: TelegramClient,
    entity,
    title: str,
    repository: UserRepository,
    state_repository: HistorySyncStateRepository,
) -> None:
    chat_id = entity.id
    checkpoint = state_repository.get(chat_id)

    if checkpoint and checkpoint.history_completed:
        logger.info(
            "Группа: %s — история уже полностью проиндексирована, пропускаю", title
        )
        return

    logger.info("Группа: %s", title)

    flood_wait_retries = 0

    while True:
        # Перечитываем checkpoint на каждой попытке — при повторе после
        # FloodWait он уже мог продвинуться благодаря периодическим save.
        checkpoint = state_repository.get(chat_id)
        offset_id = checkpoint.oldest_processed_message_id if checkpoint else 0
        processed_messages = checkpoint.processed_messages if checkpoint else 0
        saved_users_total = checkpoint.saved_users if checkpoint else 0

        if checkpoint:
            logger.info("Checkpoint: message_id=%d", offset_id)
        else:
            logger.info("Checkpoint: отсутствует, начинаю с самого начала истории")

        last_message_id = offset_id
        last_message_date = checkpoint.oldest_processed_date if checkpoint else None

        since_checkpoint = 0
        since_batch = 0
        new_users_in_batch = 0
        unique_sender_ids_in_batch: set[int] = set()
        batch_number = processed_messages // _BATCH_SIZE + 1

        def save_checkpoint(history_completed: bool) -> None:
            state_repository.save_progress(
                chat_id=chat_id,
                chat_name=title,
                oldest_processed_message_id=last_message_id,
                oldest_processed_date=last_message_date,
                processed_messages=processed_messages,
                saved_users=saved_users_total,
                history_completed=history_completed,
            )

        try:
            async for message in client.iter_messages(entity, offset_id=offset_id, limit=None):
                processed_messages += 1
                since_checkpoint += 1
                since_batch += 1
                last_message_id = message.id
                last_message_date = message.date

                sender_id = message.sender_id
                if sender_id:
                    unique_sender_ids_in_batch.add(sender_id)

                    try:
                        existing = repository.get(sender_id)
                        lookup_failed = False
                    except Exception:
                        logger.warning(
                            "Не удалось прочитать локальный кэш пользователя %s, пропускаю",
                            sender_id,
                        )
                        existing = None
                        lookup_failed = True

                    if not lookup_failed and existing is None:
                        # Пользователя ещё нет в кэше — только в этом случае есть
                        # смысл спрашивать Telegram: get_sender() может уйти в
                        # сеть (GetUsersRequest), если отправитель пришёл в
                        # составе страницы истории как "min"-сущность — а для
                        # уже известных пользователей это лишний сетевой запрос
                        # на каждое сообщение без всякой пользы.
                        try:
                            sender = await message.get_sender()
                        except Exception:
                            sender = None

                        if sender is not None:
                            try:
                                repository.upsert(
                                    TelegramUserInfo(
                                        user_id=sender_id,
                                        username=getattr(sender, "username", None),
                                        first_name=getattr(sender, "first_name", None),
                                        last_name=getattr(sender, "last_name", None),
                                        is_bot=bool(getattr(sender, "bot", False)),
                                    )
                                )
                            except Exception:
                                logger.warning(
                                    "Не удалось сохранить пользователя %s в локальный кэш",
                                    sender_id,
                                )
                            else:
                                saved_users_total += 1
                                new_users_in_batch += 1
                    # existing уже в кэше (или чтение не удалось) — ни сети,
                    # ни записи для этого сообщения не требуется.

                if since_checkpoint >= _CHECKPOINT_INTERVAL:
                    save_checkpoint(history_completed=False)
                    since_checkpoint = 0

                if since_batch >= _BATCH_SIZE:
                    logger.info(
                        "[%s] Пакет №%d\n"
                        "%s\n"
                        "%s\n"
                        "%s\n"
                        "\n"
                        "%s\n"
                        "%s\n"
                        "%s\n"
                        "\n"
                        "%s\n"
                        "%s",
                        title,
                        batch_number,
                        _SEPARATOR,
                        _log_line("Сообщений обработано в пакете:", processed_messages),
                        _log_line("Уникальных sender_id в пакете:", len(unique_sender_ids_in_batch)),
                        _log_line("Новых пользователей в этом пакете:", new_users_in_batch),
                        _log_line("Новых пользователей всего по группе:", saved_users_total),
                        _log_line("Пользователей в users.db (все группы):", _safe_users_count(repository)),
                        _log_line("Checkpoint сохранён, oldest_message_id:", last_message_id),
                        _SEPARATOR,
                    )

                    batch_number += 1
                    new_users_in_batch = 0
                    unique_sender_ids_in_batch = set()
                    since_batch = 0
        except FloodWaitError as exc:
            flood_wait_retries += 1
            if flood_wait_retries > _MAX_FLOOD_WAIT_RETRIES:
                logger.warning(
                    "Telegram снова ограничил запросы при обходе истории группы '%s' "
                    "(нужно подождать %d сек.), превышено число повторов (%d) — "
                    "обработано сообщений: %d, всего пользователей: %d. "
                    "Продолжу при следующем запуске",
                    title,
                    exc.seconds,
                    _MAX_FLOOD_WAIT_RETRIES,
                    processed_messages,
                    saved_users_total,
                    exc_info=True,
                )
                return

            logger.warning(
                "Telegram ограничил запросы при обходе истории группы '%s' "
                "(жду %d сек., попытка %d из %d) — обработано сообщений: %d, "
                "всего пользователей: %d",
                title,
                exc.seconds,
                flood_wait_retries,
                _MAX_FLOOD_WAIT_RETRIES,
                processed_messages,
                saved_users_total,
            )
            await asyncio.sleep(exc.seconds)
            continue
        except Exception:
            logger.warning(
                "Не удалось обойти историю группы '%s', синхронизация прервана "
                "(обработано сообщений: %d, всего пользователей: %d). Checkpoint "
                "сохранён максимум %d сообщений назад, продолжу со следующего запуска",
                title,
                processed_messages,
                saved_users_total,
                _CHECKPOINT_INTERVAL,
                exc_info=True,
            )
            return

        save_checkpoint(history_completed=True)
        logger.info(
            "[%s] История полностью проиндексирована\n"
            "%s\n"
            "%s\n"
            "%s\n"
            "%s\n"
            "%s",
            title,
            _SEPARATOR,
            _log_line("Обработано сообщений всего по группе:", processed_messages),
            _log_line("Новых пользователей всего по группе:", saved_users_total),
            _log_line("Пользователей в users.db (все группы):", _safe_users_count(repository)),
            _SEPARATOR,
        )
        return
