"""Явная настройка источников ЛС-кампании при деплое (без admin-бота).

    python -m reader.dm_campaigns.manage show
    python -m reader.dm_campaigns.manage set-source-chats --campaign insurance \\
        --chats VerhniyLars,sarpi_ge,Sadahlo [--apply]
    python -m reader.dm_campaigns.manage sender-states
    python -m reader.dm_campaigns.manage clear-sender-block --account-id 15 [--apply]

Без --apply — только показывает, что изменится. Меняет ТОЛЬКО
dm_campaigns.source_chats одной кампании: enabled, ресурсы, инструкции и
другие кампании не трогаются. Отказывается, если кампания включена или
группа отсутствует в config/groups.yaml. Ни Telegram, ни OpenAI."""

import argparse
import sys
from pathlib import Path

from reader.dm_campaigns.models import same_chat_identifier
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.sender_state import DmSenderStateRepository
from reader.groups import load_groups
from reader.settings import load_settings


def _format(campaign) -> str:
    sources = ", ".join(campaign.source_chats) if campaign.source_chats else "все группы"
    return f"{campaign.key}: enabled={int(campaign.enabled)} source_chats=[{sources}]"


def set_source_chats(repository: DmCampaignRepository, group_identifiers: list[str], *, key: str,
                     chats: list[str], apply: bool) -> str:
    campaign = repository.get_campaign_by_key(key)
    if campaign is None:
        raise SystemExit(f"Кампания {key!r} не найдена")
    if campaign.enabled:
        raise SystemExit(f"Кампания {key!r} включена — источники меняются только у выключенной кампании")
    unknown = [c for c in chats if not any(same_chat_identifier(c, g) for g in group_identifiers)]
    if unknown:
        raise SystemExit(f"Нет в config/groups.yaml: {', '.join(unknown)}")
    if not apply:
        return f"DRY RUN\nбыло:  {_format(campaign)}\nбудет: source_chats=[{', '.join(chats)}]"
    updated = repository.update_source_chats(campaign.id, chats)
    return f"APPLIED\nбыло:  {_format(campaign)}\nстало: {_format(updated)}"


def _sender_command(states: DmSenderStateRepository, args) -> str:
    from datetime import datetime, timezone
    try:
        if args.command == "sender-states":
            rows = states.list()
            return "\n".join(
                f"account_id={s.account_id} blocked={s.blocked_reason or '-'} flood_wait_until={s.flood_wait_until or '-'}"
                for s in rows
            ) or "Нет записей."
        current = states.get(args.account_id)
        if not current.blocked_reason:
            return f"account_id={args.account_id}: ручной блокировки нет"
        if not args.apply:
            return f"DRY RUN: снять блокировку {current.blocked_reason!r} с account_id={args.account_id}"
        states.clear_block(args.account_id, now=datetime.now(timezone.utc))
        return f"APPLIED: блокировка {current.blocked_reason!r} снята с account_id={args.account_id}"
    finally:
        states.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m reader.dm_campaigns.manage")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    set_sources = sub.add_parser("set-source-chats")
    set_sources.add_argument("--campaign", required=True)
    set_sources.add_argument("--chats", required=True, help="через запятую, как в config/groups.yaml")
    set_sources.add_argument("--apply", action="store_true")
    sub.add_parser("sender-states", help="DM-состояние отправителей (FloodWait / ручная блокировка)")
    clear = sub.add_parser("clear-sender-block",
                           help="Снять ручную блокировку ЛС-отправителя (после PeerFlood) — после проверки аккаунта")
    clear.add_argument("--account-id", type=int, required=True)
    clear.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    settings = load_settings(Path("config/config.yaml"))
    if args.command in ("sender-states", "clear-sender-block"):
        print(_sender_command(DmSenderStateRepository(settings.app.users_db_file), args))
        return
    repository = DmCampaignRepository(settings.app.users_db_file)
    try:
        if args.command == "show":
            for campaign in repository.list_campaigns():
                print(_format(campaign))
            return
        groups = [str(g.identifier) for g in load_groups(settings.app.groups_file)]
        chats = [c.strip() for c in args.chats.split(",") if c.strip()]
        print(set_source_chats(repository, groups, key=args.campaign, chats=chats, apply=args.apply))
    finally:
        repository.close()


if __name__ == "__main__":
    main(sys.argv[1:])
