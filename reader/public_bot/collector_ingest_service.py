"""Pure ingest logic for the Georgia remote collector API (see
reader/public_bot/collector_api.py for the HTTP/HMAC transport layer, and
"Georgia remote collector architecture (REDNET)") — composes the SAME
canonical persistence path the public feed job (reader/jobs/
georgia_public_feed_job.py) and the manual-bootstrap fallback (reader/
public_bot/manual_bootstrap_service.py) already use: FineCheckService.
persist_feed_match() per active task of the physical plate (fan-out),
GeorgiaPlateBootstrapRepository.mark_completed() only after persistence
succeeds (never before — if persistence raises, this function never reaches
the mark_completed() call, so bootstrap correctly stays 'pending' and the
collector's retry is safe). No new fines model, no raw SQL.

Deliberately reuses ManualBootstrapService.tasks_for_plate()/
confirm_no_fines() rather than reimplementing fan-out/idempotent-completion
a second time — a collector-driven completion is a THIRD caller of the same
"not a manager" completed_by_user_id=None path GeorgiaPlateBootstrapRepository.
mark_completed() already supports for a genuine live searchByAuto (see its
docstring) — manual_bootstrap_service.py's confirm_no_fines() type hint was
widened from `int` to `int | None` for exactly this new caller.

ingest_bootstrap_result's `mode` param (см. задачу "fix the remaining
issues discovered during live testing", production 515-plate audit):
"bootstrap" (default, same behavior as before this parameter existed — every
existing caller, including the currently-deployed collector's own
_run_bootstrap(), never sends this field at all) always calls
confirm_no_fines() after persisting, same as always. "audit" is for a
background/one-off recheck of plates that are NOT genuinely going through
first-time bootstrap right now (e.g. a full-production catch-up sweep) —
fines still persist/dedupe/notify exactly the same either way, but
confirm_no_fines() is NEVER called, so bootstrap state is NEVER touched
(not created, not completed, not reset) regardless of whether records is
empty or not, and therefore customer_notify_messages is always empty (there
is no "completed" transition to trigger it from)."""

from __future__ import annotations

from dataclasses import dataclass

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.plate_bootstrap_repository import GeorgiaPlateBootstrapRepository
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot import texts
from reader.public_bot.manual_bootstrap_service import ManualBootstrapService


@dataclass(frozen=True)
class FeedMatchResult:
    tasks_matched: int


#: Valid values for ingest_bootstrap_result's `mode` parameter — see its
#: docstring and the module docstring above.
BOOTSTRAP_MODE = "bootstrap"
AUDIT_MODE = "audit"
VALID_INGEST_MODES = frozenset({BOOTSTRAP_MODE, AUDIT_MODE})


@dataclass(frozen=True)
class BootstrapResultOutcome:
    tasks_affected: int
    fines_persisted: int
    # True ONLY when a real georgia_plate_bootstrap_state row existed AND
    # genuinely transitioned pending->completed as part of THIS call (см.
    # задачу "fix the remaining issues discovered during live testing" —
    # production-инцидент K333HY190: раньше здесь было True ДАЖЕ когда для
    # плиты вообще не было строки — ManualBootstrapService.confirm_no_fines()
    # возвращает True и для "строки нет вовсе" (mark_completed() молча
    # no-op'ает), не только для настоящего pending->completed. Эта проверка
    # добавляет условие "строка реально существовала" ЛОКАЛЬНО здесь, не
    # трогая confirm_no_fines()'s собственный контракт (которым пользуется
    # и manual-bootstrap Telegram-flow из PR #33 — там этот кейс никогда не
    # встречается на практике, менять его семантику ради этого не нужно и
    # рискованно). Всегда False для mode="audit" — там bootstrap вообще не
    # трогается, см. ingest_bootstrap_result().
    bootstrap_completed: bool
    # (chat_id, text) pairs the transport layer (collector_api.py) must send
    # directly via the bot's own Telethon client — see texts.
    # format_no_fines_found()/reader/public_bot/conversation.py's existing
    # customer_notify_messages mechanism (same canonical text/path, no
    # second format). Non-empty ONLY for a genuine zero-fines bootstrap
    # (never for mode="audit" — see ingest_bootstrap_result()).
    customer_notify_messages: list[tuple[int, str]]


