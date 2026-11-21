#!/usr/bin/env python
"""Пауза и граница уведомлений Грузии (см. reader/fines/notification_control.py).

    python -m scripts.georgia_notifications status
    python -m scripts.georgia_notifications pause --note "восстановление подписок"
    python -m scripts.georgia_notifications resume                # dry-run: сколько будет исключено
    python -m scripts.georgia_notifications resume --execute      # исключить накопленное и снять паузу
    python -m scripts.georgia_notifications status --db-path X.db # другой файл (тесты)

pause/resume не требуют перезапусков: бот (client delivery poller,
"штрафов не найдено", сообщения менеджерам) и main-процесс (операторский
чат) читают состояние на каждом тике. Новый код обоих процессов должен быть
уже запущен — старый код паузу не видит.

resume сначала явно исключает из рассылки всё, что ждёт отправки
(client_fine_deliveries.excluded_at, fine_operator_notification_exclusions,
причина notification_boundary — без отметки о доставке), потом снимает
паузу и фиксирует границу: после неё уходят только квитанции, впервые
увиденные позже. Повторный запуск безопасен (уже исключённое не
считается).

Печатаются только количества. Запускать от того же пользователя, что и
бот (sudo -u leadradar): БД в WAL-режиме."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.notification_control import (  # noqa: E402
    EXCLUSION_NOTIFICATION_BOUNDARY,
    GeorgiaNotificationControl,
)
from reader.fines.notification_coordinator import FineNotificationCoordinator  # noqa: E402
from reader.fines.payment_status import FineStatusRepository, FineStatusTracker  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository  # noqa: E402
from reader.public_bot.delivery_service import ClientDeliveryService  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.settings import FineMonitorSettings, load_settings  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


class _NoSender:
    """Скрипт ничего не отправляет — только считает и исключает."""

    async def send_message(self, chat_id, text, *, buttons=None):
        raise RuntimeError("scripts.georgia_notifications never sends messages")

    async def notify(self, events):
        raise RuntimeError("scripts.georgia_notifications never sends messages")


class _Queues:
    """Те же выборки, что у отправителей (ClientDeliveryService,
    FineNotificationCoordinator) — без отправки."""

    def __init__(self, db_path: Path, tz: ZoneInfo):
        self._repositories = [
            FineMonitoringTaskRepository(db_path),
            DetectedFineRepository(db_path),
            FineSubscriptionRepository(db_path),
            ClientFineDeliveryRepository(db_path),
            FineStatusRepository(db_path),
        ]
        tasks, fines, subscriptions, deliveries, statuses = self._repositories
        self.client = ClientDeliveryService(
            fines, subscriptions, deliveries, _NoSender(), tz=tz,
            status_lookup=FineStatusTracker(statuses),
        )
        self.operator = FineNotificationCoordinator(fines, tasks, _NoSender())

    def pending(self) -> tuple[int, int]:
        return (
            self.client.exclude_pending(reason=EXCLUSION_NOTIFICATION_BOUNDARY, dry_run=True),
            self.operator.exclude_pending(reason=EXCLUSION_NOTIFICATION_BOUNDARY, dry_run=True),
        )

    def exclude(self) -> tuple[int, int]:
        return (
            self.client.exclude_pending(reason=EXCLUSION_NOTIFICATION_BOUNDARY),
            self.operator.exclude_pending(reason=EXCLUSION_NOTIFICATION_BOUNDARY),
        )

    def close(self) -> None:
        for repository in self._repositories:
            repository.close()


def _resolve(explicit_db_path: str | None) -> tuple[Path, ZoneInfo]:
    if explicit_db_path is not None:
        return Path(explicit_db_path), ZoneInfo(FineMonitorSettings.model_fields["timezone"].default)
    settings = load_settings(CONFIG_PATH)
    return settings.app.users_db_file, ZoneInfo(settings.fine_monitor.timezone)


def _fmt(value: datetime | None) -> str:
    return value.isoformat(sep=" ") + " UTC" if value else "—"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("status", "pause", "resume"))
    parser.add_argument("--execute", action="store_true", help="resume: реально исключить и снять паузу.")
    parser.add_argument("--note", default=None, help="Пометка в журнале паузы/снятия.")
    parser.add_argument("--db-path", default=None, help="Путь к БД (default: из config/config.yaml).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path, tz = _resolve(args.db_path)
    if not db_path.exists():
        print(f"БД не найдена: {db_path}", file=sys.stderr)
        return 1

    control = GeorgiaNotificationControl(db_path)
    queues = _Queues(db_path, tz)
    try:
        if args.action == "pause":
            changed = control.pause(note=args.note)
            print("Пауза поставлена." if changed else "Пауза уже стояла — ничего не изменено.")
        elif args.action == "resume":
            if not control.is_paused():
                print("Уведомления не на паузе — снимать нечего.", file=sys.stderr)
                return 1
            client, operator = queues.pending()
            print(f"Будет явно исключено из рассылки: клиентам и менеджерам {client}, в операторский чат {operator}.")
            if not args.execute:
                print("\nDry-run — ничего не записано. Запустите с resume --execute, чтобы снять паузу.")
                return 0
            client, operator = queues.exclude()
            boundary = control.resume(client_excluded=client, operator_excluded=operator, note=args.note)
            print(f"Исключено: клиентам и менеджерам {client}, в операторский чат {operator}.")
            print(f"Пауза снята, граница уведомлений: {_fmt(boundary)}.")

        state = control.state()
        client, operator = queues.pending()
        print(f"\nСостояние: {'ПАУЗА' if state.paused else 'доставка включена'}; "
              f"пауза с {_fmt(state.paused_at)}; граница {_fmt(state.boundary_at)}.")
        print(f"Ждут отправки сейчас: клиентам и менеджерам {client}, в операторский чат {operator}.")
        for action, note, client_excluded, operator_excluded, created_at in control.log()[-5:]:
            print(f"  {created_at} {action}: исключено {client_excluded}/{operator_excluded}"
                  + (f" — {note}" if note else ""))
        return 0
    finally:
        queues.close()
        control.close()


if __name__ == "__main__":
    raise SystemExit(main())
