"""Резолв @username владельца в стабильный numeric Telegram user_id — для
trusted-operator delegated Add Car flow (см. design report).

Тот же принцип, что и reader/commands/fine.py::_resolve_and_store_new_user:
username, которого нет в локальной БД, пробуем резолвить через уже
подключённый Telegram-клиент, и если получилось — сохраняем в
UserRepository тем же upsert(), без второй реализации маппинга entity ->
TelegramUserInfo (см. TelegramUserInfo.from_telethon_user). Единственное
отличие от fine.py: там клиент — пользовательская Telethon-сессия
оператора (reader_live), здесь — САМ БОТ (см. reader/public_bot/main.py).
Это значит, что успешный резолв НЕ гарантирует возможность что-либо этому
человеку доставить — Telegram не позволяет боту первым писать
пользователю, который никогда не начинал с ним диалог. Резолв identity и
способность доставить сообщение — два разных вопроса; здесь решается
только первый (см. reader/public_bot/subscription_service.py про
pending_claim для случая, когда резолв не удался вовсе)."""

from dataclasses import dataclass
from typing import Protocol

from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError
from telethon.tl.types import User

from reader.users.models import TelegramUserInfo
from reader.users.repository import UserRepository


class OwnerUsernameResolverLike(Protocol):
    """Ровно то, что нужно отсюда от TelegramClient — тот же приём, что и
    TelegramUsernameResolverLike в reader/commands/fine.py, но отдельный
    класс: там это пользовательская сессия оператора, здесь — бот."""

    async def get_entity(self, entity): ...


class OwnerResolutionError(Exception):
    """Техническая ошибка при обращении к Telegram (сеть/FloodWait/прочий
    сбой RPC) — НЕ означает "пользователь не найден" (см.
    resolve_owner_username: тот случай возвращает None, а не бросает это
    исключение). Вызывающий код должен явно сообщить trusted-оператору об
    ошибке и НЕ создавать ни активную подписку, ни pending_claim — в
    отличие от "не найден", здесь мы не знаем, существует ли username."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ResolvedOwner:
    telegram_user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None


async def resolve_owner_username(
    username: str,
    *,
    user_repository: UserRepository,
    telegram_client: OwnerUsernameResolverLike | None,
) -> ResolvedOwner | None:
    """username уже нормализован (без "@", см.
    reader/public_bot/validation.py::normalize_telegram_username).

    Порядок: сначала локальная БД (UserRepository — накоплена ВСЕМ
    Reader'ом, не только ботом, поэтому покрывает гораздо больше людей,
    чем "кто уже писал именно этому боту"), затем — если не найден и
    telegram_client передан — живой резолв через Telegram.

    None, если Telegram подтверждённо не знает такого username
    (UsernameNotOccupiedError/UsernameInvalidError — синтаксис неверный
    или юзернейм никем не занят) либо резолвнутая entity — не обычный
    пользователь (см. fine.py — тот же критерий isinstance(entity, User)).
    telegram_client=None (например, в тестах без реального клиента)
    трактуется так же, как "не нашли" — ровно как и в fine.py.

    Может бросить OwnerResolutionError на технический сбой (сеть/
    FloodWait/прочий RPC) — это ПРОБРАСЫВАЕТСЯ, а не превращается в None,
    чтобы вызывающий код не спутал "не удалось проверить" с "точно не
    существует" (см. design report: "не создавать ложную ownership-запись")."""
    existing = user_repository.find_by_username(username)
    if existing is not None:
        return ResolvedOwner(
            telegram_user_id=existing.user_id,
            username=existing.username,
            first_name=existing.first_name,
            last_name=existing.last_name,
        )

    if telegram_client is None:
        return None

    try:
        entity = await telegram_client.get_entity(f"@{username}")
    except (UsernameNotOccupiedError, UsernameInvalidError, ValueError):
        return None
    except Exception as exc:
        raise OwnerResolutionError(
            f"Не удалось проверить Telegram-пользователя @{username} "
            "(техническая ошибка при обращении к Telegram)."
        ) from exc

    if not isinstance(entity, User):
        return None

    info = TelegramUserInfo.from_telethon_user(entity)
    # Race: тот же пользователь мог уже появиться в БД другим путём между
    # find_by_username() и этим upsert() — upsert() идемпотентен (INSERT
    # ... ON CONFLICT DO UPDATE, см. reader/users/repository.py), дубля не
    # возникает (тот же приём, что и в fine.py::_resolve_and_store_new_user).
    user_repository.upsert(info)

    return ResolvedOwner(
        telegram_user_id=info.user_id,
        username=info.username,
        first_name=info.first_name,
        last_name=info.last_name,
    )
