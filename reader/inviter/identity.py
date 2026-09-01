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
    к самому Telegram-аккаунту, а не к тому, как называется файл сессии.

    Если name реально меняется — старое значение добавляется в
    previous_names (если его там ещё нет), а не теряется молча (см. задачу:
    "DB row 7 исторически была @Misha_Offroad", даже если её name синхронизируют
    на актуальный username того же физического аккаунта)."""
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

    new_previous_names = list(account.previous_names)
    if new_name != account.name and account.name not in new_previous_names:
        new_previous_names.append(account.name)

    return account_repository.update(
        account.id,
        telegram_user_id=identity.telegram_user_id,
        name=new_name,
        phone=new_phone,
        previous_names=new_previous_names,
    )


_DUPLICATE_OLD_REASON = "duplicate_telegram_user_id"


def resolve_duplicate_group(
    account_repository: TelegramAccountRepository,
    telegram_user_id: int,
) -> None:
    """Для ВСЕХ DB-записей с этим telegram_user_id (независимо от enabled)
    гарантирует, что ровно одна остаётся CURRENT (is_old=False), а
    остальные помечены is_old=True, enabled=False, old_reason=
    "duplicate_telegram_user_id" (см. задачу про автоматическое
    обнаружение дублей после переименования/перелогина: id=6/id=7 и
    id=8/id=9).

    is_old — ЛИПКИЙ (sticky) флаг: запись, уже помеченная is_old=True, НЕ
    рассматривается как кандидат на CURRENT в последующих вызовах (даже
    если её потом вручную enable — см. задачу "OLD имеет приоритет"),
    восстановить статус CURRENT можно только вручную правкой БД. Это же
    делает функцию идемпотентной — повторный вызов без изменения входных
    данных не производит новых записей в БД.

    Выбор CURRENT среди ещё не помеченных (candidates), по убыванию
    приоритета:
      1. если ровно один из candidates enabled — он CURRENT;
      2. если ни одного или несколько enabled — CURRENT детерминированно
         наименьший id среди candidates (без изменения enabled — см.
         задачу "никогда не включать аккаунт автоматически").
    Если candidates пуст (защитный случай — не должен происходить в
    норме, т.к. группа не может стать полностью is_old сама по себе),
    CURRENT восстанавливается как наименьший id во всей группе, тоже без
    включения enabled.

    Ни одна запись не удаляется и не сливается — user_campaign_invites
    остаётся нетронутым (там нет ссылок на telegram_accounts.is_old)."""
    group = [a for a in account_repository.list() if a.telegram_user_id == telegram_user_id]
    if len(group) <= 1:
        return

    candidates = [a for a in group if not a.is_old]

    if not candidates:
        winner_id = min(a.id for a in group)
    elif len(candidates) == 1:
        winner_id = candidates[0].id
    else:
        enabled_candidates = [a for a in candidates if a.enabled]
        if len(enabled_candidates) == 1:
            winner_id = enabled_candidates[0].id
        else:
            winner_id = min(a.id for a in candidates)

    for account in group:
        if account.id == winner_id:
            if account.is_old or account.old_reason is not None:
                account_repository.update(account.id, is_old=False, old_reason=None)
        else:
            if (
                not account.is_old
                or account.enabled
                or account.old_reason != _DUPLICATE_OLD_REASON
            ):
                account_repository.update(
                    account.id, is_old=True, enabled=False, old_reason=_DUPLICATE_OLD_REASON,
                )
