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
# GetUsersRequest (внутри client.get_entity()) сам режет список на чанки по
# 200 — значит, число RPC за один флуш pending_identity_refresh не может
# стать меньше ceil(len(pending) / 200), сколько бы сообщений мы ни ждали.
# Флуш по достижении порога, близкого к 200 (а не по количеству сообщений,
# как раньше единственный флуш на _CHECKPOINT_INTERVAL), не увеличивает
# общее число RPC, но не даёт pending разрастись сильно за 200 в активных
# группах и не откладывает получение access_hash дольше необходимого.
_PENDING_RESOLVE_THRESHOLD = 190

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


async def _resolve_one_by_one(
    client: TelegramClient, user_ids: list[int]
) -> tuple[dict[int, object], list[int]]:
    """Поштучный fallback — только для ошибок, НЕ являющихся FloodWaitError
    (та означает общую временную блокировку API, а не проблему конкретной
    сущности, см. _resolve_entity_batch). Если FloodWaitError всё же
    случится на отдельном id — цикл прерывается немедленно, без попыток на
    оставшихся: иначе каждый из них тоже почти сразу получил бы тот же
    FloodWaitError, порождая лавину одинаковых ошибок.

    Возвращает (entities_by_id, failed_ids) — failed_ids это только те id,
    для которых попытка была РЕАЛЬНО совершена и завершилась НЕ-FloodWait
    ошибкой. Id, до которых не дошли из-за прерывания по FloodWaitError, в
    failed_ids не попадают — они не считаются "проблемной сущностью" и
    просто останутся неразрешёнными до следующего чекпоинта/прогона.
    """
    entities_by_id: dict[int, object] = {}
    failed_ids: list[int] = []
    for index, user_id in enumerate(user_ids):
        try:
            entities_by_id[user_id] = await client.get_entity(user_id)
        except FloodWaitError as exc:
            # ВРЕМЕННАЯ ДИАГНОСТИКА — убрать после локализации источника
            # FloodWait. Именно этот RPC — одиночный get_entity() в
            # поштучном fallback (не пакетный).
            logger.warning(
                "[DIAG] FloodWait (%ds) while resolving users "
                "(single get_entity fallback)",
                exc.seconds,
            )
            logger.warning(
                "FloodWaitError при поштучном резолве (%d сек.) — прекращаю, "
                "%d пользователей останутся неразрешёнными до следующего "
                "чекпоинта",
                exc.seconds, len(user_ids) - index,
            )
            break
        except Exception:
            failed_ids.append(user_id)
    return entities_by_id, failed_ids


async def _resolve_entity_batch(
    client: TelegramClient, user_ids: list[int]
) -> tuple[dict[int, object], list[int]]:
    """Резолвит user_ids ОДНИМ пакетным client.get_entity() вместо отдельного
    RPC на каждого — Telethon сам разбивает большие списки на запросы по 200
    (лимит GetUsersRequest).

    FloodWaitError — общая временная блокировка Telegram API (не ошибка
    конкретного пакета или пользователя): при ней ждём exc.seconds и
    повторяем ТОТ ЖЕ пакетный запрос один раз, без перехода на поштучный
    fallback (который раньше на FloodWaitError немедленно давал каждому
    пользователю в наборе тот же FloodWaitError заново — лавина ошибок).
    Если блокировка не сошла и после повтора — оставляем весь набор
    неразрешённым до следующего чекпоинта/прогона, тоже без единого
    поштучного запроса.

    Поштучный fallback (_resolve_one_by_one) применяется только если ошибка
    НЕ FloodWaitError — то есть свидетельствует о проблеме с конкретными
    сущностями внутри пакета, а не с API в целом.

    Возвращает (entities_by_id, failed_ids) — см. _resolve_one_by_one.
    """
    try:
        entities = await client.get_entity(user_ids)
        return dict(zip(user_ids, entities)), []
    except FloodWaitError as exc:
        # ВРЕМЕННАЯ ДИАГНОСТИКА — убрать после локализации источника
        # FloodWait. Именно этот RPC — пакетный client.get_entity([...]).
        logger.warning(
            "[DIAG] FloodWait (%ds) while resolving users (batch get_entity)",
            exc.seconds,
        )
        logger.warning(
            "Пакетный резолв %d пользователей получил FloodWaitError (%d сек.) — "
            "жду и повторяю пакетный запрос один раз, без поштучного fallback",
            len(user_ids), exc.seconds,
        )
        await asyncio.sleep(exc.seconds)
        try:
            entities = await client.get_entity(user_ids)
            return dict(zip(user_ids, entities)), []
        except FloodWaitError as exc2:
            # ВРЕМЕННАЯ ДИАГНОСТИКА — тот же RPC, но уже повтор.
            logger.warning(
                "[DIAG] FloodWait (%ds) while resolving users "
                "(batch get_entity, retry)",
                exc2.seconds,
            )
            logger.warning(
                "FloodWaitError повторился (%d сек.) после повтора пакета — "
                "%d пользователей останутся неразрешёнными до следующего "
                "чекпоинта, поштучный fallback не выполняется",
                exc2.seconds, len(user_ids),
            )
            return {}, []
        except Exception:
            return await _resolve_one_by_one(client, user_ids)
    except Exception:
        # Не FloodWaitError — например, битый id, который get_input_entity()
        # выше не отфильтровал. Именно такие ошибки — про конкретную
        # сущность, поэтому здесь fallback оправдан.
        return await _resolve_one_by_one(client, user_ids)


