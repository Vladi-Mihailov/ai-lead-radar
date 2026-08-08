"""Узкий, самостоятельный backfill СТРОГО для users.car_numbers по уже
доступной истории сообщений — переиспользует Telegram/history
инфраструктуру (client.iter_messages, тот же приём, что и
reader/users/history_sync.py) и extract_car_numbers(), но не делает ничего
больше:

- НЕ трогает HistorySyncStateRepository (ни читает, ни пишет checkpoint —
  ни обычный, ни reindex) — прогресс sync_users.py (инкрементальный и
  --reindex) не затрагивается никак, этот backfill полностью независим;
- НЕ пересчитывает keywords (repository.add_keywords() не вызывается) и
  НЕ резолвит/обновляет username/access_hash/is_bot/last_seen_at
  (repository.upsert()/update_access_hash() не вызываются) — user_id
  берётся прямо из message.sender_id, без единого RPC на пользователя;
  единственный вызов в UserRepository — add_car_numbers().

Запускается отдельной командой:

    python -m reader.users.backfill_car_numbers

Безопасен для повторного запуска: extract_car_numbers() детерминирован, а
UserRepository.add_car_numbers() объединяет новые номера с уже
сохранёнными через множество — повторное обнаружение уже известного
номера не создаёт дублей и не меняет результат (см. тесты).

Без checkpoint'а каждый запуск читает историю каждой группы с самого
начала — при большом объёме истории это может занять продолжительное
время, как и sync_users.py --reindex, но проще и уже даёт весь нужный
результат за один прогон.
"""

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon import TelegramClient  # noqa: E402
from telethon.errors import FloodWaitError  # noqa: E402

from reader.groups import Group, GroupLoadError, load_groups  # noqa: E402
from reader.logging_setup import setup_logging  # noqa: E402
from reader.settings import ConfigError, load_settings  # noqa: E402
from reader.users.car_numbers import extract_car_numbers  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

logger = logging.getLogger(__name__)

_MAX_FLOOD_WAIT_RETRIES = 3
_LOG_EVERY_MESSAGES = 10_000

_SEPARATOR = "=" * 60


@dataclass
class BackfillStats:
    groups_scanned: int = 0
    messages_scanned: int = 0
    users_with_car_numbers: int = 0
    car_numbers_written: int = 0
    numbers_by_user: dict[int, set[str]] = field(default_factory=dict, repr=False)


async def _scan_group(client, group: Group) -> tuple[dict[int, set[str]], int]:
    """Читает ВСЮ историю ОДНОЙ группы (без checkpoint, без offset между
    запусками) и возвращает ({user_id: {номера}}, число обработанных
    сообщений). Ничего не пишет в UserRepository — только извлекает и
    группирует (см. backfill_car_numbers, который пишет один раз на
    пользователя по итогам ВСЕХ групп)."""
    try:
        entity = await client.get_entity(group.identifier)
    except Exception:
        logger.warning(
            "✖ Группа '%s' не найдена, backfill car_numbers для неё пропущен",
            group.title or group.identifier,
            exc_info=True,
        )
        return {}, 0

    title = group.title or getattr(entity, "title", None) or str(group.identifier)
    numbers_by_user: dict[int, set[str]] = {}
    messages_scanned = 0
    offset_id = 0
    retries = 0

    while True:
        try:
            async for message in client.iter_messages(entity, offset_id=offset_id, limit=None):
                messages_scanned += 1
                offset_id = message.id

                sender_id = message.sender_id
                if sender_id:
                    car_numbers = extract_car_numbers(message.raw_text or "")
                    if car_numbers:
                        numbers_by_user.setdefault(sender_id, set()).update(car_numbers)

                if messages_scanned % _LOG_EVERY_MESSAGES == 0:
                    logger.info(
                        "Группа: %s — обработано сообщений: %d, пользователей с номерами "
                        "на данный момент: %d",
                        title, messages_scanned, len(numbers_by_user),
                    )
            break
        except FloodWaitError as exc:
            retries += 1
            if retries > _MAX_FLOOD_WAIT_RETRIES:
                logger.warning(
                    "Telegram ограничил чтение истории группы '%s' (%d сек.) — "
                    "превышено число повторов (%d), группа обработана частично "
                    "(%d сообщений)",
                    title, exc.seconds, _MAX_FLOOD_WAIT_RETRIES, messages_scanned,
                )
                break
            logger.warning(
                "Telegram ограничил чтение истории группы '%s' (жду %d сек., "
                "попытка %d из %d)",
                title, exc.seconds, retries, _MAX_FLOOD_WAIT_RETRIES,
            )
            await asyncio.sleep(exc.seconds)
            continue
        except Exception:
            logger.warning(
                "Не удалось дочитать историю группы '%s' — обработано %d сообщений",
                title, messages_scanned, exc_info=True,
            )
            break

    logger.info(
        "Группа: %s — история прочитана. Сообщений: %d, пользователей с номерами: %d",
        title, messages_scanned, len(numbers_by_user),
    )
    return numbers_by_user, messages_scanned


