"""Чистые функции расчёта расписания архивных проверок — без I/O, без
Repository, без сети (тот же архитектурный принцип, что и
reader/fines/validation.py). Раздельно от reader/fines/archive_enrollment.py
(которое уже читает/пишет через FineMonitoringTaskRepository), чтобы саму
математику распределения можно было протестировать без единой SQLite-базы.
"""

from datetime import date, datetime, timedelta
from datetime import time as dt_time
from zoneinfo import ZoneInfo


def distribute_evenly(count: int, buckets: int) -> list[int]:
    """Сколько элементов положить в каждый из `buckets` дней (по порядку),
    чтобы:
    - сумма размеров была ровно `count`;
    - разница между размерами разных buckets была не больше 1;
    - результат был детерминированным (первые `count % buckets` дней
      получают на 1 больше, а не разбросаны случайно/интерливингом).

    Пример: distribute_evenly(998, 30) -> [34]*8 + [33]*22 (сумма 998).
    """
    if buckets <= 0:
        raise ValueError("buckets должно быть положительным")
    if count < 0:
        raise ValueError("count не может быть отрицательным")

    base, remainder = divmod(count, buckets)
    return [base + 1 if day < remainder else base for day in range(buckets)]


def local_time_to_utc(day: date, hour: int, tz: ZoneInfo) -> datetime:
    """day в hour:00 по tz -> aware datetime в UTC. Общая точка конвертации
    для build_archive_schedule() (массовый enrollment) и FineJob (первая
    архивная проверка только что завершившейся задачи) — чтобы оба места
    трактовали "~04:00 по таймзоне мониторинга" одинаково."""
    local_dt = datetime.combine(day, dt_time(hour, 0), tzinfo=tz)
    return local_dt.astimezone(ZoneInfo("UTC"))


def build_archive_schedule(
    task_ids: list[int],
    *,
    start_date: date,
    days: int,
    hour: int,
    tz: ZoneInfo,
) -> dict[int, datetime]:
    """task_ids — уже в детерминированном порядке (вызывающий код передаёт
    их отсортированными по id, см. enroll_tasks_in_archive_mode) — задачи
    раскладываются по `days` дням начиная с start_date, равномерно (см.
    distribute_evenly), время каждого дня — hour:00 по tz, переведённое в
    UTC. Чисто вычислительная функция — ничего не пишет в БД, поэтому
    повторный вызов с теми же аргументами всегда даёт тот же результат."""
    sizes = distribute_evenly(len(task_ids), days)

    schedule: dict[int, datetime] = {}
    cursor = 0
    for day_offset, size in enumerate(sizes):
        due_at = local_time_to_utc(start_date + timedelta(days=day_offset), hour, tz)
        for task_id in task_ids[cursor : cursor + size]:
            schedule[task_id] = due_at
        cursor += size

    return schedule
