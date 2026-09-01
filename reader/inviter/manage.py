"""
Штатное создание/обновление аккаунтов и кампаний инвайтера — без ручного
редактирования SQLite. data/users.db не входит в git (см. .gitignore), так
что TelegramAccountRepository/InviteCampaignRepository на новом окружении
(например, на сервере) всегда пустые — эта команда заполняет их идемпотентно
(создаёт запись, если её ещё нет, иначе обновляет существующую по name, а не
плодит дубликаты).

Использование:
    python -m reader.inviter.manage add-account --name @vladimihailov \
        --session-name vladimihailov --session-path data/sessions/vladimihailov \
        --daily-limit 1

    python -m reader.inviter.manage add-campaign --name "Страхование" \
        --keyword страх --target-chat @tplgee

Повторный запуск с теми же --name обновляет уже существующую запись, а не
создаёт вторую. Это касается и всех остальных полей add-account, включая
--verify-membership: при обновлении нужно передавать ПОЛНЫЙ набор значений
(как и для --daily-limit/--enabled уже сегодня), не переданное явно поле
вернётся к своему умолчанию, а не сохранит прежнее значение.

Управление verify_membership (см. TelegramAccount.verify_membership) —
отдельно от enabled, тем же способом (--enabled/--no-enabled), что и
существующий флаг enabled: обычный (не admin) аккаунт может не иметь прав
на GetParticipantRequest в конкретной target-группе — --no-verify-membership
выключает именно проверку pending-приглашений
(InviterService._verify_pending_invites) для этого аккаунта, сама отправка
приглашений продолжает работать как обычно:

    python -m reader.inviter.manage add-account --name @car_ins_account \
        --session-name car_ins_account --session-path data/sessions/car_ins_account \
        --daily-limit 24 --no-verify-membership

    python -m reader.inviter.manage add-account --name @car_ins_account \
        --session-name car_ins_account --session-path data/sessions/car_ins_account \
        --daily-limit 24 --verify-membership

Посмотреть текущее состояние всех аккаунтов (enabled/verify_membership/
daily_limit/blocked_until):

    python -m reader.inviter.manage list-accounts

Заполнить/сверить telegram_user_id (см. TelegramAccount.telegram_user_id и
reader/inviter/identity.py) для ВСЕХ существующих аккаунтов (в т.ч.
enabled=False) — по очереди подключается к каждому по его .session-файлу,
читает get_me() и сохраняет в БД реальный telegram_user_id/username/phone
(старое имя сохраняется в TelegramAccount.previous_names, а не теряется).
Ничего не удаляет, session_name/session_path не трогает,
user_campaign_invites не изменяет.

После этого автоматически разрешает дубликаты telegram_user_id (см.
reader/inviter/identity.py resolve_duplicate_group): среди всех DB-записей
с одним и тем же telegram_user_id ровно одна остаётся CURRENT
(is_old=False), остальные автоматически помечаются is_old=True и
enabled=False (но НЕ удаляются — user_campaign_invites у них остаётся
нетронутым). Никогда не включает enabled=True автоматически — если ни
одна из дублирующихся записей не была enabled, ни одна и не станет:

    python -m reader.inviter.manage sync-accounts

Пример вывода:
    id=6 @Iv_vla_sov TG_ID=8838087889 CURRENT enabled=1
    id=7 @Iv_vla_sov TG_ID=8838087889 OLD enabled=0 (previously: @Misha_Offroad)

    CURRENT: 9
    OLD: 2
    DUPLICATES RESOLVED: 2
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon import TelegramClient  # noqa: E402

from reader.inviter.identity import (  # noqa: E402
    AccountIdentityMismatchError,
    SessionNotAuthorizedError,
    fetch_telegram_identity,
    reconcile_account_identity,
    resolve_duplicate_group,
)
from reader.inviter.models import InviteCampaign, TelegramAccount  # noqa: E402
from reader.inviter.repository import (  # noqa: E402
    InviteCampaignRepository,
    TelegramAccountRepository,
)
from reader.settings import ConfigError, Settings, load_settings  # noqa: E402
from reader.time_display import format_tbilisi  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Создание/обновление аккаунтов и кампаний инвайтера напрямую через "
            "TelegramAccountRepository/InviteCampaignRepository — без ручных "
            "правок SQLite."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_account = subparsers.add_parser(
        "add-account",
        help="Создать аккаунт инвайтера либо обновить его (по --name), если он уже существует.",
    )
    add_account.add_argument("--name", required=True, help='Например, "@vladimihailov".')
    add_account.add_argument("--phone", default="", help="Опционально.")
    add_account.add_argument(
        "--session-name", required=True,
        help='Имя сессии — то же, что ожидает "python -m reader.inviter.authorize".',
    )
    add_account.add_argument(
        "--session-path", required=True,
        help='Путь без ".session" — Telethon сам дописывает расширение.',
    )
    add_account.add_argument("--daily-limit", type=int, default=30)
    add_account.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    add_account.add_argument(
        "--verify-membership", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "Разрешить проверку pending-приглашений этого аккаунта через "
            "GetParticipantRequest (см. InviterService._verify_pending_invites). "
            "Выключите (--no-verify-membership) для обычных (не admin) "
            "аккаунтов, у которых эта проверка гарантированно проваливается "
            "('Chat admin privileges are required...') — отправка приглашений "
            "продолжит работать как обычно, только сам pending не проверяется. "
            "НЕ то же самое, что --enabled. По умолчанию включено."
        ),
    )

    subparsers.add_parser(
        "list-accounts",
        help="Показать все аккаунты инвайтера и их флаги (enabled, verify_membership, daily_limit, blocked_until).",
    )

    add_campaign = subparsers.add_parser(
        "add-campaign",
        help="Создать кампанию инвайтера либо обновить её (по --name), если она уже существует.",
    )
    add_campaign.add_argument("--name", required=True, help='Например, "Страхование".')
    add_campaign.add_argument("--keyword", required=True)
    add_campaign.add_argument("--target-chat", required=True, help='Например, "@tplgee".')
    add_campaign.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)

    subparsers.add_parser(
        "sync-accounts",
        help=(
            "Подключиться к каждому существующему аккаунту (по .session-файлу) и "
            "сверить/заполнить telegram_user_id/username/phone через get_me(), затем "
            "автоматически разрешить дубликаты telegram_user_id: ровно одна запись "
            "на физический аккаунт остаётся CURRENT, остальные помечаются OLD и "
            "enabled=false (без удаления строк и без автовключения enabled)."
        ),
    )

    return parser.parse_args(argv)


def ensure_account(
    db_path,
    *,
    name: str,
    phone: str,
    session_name: str,
    session_path: str,
    daily_limit: int,
    enabled: bool,
    verify_membership: bool = True,
) -> TelegramAccount:
    """Идемпотентно: если аккаунт с таким name уже есть — обновляет его,
    иначе создаёт новый. Ни при каких повторных вызовах не плодит дубликаты."""
    repository = TelegramAccountRepository(db_path)
    try:
        existing = next((a for a in repository.list() if a.name == name), None)
        if existing is None:
            return repository.create(
                name=name, phone=phone, session_name=session_name,
                session_path=session_path, daily_limit=daily_limit, enabled=enabled,
                verify_membership=verify_membership,
            )
        return repository.update(
            existing.id, name=name, phone=phone, session_name=session_name,
            session_path=session_path, daily_limit=daily_limit, enabled=enabled,
            verify_membership=verify_membership,
        )
    finally:
        repository.close()


def list_accounts(db_path) -> list[TelegramAccount]:
    """Только чтение — используется CLI-командой list-accounts (см. main())."""
    repository = TelegramAccountRepository(db_path)
    try:
        return repository.list()
    finally:
        repository.close()


def _format_account_line(account: TelegramAccount) -> str:
    """Одна строка вывода list-accounts. blocked_until хранится в БД в
    UTC (не меняется) — здесь только показывается по Asia/Tbilisi (см.
    reader/time_display.py и задачу про перевод отображения времени)."""
    blocked = f"до {format_tbilisi(account.blocked_until)}" if account.blocked_until else "нет"
    return (
        f"id={account.id} {account.name}: enabled={account.enabled}, "
        f"verify_membership={account.verify_membership}, "
        f"daily_limit={account.daily_limit}, blocked_until={blocked}"
    )


def ensure_campaign(
    db_path,
    *,
    name: str,
    keyword: str,
    target_chat: str,
    enabled: bool,
) -> InviteCampaign:
    """Идемпотентно: если кампания с таким name уже есть — обновляет её,
    иначе создаёт новую. Ни при каких повторных вызовах не плодит дубликаты."""
    repository = InviteCampaignRepository(db_path)
    try:
        existing = next((c for c in repository.list() if c.name == name), None)
        if existing is None:
            return repository.create(
                name=name, keyword=keyword, target_chat=target_chat, enabled=enabled,
            )
        return repository.update(
            existing.id, name=name, keyword=keyword, target_chat=target_chat, enabled=enabled,
        )
    finally:
        repository.close()


@dataclass(frozen=True)
class AccountSyncResult:
    """Один результат sync_accounts() — для читаемого вывода CLI и для
    тестов (см. tests/test_inviter_manage.py). detail — свободный текст
    (сообщение исключения при connect_failed/identity_mismatch, либо
    итоговый telegram_user_id при updated/unchanged)."""

    account_id: int
    name: str
    status: str
    detail: str = ""


def _build_sync_client_factory(settings: Settings) -> Callable[[TelegramAccount], TelegramClient]:
    """Та же конвенция инлайн-создания TelegramClient, что и в
    reader/inviter/authorize.py — без импорта фабрики из
    reader/inviter/main.py (та собрана вокруг остальных зависимостей
    InviterService, здесь они не нужны)."""

    def factory(account: TelegramAccount) -> TelegramClient:
        return TelegramClient(
            account.session_path, settings.telegram.api_id, settings.telegram.api_hash,
            receive_updates=False,
        )

    return factory


async def sync_accounts(
    db_path, *, client_factory: Callable[[TelegramAccount], TelegramClient],
) -> list[AccountSyncResult]:
    """Backfill/сверка telegram_user_id + name/phone для ВСЕХ аккаунтов
    (в т.ч. enabled=False) — см. reader/inviter/identity.py. Никогда не
    удаляет строки, никогда не трогает user_campaign_invites, никогда не
    меняет session_name/session_path. Пишет в БД ТОЛЬКО после успешного
    is_user_authorized() (см. fetch_telegram_identity).

    После сверки identity автоматически разрешает дубликаты
    telegram_user_id (см. resolve_all_duplicates/resolve_duplicate_group)
    — это ЕДИНСТВЕННОЕ место, где sync-accounts меняет enabled: только
    принудительно ВЫКЛЮЧАЕТ проигравшие дубликаты (is_old=True), никогда
    не включает ни одну запись автоматически."""
    account_repository = TelegramAccountRepository(db_path)
    results: list[AccountSyncResult] = []
    try:
        for account in account_repository.list():
            client = client_factory(account)
            try:
                await client.connect()
            except Exception as exc:
                results.append(AccountSyncResult(account.id, account.name, "connect_failed", str(exc)))
                continue

            try:
                try:
                    identity = await fetch_telegram_identity(client)
                except SessionNotAuthorizedError:
                    results.append(AccountSyncResult(account.id, account.name, "not_authorized"))
                    continue

                try:
                    updated = reconcile_account_identity(account_repository, account, identity)
                except AccountIdentityMismatchError as exc:
                    results.append(
                        AccountSyncResult(account.id, account.name, "identity_mismatch", str(exc))
                    )
                    continue

                status = "updated" if updated != account else "unchanged"
                results.append(
                    AccountSyncResult(
                        account.id, updated.name, status,
                        f"telegram_user_id={updated.telegram_user_id}",
                    )
                )
            finally:
                await client.disconnect()

        summary = resolve_all_duplicates(account_repository)
        print()
        for account in account_repository.list():
            if account.telegram_user_id is not None:
                print(_format_current_old_line(account))
        print()
        print(f"CURRENT: {summary.current}")
        print(f"OLD: {summary.old}")
        print(f"DUPLICATES RESOLVED: {summary.duplicates_resolved}")
    finally:
        account_repository.close()
    return results


@dataclass(frozen=True)
class DuplicateResolutionSummary:
    """Итог resolve_all_duplicates() — для вывода sync-accounts и тестов."""

    current: int
    old: int
    duplicates_resolved: int


def resolve_all_duplicates(account_repository: TelegramAccountRepository) -> DuplicateResolutionSummary:
    """Для КАЖДОГО непустого telegram_user_id в БД вызывает
    resolve_duplicate_group (см. reader/inviter/identity.py) — гарантирует
    ровно одну CURRENT-запись на физический Telegram-аккаунт, остальные
    помечает is_old=True/enabled=False. Идемпотентно — повторный вызов без
    изменения входных данных не производит новых записей в БД.

    duplicates_resolved считает группы (telegram_user_id), у которых
    сейчас БОЛЬШЕ ОДНОЙ DB-записи — то есть присутствующие дубликаты,
    поддерживаемые в разрешённом состоянии, а не только вновь
    обнаруженные в этом конкретном запуске."""
    accounts = account_repository.list()
    telegram_user_ids = sorted({a.telegram_user_id for a in accounts if a.telegram_user_id is not None})

    duplicates_resolved = 0
    for telegram_user_id in telegram_user_ids:
        group = [a for a in accounts if a.telegram_user_id == telegram_user_id]
        if len(group) > 1:
            duplicates_resolved += 1
        resolve_duplicate_group(account_repository, telegram_user_id)

    refreshed = account_repository.list()
    current = sum(1 for a in refreshed if a.telegram_user_id is not None and not a.is_old)
    old = sum(1 for a in refreshed if a.is_old)
    return DuplicateResolutionSummary(current=current, old=old, duplicates_resolved=duplicates_resolved)


def _format_current_old_line(account: TelegramAccount) -> str:
    """Одна строка отчёта sync-accounts (см. docstring модуля). Показывает
    АКТУАЛЬНЫЙ синхронизированный name (не историческое имя — Telegram
    правдиво возвращает один и тот же username для обеих session-записей
    одного физического аккаунта), но не теряет старое имя — оно видно в
    "(previously: ...)", если previous_names не пуст (см. задачу: "DB row
    7 исторически была @Misha_Offroad")."""
    state = "OLD" if account.is_old else "CURRENT"
    suffix = f" (previously: {', '.join(account.previous_names)})" if account.previous_names else ""
    return (
        f"id={account.id} {account.name} TG_ID={account.telegram_user_id} "
        f"{state} enabled={int(account.enabled)}{suffix}"
    )


def main() -> None:
    args = _parse_args(sys.argv[1:])
    try:
        settings = load_settings(CONFIG_PATH)

        if args.command == "add-account":
            account = ensure_account(
                settings.app.users_db_file,
                name=args.name, phone=args.phone, session_name=args.session_name,
                session_path=args.session_path, daily_limit=args.daily_limit, enabled=args.enabled,
                verify_membership=args.verify_membership,
            )
            print(
                f"✔ Аккаунт готов: {account.name} (id={account.id}, "
                f"daily_limit={account.daily_limit}, enabled={account.enabled}, "
                f"verify_membership={account.verify_membership})"
            )
        elif args.command == "list-accounts":
            accounts = list_accounts(settings.app.users_db_file)
            if not accounts:
                print("Аккаунтов нет.")
            for account in accounts:
                print(_format_account_line(account))
        elif args.command == "add-campaign":
            campaign = ensure_campaign(
                settings.app.users_db_file,
                name=args.name, keyword=args.keyword, target_chat=args.target_chat,
                enabled=args.enabled,
            )
            print(
                f"✔ Кампания готова: {campaign.name} (id={campaign.id}, "
                f"keyword={campaign.keyword}, target_chat={campaign.target_chat}, "
                f"enabled={campaign.enabled})"
            )
        elif args.command == "sync-accounts":
            results = asyncio.run(
                sync_accounts(
                    settings.app.users_db_file,
                    client_factory=_build_sync_client_factory(settings),
                )
            )
            for result in results:
                print(f"[{result.status}] id={result.account_id} {result.name} {result.detail}")
    except ConfigError as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)


if __name__ == "__main__":
    main()
