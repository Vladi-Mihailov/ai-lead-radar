"""Разовый CLI-скрипт для перевода конкретных задач мониторинга штрафов в
архивный режим (см. reader/fines/archive_enrollment.py — там же объяснение,
почему это НЕ делается автоматически для уже существующих завершённых
задач).

Ничего не хардкодит: список/диапазон id (или --all-completed) передаётся
аргументом командной строки — например, для одноразового перевода
production-партии id 144..1141:

    python -m reader.fines.enroll_archive --ids 144-1141
    python -m reader.fines.enroll_archive --ids 144-1141 --apply

Без --apply — dry-run (поведение по умолчанию): показывает "найдено задач:
N", сколько будет пропущено и почему, и как распределились бы
next_archive_check_at по дням, но НИЧЕГО не пишет в БД. Только с явным
--apply происходит реальное изменение production-данных — после чего скрипт
дополнительно печатает уже реально сохранённое распределение по дням.
"""

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.archive_enrollment import (  # noqa: E402
    ArchiveEnrollmentResult,
    enroll_tasks_in_archive_mode,
)
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.settings import ConfigError, load_settings  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def parse_ids(raw: str) -> list[int]:
    """"144-1141" -> [144..1141]; "1,2,5-7" -> [1,2,5,6,7]. Без дублей,
    отсортировано по возрастанию."""
    ids: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_str, _, end_str = chunk.partition("-")
            start, end = int(start_str), int(end_str)
            if end < start:
                raise ValueError(f"Некорректный диапазон: {chunk!r} (конец раньше начала)")
            ids.update(range(start, end + 1))
        else:
            ids.add(int(chunk))
    return sorted(ids)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Разовый перевод задач мониторинга штрафов в архивный режим "
            "(reader/fines/archive_enrollment.py). По умолчанию — dry-run, "
            "ничего не пишет в БД без --apply."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--ids",
        metavar="RANGE",
        help='Список/диапазон id через запятую, например "144-1141" или "1,2,5-7"',
    )
    source.add_argument(
        "--all-completed",
        action="store_true",
        help="Взять ВСЕ задачи со status='completed', ещё не в архивном режиме",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Реально записать изменения (без этого флага — только просмотр, ничего не пишет)",
    )
    return parser.parse_args(argv)


def _print_schedule_by_day(schedule: dict, tz: ZoneInfo) -> None:
    if not schedule:
        print("  (пусто — распределять нечего)")
        return

    by_day = Counter(due_at.astimezone(tz).date() for due_at in schedule.values())
    for day in sorted(by_day):
        print(f"  {day.isoformat()}: {by_day[day]}")
    print(f"  Итого: {len(schedule)}")


def _print_result(result: ArchiveEnrollmentResult, *, tz: ZoneInfo, applied: bool) -> None:
    verb = "Переведено в архивный режим" if applied else "Будет переведено в архивный режим"
    print(f"{verb}: {len(result.enrolled_task_ids)}")
    if result.transitioned_to_completed:
        verb2 = "Дополнительно переведено" if applied else "Дополнительно будет переведено"
        print(
            f"{verb2} из 'active' в 'completed' (end_date уже в прошлом): "
            f"{len(result.transitioned_to_completed)} — {result.transitioned_to_completed}"
        )
    print(f"Пропущено (не завершено — активный период ещё не истёк, либо остановлено): "
          f"{len(result.skipped_not_completed)}")
    print(f"Пропущено (уже в архивном режиме): {len(result.skipped_already_archived)}")
    if result.not_found_task_ids:
        print(f"Не найдено в БД: {len(result.not_found_task_ids)} — {result.not_found_task_ids}")

    print()
    print("Распределение по дням (next_archive_check_at):" if applied else "Распределение по дням (превью):")
    _print_schedule_by_day(result.schedule, tz)


def run_enrollment(
    task_repository: FineMonitoringTaskRepository,
    *,
    task_ids: list[int] | None,
    all_completed: bool,
    apply: bool,
    today,
    days: int,
    hour: int,
    tz: ZoneInfo,
) -> int:
    """Тестируемое ядро скрипта — без load_settings()/CONFIG_PATH: принимает
    уже открытый repository и уже разрешённые параметры (тесты передают
    tmp_path-репозиторий и произвольные days/hour/tz напрямую, не трогая
    реальный config.yaml). run() — тонкая обвязка поверх этой функции."""
    if all_completed:
        # list_archive_enrollment_candidates(), не list_completed_not_archived():
        # включает и status='active' с уже прошедшим end_date (см.
        # reader/fines/archive_enrollment.py про production-партию
        # 144..1141) — иначе такие задачи --all-completed вообще бы не увидел.
        ids = [task.id for task in task_repository.list_archive_enrollment_candidates(today)]
    else:
        ids = list(task_ids or [])

    print(f"Найдено задач: {len(ids)}")
    if not ids:
        return 0

    result = enroll_tasks_in_archive_mode(
        task_repository, ids, today=today, days=days, hour=hour, tz=tz, dry_run=not apply,
    )

    _print_result(result, tz=tz, applied=apply)

    if not apply:
        print()
        print("Это был dry-run — ничего не записано. Повторите с --apply, чтобы применить.")

    return 0


def run(argv: list[str]) -> int:
    args = _parse_args(argv)

    settings = load_settings(CONFIG_PATH)
    tz = ZoneInfo(settings.fine_monitor.timezone)
    today = datetime.now(timezone.utc).astimezone(tz).date()

    task_repository = FineMonitoringTaskRepository(settings.app.users_db_file)
    try:
        return run_enrollment(
            task_repository,
            task_ids=parse_ids(args.ids) if args.ids else None,
            all_completed=args.all_completed,
            apply=args.apply,
            today=today,
            days=settings.fine_monitor.archive_interval_days,
            hour=settings.fine_monitor.archive_check_hour,
            tz=tz,
        )
    finally:
        task_repository.close()


def main() -> None:
    try:
        sys.exit(run(sys.argv[1:]))
    except (ConfigError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)


if __name__ == "__main__":
    main()
