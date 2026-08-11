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
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter.models import InviteCampaign, TelegramAccount  # noqa: E402
from reader.inviter.repository import (  # noqa: E402
    InviteCampaignRepository,
    TelegramAccountRepository,
)
from reader.settings import ConfigError, load_settings  # noqa: E402

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
                blocked = (
                    f"до {account.blocked_until.strftime('%Y-%m-%d %H:%M')} UTC"
                    if account.blocked_until else "нет"
                )
                print(
                    f"id={account.id} {account.name}: enabled={account.enabled}, "
                    f"verify_membership={account.verify_membership}, "
                    f"daily_limit={account.daily_limit}, blocked_until={blocked}"
                )
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
    except ConfigError as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)


if __name__ == "__main__":
    main()