async def backfill_car_numbers(
    client, groups: list[Group], repository: UserRepository,
) -> BackfillStats:
    """Читает историю всех groups (см. _scan_group), группирует найденные
    госномера по user_id по итогам ВСЕХ групп сразу, и для каждого
    пользователя вызывает ОДИН раз repository.add_car_numbers(user_id,
    numbers) — ровно один UPDATE на пользователя за весь прогон, а не на
    каждое сообщение.

    Кроме add_car_numbers() ни один другой метод UserRepository не
    вызывается — keywords/username/access_hash/last_seen_at не читаются и
    не изменяются (см. докстрок модуля)."""
    stats = BackfillStats()

    for group in groups:
        group_numbers, messages_scanned = await _scan_group(client, group)
        stats.groups_scanned += 1
        stats.messages_scanned += messages_scanned
        for user_id, numbers in group_numbers.items():
            stats.numbers_by_user.setdefault(user_id, set()).update(numbers)

    for user_id, numbers in stats.numbers_by_user.items():
        repository.add_car_numbers(user_id, sorted(numbers))
        stats.users_with_car_numbers += 1
        stats.car_numbers_written += len(numbers)

    return stats


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Узкий backfill car_numbers по уже накопленной истории "
            "сообщений всех групп — только users.car_numbers, без keywords, "
            "access_hash, username, last_seen_at и без изменения checkpoint "
            "sync_users.py."
        )
    )
    return parser.parse_args(argv)


async def run() -> None:
    settings = load_settings(CONFIG_PATH)
    setup_logging(settings.app.log_level)
    logger.info(
        "%s\nBackfill car_numbers — узкий, отдельный от sync_users.py "
        "проход по истории (checkpoint не используется и не изменяется).\n%s",
        _SEPARATOR, _SEPARATOR,
    )

    groups = load_groups(settings.app.groups_file)

    client = TelegramClient(
        str(settings.telegram.session_path_sync),
        settings.telegram.api_id,
        settings.telegram.api_hash,
        # Только точечные запросы (get_entity/iter_messages) — живые
        # апдейты этому процессу не нужны, как и у sync_users.py.
        receive_updates=False,
    )
    await client.start(phone=settings.telegram.phone)

    repository = UserRepository(settings.app.users_db_file)
    try:
        stats = await backfill_car_numbers(client, groups, repository)
        logger.info(
            "%s\nBackfill car_numbers завершён\n"
            "Групп обработано: %d\n"
            "Сообщений просмотрено: %d\n"
            "Пользователей с номерами: %d\n"
            "Номеров записано (с учётом уже известных): %d\n"
            "%s",
            _SEPARATOR,
            stats.groups_scanned, stats.messages_scanned,
            stats.users_with_car_numbers, stats.car_numbers_written,
            _SEPARATOR,
        )
    finally:
        repository.close()
        await client.disconnect()


def main() -> None:
    _parse_args(sys.argv[1:])
    try:
        asyncio.run(run())
    except (ConfigError, GroupLoadError) as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)


if __name__ == "__main__":
    main()
