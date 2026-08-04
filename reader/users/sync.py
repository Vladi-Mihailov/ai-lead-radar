import logging

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from reader.groups import Group
from reader.users.models import TelegramUserInfo
from reader.users.repository import UserRepository

logger = logging.getLogger(__name__)


async def sync_all_users(
    client: TelegramClient,
    groups: list[Group],
    repository: UserRepository,
) -> None:
    """Проходит по всем группам и сохраняет/обновляет всех доступных участников."""

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
                "✖ Группа '%s' не найдена, синхронизация участников пропущена",
                group.identifier,
            )
            continue

        title = group.title or getattr(entity, "title", None) or str(group.identifier)

        synced = 0
        try:
            async for participant in client.iter_participants(entity):
                repository.upsert(
                    TelegramUserInfo(
                        user_id=participant.id,
                        username=participant.username,
                        first_name=participant.first_name,
                        last_name=participant.last_name,
                        is_bot=bool(getattr(participant, "bot", False)),
                        # participant — полноценный объект Telethon User —
                        # сохраняем access_hash для восстановления
                        # InputPeerUser без @username.
                        access_hash=getattr(participant, "access_hash", None),
                        peer_type=type(participant).__name__,
                    )
                )
                synced += 1
        except FloodWaitError as exc:
            # ВРЕМЕННАЯ ДИАГНОСТИКА — убрать после локализации источника
            # FloodWait. Именно этот RPC — client.iter_participants().
            logger.warning(
                "[DIAG] FloodWait (%ds) while loading participants (группа '%s')",
                exc.seconds, title,
            )
            logger.warning(
                "Telegram ограничил запросы при синхронизации группы '%s' "
                "(нужно подождать %d сек.) — сохранено участников: %d, "
                "остальные будут получены при повторном запуске",
                title,
                exc.seconds,
                synced,
            )
            continue
        except Exception:
            logger.warning(
                "Не удалось получить участников группы '%s', синхронизация пропущена (сохранено: %d)",
                title,
                synced,
            )
            continue

        logger.info("✔ Синхронизировано участников группы '%s': %d", title, synced)
