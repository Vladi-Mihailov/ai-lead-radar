"""Узкий, самостоятельный backfill СТРОГО для users.car_numbers по уже
доступной истории сообщений — переиспользует Telegram/history
инфраструктуру (client.iter_messages, тот же приём, что и
reader/users/history_sync.py) и extract_car_numbers(), но не делает ничего
больше:

- НЕ трогает HistorySyncStateRepository (ни читает, ни пишет checkpoint —
  ни обычный, ни reindex) — прогресс sync_users.py (инкрементальный и
  --reindex) не затрагивается никак, этот backfill полностью независим;
  вместо этого ведёт СВОЙ ОТДЕЛЬНЫЙ checkpoint в
  CarNumbersBackfillStateRepository (своя таблица car_numbers_backfill_state
  в том же users.db) — см. reader/users/car_numbers_backfill_state.py;
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

Рассчитан на историю произвольного размера (условно
миллионы/миллиарды сообщений на группу) без накопления результата в
памяти: история читается и обрабатывается пакетами по
FLUSH_EVERY_MESSAGES сообщений, после каждого пакета накопленные номера
сразу пишутся в UserRepository и сразу же сохраняется checkpoint
(CarNumbersBackfillStateRepository) — потеря процесса (SSH, Ctrl+C,
падение, перезагрузка сервера) стоит максимум одного незавершённого
пакета, а не всей проделанной работы. Порядок ВСЕГДА: сначала
add_car_numbers() (и его внутренний commit), только потом
save_progress() checkpoint'а (и его commit), и только потом очистка
накопителя в памяти — если между этими шагами процесс упадёт, при
следующем запуске часть сообщений обработается повторно, но не будет
дублей (add_car_numbers идемпотентен), а обратной ситуации (checkpoint
продвинут, а номера не сохранены) быть не может.

При повторном запуске группы с завершённой историей (completed=True в
checkpoint'е) пропускаются мгновенно, а прерванная группа продолжается с
last_message_id, а не читается заново с начала — направление и семантика
offset_id в client.iter_messages() те же, что проверены и уже
эксплуатируются в reader/users/history_sync.py (обход от новых сообщений к
старым, offset_id — строго исключающая нижняя граница уже пройденных id).
"""

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon import TelegramClient  # noqa: E402
from telethon.errors import FloodWaitError  # noqa: E402

from reader.groups import Group, GroupLoadError, load_groups  # noqa: E402
from reader.logging_setup import setup_logging  # noqa: E402
from reader.settings import ConfigError, load_settings  # noqa: E402
from reader.users.car_numbers import extract_car_numbers  # noqa: E402
from reader.users.car_numbers_backfill_state import (  # noqa: E402
    CarNumbersBackfillStateRepository,
)
from reader.users.repository import UserRepository  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

logger = logging.getLogger(__name__)

_MAX_FLOOD_WAIT_RETRIES = 3

# Сколько сообщений обрабатывается между flush'ами (запись car_numbers в
# UserRepository + сохранение checkpoint'а). Ограничивает объём памяти под
# накопитель номеров (pending_numbers) размером одного пакета, а не всей
# историей группы, и ограничивает объём повторной обработки при аварии
# этим же размером.
FLUSH_EVERY_MESSAGES = 10_000

_SEPARATOR = "=" * 60


@dataclass
class BackfillStats:
    groups_scanned: int = 0
    messages_scanned: int = 0
    users_with_car_numbers: int = 0
    car_numbers_written: int = 0


@dataclass
class _GroupScanResult:
    messages_scanned: int = 0
    users_with_car_numbers: int = 0
    car_numbers_written: int = 0


