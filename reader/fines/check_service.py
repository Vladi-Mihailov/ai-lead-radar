"""Бизнес-логика проверки одной задачи мониторинга штрафов. Ничего не знает
про Telegram/Scheduler/CommandDispatcher/конкретный сайт-источник — только
FineProvider (интерфейс) и оба Repository. Уведомления не отправляет и
notification_sent_at не выставляет — это ответственность
FineNotificationCoordinator (используется FineJob и FineCommand).
"""

import json
import sqlite3
import time

from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import CheckResult, FineMonitoringTask, NewFineEvent
from reader.fines.provider import FineProvider, FineProviderError
from reader.fines.task_repository import FineMonitoringTaskRepository


def _elapsed_ms(started_at: float) -> int:
    return round((time.monotonic() - started_at) * 1000)


class FineCheckService:
    def __init__(
        self,
        provider: FineProvider,
        task_repository: FineMonitoringTaskRepository,
        detected_fine_repository: DetectedFineRepository,
    ):
        self._provider = provider
        self._task_repository = task_repository
        self._detected_fine_repository = detected_fine_repository

    async def check_task(self, task: FineMonitoringTask) -> CheckResult:
        started_at = time.monotonic()

        try:
            records = await self._provider.search_by_plate(task.car_number)
        except FineProviderError as exc:
            error_message = str(exc)
            self._task_repository.record_check_result(
                task.id, last_check_status="error", last_error=error_message
            )
            # Существующие detected_fines не трогаем вообще — сбой источника
            # не означает "штрафов нет", ничего не удаляем и не помечаем.
            return CheckResult(
                status="error",
                new_fines=[],
                error_message=error_message,
                total_fines_found=0,
                duration_ms=_elapsed_ms(started_at),
            )

        new_fines: list[NewFineEvent] = []

        for record in records:
            existing = self._detected_fine_repository.get_by_fingerprint(
                task.id, record.fingerprint
            )

            if existing is not None:
                self._detected_fine_repository.mark_seen(existing.id)
                continue

            try:
                created = self._detected_fine_repository.create(
                    monitoring_task_id=task.id,
                    car_number=record.car_number,
                    external_fine_id=record.external_fine_id,
                    fingerprint=record.fingerprint,
                    penalty_date=record.penalty_date,
                    due_date=record.due_date,
                    delivered_status=record.delivered_status,
                    raw_data=json.dumps(record.raw_data, ensure_ascii=False),
                )
            except sqlite3.IntegrityError:
                # Конкурентная вставка между get_by_fingerprint() и create() —
                # кто-то другой уже создал эту же запись (тот же
                # (monitoring_task_id, fingerprint)). UNIQUE constraint не
                # должен ронять всю проверку — считаем штраф уже известным,
                # а не новым.
                existing = self._detected_fine_repository.get_by_fingerprint(
                    task.id, record.fingerprint
                )
                if existing is not None:
                    self._detected_fine_repository.mark_seen(existing.id)
                continue

            new_fines.append(NewFineEvent.from_detected_fine(created, label=task.label))

        self._task_repository.record_check_result(task.id, last_check_status="ok", last_error=None)

        return CheckResult(
            status="ok",
            new_fines=new_fines,
            error_message=None,
            total_fines_found=len(records),
            duration_ms=_elapsed_ms(started_at),
        )
