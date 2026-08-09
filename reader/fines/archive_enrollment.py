"""Разовый перевод задач мониторинга в архивный режим (см.
reader/jobs/archive_fine_job.py) — НЕ вызывается автоматически ни из какого
job'а или фонового процесса.

Почему не автоматически: у уже существующих задач с status='completed' (или
'active' с уже прошедшим end_date, см. ниже) нет надёжного способа отличить
"обычный период закончился, кандидат на архив" от исторических записей,
завершённых по любой другой причине задолго до появления архивного режима
(тестовые данные, ручные правки и т.п.) — молча включать архивные проверки
для ВСЕХ них означало бы неожиданную для оператора нагрузку на police.ge.
Поэтому enrollment — явное действие: конкретный список task_id передаёт
вызывающий код (see reader/fines/enroll_archive.py, CLI-скрипт), а не
эвристика внутри этого модуля.

Для НОВЫХ задач, завершающихся уже после появления этой фичи, действует
отдельный, безопасный автоматический путь: FineJob сам переводит задачу в
архивный режим сразу в момент обычного завершения (см.
FineMonitoringTaskRepository.schedule_first_archive_check(), вызываемый из
reader/jobs/fine_job.py) — там источник события однозначен, поэтому эта
осторожность не нужна.

Про status='active' с end_date < today: такие задачи считаются ФАКТИЧЕСКИ
завершёнными (обычный период истёк), даже если ни один прогон FineJob ещё
не успел формально перевести их в 'completed' — например, партия задач,
вставленная напрямую в БД со status='active' и уже прошедшим end_date (см.
инцидент с production-партией id 144..1141: массово добавлены как active,
end_date проставлен вручную в прошлом). Без этой ветки enrollment молча
пропустил бы такие задачи (skipped_not_completed) и archive_check_enabled=1
выставился бы поверх status='active' — задача продолжала бы попадать в
list_active()/обычные 3 проверки в день ОДНОВРЕМЕННО с архивными, что прямо
противоречит требованию "обычный FineJob не должен видеть архивные задачи".
Поэтому enrollment сам довершает переход active -> completed для таких
задач, ДО того, как выставить архивное расписание (и делает это тоже под
dry_run — при dry_run=True ничего не пишется, включая этот переход)."""

from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from reader.fines.archive_scheduling import build_archive_schedule
from reader.fines.task_repository import FineMonitoringTaskRepository


@dataclass(frozen=True)
class ArchiveEnrollmentResult:
    enrolled_task_ids: list[int]
    skipped_not_completed: list[int]
    skipped_already_archived: list[int]
    not_found_task_ids: list[int]
    schedule: dict[int, datetime]
    # Подмножество enrolled_task_ids, которое до enrollment было
    # status='active' с уже прошедшим end_date — enrollment сам перевёл их
    # в 'completed' (см. докстрок модуля). Отдельное поле — чтобы CLI мог
    # явно показать оператору, что произошло что-то больше, чем просто
    # включение архивного режима.
    transitioned_to_completed: list[int] = field(default_factory=list)


def enroll_tasks_in_archive_mode(
    task_repository: FineMonitoringTaskRepository,
    task_ids: list[int],
    *,
    today: date,
    days: int,
    hour: int,
    tz: ZoneInfo,
    dry_run: bool = False,
) -> ArchiveEnrollmentResult:
    """Переводит подмножество task_ids в архивный режим, с равномерным
    распределением next_archive_check_at по ближайшим `days` дням (см.
    build_archive_schedule). Полностью generic — НЕ содержит ни одного
    захардкоженного id; конкретный диапазон/список передаёт вызывающий код.

    Задача считается кандидатом, если:
    - status == 'completed' (обычный, ожидаемый случай), ИЛИ
    - status == 'active' И end_date < today (период фактически истёк, но
      формального перехода в 'completed' ещё не было — см. докстрок модуля
      про production-партию 144..1141). В этом случае enrollment САМ
      переводит задачу в 'completed' перед тем, как включить архивный
      режим — иначе она осталась бы одновременно и в list_active(), и в
      архивной выборке.

    Задачи со status == 'active' и ещё НЕ истёкшим end_date — пропускаются
    (skipped_not_completed): архивный режим осмыслен только после обычного
    периода. status == 'stopped' — тоже пропускаются (оператор явно
    остановил мониторинг, это не то же самое, что "период закончился").

    Идемпотентно: задачи, у которых archive_check_enabled уже 1, повторно
    не трогаются (не переезжают на новое расписание) — повторный запуск с
    тем же (или пересекающимся) списком id не меняет уже enrolled задачи.

    task_ids сортируются по id перед распределением — тот самый
    "ORDER BY id" детерминизм, который требует построение расписания.

    dry_run=True — считает и возвращает ровно то же самое (найденные/
    пропущенные/расписание/переводимые в completed), но НЕ пишет в БД
    вообще ничего (ни set_status, ни enroll_in_archive_mode) — используется
    CLI-превью (см. reader/fines/enroll_archive.py) перед реальным
    изменением production-данных."""
    tasks_by_id = {task.id: task for task in task_repository.get_many(list(task_ids))}

    found_ids = sorted(tasks_by_id)
    not_found_task_ids = sorted(set(task_ids) - set(found_ids))

    eligible_ids: list[int] = []
    transitioned_to_completed: list[int] = []
    skipped_not_completed: list[int] = []
    skipped_already_archived: list[int] = []

    for task_id in found_ids:
        task = tasks_by_id[task_id]

        if task.archive_check_enabled:
            skipped_already_archived.append(task_id)
            continue

        if task.status == "completed":
            eligible_ids.append(task_id)
            continue

        if task.status == "active" and task.end_date < today:
            eligible_ids.append(task_id)
            transitioned_to_completed.append(task_id)
            continue

        # status == 'active' с ещё не истёкшим end_date, либо 'stopped'.
        skipped_not_completed.append(task_id)

    if not dry_run:
        for task_id in transitioned_to_completed:
            task_repository.set_status(task_id, "completed")

    schedule = build_archive_schedule(eligible_ids, start_date=today, days=days, hour=hour, tz=tz)
    if schedule and not dry_run:
        task_repository.enroll_in_archive_mode(schedule)

    return ArchiveEnrollmentResult(
        enrolled_task_ids=eligible_ids,
        skipped_not_completed=skipped_not_completed,
        skipped_already_archived=skipped_already_archived,
        not_found_task_ids=not_found_task_ids,
        schedule=schedule,
        transitioned_to_completed=transitioned_to_completed,
    )
