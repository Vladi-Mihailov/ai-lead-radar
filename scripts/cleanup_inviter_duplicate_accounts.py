#!/usr/bin/env python
"""Maintenance: закрывает production-дубли telegram_accounts (см. задачу
про id=6/7, id=8/9 — тот же физический Telegram-аккаунт под двумя DB-
записями/session-файлами, resolve_duplicate_group уже верно проставила
is_old, но enabled осталась True — см. reader/inviter/duplicate_cleanup.py
module docstring за полным объяснением алгоритма/ограничений).

По умолчанию — dry-run: ТОЛЬКО читает БД (build_cleanup_plan), печатает
план, НИЧЕГО не пишет.

Только --execute реально пишет — и делает это ТОЛЬКО через
apply_cleanup_plan (единственное изменение — enabled=False у уже
подтверждённых is_old=True duplicate-строк, через generic
TelegramAccountRepository.update()). Никакого DELETE, никакого переноса
user_campaign_invites, никакого удаления session-файлов — см.
reader/inviter/duplicate_cleanup.py.

Использование:
    python -m scripts.cleanup_inviter_duplicate_accounts              # dry-run (default)
    python -m scripts.cleanup_inviter_duplicate_accounts --execute    # реально пишет
    python -m scripts.cleanup_inviter_duplicate_accounts --db-path X.db  # другой файл (тесты)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter.duplicate_cleanup import apply_cleanup_plan, build_cleanup_plan
from reader.inviter.repository import (
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)
from reader.settings import load_settings

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def _resolve_db_path(explicit: str | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    settings = load_settings(CONFIG_PATH)
    return settings.app.users_db_file


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="Реально записать enabled=False для подтверждённых duplicate-записей "
             "(default: dry-run, ничего не пишет).",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Путь к БД (default: settings.app.users_db_file из config/config.yaml).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = _resolve_db_path(args.db_path)

    if not db_path.exists():
        print(f"БД не найдена: {db_path}", file=sys.stderr)
        return 1

    accounts = TelegramAccountRepository(db_path)
    invites = UserCampaignInviteRepository(db_path)
    try:
        plan = build_cleanup_plan(accounts, invites)
        print(plan.format_report())

        if not args.execute:
            print("\nDry-run — ничего не записано. Запустите с --execute, чтобы применить.")
            return 0

        if not plan.actions:
            print("\nНечего применять.")
            return 0

        print("\n--execute: применяю cleanup...")
        result = apply_cleanup_plan(accounts, plan)
        print(result.format_report())
        return 0
    finally:
        accounts.close()
        invites.close()


if __name__ == "__main__":
    raise SystemExit(main())