class CollectorIngestService:
    def __init__(
        self,
        task_repository: FineMonitoringTaskRepository,
        detected_fine_repository: DetectedFineRepository,
        check_service: FineCheckService,
        plate_bootstrap_repository: GeorgiaPlateBootstrapRepository,
        manual_bootstrap_service: ManualBootstrapService,
    ):
        self._tasks = task_repository
        self._detected_fines = detected_fine_repository
        self._check_service = check_service
        self._bootstrap = plate_bootstrap_repository
        self._manual_bootstrap = manual_bootstrap_service

    def active_plates(self) -> list[str]:
        """Нормализованные car_number'ы всех активных задач — только ключи
        (data minimization, см. задачу п.D: "Do not expose unrelated data"),
        никаких per-task/per-customer деталей коллектору не нужно и не
        передаётся."""
        plates = {task.car_number for task in self._tasks.list_active()}
        return sorted(plates)

    def pending_bootstrap_plates(self) -> list[str]:
        return self._bootstrap.list_pending()

    async def ingest_feed_match(self, car_number: str, record: ParsedFineRecord) -> FeedMatchResult:
        """tasks пуст (плита больше не отслеживается — race: список plates
        мог устареть за время похода коллектора к police.ge) -> tasks_matched=0,
        НЕ ошибка — см. задачу п.D про "no_active_tasks"."""
        tasks = self._manual_bootstrap.tasks_for_plate(car_number)
        for task in tasks:
            await self._check_service.persist_feed_match(task, record)
        return FeedMatchResult(tasks_matched=len(tasks))

    async def ingest_bootstrap_result(
        self, car_number: str, records: list[ParsedFineRecord], *, mode: str = BOOTSTRAP_MODE,
    ) -> BootstrapResultOutcome:
        """mode="bootstrap" (default) — see module docstring: same behavior
        as always, confirm_no_fines() runs after persistence. mode="audit" —
        fines still persist/dedupe/notify exactly the same, but bootstrap
        state is NEVER touched (confirm_no_fines() is simply never called),
        regardless of whether records is empty or not."""
        tasks = self._manual_bootstrap.tasks_for_plate(car_number)
        if not tasks:
            return BootstrapResultOutcome(
                tasks_affected=0, fines_persisted=0, bootstrap_completed=False,
                customer_notify_messages=[],
            )

        # Персист ДО mark_completed (см. модульный докстрок) — исключение
        # здесь не даёт дойти до completion-вызова ниже, bootstrap остаётся
        # 'pending', следующая попытка коллектора безопасна (дедуп по
        # fingerprint уже делает повторный persist этих же records idempotent).
        for record in records:
            for task in tasks:
                await self._check_service.persist_feed_match(task, record)

        if mode == AUDIT_MODE:
            # НИКОГДА не вызываем confirm_no_fines() — bootstrap остаётся
            # ровно таким, каким был ДО этого вызова (pending остаётся
            # pending, отсутствующая строка остаётся отсутствующей,
            # completed остаётся completed) — см. модульный докстрок и
            # задачу. Соответственно bootstrap_completed=False (нечего
            # репортить как completed) и customer_notify_messages всегда
            # пуст (некому срабатывать без completed=True).
            return BootstrapResultOutcome(
                tasks_affected=len(tasks),
                fines_persisted=len(records),
                bootstrap_completed=False,
                customer_notify_messages=[],
            )

        had_row = self._bootstrap.get(car_number) is not None
        confirmed = self._manual_bootstrap.confirm_no_fines(car_number, completed_by_user_id=None)
        # См. BootstrapResultOutcome.bootstrap_completed докстрок —
        # confirm_no_fines() само по себе возвращает True и для "строки нет
        # вовсе" (mark_completed() no-op), это условие исправляет это ЛОКАЛЬНО
        # здесь, не трогая confirm_no_fines()'s собственный контракт.
        completed = had_row and confirmed

        customer_notify_messages: list[tuple[int, str]] = []
        if completed and not records:
            customer_text = texts.format_no_fines_found(car_number)
            seen_chat_ids: set[int] = set()
            for task in tasks:
                if task.telegram_chat_id not in seen_chat_ids:
                    seen_chat_ids.add(task.telegram_chat_id)
                    customer_notify_messages.append((task.telegram_chat_id, customer_text))

        return BootstrapResultOutcome(
            tasks_affected=len(tasks),
            fines_persisted=len(records),
            bootstrap_completed=completed,
            customer_notify_messages=customer_notify_messages,
        )
