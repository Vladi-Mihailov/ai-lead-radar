import asyncio
import logging

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from reader.groups import Group
from reader.scenarios import KeywordMatcher
from reader.users.history_state_repository import HistorySyncStateRepository
from reader.users.keyword_matches import unique_keywords
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


def _upsert_sender(repository: UserRepository, sender_id: int, sender) -> bool:
    """Строит TelegramUserInfo из настоящего объекта Telethon User/Channel и
    сохраняет его. Возвращает True при успехе."""
    try:
        repository.upsert(
            TelegramUserInfo(
                user_id=sender_id,
                username=getattr(sender, "username", None),
                first_name=getattr(sender, "first_name", None),
                last_name=getattr(sender, "last_name", None),
                is_bot=bool(getattr(sender, "bot", False)),
                # sender — полноценный объект Telethon User — сохраняем
                # access_hash для восстановления InputPeerUser без @username.
                access_hash=getattr(sender, "access_hash", None),
                peer_type=type(sender).__name__,
            )
        )
    except Exception:
        logger.warning("Не удалось сохранить пользователя %s в локальный кэш", sender_id)
        return False
    return True


async def _resolve_and_upsert_pending(
    client: TelegramClient,
    repository: UserRepository,
    pending: dict[int, bool],
    failed_identity_refresh: set[int],
) -> tuple[int, int]:
    """Резолвит все накопленные user_id ОДНИМ пакетным client.get_entity()
    вместо отдельного RPC на каждого — Telethon сам разбивает большие списки
    на запросы по 200 (лимит GetUsersRequest), так что даже сотни
    пользователей обходятся считанными запросами. Именно вызов get_sender()
    (или get_entity()) один раз НА КАЖДОЕ СООБЩЕНИЕ активного автора вместо
    одного раза на пользователя и был причиной практически непрерывного
    FloodWait при --reindex.

    pending — {user_id: is_new}, is_new только для статистики (не считать
    уже известных пользователей "новыми" при переиндексации). Очищается
    после вызова независимо от результата.

    Сначала для каждого id проверяется, есть ли он в локальном кэше сущностей
    Telethon (client.get_input_entity() — чистый lookup, без единого RPC для
    голого положительного int, см. докстрок get_input_entity()). Только
    закэшированные id идут в пакетный get_entity(); те, что не резолвятся
    даже так, изолируются заранее — иначе один такой id рушит get_entity()
    целиком (см. диагностику и объяснение в history_sync.py issue про
    "Пакетное получение ... не удалось").

    Любой user_id, для которого резолв так и не удался (ни пакетно, ни по
    одному), добавляется в failed_identity_refresh — до конца текущего
    вызова sync_users_from_history() к нему больше не будет попытки RPC
    (см. needs_refresh в _sync_group_history). В следующем запуске программы
    множество создаётся заново, так что попытка повторится.
    """
    if not pending:
        return 0, 0

    user_ids = list(pending.keys())

    # ВРЕМЕННАЯ ДИАГНОСТИКА — убрать после того, как причина падения
    # пакетного get_entity() будет подтверждена в проде. Печатает, какие
    # именно id не проходят даже локальный (безсетевой) lookup, и почему.
    for user_id in user_ids:
        if user_id is None:
            logger.warning("[DIAG] None среди id, ожидающих резолва — пропускаю")
            continue
        if not isinstance(user_id, int) or user_id <= 0:
            logger.warning(
                "[DIAG] Подозрительный id среди ожидающих резолва: %r (тип %s)",
                user_id, type(user_id).__name__,
            )

    resolvable_ids = []
    unresolvable_ids = []
    for user_id in user_ids:
        try:
            input_entity = await client.get_input_entity(user_id)
        except Exception as exc:
            unresolvable_ids.append(user_id)
            logger.warning(
                "[DIAG] user_id=%s не резолвится даже в input_entity "
                "(нет в локальном кэше Telethon, без сети): %s: %s",
                user_id, type(exc).__name__, exc,
            )
        else:
            resolvable_ids.append(user_id)
            logger.warning(
                "[DIAG] user_id=%s -> %s", user_id, type(input_entity).__name__
            )

    if unresolvable_ids:
        logger.warning(
            "[DIAG] Исключено из пакетного резолва %d из %d id (не в локальном "
            "кэше Telethon): %s",
            len(unresolvable_ids), len(user_ids), unresolvable_ids,
        )

    entities_by_id: dict[int, object] = {}
    if resolvable_ids:
        try:
            entities = await client.get_entity(resolvable_ids)
            entities_by_id = dict(zip(resolvable_ids, entities))
        except Exception as exc:
            # Пакетный запрос всё равно может упасть целиком (например,
            # get_input_entity() выше был не единственным местом, где
            # возможна ошибка) — дорезолвливаем по одному, чтобы не терять
            # остальных валидных из этого набора.
            logger.warning(
                "[DIAG] Пакетное получение %d пользователей не удалось: %s: %s "
                "— дорезолвливаю по одному",
                len(resolvable_ids), type(exc).__name__, exc,
            )
            for user_id in resolvable_ids:
                try:
                    entities_by_id[user_id] = await client.get_entity(user_id)
                except Exception as exc2:
                    logger.warning(
                        "[DIAG] user_id=%s не резолвился и по одному: %s: %s",
                        user_id, type(exc2).__name__, exc2,
                    )
                    entities_by_id[user_id] = None
    # --- конец временной диагностики ---

    upserted = 0
    new_count = 0
    for user_id in user_ids:
        sender = entities_by_id.get(user_id)
        if sender is None:
            # Резолв не удался ни пакетно, ни по одному (см. unresolvable_ids
            # выше и entities_by_id[user_id] = None в фоллбэке) — не пытаемся
            # снова до конца этого запуска sync_users.py.
            failed_identity_refresh.add(user_id)
            continue
        if _upsert_sender(repository, user_id, sender):
            upserted += 1
            if pending[user_id]:
                new_count += 1
        # _upsert_sender() сам залогировал сбой записи в локальный кэш —
        # сущность РЕЗОЛВИЛАСЬ успешно, поэтому в failed_identity_refresh не
        # попадает: следующее сообщение того же автора получит ещё одну
        # попытку записи (см. existing is None), а не будет пропущено вовсе.

    pending.clear()
    return upserted, new_count


