"""Явная настройка источников ЛС-кампании при деплое (без admin-бота).

    python -m reader.dm_campaigns.manage show
    python -m reader.dm_campaigns.manage set-source-chats --campaign insurance \\
        --chats VerhniyLars,sarpi_ge,Sadahlo [--apply]

Без --apply — только показывает, что изменится. Меняет ТОЛЬКО
dm_campaigns.source_chats одной кампании: enabled, ресурсы, инструкции и
другие кампании не трогаются. Отказывается, если кампания включена или
группа отсутствует в config/groups.yaml. Ни Telegram, ни OpenAI."""

import argparse
import sys
from pathlib import Path

from reader.dm_campaigns.models import same_chat_identifier
from reader.dm_campaigns.repository import DmCampaignRepository
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m reader.dm_campaigns.manage")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    set_sources = sub.add_parser("set-source-chats")
    set_sources.add_argument("--campaign", required=True)
    set_sources.add_argument("--chats", required=True, help="через запятую, как в config/groups.yaml")
    set_sources.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    settings = load_settings(Path("config/config.yaml"))
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
