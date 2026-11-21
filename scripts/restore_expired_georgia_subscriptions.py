#!/usr/bin/env python
"""Восстановление подписок Грузии, автоматически истёкших по старому сроку
до перехода на бессрочный мониторинг (см. задачу; переход —
scripts/migrate_georgia_unlimited_monitoring.py, 2026-11-17, он такие
подписки сознательно не трогал).

Восстанавливается только СУЩЕСТВУЮЩАЯ подписка (status='expired' — этот
статус ставит только FineSubscriptionRepository.expire_elapsed по
end_date), для которой:
- истечение было до перехода (updated_at раньше UNLIMITED_MIGRATION_AT);
- сохранилась связь с клиентом и машиной (telegram_user_id/chat_id,
  car_number);
- у клиента нет действующей подписки на эту машину (иначе дубль) и нет
  более позднего явного OFF/удаления этой машины;
- задача не остановлена явно (status='stopped' — менеджер или оператор),
  номер не скрыт (denylist).
Из нескольких истёкших строк одного клиента на одну машину — только
последняя. Непринятые приглашения (pending_claim) не истекают и не
трогаются. Всё, что не укладывается в эти правила надёжно, — отдельный
агрегат "сомнительные".

Включение — существующий механизм (SubscriptionService.turn_on_car, тот
же, что "▶️ Включить" у клиента): та же строка, бессрочно, задача номера
поднимается или подписка переезжает на уже активную задачу номера — без
дублей подписок и задач, без проверок police.ge и без нового bootstrap'а.
История доставок не сбрасывается; все уже сохранённые штрафы номера явно
исключаются из рассылки этой подписке (subscription_restored) — старые
квитанции не становятся "новыми штрафами".

    python -m scripts.restore_expired_georgia_subscriptions                       # dry-run
    python -m scripts.restore_expired_georgia_subscriptions --execute             # применить
    python -m scripts.restore_expired_georgia_subscriptions --rollback            # что вернёт откат
    python -m scripts.restore_expired_georgia_subscriptions --rollback --execute  # откатить
    python -m scripts.restore_expired_georgia_subscriptions --db-path X.db        # другой файл (тесты)

--execute требует паузы уведомлений Грузии (scripts/georgia_notifications.py
pause). Перед записью каждая строка перечитывается: если она изменилась
после предварительного просмотра (клиент сам включил, удалил и т.п.), она
пропускается. Повторный --execute ничего не меняет. Прежнее состояние
подписки и задачи сохраняется в ge_subscription_restore_backup; откат
возвращает только строки, которые с тех пор никто не менял. Backup файла
БД скрипт сам не делает. Печатаются только количества; запускать от того
же пользователя, что и бот (sudo -u leadradar)."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import UNLIMITED_END_DATE  # noqa: E402
from reader.fines.notification_control import (  # noqa: E402
    EXCLUSION_SUBSCRIPTION_RESTORED,
    GeorgiaNotificationControl,
)
from reader.fines.suppression import is_fine_output_suppressed  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import SubscriptionService  # noqa: E402
from reader.settings import FineMonitorSettings, load_settings  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

# Перезапуск бота с бессрочным мониторингом (PR #53) — 2026-11-17 18:49:06 UTC.
# Истечение позже — уже не "по старому сроку до перехода".
UNLIMITED_MIGRATION_AT = "2026-11-17 18:49:06"

BACKUP_TABLE = "ge_subscription_restore_backup"
_CREATE_BACKUP_TABLE = f"""
CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id          INTEGER NOT NULL UNIQUE,
    old_status               TEXT NOT NULL,
    old_start_date           TEXT NOT NULL,
    old_end_date             TEXT NOT NULL,
    old_task_id              INTEGER NOT NULL,
    old_updated_at           TEXT,
    task_id                  INTEGER NOT NULL,
    task_old_status          TEXT NOT NULL,
    task_old_start_date      TEXT NOT NULL,
    task_old_end_date        TEXT NOT NULL,
    task_old_archive_check   INTEGER,
    task_old_next_archive_at TEXT,
    excluded_deliveries      INTEGER NOT NULL DEFAULT 0,
    outcome                  TEXT NOT NULL DEFAULT 'pending',
    restored_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    rolled_back_at           TIMESTAMP
)
"""

_EXCLUDED = "исключено"
_DOUBTFUL = "сомнительно"


@dataclass(frozen=True)
class Candidate:
    """Снимок строки на момент предварительного просмотра — перед записью
    сравнивается с её текущим состоянием."""

    id: int
    car_number: str
    telegram_user_id: int
    status: str
    start_date: str
    end_date: str
    updated_at: str | None
    monitoring_task_id: int
    delegated: bool


@dataclass
class RestorePlan:
    """Только количества — никаких номеров/идентификаторов."""

    expired_total: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)
    task_status: Counter = field(default_factory=Counter)
    moves_to_other_task: int = 0
    new_plates: int = 0
    new_plates_pending_bootstrap: int = 0
    stored_fines_to_exclude: int = 0

    def format_report(self) -> str:
        lines = [
            f"Истёкших подписок (status='expired'): {self.expired_total}",
            f"К восстановлению: {len(self.candidates)} "
            f"(машин {len({c.car_number for c in self.candidates})}, "
            f"клиентов {len({c.telegram_user_id for c in self.candidates})}, "
            f"из них делегированных менеджером {sum(c.delegated for c in self.candidates)})",
        ]
        excluded = {k: v for k, v in self.reasons.items() if k[0] == _EXCLUDED}
        doubtful = {k: v for k, v in self.reasons.items() if k[0] == _DOUBTFUL}
        lines.append(f"Исключено: {sum(excluded.values())}")
        lines += [f"  - {reason}: {count}" for (_, reason), count in sorted(excluded.items())]
        lines.append(f"Сомнительные (не восстанавливаются): {sum(doubtful.values())}")
        lines += [f"  - {reason}: {count}" for (_, reason), count in sorted(doubtful.items())]
        lines += [
            f"Задача восстанавливаемых: {dict(self.task_status)}; переедут на уже активную задачу номера: "
            f"{self.moves_to_other_task}",
            f"Дополнительно начнут отслеживаться номеров: {self.new_plates} "
            f"(первичных поисков: {self.new_plates_pending_bootstrap} — bootstrap ждёт только у них)",
            f"Сохранённых штрафов, которые будут явно исключены из рассылки: {self.stored_fines_to_exclude}",
        ]
        return "\n".join(lines)


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, params).fetchall()


def _blocking_reason(row, siblings, task, *, today: str) -> tuple[str, str] | None:
    """Почему эту истёкшую строку нельзя восстановить (None — можно).
    siblings — другие подписки того же клиента на ту же машину."""
    if row["telegram_user_id"] is None or row["telegram_chat_id"] is None or not row["car_number"]:
        return _EXCLUDED, "нет связи с клиентом или машиной"
    if any(s["status"] in ("active", "pending_claim") and s["end_date"] >= today for s in siblings):
        return _EXCLUDED, "у клиента уже есть действующая подписка на эту машину"
    if any(s["status"] == "active" for s in siblings):
        # Ещё 'active' со старым сроком (до ближайшего expire_elapsed):
        # turn_on_car включил бы её, а эту строку отправил в архив.
        return _DOUBTFUL, "у клиента есть другая активная строка этой машины с прошедшим сроком"
    if any(
        s["status"] in ("stopped", "archived") and (s["updated_at"] or "") > (row["updated_at"] or "")
        for s in siblings
    ):
        return _EXCLUDED, "позже клиент сам выключил или удалил эту машину"
    if is_fine_output_suppressed(row["car_number"]):
        return _EXCLUDED, "номер скрыт (denylist)"
    if task is None:
        return _DOUBTFUL, "задача подписки не найдена"
    if task["status"] == "stopped":
        return _EXCLUDED, "обслуживание остановлено явно (задача остановлена)"
    if task["status"] not in ("active", "completed"):
        return _DOUBTFUL, f"неожиданный статус задачи: {task['status']}"
    if (row["updated_at"] or "") >= UNLIMITED_MIGRATION_AT or row["end_date"] >= UNLIMITED_END_DATE.isoformat():
        return _DOUBTFUL, "истекла не до перехода на бессрочный мониторинг"
    return None


def build_restore_plan(conn: sqlite3.Connection, *, today: date) -> RestorePlan:
    plan = RestorePlan()
    today_s = today.isoformat()
    subscriptions = _rows(conn, "SELECT * FROM fine_monitoring_subscriptions")
    tasks = {t["id"]: t for t in _rows(conn, "SELECT * FROM fine_monitoring_tasks")}
    active_plates = {t["car_number"] for t in tasks.values() if t["status"] == "active"}
    try:
        pending_bootstrap = {
            r[0] for r in conn.execute(
                "SELECT car_number FROM georgia_plate_bootstrap_state WHERE bootstrap_status = 'pending'"
            )
        }
    except sqlite3.OperationalError:
        pending_bootstrap = set()

    by_client_car: dict[tuple, list] = {}
    for row in subscriptions:
        by_client_car.setdefault((row["telegram_user_id"], row["car_number"]), []).append(row)

    expired = [row for row in subscriptions if row["status"] == "expired"]
    plan.expired_total = len(expired)
    groups: dict[tuple, list] = {}
    for row in expired:
        groups.setdefault((row["telegram_user_id"], row["car_number"]), []).append(row)

    for key, group in groups.items():
        group.sort(key=lambda r: (r["end_date"], r["id"]), reverse=True)
        for _ in group[1:]:
            plan.reasons[(_EXCLUDED, "более старый дубль истёкшей подписки той же машины")] += 1
        row = group[0]
        siblings = [s for s in by_client_car.get(key, []) if s["id"] != row["id"]]
        task = tasks.get(row["monitoring_task_id"])
        reason = _blocking_reason(row, siblings, task, today=today_s)
        if reason is not None:
            plan.reasons[reason] += 1
            continue
        plan.candidates.append(Candidate(
            id=row["id"], car_number=row["car_number"], telegram_user_id=row["telegram_user_id"],
            status=row["status"], start_date=row["start_date"], end_date=row["end_date"],
            updated_at=row["updated_at"], monitoring_task_id=row["monitoring_task_id"],
            delegated=row["created_by_telegram_user_id"] is not None,
        ))
        plan.task_status[task["status"]] += 1
        if task["status"] != "active" and row["car_number"] in active_plates:
            plan.moves_to_other_task += 1

    new_plates = {c.car_number for c in plan.candidates} - active_plates
    plan.new_plates = len(new_plates)
    plan.new_plates_pending_bootstrap = len(new_plates & pending_bootstrap)
    for candidate in plan.candidates:
        plan.stored_fines_to_exclude += conn.execute(
            "SELECT COUNT(*) FROM detected_fines df WHERE df.car_number = ? AND NOT EXISTS ("
            " SELECT 1 FROM client_fine_deliveries d WHERE d.detected_fine_id = df.id AND d.subscription_id = ?"
            " AND d.recipient_role = 'owner' AND (d.delivered_at IS NOT NULL OR d.excluded_at IS NOT NULL))",
            (candidate.car_number, candidate.id),
        ).fetchone()[0]
    return plan


class _NoCheckService:
    """turn_on_car ничего не проверяет; любое обращение к police.ge — ошибка."""

    def __getattr__(self, name):
        raise RuntimeError(f"restore must not call the check service ({name})")


@dataclass
class RestoreResult:
    restored: int = 0
    skipped: Counter = field(default_factory=Counter)
    excluded_deliveries: int = 0


def apply_restore(db_path: Path, plan: RestorePlan, *, today: date) -> RestoreResult:
    """Восстанавливает кандидатов плана по одному: перечитать строку,
    сравнить со снимком и заново проверить правила, сохранить прежнее
    состояние, turn_on_car, явно исключить сохранённые штрафы номера из
    рассылки этой подписке."""
    result = RestoreResult()
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.execute(_CREATE_BACKUP_TABLE)
    tasks = FineMonitoringTaskRepository(db_path)
    subscriptions = FineSubscriptionRepository(db_path)
    deliveries = ClientFineDeliveryRepository(db_path)
    fines = DetectedFineRepository(db_path)
    service = SubscriptionService(tasks, subscriptions, None, _NoCheckService())
    try:
        for candidate in plan.candidates:
            current = _rows(conn, "SELECT * FROM fine_monitoring_subscriptions WHERE id = ?", (candidate.id,))
            if not current:
                result.skipped["строка исчезла"] += 1
                continue
            row = current[0]
            snapshot = (candidate.status, candidate.end_date, candidate.updated_at, candidate.monitoring_task_id,
                        candidate.telegram_user_id, candidate.car_number)
            if (row["status"], row["end_date"], row["updated_at"], row["monitoring_task_id"],
                    row["telegram_user_id"], row["car_number"]) != snapshot:
                result.skipped["изменилась после предварительного просмотра"] += 1
                continue
            siblings = _rows(
                conn, "SELECT * FROM fine_monitoring_subscriptions WHERE telegram_user_id = ? AND car_number = ? "
                "AND id != ?", (row["telegram_user_id"], row["car_number"], row["id"]),
            )
            task_rows = _rows(conn, "SELECT * FROM fine_monitoring_tasks WHERE id = ?", (row["monitoring_task_id"],))
            if _blocking_reason(row, siblings, task_rows[0] if task_rows else None, today=today.isoformat()):
                result.skipped["правила больше не выполняются"] += 1
                continue

            subscription = subscriptions.get(candidate.id)
            target_task = tasks.get(service._task_id_to_monitor(subscription))
            # OR REPLACE: строка от прерванного или откаченного раньше запуска
            # устарела — подписка сейчас снова 'expired' и проверена выше.
            conn.execute(
                f"INSERT OR REPLACE INTO {BACKUP_TABLE} (subscription_id, old_status, old_start_date, old_end_date, "
                "old_task_id, old_updated_at, task_id, task_old_status, task_old_start_date, task_old_end_date, "
                "task_old_archive_check, task_old_next_archive_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["id"], row["status"], row["start_date"], row["end_date"], row["monitoring_task_id"],
                 row["updated_at"], target_task.id, target_task.status, target_task.start_date.isoformat(),
                 target_task.end_date.isoformat(), int(target_task.archive_check_enabled),
                 target_task.next_archive_check_at.isoformat() if target_task.next_archive_check_at else None),
            )
            restored = service.turn_on_car(candidate.id, telegram_user_id=candidate.telegram_user_id, today=today)
            if restored is None or restored.id != candidate.id:
                # Строка остаётся для разбора (outcome='refused'); откат её не
                # трогает — подписка не в состоянии "восстановлена".
                conn.execute(f"UPDATE {BACKUP_TABLE} SET outcome = 'refused' WHERE subscription_id = ?",
                             (candidate.id,))
                result.skipped["turn_on_car отказал"] += 1
                continue

            roles = ["owner"] + (["trusted_operator"] if restored.is_delegated() else [])
            excluded = 0
            for fine in fines.list_by_car_number(restored.car_number):
                for role in roles:
                    excluded += deliveries.exclude(fine.id, restored.id, role, reason=EXCLUSION_SUBSCRIPTION_RESTORED)
            conn.execute(
                f"UPDATE {BACKUP_TABLE} SET task_id = ?, excluded_deliveries = ?, outcome = 'restored' "
                "WHERE subscription_id = ?",
                (restored.monitoring_task_id, excluded, candidate.id),
            )
            result.restored += 1
            result.excluded_deliveries += excluded
    finally:
        for repository in (tasks, subscriptions, deliveries, fines):
            repository.close()
        conn.close()
    return result


def _backup_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (BACKUP_TABLE,)).fetchone():
        return []
    return _rows(
        conn, f"SELECT * FROM {BACKUP_TABLE} WHERE rolled_back_at IS NULL AND outcome = 'restored' ORDER BY id DESC",
    )


def rollback_restore(conn: sqlite3.Connection, *, today: date, execute: bool) -> Counter:
    """Возвращает подписку в 'expired' с прежними датами, задачей и
    updated_at (её снова можно будет восстановить), только если с
    восстановления её никто не трогал (active, бессрочная, та же задача).
    Задачу — к прежнему состоянию, только если на ней не осталось других
    действующих подписок и её с тех пор не меняли. В порядке, обратном
    восстановлению (id backup-строки): первая восстановленная строка задачи
    хранит её исходное состояние и откатывается последней."""
    outcome: Counter = Counter()
    unlimited = UNLIMITED_END_DATE.isoformat()
    # Подписки, которые этот проход возвращает, — в dry-run их строки ещё не
    # изменены, но "другими действующими" на задаче они уже не считаются:
    # иначе предварительный просмотр расходился бы с откатом общих задач.
    reverted: set[int] = set()
    for backup in _backup_rows(conn):
        row = _rows(conn, "SELECT * FROM fine_monitoring_subscriptions WHERE id = ?", (backup["subscription_id"],))
        if not row or row[0]["status"] != "active" or row[0]["end_date"] != unlimited \
                or row[0]["monitoring_task_id"] != backup["task_id"]:
            outcome["подписка изменена после восстановления — не трогаем"] += 1
            continue
        outcome["подписка вернётся в 'expired'"] += 1
        reverted.add(backup["subscription_id"])
        if execute:
            conn.execute(
                "UPDATE fine_monitoring_subscriptions SET status = ?, start_date = ?, end_date = ?, "
                "monitoring_task_id = ?, updated_at = ? WHERE id = ?",
                (backup["old_status"], backup["old_start_date"], backup["old_end_date"], backup["old_task_id"],
                 backup["old_updated_at"], backup["subscription_id"]),
            )
            conn.execute(f"UPDATE {BACKUP_TABLE} SET rolled_back_at = CURRENT_TIMESTAMP WHERE subscription_id = ?",
                         (backup["subscription_id"],))
        task = _rows(conn, "SELECT * FROM fine_monitoring_tasks WHERE id = ?", (backup["task_id"],))
        if not task or (task[0]["status"], task[0]["start_date"], task[0]["end_date"]) == (
            backup["task_old_status"], backup["task_old_start_date"], backup["task_old_end_date"],
        ):
            continue
        others = sum(
            1 for (subscription_id,) in conn.execute(
                "SELECT id FROM fine_monitoring_subscriptions WHERE monitoring_task_id = ? "
                "AND status IN ('active', 'pending_claim') AND end_date >= ?",
                (backup["task_id"], today.isoformat()),
            )
            if subscription_id not in reverted
        )
        if others or task[0]["status"] != "active" or task[0]["end_date"] != unlimited:
            outcome["задача остаётся как есть (на ней есть другие подписки или её меняли)"] += 1
            continue
        outcome["задача вернётся к прежнему состоянию"] += 1
        if execute:
            conn.execute(
                "UPDATE fine_monitoring_tasks SET status = ?, start_date = ?, end_date = ?, archive_check_enabled = ?, "
                "next_archive_check_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (backup["task_old_status"], backup["task_old_start_date"], backup["task_old_end_date"],
                 backup["task_old_archive_check"], backup["task_old_next_archive_at"], backup["task_id"]),
            )
    return outcome


def _resolve(explicit_db_path: str | None) -> tuple[Path, date]:
    if explicit_db_path is not None:
        db_path = Path(explicit_db_path)
        tz_name = FineMonitorSettings.model_fields["timezone"].default
    else:
        settings = load_settings(CONFIG_PATH)
        db_path = settings.app.users_db_file
        tz_name = settings.fine_monitor.timezone
    return db_path, datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).date()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="Реально записать (default: dry-run).")
    parser.add_argument("--rollback", action="store_true", help="Откат предыдущего --execute.")
    parser.add_argument("--db-path", default=None, help="Путь к БД (default: из config/config.yaml).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path, today = _resolve(args.db_path)
    if not db_path.exists():
        print(f"БД не найдена: {db_path}", file=sys.stderr)
        return 1

    if args.rollback:
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            outcome = rollback_restore(conn, today=today, execute=args.execute)
            conn.execute("COMMIT" if args.execute else "ROLLBACK")
        finally:
            conn.close()
        for reason, count in sorted(outcome.items()):
            print(f"{reason}: {count}")
        if not args.execute:
            print("\nDry-run — ничего не записано. Запустите с --rollback --execute, чтобы откатить.")
        return 0

    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        plan = build_restore_plan(ro, today=today)
    finally:
        ro.close()
    print(f"Сегодня (по часовому поясу мониторинга): {today.isoformat()}")
    print(plan.format_report())
    if not args.execute:
        print("\nDry-run — ничего не записано. Запустите с --execute, чтобы восстановить.")
        return 0

    control = GeorgiaNotificationControl(db_path)
    try:
        paused = control.is_paused()
    finally:
        control.close()
    if not paused:
        print("\nУведомления Грузии не на паузе — сначала: python -m scripts.georgia_notifications pause",
              file=sys.stderr)
        return 1

    print("\n--execute: восстанавливаю...")
    result = apply_restore(db_path, plan, today=today)
    print(f"Восстановлено подписок: {result.restored}; явно исключено из рассылки сохранённых штрафов: "
          f"{result.excluded_deliveries}")
    for reason, count in sorted(result.skipped.items()):
        print(f"  пропущено ({reason}): {count}")
    print(f"Прежнее состояние сохранено в {BACKUP_TABLE}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