async def sync_users_from_history(
    client: TelegramClient,
    groups: list[Group],
    repository: UserRepository,
    state_repository: HistorySyncStateRepository,
    matcher: KeywordMatcher,
    *,
    force: bool = False,
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

    matcher — тот же KeywordMatcher, что использует reader/main.py (Pipeline)
    для новых сообщений: для каждого сообщения с известным отправителем
    сообщение прогоняется через него, и найденные keywords сохраняются в
    UserRepository (см. _sync_group_history) — независимо от того, штатный
    ли это отправитель или уже известный локально.

    force=True (см. sync_users.py --reindex) — полностью игнорирует
    checkpoint: не пропускает уже "завершённые" группы и заново запрашивает
    полную информацию об отправителе (а не только для новых) для каждой
    группы, чтобы досчитать поля, добавленные после того, как группа была
    полностью проиндексирована в прошлый раз (например, keywords/access_hash).
    Информация запрашивается не более одного раза на пользователя за весь
    этот вызов (не на каждое его сообщение и не повторно в каждой следующей
    группе, см. refreshed_user_ids в _sync_group_history), и по возможности
    вообще без RPC — сначала используется message.sender (уже пришедший
    вместе со страницей истории), а если его не хватает — резолв идёт одним
    пакетным запросом на чекпоинт, а не по одному (см.
    _resolve_and_upsert_pending).

    Отдельно: если для пользователя (обычно совсем нового, ранее не
    встречавшегося) резолв так и не удался — ни пакетно, ни по одному — до
    конца ЭТОГО вызова к нему больше не будет попытки RPC (см.
    failed_identity_refresh); в следующем запуске sync_users.py попытка
    повторится, так как это множество создаётся заново при каждом вызове.
    """

    logger.info("Синхронизация истории начата%s", " (принудительная переиндексация)" if force else "")

    # Пользователи, для которых access_hash/username уже переспрошены в
    # этом прогоне --reindex — общий для всех групп набор, чтобы активный
    # автор, написавший тысячи сообщений (в одной группе или в нескольких),
    # не резолвился повторно на каждое из них: раньше это приводило к
    # практически непрерывному FloodWait вместо пакетов по 10000 сообщений.
    refreshed_user_ids: set[int] = set()

    # Пользователи, для которых попытка полного резолва (пакетного и
    # одиночного) уже провалилась в этом прогоне — например, id, которого
    # Telethon принципиально не может найти без контекста сообщения. Без
    # этого множества такой пользователь заново попадал бы в пакетный запрос
    # на каждом следующем чекпоинт-окне (existing остаётся None навсегда, а
    # значит needs_refresh остаётся истинным), пока продолжает встречаться
    # в истории. Общий для всех групп по той же причине, что и
    # refreshed_user_ids.
    failed_identity_refresh: set[int] = set()

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
        await _sync_group_history(
            client, entity, title, repository, state_repository, matcher,
            force=force, refreshed_user_ids=refreshed_user_ids,
            failed_identity_refresh=failed_identity_refresh,
        )


async def _sync_group_history(
    client: TelegramClient,
    entity,
    title: str,
    repository: UserRepository,
    state_repository: HistorySyncStateRepository,
    matcher: KeywordMatcher,
    *,
    force: bool = False,
    refreshed_user_ids: set[int],
    failed_identity_refresh: set[int],
) -> None:
    chat_id = entity.id
    checkpoint = state_repository.get(chat_id)

    if checkpoint and checkpoint.history_completed and not force:
        logger.info(
            "Группа: %s — история уже полностью проиндексирована, пропускаю", title
        )
        return

    if force and checkpoint:
        logger.info(
            "Группа: %s — принудительная переиндексация, игнорирую checkpoint "
            "(был: message_id=%s, завершён=%s)",
            title,
            checkpoint.oldest_processed_message_id,
            checkpoint.history_completed,
        )

    logger.info("Группа: %s", title)

    flood_wait_retries = 0
    # Игнорируем сохранённый checkpoint только для самого первого прохода
    # цикла этой группы — если после него потребуется повтор (FloodWait),
    # дальше резюмируемся уже от прогресса, который сохранил сам этот же
    # прогон переиндексации, а не от старого checkpoint.
    ignore_checkpoint = force

    while True:
        # Перечитываем checkpoint на каждой попытке — при повторе после
        # FloodWait он уже мог продвинуться благодаря периодическим save.
        checkpoint = None if ignore_checkpoint else state_repository.get(chat_id)
        ignore_checkpoint = False
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
        # Отправители, которых нужно обновить, но для которых сообщение НЕ
        # принесло готовый объект User (см. ниже) — резолвятся одним пакетным
        # запросом на чекпоинте, а не по одному сразу же. Сбрасывается на
        # каждой новой попытке цикла (после FloodWait/сбоя) — неотправленные
        # записи всё равно попадут в него снова при переобработке тех же
        # сообщений после возобновления с последнего сохранённого checkpoint.
        pending_identity_refresh: dict[int, bool] = {}

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

                    # Ключевые слова ищем независимо от того, известен ли
                    # уже отправитель локально — тем же KeywordMatcher, что
                    # использует reader/main.py (Pipeline) для новых
                    # сообщений. Никакого сетевого запроса не требует, текст
                    # уже пришёл вместе с сообщением истории.
                    matches = matcher.match(message.raw_text or "")
                    if matches:
                        try:
                            repository.add_keywords(sender_id, unique_keywords(matches))
                        except Exception:
                            logger.warning(
                                "Не удалось обновить keywords пользователя %s",
                                sender_id,
                            )

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

                    # Пользователя ещё нет в кэше — только в этом случае есть
                    # смысл спрашивать Telegram: резолв может уйти в сеть, если
                    # отправитель пришёл в составе страницы истории как
                    # "min"-сущность — а для уже известных пользователей это
                    # лишний сетевой запрос на каждое сообщение без всякой
                    # пользы.
                    #
                    # force=True (переиндексация) — исключение: обновляем
                    # даже уже известных отправителей, чтобы досчитать поля
                    # вроде access_hash, которых не было при первом проходе
                    # (см. sync_users.py --reindex). НО только один раз на
                    # пользователя за весь этот вызов sync_users_from_history()
                    # (refreshed_user_ids), а не на каждое его сообщение.
                    needs_refresh = existing is None or (
                        force and sender_id not in refreshed_user_ids
                    )
                    if not lookup_failed and needs_refresh:
                        if force:
                            # Не более одной попытки на пользователя за
                            # прогон — независимо от того, как именно она
                            # будет разрешена (сразу или пакетно ниже).
                            refreshed_user_ids.add(sender_id)

                        # message.sender — то, что Telegram уже прислал
                        # вместе с этой же страницей истории (без единого
                        # RPC): для iter_messages() это обычно полноценный
                        # объект, а не "min"-заглушка, потому что автор
                        # сообщения в этом же чате был отправлен вместе с
                        # ответом. Раньше здесь всегда вызывался
                        # message.get_sender(), который для КАЖДОГО такого
                        # сообщения повторно уходил в сеть — это и было
                        # причиной практически непрерывного FloodWait при
                        # --reindex (RPC на каждое сообщение активного автора,
                        # а не один раз на автора).
                        sender = message.sender
                        if sender is not None and not getattr(sender, "min", False):
                            if _upsert_sender(repository, sender_id, sender) and existing is None:
                                saved_users_total += 1
                                new_users_in_batch += 1
                        elif sender_id not in failed_identity_refresh:
                            # Не хватает данных в самом сообщении — резолвим
                            # позже одним пакетным запросом на чекпоинте
                            # (см. _resolve_and_upsert_pending), а не отдельным
                            # RPC прямо здесь.
                            pending_identity_refresh[sender_id] = existing is None
                        # else: RPC-резолв этого пользователя уже пробовали и
                        # не смогли в этом же прогоне (failed_identity_refresh)
                        # — не повторяем попытку до следующего запуска
                        # sync_users.py. message.sender мы всё равно уже
                        # проверили бесплатно чуть выше — если бы он оказался
                        # заполнен, пользователь был бы обновлён в ветке if.
                    # existing уже в кэше и не идёт переиндексация (или чтение
                    # не удалось) — ни сети, ни записи для этого сообщения не
                    # требуется.

                if since_checkpoint >= _CHECKPOINT_INTERVAL:
                    _, new_count = await _resolve_and_upsert_pending(
                        client, repository, pending_identity_refresh, failed_identity_refresh,
                    )
                    saved_users_total += new_count
                    new_users_in_batch += new_count
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

        # Дорезолвливаем всё, что накопилось после последнего checkpoint —
        # иначе "хвост" группы (< _CHECKPOINT_INTERVAL сообщений) остался бы
        # без access_hash/данных до следующего запуска.
        _, new_count = await _resolve_and_upsert_pending(
            client, repository, pending_identity_refresh, failed_identity_refresh,
        )
        saved_users_total += new_count
        new_users_in_batch += new_count

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
