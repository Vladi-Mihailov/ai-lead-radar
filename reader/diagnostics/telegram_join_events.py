"""ДИАГНОСТИЧЕСКИЙ, read-only скрипт — НЕ часть production-кода.

Проверяет, какие membership-события (вступление/добавление/выход/удаление
участника) реально долетают в реальном времени ДО ОБЫЧНОГО (не-админского)
Telegram-аккаунта для ОДНОЙ конкретной закрытой группы — без запроса
полного списка участников (он этому аккаунту может быть недоступен).

Ничего не пишет в users.db/fine_monitoring_tasks/inviter-таблицы и любые
другие production-БД, никого не приглашает, не пишет и не удаляет
сообщения, не меняет группу — только слушает и печатает в stdout/лог.
reader/main.py, существующие обработчики и БД этим модулем не трогаются
и не импортируются.

--- Про сессию (ВАЖНО, см. задачу) ---

Используется settings.telegram.session_path_sync — ТА ЖЕ сессия, которой
уже пользуются reader/sync_users.py и reader/users/backfill_car_numbers.py,
а НЕ settings.telegram.session_path_live. session_path_live непрерывно
держит открытым reader.main (см. reader/sources/telegram_source.py) —
второй Telethon-клиент на том же файле сессии рисковал бы конфликтом
auth_key/повреждением сессии. Ровно для этого session_path_sync и была
заведена изначально (см. docstring _migrate_legacy_session в
reader/settings.py) — это ПОЛНОСТЬЮ отдельная, уже авторизованная
Telegram-сессия того же аккаунта, никак не связанная с session_path_live.

Поэтому: reader.main / ai-lead-radar.service ОСТАНАВЛИВАТЬ НЕ НУЖНО —
этот скрипт использует другой файл сессии и не конфликтует с ним.

Единственное практическое ограничение: НЕ запускайте этот скрипт
ОДНОВРЕМЕННО с reader/sync_users.py или
reader/users/backfill_car_numbers.py — все трое используют один и тот же
файл session_path_sync, и два Telethon-клиента одновременно на одном и
том же auth_key — это уже не поддерживаемый Telegram/Telethon сценарий
(риск обрыва соединения/ошибок авторизации у одного из них). Дождитесь
завершения одного скрипта, прежде чем запускать другой.

Второе отличие от sync_users.py/backfill_car_numbers.py: они передают
receive_updates=False (им нужны только точечные запросы). Этому скрипту
живые апдейты нужны по сути диагностики, поэтому receive_updates здесь
НЕ отключается (остаётся Telethon-умолчание True).

--- Что слушаем и почему сразу три способа ---

Специально не полагаемся только на один класс событий Telethon — Telegram
может присылать обычному участнику ЧАСТЬ информации не через тот путь,
который выглядит "самым подходящим":

1. events.ChatAction — высокоуровневое Telethon-событие, само уже умеет
   разбирать и raw-апдейты об участниках (UpdateChatParticipant/
   UpdateChannelParticipant), и сервисные сообщения ("X добавил Y" и
   т.п.) в единый набор атрибутов (user_joined/user_added/user_left/
   user_kicked, документированы самим Telethon) — основной, ожидаемо
   самый информативный источник.
2. events.NewMessage на тот же чат — если в чате появляется сервисное
   сообщение (message.action не None), логируем его отдельно, даже если
   ChatAction почему-то не сработал для него (перестраховка).
3. events.Raw() — совсем без фильтра по чату (Raw не поддерживает
   chats=): логирует любой raw-апдейт, похожий на изменение состава
   участников (по имени типа) или несущий сервисное сообщение, плюс
   тихий DEBUG-лог для всего остального — чтобы не получить ложный вывод
   "событий нет" только потому, что мы слушали не тот класс событий.

Запуск:
    python -m reader.diagnostics.telegram_join_events --chat <id_или_@username>

Остановка — Ctrl+C.
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon import TelegramClient, events  # noqa: E402

from reader.logging_setup import setup_logging  # noqa: E402
from reader.settings import ConfigError, load_settings  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

logger = logging.getLogger(__name__)

# raw-update типы, потенциально несущие информацию именно об изменении
# состава участников — список НЕ считается исчерпывающим (это диагностика,
# а не финальная реализация, см. докстрок модуля): цель — не пропустить
# что-то неожиданное, а не заранее угадать точный набор.
_PARTICIPANT_RAW_UPDATE_TYPES = {
    "UpdateChatParticipant",
    "UpdateChannelParticipant",
    "UpdateChatParticipantAdd",
    "UpdateChatParticipantDelete",
    "UpdateChatParticipantAdmin",
}


def classify_membership_event(
    *, user_joined: bool, user_added: bool, user_left: bool, user_kicked: bool,
) -> str:
    """"joined"/"added"/"left"/"removed"/"unknown" — прямое сопоставление с
    уже готовой классификацией events.ChatAction.Event самого Telethon
    (user_joined/user_added/user_left/user_kicked — публичные,
    документированные атрибуты этого события, а не наша эвристика поверх
    raw MessageAction). Чистая функция, без единого обращения к
    Telethon/сети — тестируется напрямую (см.
    tests/test_telegram_join_events.py)."""
    if user_joined:
        return "joined"
    if user_added:
        return "added"
    if user_left:
        return "left"
    if user_kicked:
        return "removed"
    return "unknown"


def parse_chat_identifier(raw: str) -> int | str:
    """--chat принимает и числовой id, и @username/username — тот же
    принцип, что и Group.identifier (reader/groups.py), только это
    отдельный ad-hoc параметр CLI, а не запись из groups.yaml."""
    try:
        return int(raw)
    except ValueError:
        return raw.lstrip("@")


def _fmt_username(username: str | None) -> str | None:
    return f"@{username}" if username else None


def _print_join_event(
    *,
    chat_id: int,
    chat_title: str | None,
    event_type: str,
    user_id: int | None,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    actor_user_id: int | None,
    raw_update_type: str,
) -> None:
    lines = [
        "",
        "[JOIN EVENT]",
        f"chat_id: {chat_id}",
        f"chat_title: {chat_title}",
        f"event_type: {event_type}",
        f"user_id: {user_id}",
        f"username: {_fmt_username(username)}",
        f"first_name: {first_name}",
        f"last_name: {last_name}",
        f"actor_user_id: {actor_user_id}",
        f"raw_update_type: {raw_update_type}",
        f"timestamp: {datetime.now(timezone.utc).isoformat()}",
    ]
    print("\n".join(lines))


async def _resolve_actor_user_id(event: "events.ChatAction.Event") -> int | None:
    """Кто добавил/кикнул (event.added_by/event.kicked_by, документированы
    Telethon) — только для user_added/user_kicked: для user_joined/
    user_left отдельного "актора" нет (пользователь действовал сам),
    возвращаем None, а не подставляем его же id."""
    if event.user_added:
        actor = await event.get_added_by()
    elif event.user_kicked:
        actor = await event.get_kicked_by()
    else:
        return None

    if actor is None:
        return None
    if isinstance(actor, int):
        return actor
    return getattr(actor, "id", None)


async def _handle_chat_action(event: "events.ChatAction.Event", *, chat_title: str | None) -> None:
    event_type = classify_membership_event(
        user_joined=event.user_joined,
        user_added=event.user_added,
        user_left=event.user_left,
        user_kicked=event.user_kicked,
    )

    username = first_name = last_name = None
    try:
        user = await event.get_user()
    except Exception:
        logger.exception("Не удалось получить информацию о пользователе события ChatAction")
        user = None
    if user is not None:
        username = getattr(user, "username", None)
        first_name = getattr(user, "first_name", None)
        last_name = getattr(user, "last_name", None)

    try:
        actor_user_id = await _resolve_actor_user_id(event)
    except Exception:
        logger.exception("Не удалось определить actor_user_id события ChatAction")
        actor_user_id = None

    raw_update_type = type(event.original_update).__name__ if event.original_update is not None else "None"

    _print_join_event(
        chat_id=event.chat_id,
        chat_title=chat_title,
        event_type=event_type,
        user_id=event.user_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        actor_user_id=actor_user_id,
        raw_update_type=raw_update_type,
    )


async def _handle_service_message(event: "events.NewMessage.Event", *, chat_title: str | None) -> None:
    """events.NewMessage на тот же чат — перестраховка на случай, если
    events.ChatAction по какой-то причине не сработает для конкретного
    вида сервисного сообщения (см. докстрок модуля). Обычные (не
    сервисные) сообщения игнорируются — не имеют отношения к составу
    участников."""
    message = event.message
    if message.action is None:
        return

    print("")
    print("[SERVICE MESSAGE] (получено также через events.NewMessage)")
    print(f"chat_id: {event.chat_id}")
    print(f"chat_title: {chat_title}")
    print(f"action_type: {type(message.action).__name__}")
    print(f"sender_id (actor): {message.sender_id}")
    print(f"raw_action: {message.action}")
    print(f"timestamp: {datetime.now(timezone.utc).isoformat()}")


async def _handle_raw_update(update) -> None:
    """events.Raw() — без фильтра по чату (Raw его не поддерживает), см.
    докстрок модуля. Секреты (session/auth_key) в raw-апдейтах не
    присутствуют — это обычные Telegram TL-объекты об апдейтах, не
    авторизационные данные."""
    type_name = type(update).__name__

    if type_name in _PARTICIPANT_RAW_UPDATE_TYPES:
        print("")
        print("[RAW UPDATE] потенциально про изменение состава участников")
        print(f"raw_update_type: {type_name}")
        print(f"raw_repr: {update}")
        print(f"timestamp: {datetime.now(timezone.utc).isoformat()}")
        return

    message = getattr(update, "message", None)
    if message is not None and getattr(message, "action", None) is not None:
        print("")
        print("[RAW UPDATE] service-сообщение внутри NewMessage-обновления")
        print(f"raw_update_type: {type_name}")
        print(f"action_type: {type(message.action).__name__}")
        print(f"raw_repr: {update}")
        print(f"timestamp: {datetime.now(timezone.utc).isoformat()}")
        return

    logger.debug("[raw update] %s", type_name)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Диагностика (read-only): слушает membership-события "
            "(join/add/leave/kick) ОДНОЙ закрытой группы для обычного "
            "(не-администраторского) аккаунта. Ничего не пишет в БД, "
            "никого не приглашает, ничего не отправляет и не меняет."
        )
    )
    parser.add_argument(
        "--chat", required=True,
        help="id или @username закрытой группы, которую слушать",
    )
    return parser.parse_args(argv)


async def run(chat_identifier_raw: str) -> None:
    settings = load_settings(CONFIG_PATH)
    setup_logging(settings.app.log_level)

    chat_identifier = parse_chat_identifier(chat_identifier_raw)

    # session_path_sync, БЕЗ receive_updates=False — см. докстрок модуля.
    client = TelegramClient(
        str(settings.telegram.session_path_sync),
        settings.telegram.api_id,
        settings.telegram.api_hash,
    )
    await client.start(phone=settings.telegram.phone)

    try:
        try:
            entity = await client.get_entity(chat_identifier)
        except Exception as exc:
            print(
                f"❌ Не удалось получить чат '{chat_identifier_raw}': {exc}\n"
                "Проверьте id/username и убедитесь, что этот аккаунт состоит в группе.",
                file=sys.stderr,
            )
            return

        chat_title = getattr(entity, "title", None) or str(chat_identifier_raw)
        print("=" * 60)
        print("Диагностика membership-событий (read-only, ничего не меняет)")
        print(f"Чат: {chat_title} (id={entity.id})")
        print("Аккаунт: обычный участник (права администратора не требуются и не проверяются)")
        print("Ctrl+C — остановить")
        print("=" * 60)

        client.add_event_handler(
            lambda event: _handle_chat_action(event, chat_title=chat_title),
            events.ChatAction(chats=[entity]),
        )
        client.add_event_handler(
            lambda event: _handle_service_message(event, chat_title=chat_title),
            events.NewMessage(chats=[entity]),
        )
        client.add_event_handler(_handle_raw_update, events.Raw())

        await client.run_until_disconnected()
    finally:
        await client.disconnect()


def main() -> None:
    args = _parse_args(sys.argv[1:])
    try:
        asyncio.run(run(args.chat))
    except ConfigError as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем (Ctrl+C).")
        sys.exit(0)


if __name__ == "__main__":
    main()
