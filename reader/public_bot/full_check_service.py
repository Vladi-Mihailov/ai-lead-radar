"""FullCheckService — скрытая trusted/fine-admin-only maintenance-команда
"fine check-all" (@ProtocolGEbot, задача "add silent Georgia full database
check command"). Единоразовый полный обход ВСЕХ существующих Georgia
fine-monitoring задач через ЕДИНСТВЕННЫЙ authoritative
FineCheckService.check_task() — ни отдельного police.ge client/parser/
check здесь нет, ни дублирования false-zero guard (та защита живёт
ИСКЛЮЧИТЕЛЬНО внутри check_task(), см. reader/fines/check_service.py, и
работает одинаково независимо от того, кто его вызывает).

notification_policy="silent" (см. reader/fines/check_service.py::
NotificationPolicy) — штрафы, впервые обнаруженные ИМЕННО этим сканом,
рождаются с уже проставленным notification_sent_at и поэтому никогда не
попадут в FineNotificationCoordinator.flush_pending() — ни сейчас, ни
позже. Обычный мониторинг (FineJob/ClientFineJob/manual "Проверить
сейчас") продолжает работать штатно (notification_policy="normal" по
умолчанию) — генуинно новый штраф, найденный ПОСЛЕ этого скана, создаёт
свою собственную detected_fines-строку (дедуп — (monitoring_task_id,
fingerprint)) с обычным notification_sent_at=NULL и будет доставлен как
всегда.

Прогресс — ОДНО сообщение, периодически редактируемое (см.
ProgressSenderLike) — НЕ отдельное сообщение на каждую машину и НИКОГДА не
отправляется клиентам (progress/summary идут только оператору, инициировавшему
"fine check-all confirm", в тот же chat_id)."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from reader.fines.check_service import FineCheckService
from reader.fines.task_repository import FineMonitoringTaskRepository

# См. задачу п.2 — 2 секунды МЕЖДУ РАЗНЫМИ машинами (НЕ путать с 5-секундным
# false-zero confirmation delay внутри FineCheckService.check_task() —
# задача явно требует "ОСТАВИТЬ 5 секунд", не заменять на 2).
_INTER_CAR_DELAY_SECONDS = 2.0
# Как часто редактировать progress-сообщение (см. задачу п.9).
_PROGRESS_EVERY = 10


class ProgressSenderLike(Protocol):
    """Ровно то, что нужно отсюда от Telethon (тот же приём Protocol, что и
    BotMessageSenderLike в reader/public_bot/delivery_service.py) — этот
    модуль ничего не знает о Telegram API напрямую. edit() должен сам
    проглатывать ошибки редактирования (сообщение удалено/не изменилось) —
    сбой progress-репортинга не должен прерывать сам скан."""

    async def send(self, chat_id: int, text: str) -> int: ...

    async def edit(self, chat_id: int, message_id: int, text: str) -> None: ...


@dataclass(frozen=True)
class FullCheckOutcome:
    total: int
    checked_ok: int
    with_debt: int
    without_debt: int
    failed: int
    duration_seconds: float
    failed_car_numbers: tuple[str, ...] = field(default_factory=tuple)


class FullCheckAlreadyInProgressError(Exception):
    """См. задачу п.11 "CONCURRENCY GUARD" — in-memory флаг (см.
    _in_progress), тот же осознанно простой приём, что и
    reader/public_bot/debt_refresh_service.py::RefreshAlreadyInProgressError:
    один процесс/один event loop, второй confirm физически может прийти
    только вторым сообщением в том же процессе."""


class FullCheckService:
    def __init__(
        self,
        task_repository: FineMonitoringTaskRepository,
        check_service: FineCheckService,
        progress_sender: ProgressSenderLike,
        *,
        inter_car_delay_seconds: float = _INTER_CAR_DELAY_SECONDS,
        progress_every: int = _PROGRESS_EVERY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._task_repository = task_repository
        self._check_service = check_service
        self._progress_sender = progress_sender
        self._inter_car_delay_seconds = inter_car_delay_seconds
        self._progress_every = progress_every
        self._sleep = sleep
        self._in_progress = False

    def is_in_progress(self) -> bool:
        return self._in_progress

    @property
    def inter_car_delay_seconds(self) -> float:
        """Для превью-текста ("Интервал: N сек.", см. задачу п.8) — ОДИН
        источник истины, чтобы текст не мог разойтись с реальным интервалом
        run()."""
        return self._inter_car_delay_seconds

    def list_candidate_task_ids(self) -> list[int]:
        """Только чтение (см. задачу п.1/п.8: "повторно пересчитать
        candidate set непосредственно перед confirm") — вызывается И для
        превью ("fine check-all"), И как ПЕРВЫЙ шаг run() ("fine check-all
        confirm") — оба раза свежий, не кэшированный список."""
        return self._task_repository.list_all_task_ids()

    async def run(self, *, chat_id: int) -> FullCheckOutcome:
        """chat_id — куда слать/редактировать progress И откуда физически
        пришла команда confirm (см. reader/public_bot/conversation.py) —
        НИКОГДА не chat_id клиента: этот скан не привязан ни к какому
        конкретному владельцу машины."""
        if self._in_progress:
            raise FullCheckAlreadyInProgressError()

        self._in_progress = True
        started_at = time.monotonic()
        try:
            task_ids = self.list_candidate_task_ids()
            total = len(task_ids)

            checked_ok = 0
            with_debt = 0
            without_debt = 0
            failed = 0
            failed_car_numbers: list[str] = []

            message_id = await self._progress_sender.send(
                chat_id, _format_progress(done=0, total=total, ok=0, failed=0),
            )

            for index, task_id in enumerate(task_ids):
                if index > 0:
                    # См. задачу п.2 — пауза МЕЖДУ РАЗНЫМИ машинами, не
                    # после последней (тот же приём, что и
                    # DebtRefreshService.refresh()).
                    await self._sleep(self._inter_car_delay_seconds)

                task = self._task_repository.get(task_id)
                if task is None:
                    # Задача удалена конкурентно между чтением списка и
                    # проверкой — просто пропускаем, не считаем ни успехом,
                    # ни ошибкой (её больше нет вообще), не входит ни в один
                    # счётчик итогового summary.
                    continue

                # notification_policy="silent" — ЕДИНСТВЕННОЕ отличие от
                # обычного вызова (см. задачу п.4/п.5 и reader/fines/
                # check_service.py::NotificationPolicy) — false-zero guard
                # внутри check_task() работает совершенно одинаково.
                result = await self._check_service.check_task(task, notification_policy="silent")
                if result.status == "error":
                    failed += 1
                    failed_car_numbers.append(task.car_number)
                else:
                    checked_ok += 1
                    updated = self._task_repository.get(task_id)
                    amount = (updated.last_successful_total_amount or 0.0) if updated else 0.0
                    if amount > 0:
                        with_debt += 1
                    else:
                        without_debt += 1

                done = index + 1
                if done % self._progress_every == 0 or done == total:
                    await self._progress_sender.edit(
                        chat_id, message_id,
                        _format_progress(done=done, total=total, ok=checked_ok, failed=failed),
                    )

            return FullCheckOutcome(
                total=total,
                checked_ok=checked_ok,
                with_debt=with_debt,
                without_debt=without_debt,
                failed=failed,
                duration_seconds=time.monotonic() - started_at,
                failed_car_numbers=tuple(failed_car_numbers),
            )
        finally:
            self._in_progress = False


def _format_progress(*, done: int, total: int, ok: int, failed: int) -> str:
    """Приватная, минимальная progress-строка (см. задачу п.9) — НЕ в
    reader/public_bot/texts.py: в отличие от DebtRefreshService (одна
    финальная строка, собираемая ConversationController ПОСЛЕ await
    refresh()), здесь редактирование происходит МНОГОКРАТНО ВНУТРИ самого
    долгого run() — заводить обратную зависимость texts.py -> этот модуль
    (при уже существующей texts.py -> full_check_service.py для типа
    FullCheckOutcome, см. ниже) означало бы цикл импортов."""
    return (
        "🔄 Полная проверка\n\n"
        f"Проверено: {done} / {total}\n"
        f"✅ Успешно: {ok}\n"
        f"⚠️ Ошибка: {failed}"
    )
