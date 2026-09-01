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
читает get_me() и сохраняет в БД реальный telegram_user_id/username/phone.
Ничего не включает/выключает, ничего не удаляет, session_name/session_path
не трогает, user_campaign_invites не изменяет. В конце печатает
предупреждение (без автослияния), если несколько DB-записей оказались
одним и тем же физическим Telegram-аккаунтом:

    python -m reader.inviter.manage sync-accounts
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
            "сверить/заполнить telegram_user_id/username/phone через get_me(). "
            "Ничего не включает/выключает, ничего не удаляет, дубликаты "
            "telegram_user_id только печатаются, не сливаются автоматически."
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
    меняет enabled, никогда не удаляет строки, никогда не трогает
    user_campaign_invites, никогда не меняет session_name/session_path.
    Пишет в БД ТОЛЬКО после успешного is_user_authorized() (см.
    fetch_telegram_identity). Дубликаты telegram_user_id только
    печатаются (см. report_duplicate_accounts) — без автослияния."""
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

        report_duplicate_accounts(account_repository.list())
    finally:
        account_repository.close()
    return results


def report_duplicate_accounts(accounts: list[TelegramAccount]) -> list[tuple[int, list[int]]]:
    """Группирует ВСЕ аккаунты (в т.ч. enabled=False) по непустому
    telegram_user_id и печатает предупреждение для каждой группы из более
    чем одной записи — см. задачу про id=6/id=7 и id=8/id=9. Никогда не
    сливает и не меняет записи, только сообщает оператору."""
    by_tg_id: dict[int, list[TelegramAccount]] = {}
    for account in accounts:
        if account.telegram_user_id is not None:
            by_tg_id.setdefault(account.telegram_user_id, []).append(account)

    duplicates: list[tuple[int, list[int]]] = []
    for tg_id, group in sorted(by_tg_id.items()):
        if len(group) > 1:
            ids = [a.id for a in group]
            names = ", ".join(a.name for a in group)
            print(f"⚠️  ДУБЛИКАТ: telegram_user_id={tg_id} — DB IDs {ids} ({names})")
            duplicates.append((tg_id, ids))
    return duplicates


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
