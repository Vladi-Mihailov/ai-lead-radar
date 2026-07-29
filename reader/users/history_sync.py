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

# ---- ВРЕМЕННАЯ ДИАГНОСТИКА ----
# GroupAnonymousBot — служебный аккаунт Telegram (не Telethon), от имени
# которого приходят сообщения анонимных администраторов в группах.
_ANONYMOUS_ADMIN_ID = 1087968824
_DIAG_DUMP_LIMIT = 50
# --------------------------------

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

    # ---- ВРЕМЕННАЯ ДИАГНОСТИКА: переживает FloodWait-повторы, не сбрасывается по пакетам ----
    diag_ever_unresolvable: set[int] = set()
    # -----------------------------------------------------------------------------------------

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

        # ---- ВРЕМЕННАЯ ДИАГНОСТИКА (см. обсуждение "мало новых пользователей") ----
        diag_with_sender_id = 0
        diag_without_sender_id = 0
        diag_unique_sender_ids: set[int] = set()
        diag_found_in_repository = 0
        diag_missing_from_repository = 0
        diag_get_sender_success = 0
        diag_get_sender_failed = 0
        diag_channel_sender = 0
        diag_bot_sender = 0
        diag_service_messages = 0

        # разбивка уникальных sender_id на категории (считается один раз за пакет на sender_id)
        diag_classified_this_batch: set[int] = set()
        diag_cat_already_in_db = 0
        diag_cat_resolved_ok = 0
        diag_cat_unresolvable = 0
        diag_cat_anonymous_admin = 0
        diag_cat_channel = 0
        diag_cat_deleted = 0
        diag_repeated_failed_attempts = 0

        # построчный дамп первых N уникальных sender_id — только для пакета №1
        diag_dump_seen: set[int] = set()
        diag_dump_count = 0
        # -----------------------------------------------------------------------

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

                # ---- ВРЕМЕННАЯ ДИАГНОСТИКА ----
                if getattr(message, "action", None) is not None:
                    diag_service_messages += 1
                # --------------------------------

                if sender_id:
                    diag_with_sender_id += 1
                    diag_unique_sender_ids.add(sender_id)
                    if sender_id == chat_id:
                        diag_channel_sender += 1

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

                    # для диагностического дампа ниже — остаётся None, если
                    # get_sender() в этой итерации не вызывался вовсе
                    sender = None
                    get_sender_called = not lookup_failed and existing is None

                    if not lookup_failed and existing is None:
                        diag_missing_from_repository += 1

                        # ---- ВРЕМЕННАЯ ДИАГНОСТИКА: повторная попытка для того,
                        # кто уже не резолвился раньше в этом запуске? ----
                        if sender_id in diag_ever_unresolvable:
                            diag_repeated_failed_attempts += 1
                        # -----------------------------------------------------

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

                        # ---- ВРЕМЕННАЯ ДИАГНОСТИКА: категория уникального sender_id ----
                        if sender_id not in diag_classified_this_batch:
                            diag_classified_this_batch.add(sender_id)
                            if sender_id == chat_id:
                                diag_cat_channel += 1
                            elif sender_id == _ANONYMOUS_ADMIN_ID:
                                diag_cat_anonymous_admin += 1
                            elif sender is None:
                                diag_cat_unresolvable += 1
                            elif bool(getattr(sender, "deleted", False)):
                                diag_cat_deleted += 1
                            else:
                                diag_cat_resolved_ok += 1
                        if sender is None:
                            diag_ever_unresolvable.add(sender_id)
                        # -----------------------------------------------------------------

                        if sender is not None:
                            diag_get_sender_success += 1
                            if bool(getattr(sender, "bot", False)):
                                diag_bot_sender += 1

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
                        else:
                            diag_get_sender_failed += 1
                    elif not lookup_failed:
                        diag_found_in_repository += 1
                        if sender_id not in diag_classified_this_batch:
                            diag_classified_this_batch.add(sender_id)
                            diag_cat_already_in_db += 1
                        if existing.is_bot:
                            diag_bot_sender += 1
                    # existing уже в кэше (или чтение не удалось) — ни сети,
                    # ни записи для этого сообщения не требуется.

                    # ---- ВРЕМЕННАЯ ДИАГНОСТИКА: построчный дамп первых N уникальных sender_id пакета №1 ----
                    if (
                        batch_number == 1
                        and diag_dump_count < _DIAG_DUMP_LIMIT
                        and sender_id not in diag_dump_seen
                    ):
                        diag_dump_seen.add(sender_id)
                        diag_dump_count += 1

                        raw_sender = message.sender
                        entity_type = type(raw_sender).__name__ if raw_sender is not None else "None"

                        logger.info(
                            "[ДИАГНОСТИКА, дамп #%d/%d]\n"
                            "  message_id: %d\n"
                            "  sender_id: %s\n"
                            "  type(sender_id): %s\n"
                            "  message.sender: %r\n"
                            "  entity_type: %s\n"
                            "  username: %s\n"
                            "  first_name: %s\n"
                            "  last_name: %s\n"
                            "  найден в UserRepository: %s\n"
                            "  get_sender() вызывался: %s\n"
                            "  результат get_sender(): %r",
                            diag_dump_count,
                            _DIAG_DUMP_LIMIT,
                            message.id,
                            sender_id,
                            type(sender_id).__name__,
                            raw_sender,
                            entity_type,
                            getattr(raw_sender, "username", None),
                            getattr(raw_sender, "first_name", None),
                            getattr(raw_sender, "last_name", None),
                            existing is not None,
                            get_sender_called,
                            sender if get_sender_called else "не вызывался (найден в кэше)",
                        )
                    # -------------------------------------------------------------------------------
                else:
                    diag_without_sender_id += 1

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

                    # ---- ВРЕМЕННАЯ ДИАГНОСТИКА ----
                    logger.info(
                        "[ДИАГНОСТИКА, временно] Пакет №%d\n"
                        "  сообщений с sender_id: %d\n"
                        "  сообщений без sender_id: %d\n"
                        "  уникальных sender_id в пакете: %d\n"
                        "    из них — уже были в users.db: %d\n"
                        "    из них — успешно дорезолвились через get_sender(): %d\n"
                        "    из них — не удалось дорезолвить: %d\n"
                        "    из них — анонимный администратор: %d\n"
                        "    из них — канал: %d\n"
                        "    из них — удалённый пользователь: %d\n"
                        "  найдено в локальной БД (по сообщениям): %d\n"
                        "  отсутствовало в локальной БД (запрошен get_sender, по сообщениям): %d\n"
                        "  get_sender() успешно (по сообщениям): %d\n"
                        "  get_sender() неуспешно (None/исключение, по сообщениям): %d\n"
                        "  повторных попыток get_sender() для уже нерезолвившихся sender_id: %d\n"
                        "  отправитель — бот: %d\n"
                        "  служебных сообщений (join/leave/pin и т.п.): %d",
                        batch_number,
                        diag_with_sender_id,
                        diag_without_sender_id,
                        len(diag_unique_sender_ids),
                        diag_cat_already_in_db,
                        diag_cat_resolved_ok,
                        diag_cat_unresolvable,
                        diag_cat_anonymous_admin,
                        diag_cat_channel,
                        diag_cat_deleted,
                        diag_found_in_repository,
                        diag_missing_from_repository,
                        diag_get_sender_success,
                        diag_get_sender_failed,
                        diag_repeated_failed_attempts,
                        diag_bot_sender,
                        diag_service_messages,
                    )
                    diag_with_sender_id = 0
                    diag_without_sender_id = 0
                    diag_unique_sender_ids = set()
                    diag_found_in_repository = 0
                    diag_missing_from_repository = 0
                    diag_get_sender_success = 0
                    diag_get_sender_failed = 0
                    diag_channel_sender = 0
                    diag_bot_sender = 0
                    diag_service_messages = 0
                    diag_classified_this_batch = set()
                    diag_cat_already_in_db = 0
                    diag_cat_resolved_ok = 0
                    diag_cat_unresolvable = 0
                    diag_cat_anonymous_admin = 0
                    diag_cat_channel = 0
                    diag_cat_deleted = 0
                    diag_repeated_failed_attempts = 0
                    diag_dump_seen = set()
                    diag_dump_count = 0
                    # --------------------------------

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
