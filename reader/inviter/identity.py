"""Идентичность физического Telegram-аккаунта (см. задачу про обнаруженные
production-дубли: два разных DB-ряда/session-файла, фактически
авторизованные как ОДИН и тот же Telegram-аккаунт, и переименование
@alena_ogi -> @ao777oa777 БЕЗ создания нового физического аккаунта).

name/phone/session_name — изменяемые атрибуты; единственный стабильный
идентификатор — telegram_user_id (me.id из get_me()). Этот модуль — чистая
логика поверх TelegramAccountRepository, без самого TelegramClient
(вызывающий код — reader/inviter/service.py _execute_account и
reader/inviter/manage.py sync_accounts — сам открывает сессию и вызывает
is_user_authorized()/get_me(), см. IdentityTelegramClientLike ниже)."""

from dataclasses import dataclass
from typing import Protocol

from reader.inviter.models import TelegramAccount
from reader.inviter.repository import TelegramAccountRepository


class IdentityTelegramClientLike(Protocol):
    """Часть интерфейса TelegramClient, нужная только для проверки
    идентичности — подмножество DryRunTelegramClient/реального Telethon-
    клиента (см. reader/inviter/service.py)."""

    async def is_user_authorized(self) -> bool: ...

    async def get_me(self): ...


@dataclass(frozen=True)
class TelegramIdentity:
    """Результат get_me() в удобном для reconcile_account_identity()
    виде — username/phone здесь МОГУТ быть None (Telegram не гарантирует
    ни то, ни другое)."""

    telegram_user_id: int
    username: str | None
    phone: str | None


class SessionNotAuthorizedError(Exception):
    """Сессия подключилась (connect() успешен), но is_user_authorized()
    вернул False — использовать такой аккаунт для приглашений нельзя,
    но это НЕ ошибка несовпадения identity (см.
    AccountIdentityMismatchError) — сессия просто не авторизована."""


class AccountIdentityMismatchError(Exception):
    """У DB-записи уже сохранён telegram_user_id, но подключённая по её
    session_path/session_name сессия оказалась авторизована под ДРУГИМ
    Telegram-аккаунтом (пересозданный/перезалогиненный файл сессии, см.
    задачу) — использовать эту запись для приглашений нельзя."""


async def fetch_telegram_identity(client: IdentityTelegramClientLike) -> TelegramIdentity:
    """connect() уже должен быть выполнен вызывающим кодом — эта функция
    только проверяет авторизацию и читает identity, ничего не
    подключает/отключает."""
    if not await client.is_user_authorized():
        raise SessionNotAuthorizedError

    me = await client.get_me()
    username = getattr(me, "username", None)
    phone = getattr(me, "phone", None)
    return TelegramIdentity(
        telegram_user_id=me.id,
        username=username or None,
        phone=phone or None,
    )


def reconcile_account_identity(
    account_repository: TelegramAccountRepository,
    account: TelegramAccount,
    identity: TelegramIdentity,
) -> TelegramAccount:
    """Сверяет сохранённый account.telegram_user_id с фактическим
    identity.telegram_user_id:

    - account.telegram_user_id is None -> первая проверка этого аккаунта,
      сохраняем telegram_user_id и синхронизируем name/phone (см. задачу
      про backfill).
    - совпадает -> тот же физический аккаунт, синхронизируем
      name/phone (username мог смениться — см. задачу про
      @alena_ogi -> @ao777oa777).
    - не совпадает -> AccountIdentityMismatchError, ничего не пишем в БД.

    session_name/session_path НИКОГДА не трогаются — идентичность привязана
    к самому Telegram-аккаунту, а не к тому, как называется файл сессии."""
    if (
        account.telegram_user_id is not None
        and account.telegram_user_id != identity.telegram_user_id
    ):
        raise AccountIdentityMismatchError(
            f"Аккаунт id={account.id} ({account.name}): сохранённый "
            f"telegram_user_id={account.telegram_user_id}, но сессия авторизована "
            f"как telegram_user_id={identity.telegram_user_id} — сессия перезалогинена "
            f"на другой Telegram-аккаунт, использование заблокировано."
        )

    new_name = f"@{identity.username}" if identity.username else account.name
    new_phone = identity.phone if identity.phone else account.phone

    if (
        account.telegram_user_id == identity.telegram_user_id
        and new_name == account.name
        and new_phone == account.phone
    ):
        return account

    return account_repository.update(
        account.id,
        telegram_user_id=identity.telegram_user_id,
        name=new_name,
        phone=new_phone,
    )


def find_duplicate_account(
    account_repository: TelegramAccountRepository,
    account: TelegramAccount,
) -> TelegramAccount | None:
    """Другая запись telegram_accounts с тем же telegram_user_id, что и
    account — физический дубликат (см. задачу про id=6/id=7 и id=8/id=9).
    Среди ВСЕХ совпадающих (включая сам account) детерминированно выбираем
    запись с наименьшим id как "основную" — если ею оказался не account,
    значит account — дубликат и его использовать нельзя. Отключённые
    (enabled=False) записи не считаются конфликтом — они не участвуют в
    рантайме инвайтера (см. reader/inviter/worker.py _enabled_pairs)."""
    if account.telegram_user_id is None:
        return None

    matching = [
        other
        for other in account_repository.list()
        if other.enabled and other.telegram_user_id == account.telegram_user_id
    ]
    if len(matching) <= 1:
        return None

    primary = min(matching, key=lambda other: other.id)
    return None if primary.id == account.id else primary