async def _resolve_and_upsert_pending(
    client: TelegramClient,
    repository: UserRepository,
    pending: dict[int, bool],
    failed_identity_refresh: set[int],
) -> tuple[int, int]:
    """Резолвит все накопленные user_id одним пакетным запросом (см.
    _resolve_entity_batch) вместо отдельного RPC на каждого — именно вызов
    get_sender()/get_entity() один раз НА КАЖДОЕ СООБЩЕНИЕ активного автора
    вместо одного раза на пользователя был причиной практически
    непрерывного FloodWait при --reindex.

    pending — {user_id: is_new}, is_new только для статистики (не считать
    уже известных пользователей "новыми" при переиндексации). Очищается
    после вызова независимо от результата.

    Сначала для каждого id проверяется, есть ли он в локальном кэше сущностей
    Telethon (client.get_input_entity() — чистый lookup, без единого RPC для
    голого положительного int, см. докстрок get_input_entity()). Только
    закэшированные id идут в пакетный get_entity(); те, что не резолвятся
    даже так, изолируются заранее — иначе один такой id рушит get_entity()
    целиком.

    user_id считается окончательно неразрешимым (и попадает в
    failed_identity_refresh — до конца текущего вызова sync_users_from_history()
    к нему больше не будет попытки, см. needs_refresh в _sync_group_history)
    только если он не в локальном кэше ВООБЩЕ, либо был реально
    ЗАПРОШЕН и получил не-FloodWait ошибку. Id, отложенные из-за
    FloodWaitError, туда не попадают — они не "проблемная сущность", а
    жертва общего ограничения API, и должны получить новую попытку на
    следующем чекпоинте/прогоне.
    """
    if not pending:
        return 0, 0

    user_ids = list(pending.keys())

    resolvable_ids = []
    not_in_cache_ids = []
    for user_id in user_ids:
        try:
            await client.get_input_entity(user_id)
        except Exception:
            not_in_cache_ids.append(user_id)
        else:
            resolvable_ids.append(user_id)

    entities_by_id: dict[int, object] = {}
    failed_ids: list[int] = []
    if resolvable_ids:
        entities_by_id, failed_ids = await _resolve_entity_batch(client, resolvable_ids)

    for user_id in not_in_cache_ids:
        failed_identity_refresh.add(user_id)
    for user_id in failed_ids:
        failed_identity_refresh.add(user_id)

    upserted = 0
    new_count = 0
    for user_id in user_ids:
        sender = entities_by_id.get(user_id)
        if sender is None:
            continue
        if _upsert_sender(repository, user_id, sender):
            upserted += 1
            if pending[user_id]:
                new_count += 1
        # _upsert_sender() сам залогировал сбой записи в локальный кэш —
        # сущность РЕЗОЛВИЛАСЬ успешно, поэтому в failed_identity_refresh не
        # попадает: следующее сообщение того же автора получит ещё одну
        # попытку записи (см. existing is None), а не будет пропущено вовсе.

    deferred = len(user_ids) - upserted - len(not_in_cache_ids) - len(failed_ids)
    logger.info(
        "Резолв пользователей за чекпоинт: успешно %d, не в локальном кэше %d, "
        "не удалось %d, отложено (FloodWait) %d — всего %d",
        upserted, len(not_in_cache_ids), len(failed_ids), deferred, len(user_ids),
    )

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

    car_numbers сюда намеренно не входят — узкий отдельный backfill (см.
    reader/users/backfill_car_numbers.py) не должен пересчитывать keywords
    или заново резолвить access_hash/username, что делает --reindex, поэтому
    их не стоит связывать одним и тем же проходом (см. задачу).

    force=True (см. sync_users.py --reindex) — ведёт свой ОТДЕЛЬНЫЙ checkpoint
    (mode="reindex" в HistorySyncStateRepository), независимый от обычного
    инкрементального: первый запуск --reindex по группе начинает историю с
    самого начала (не глядя на инкрементальный checkpoint), а прерванный
    прогон --reindex (SSH/reboot/Ctrl+C) при следующем запуске продолжается
    именно с места остановки, не начиная заново. Заново запрашивает полную
    информацию об отправителе (а не только для новых), чтобы досчитать поля,
    добавленные после того, как группа была полностью проиндексирована в
    прошлый раз (например, keywords/access_hash). Информация запрашивается не
    более одного раза на пользователя за весь
    этот вызов (не на каждое его сообщение и не повторно в каждой следующей
    группе, см. refreshed_user_ids в _sync_group_history), и по возможности
    вообще без RPC — сначала используется message.sender (уже пришедший
    вместе со страницей истории), а если его не хватает — резолв идёт одним
    пакетным запросом, а не по одному (см. _resolve_and_upsert_pending).
    Флуш пакета происходит по чекпоинту ИЛИ раньше, если накопилось близко к
    _PENDING_RESOLVE_THRESHOLD уникальных пользователей — то, что наступит
    раньше (см. _PENDING_RESOLVE_THRESHOLD и _sync_group_history).

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
        except Exception as exc:
            if isinstance(exc, FloodWaitError):
                # ВРЕМЕННАЯ ДИАГНОСТИКА — убрать после локализации источника
                # FloodWait. Именно этот RPC — резолв самой группы.
                logger.warning(
                    "[DIAG] FloodWait (%ds) while resolving group entity ('%s')",
                    exc.seconds, group.identifier,
                )
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
    # Инкрементальный режим и --reindex ведут независимый прогресс по одной и
    # той же группе (см. HistorySyncStateRepository) — обычный checkpoint не
    # трогается и не читается в режиме reindex, и наоборот, поэтому прогон
    # --reindex, прерванный (SSH/reboot/Ctrl+C), продолжается со своего же
    # места, а не с начала истории заново.
    mode = "reindex" if force else "incremental"
    checkpoint = state_repository.get(chat_id, mode=mode)

    if checkpoint and checkpoint.history_completed:
        logger.info(
            "Группа: %s — история уже полностью проиндексирована (%s), пропускаю",
            title, mode,
        )
        return

    logger.info("Группа: %s", title)

    flood_wait_retries = 0

    while True:
        # Перечитываем checkpoint на каждой попытке — при повторе после
        # FloodWait (или при возобновлении прерванного прогона в новом
        # процессе) он уже мог продвинуться благодаря периодическим save.
        checkpoint = state_repository.get(chat_id, mode=mode)
        offset_id = checkpoint.oldest_processed_message_id if checkpoint else 0
        processed_messages = checkpoint.processed_messages if checkpoint else 0
        saved_users_total = checkpoint.saved_users if checkpoint else 0

        if checkpoint:
            logger.info("Checkpoint (%s): message_id=%d", mode, offset_id)
        else:
            logger.info("Checkpoint (%s): отсутствует, начинаю с самого начала истории", mode)

        last_message_id = offset_id
        last_message_date = checkpoint.oldest_processed_date if checkpoint else None

        since_checkpoint = 0
        since_batch = 0
        new_users_in_batch = 0
        unique_sender_ids_in_batch: set[int] = set()
        batch_number = processed_messages // _BATCH_SIZE + 1
        # Отправители, которых нужно обновить, но для которых сообщение НЕ
        # принесло готовый объект User (см. ниже) — резолвятся одним пакетным
        # запросом (на чекпоинте или раньше, при достижении
        # _PENDING_RESOLVE_THRESHOLD), а не по одному сразу же. Сбрасывается на
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
                mode=mode,
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
                            # позже одним пакетным запросом (на чекпоинте
                            # или раньше, если накопится _PENDING_RESOLVE_
                            # THRESHOLD — см. ниже), а не отдельным RPC
                            # прямо здесь.
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

                # Флуш раньше чекпоинта, если накопилось близко к лимиту
                # одного RPC (см. _PENDING_RESOLVE_THRESHOLD выше) — не
                # увеличивает общее число запросов (GetUsersRequest всё
                # равно режет по 200), но не даёт pending разрастись
                # намного больше 200 в активных группах.
                if len(pending_identity_refresh) >= _PENDING_RESOLVE_THRESHOLD:
                    _, new_count = await _resolve_and_upsert_pending(
                        client, repository, pending_identity_refresh, failed_identity_refresh,
                    )
                    saved_users_total += new_count
                    new_users_in_batch += new_count

                if since_checkpoint >= _CHECKPOINT_INTERVAL:
                    # pending_identity_refresh почти всегда уже пуст здесь
                    # (см. флуш по порогу выше) — но если чекпоинт наступил
                    # раньше, чем накопился порог, дорезолвливаем остаток
                    # перед сохранением checkpoint (_resolve_and_upsert_pending
                    # ничего не делает и возвращает (0, 0), если pending пуст).
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
            # ВРЕМЕННАЯ ДИАГНОСТИКА — убрать после локализации источника
            # FloodWait. Именно этот RPC — client.iter_messages() (чтение
            # истории), а не резолв пользователей/участников.
            logger.warning(
                "[DIAG] FloodWait (%ds) while reading history (группа '%s')",
                exc.seconds, title,
            )
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