async def _scan_group(
    client,
    group: Group,
    repository: UserRepository,
    state_repository: CarNumbersBackfillStateRepository,
) -> _GroupScanResult:
    """Читает историю ОДНОЙ группы пакетами по FLUSH_EVERY_MESSAGES
    сообщений, после каждого пакета сразу пишет найденные номера в
    UserRepository и сохраняет checkpoint (CarNumbersBackfillStateRepository)
    — см. докстрок модуля про порядок flush → checkpoint → clear.

    Продолжает с checkpoint'а группы, если он есть и не completed; группу с
    completed=True пропускает мгновенно, не открывая client.iter_messages."""
    try:
        entity = await client.get_entity(group.identifier)
    except Exception:
        logger.warning(
            "✖ Группа '%s' не найдена, backfill car_numbers для неё пропущен",
            group.title or group.identifier,
            exc_info=True,
        )
        return _GroupScanResult()

    group_id = entity.id
    title = group.title or getattr(entity, "title", None) or str(group.identifier)

    checkpoint = state_repository.get(group_id)
    if checkpoint and checkpoint.completed:
        logger.info(
            "Группа: %s\nУже полностью обработана (car_numbers backfill), пропускаю",
            title,
        )
        return _GroupScanResult()

    offset_id = checkpoint.last_message_id if checkpoint else 0
    if checkpoint:
        logger.info("Группа: %s\nПродолжаем с checkpoint message_id=%d", title, offset_id)
    else:
        logger.info("Группа: %s", title)

    pending_numbers: dict[int, set[str]] = {}
    result = _GroupScanResult()
    messages_since_flush = 0
    last_message_id = offset_id
    retries = 0

    def flush(*, completed: bool) -> None:
        """save car_numbers (commit) -> save checkpoint (commit) -> clear
        pending_numbers. Если repository.add_car_numbers() бросит
        исключение, до state_repository.save_progress() дело не дойдёт —
        checkpoint не продвинется дальше уже гарантированно сохранённых
        номеров (см. докстрок модуля)."""
        nonlocal messages_since_flush
        users_in_batch = len(pending_numbers)

        for user_id, numbers in pending_numbers.items():
            repository.add_car_numbers(user_id, sorted(numbers))

        state_repository.save_progress(
            group_id=group_id,
            chat_name=title,
            last_message_id=last_message_id,
            completed=completed,
        )

        result.users_with_car_numbers += users_in_batch
        result.car_numbers_written += sum(len(numbers) for numbers in pending_numbers.values())

        logger.info(
            "Группа: %s\n"
            "Обработано сообщений: %d\n"
            "Найдено пользователей с номерами в batch: %d\n"
            "Результат сохранён в БД\n"
            "Checkpoint: message_id=%d",
            title, result.messages_scanned, users_in_batch, last_message_id,
        )

        pending_numbers.clear()
        messages_since_flush = 0

    while True:
        try:
            async for message in client.iter_messages(entity, offset_id=offset_id, limit=None):
                result.messages_scanned += 1
                messages_since_flush += 1
                last_message_id = message.id
                offset_id = message.id

                sender_id = message.sender_id
                if sender_id:
                    car_numbers = extract_car_numbers(message.raw_text or "")
                    if car_numbers:
                        pending_numbers.setdefault(sender_id, set()).update(car_numbers)

                if messages_since_flush >= FLUSH_EVERY_MESSAGES:
                    flush(completed=False)
            break
        except FloodWaitError as exc:
            # Флуш перед сном — иначе накопленный (потенциально почти
            # целый FLUSH_EVERY_MESSAGES) пакет рискует пропасть, если
            # процесс убьют во время долгого ожидания.
            if pending_numbers or messages_since_flush:
                flush(completed=False)

            retries += 1
            if retries > _MAX_FLOOD_WAIT_RETRIES:
                logger.warning(
                    "Telegram ограничил чтение истории группы '%s' (%d сек.) — "
                    "превышено число повторов (%d), группа обработана частично "
                    "(%d сообщений в этом запуске), продолжу со следующего запуска",
                    title, exc.seconds, _MAX_FLOOD_WAIT_RETRIES, result.messages_scanned,
                )
                return result
            logger.warning(
                "Telegram ограничил чтение истории группы '%s' (жду %d сек., "
                "попытка %d из %d)",
                title, exc.seconds, retries, _MAX_FLOOD_WAIT_RETRIES,
            )
            await asyncio.sleep(exc.seconds)
            continue
        except (KeyboardInterrupt, asyncio.CancelledError):
            if pending_numbers or messages_since_flush:
                flush(completed=False)
            raise
        except Exception:
            # Умышленно НЕ пытаемся флушить pending_numbers здесь: если
            # причина исключения — сам flush() (сбой add_car_numbers/
            # save_progress внутри плановой периодической точки выше),
            # повторный вызов flush() из этой ветки почти наверняка
            # столкнётся с той же ошибкой ещё раз, уже вне try/except (см.
            # докстрок модуля про порядок car_numbers -> checkpoint) — а
            # если ошибка от чтения истории, а не от записи, то
            # несохранённый остаток пакета безопасно теряется: он будет
            # переобработан со следующего запуска (последний сохранённый
            # checkpoint не продвинулся) благодаря идемпотентности
            # add_car_numbers, без единого дубля.
            logger.warning(
                "Не удалось дочитать историю группы '%s' — обработано %d сообщений "
                "в этом запуске, продолжу со следующего запуска",
                title, result.messages_scanned, exc_info=True,
            )
            return result

    flush(completed=True)
    logger.info(
        "Группа завершена\n"
        "сообщений обработано: %d\n"
        "пользователей/номеров найдено: %d\n"
        "checkpoint: completed",
        result.messages_scanned, result.users_with_car_numbers,
    )
    return result


async def backfill_car_numbers(
    client,
    groups: list[Group],
    repository: UserRepository,
    state_repository: CarNumbersBackfillStateRepository,
) -> BackfillStats:
    """Читает историю всех groups (см. _scan_group), пакетами по
    FLUSH_EVERY_MESSAGES сообщений — car_numbers пишутся в UserRepository и
    checkpoint сохраняется в CarNumbersBackfillStateRepository сразу после
    каждого пакета, а не в конце всего прохода: результат не теряется при
    прерывании процесса, а память не растёт с объёмом всей истории.

    Кроме add_car_numbers() ни один другой метод UserRepository не
    вызывается — keywords/username/access_hash/last_seen_at не читаются и
    не изменяются (см. докстрок модуля)."""
    stats = BackfillStats()

    for group in groups:
        group_result = await _scan_group(client, group, repository, state_repository)
        stats.groups_scanned += 1
        stats.messages_scanned += group_result.messages_scanned
        stats.users_with_car_numbers += group_result.users_with_car_numbers
        stats.car_numbers_written += group_result.car_numbers_written

    return stats


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Узкий backfill car_numbers по уже накопленной истории "
            "сообщений всех групп — только users.car_numbers, без keywords, "
            "access_hash, username, last_seen_at и без изменения checkpoint "
            "sync_users.py. Ведёт свой отдельный checkpoint (см. "
            "car_numbers_backfill_state.py), поэтому прерванный прогон "
            "продолжается с места остановки, а не с начала истории."
        )
    )
    return parser.parse_args(argv)


async def run() -> None:
    settings = load_settings(CONFIG_PATH)
    setup_logging(settings.app.log_level)
    logger.info(
        "%s\nBackfill car_numbers — узкий, отдельный от sync_users.py "
        "проход по истории (checkpoint sync_users.py не используется и не "
        "изменяется; свой отдельный checkpoint — см. "
        "car_numbers_backfill_state.py).\n%s",
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
    state_repository = CarNumbersBackfillStateRepository(settings.app.users_db_file)
    try:
        stats = await backfill_car_numbers(client, groups, repository, state_repository)
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
        state_repository.close()
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
