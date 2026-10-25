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

AUDIT MODE (2026-10-25, "Extend REDNET ingestion for a background mass
audit"): `mode` distinguishes a genuine first-time bootstrap submission
(default, "bootstrap" — completely unchanged) from a background mass
RECHECK of plates that may already be bootstrapped ("audit"). Root cause
this fixes: `ingest_bootstrap_result()` used to call confirm_no_fines()
unconditionally. For a plate whose fine_monitoring_tasks already exist but
which has NO row at all in georgia_plate_bootstrap_state (confirmed
reproducible — e.g. K333HY190, a task created before the bootstrap system
existed, never got an ensure_pending() row), confirm_no_fines() treats a
missing row the SAME as a genuinely-pending one: GeorgiaPlateBootstrapRepository.
mark_completed() is an UPDATE ... WHERE car_number = ... that silently no-ops
when no row exists, yet confirm_no_fines() still returns True (its own guard
`state is not None and state.is_completed` is False when state is None, so
it falls through to the no-op UPDATE and returns True regardless) — so a
mass recheck returning zero fines for such a plate would send a real
"штрафов не найдено" message to a real customer, during what is meant to be
a SILENT audit, while never actually writing a bootstrap row at all. The
same unconditional call would also genuinely flip an already-pending (but
not-yet-bootstrapped) plate from pending -> completed, which a mere RECHECK
must not do either — only a genuine bootstrap submission may ever make that
transition.

Audit mode's fix is narrow and additive, not a reimplementation: persistence
(persist_feed_match() fan-out, dedup/mark_seen/create exactly as before) is
IDENTICAL in both modes — only the completion/notification tail differs.
Audit mode:
  - NEVER calls confirm_no_fines() — bootstrap_status/bootstrap_completed_at/
    bootstrap_completed_by_user_id (completed, pending, OR missing entirely)
    stay byte-for-byte untouched, whatever they already were.
  - NEVER sends the "штрафов не найдено"/any zero-fine customer message —
    customer_notify_messages is always [] in this mode.
  - Still persists every returned record through the exact same canonical
    FineCheckService path (existing fines dedupe/mark_seen, genuinely new
    fines are created normally with notification_sent_at=NULL, so the
    existing NotificationFlushJob still delivers them through the normal
    new-fine pipeline — audit mode changes nothing about new-fine delivery,
    only suppresses the SEPARATE zero-fine announcement and the bootstrap
    state transition).
`bootstrap_completed` in the returned outcome, for audit mode, reports the
plate's CURRENT actual status (a read-only check, never a side effect of
this call) — honest information for logging/the mass-audit script, never
implying a state change that didn't happen.

The SAME missing-row gap also exists on the mode="bootstrap" path itself
(confirm_no_fines()'s own return value doesn't distinguish "row existed and
genuinely transitioned" from "no row at all, UPDATE no-opped") — fixed there
too, locally, by checking whether a bootstrap row existed immediately before
calling confirm_no_fines() (see ingest_bootstrap_result()'s `had_bootstrap_row`
check). confirm_no_fines()'s own contract/return value is intentionally left
unchanged — it has its own existing idempotent-completed caller (the manual
Telegram bootstrap flow, reader/public_bot/conversation.py) that must not be
affected by this.

Do not silently infer audit mode anywhere: `mode` must be passed explicitly;
omitting it keeps today's bootstrap behavior exactly as it was before this
feature existed. See collector_api.py's post_bootstrap_result for the one
caller and its own payload-level `mode` validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.plate_bootstrap_repository import GeorgiaPlateBootstrapRepository
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot import texts
from reader.public_bot.manual_bootstrap_service import ManualBootstrapService

IngestMode = Literal["bootstrap", "audit"]
_VALID_MODES: frozenset[str] = frozenset({"bootstrap", "audit"})


@dataclass(frozen=True)
class FeedMatchResult:
    tasks_matched: int


@dataclass(frozen=True)
class BootstrapResultOutcome:
    tasks_affected: int
    fines_persisted: int
    bootstrap_completed: bool
    # (chat_id, text) pairs the transport layer (collector_api.py) must send
    # directly via the bot's own Telethon client — see texts.
    # format_no_fines_found()/reader/public_bot/conversation.py's existing
    # customer_notify_messages mechanism (same canonical text/path, no
    # second format). Non-empty ONLY for a genuine zero-fines bootstrap
    # (mode="bootstrap") — ALWAYS empty for mode="audit" (see module
    # docstring, "AUDIT MODE").
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
        self, car_number: str, records: list[ParsedFineRecord], *, mode: IngestMode = "bootstrap",
    ) -> BootstrapResultOutcome:
        """mode="bootstrap" (default): completely unchanged genuine first-time
        bootstrap-completion behavior. mode="audit": a background mass
        recheck — see module docstring, "AUDIT MODE", for why this exists
        and exactly what it does and does not do. Do not silently infer
        audit mode from any other signal (e.g. plate already completed) —
        the caller must pass it explicitly."""
        if mode not in _VALID_MODES:
            raise ValueError(f"invalid ingest mode: {mode!r}")

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
        # Identical in both modes — audit mode changes ONLY the completion/
        # notification tail below, never persistence itself.
        for record in records:
            for task in tasks:
                await self._check_service.persist_feed_match(task, record)

        if mode == "audit":
            # Never confirm_no_fines() here — a mass recheck must never
            # create/advance bootstrap state (missing stays missing, pending
            # stays pending, completed stays completed — see module
            # docstring's root-cause explanation). bootstrap_completed below
            # reports the plate's CURRENT status, a read-only check, never a
            # side effect of this call.
            return BootstrapResultOutcome(
                tasks_affected=len(tasks),
                fines_persisted=len(records),
                bootstrap_completed=self._bootstrap.is_completed(car_number),
                # Never the zero-fine announcement in audit mode, regardless
                # of whether records is empty — see module docstring.
                customer_notify_messages=[],
            )

        # Defense in depth, mode="bootstrap" path too (not just audit mode
        # above): confirm_no_fines() itself returns True for BOTH a genuine
        # pending->completed transition AND a car_number with no bootstrap
        # row at all (mark_completed()'s UPDATE silently no-ops when no row
        # matches, yet confirm_no_fines() still reports success — see module
        # docstring's root-cause explanation). Checking "did a row exist
        # before this call" locally, here, closes the SAME K333HY190 gap for
        # a genuine bootstrap submission too, without changing
        # confirm_no_fines()'s own return contract (the manual Telegram
        # bootstrap flow, reader/public_bot/conversation.py, also calls it
        # and must keep its existing idempotent-completed semantics
        # untouched — this check is local to this one call site).
        had_bootstrap_row = self._bootstrap.get(car_number) is not None
        confirmed = self._manual_bootstrap.confirm_no_fines(car_number, completed_by_user_id=None)
        completed = had_bootstrap_row and confirmed

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
