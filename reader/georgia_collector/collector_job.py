"""REDNET-side public-feed poller — the collector half of "Georgia remote
collector architecture (REDNET)". The traversal of police.ge's public list
(FAST/DEEP modes, the DEEP horizon, recovery over several cycles after a
long outage) is reader/fines/public_feed_traversal.py, shared with the
in-process job (reader/jobs/georgia_public_feed_job.py) so the two can no
longer drift apart. What is specific here: (1) the monitored-plate set
comes from production's API each cycle, not a local repository; (2) a
matched row is POSTed to production instead of persisted locally.

A matched row's payload goes to the local outbox BEFORE it is submitted and
leaves it after production accepted it, so a cycle killed mid-submit loses
nothing: the next cycle flushes the outbox first (production dedups by
fingerprint). A POST failure (after the production client's own bounded
retries) or an unexpected answer leaves the payload queued and the
traversal goes on.

After every feed poll the collector reports the outcome to production's
/internal/georgia/health (TraversalOutcome.to_payload: status, mode, counts,
dates, the last success — plus the cycle time; no plates or protocol
numbers), which alerts the managers' group (reader/public_bot/
collector_health.py). A cycle skipped because the police.ge circuit breaker
is open is reported too, with the last success. A report that fails never
affects the cycle.

should_run()/run_times/_last_run_slot is the SAME exact-wall-clock pattern
as reader/jobs/fine_job.py::FineJob.should_run()/reader/jobs/
georgia_public_feed_job.py::GeorgiaPublicFineFeedJob.should_run() — an
independent reimplementation (this module cannot import reader/jobs/, see
the "no Telegram/production-DB imports" requirement below), but identical
semantics: exact wall-clock match, no catch-up, no persistence across a
collector restart.

This module (and the production_client.py/outbox_repository.py/__main__.py
siblings it belongs to) imports NOTHING Telegram-related and NOTHING from
reader/public_bot, reader/jobs, or reader/users — only the specific,
DB-agnostic reader/fines/* pieces imported below, plus its own sibling
modules. See tests/test_georgia_collector_job.py for the import-graph
assertion enforcing this."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from reader.fines.collector_payload import record_to_payload
from reader.fines.parser import (
    FineParseError,
    parse_published_protocol_entry,
    parse_search_response,
)
from reader.fines.police_ge_session import PoliceGeSession
from reader.fines.provider import FineProviderError
from reader.fines.public_feed_seen_repository import GeorgiaPublicFeedSeenRepository
from reader.fines.public_feed_state_repository import GeorgiaPublicFeedStateRepository
from reader.fines.public_feed_traversal import PublicFeedTraversal, TraversalOutcome
from reader.fines.validation import FineValidationError, normalize_car_number
from reader.georgia_collector.outbox_repository import (
    KIND_BOOTSTRAP_RESULT,
    KIND_FEED_MATCH,
    CollectorOutboxRepository,
)
from reader.georgia_collector.production_client import (
    ProductionApiClient,
    ProductionApiError,
)

logger = logging.getLogger(__name__)

# Настройки лимита страниц, которые называют сообщения об упоре в лимит —
# переменные окружения (см. __main__.load_page_limits).
_FIRST_RUN_PAGES_SETTING = "GEORGIA_COLLECTOR_FIRST_RUN_MAX_PAGES"
_PAGES_PER_CYCLE_SETTING = "GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE"

# Outbox durability review ("Georgia remote collector architecture"
# integration, PR #34): once an entry has failed this many cross-cycle
# attempts (~2.5 days at the 4x/day schedule), escalate its log level from
# INFO to WARNING on every subsequent cycle so a permanently-stuck entry
# (e.g. a production-side bug that 400s every retry) becomes visible to an
# operator instead of retrying silently forever. Deliberately does NOT
# delete/give up on the entry at any attempt count — requirement G is
# explicit that the collector "does not lose already fetched matched
# fines," and an unbounded-but-slow cross-cycle retry (never a tight loop —
# bounded to once per scheduled 4x/day cycle, with its own 3-attempt
# in-call backoff per attempt, see production_client.py) is the correct way
# to honor that during an outage of unknown duration.
_STUCK_OUTBOX_ATTEMPTS_WARNING_THRESHOLD = 10


@dataclass
class CollectorJobStatus:
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    error_count: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None


class GeorgiaCollectorJob:
    def __init__(
        self,
        session: PoliceGeSession,
        production_client: ProductionApiClient,
        feed_state_repository: GeorgiaPublicFeedStateRepository,
        seen_repository: GeorgiaPublicFeedSeenRepository,
        outbox_repository: CollectorOutboxRepository,
        *,
        run_times: list[dt_time],
        tz: ZoneInfo,
        max_pages_per_cycle: int = 30,
        first_run_max_pages: int = 100,
        retention_days: int = 7,
    ):
        self._session = session
        self._production = production_client
        self._feed_state = feed_state_repository
        self._seen = seen_repository
        self._outbox = outbox_repository
        self._run_times = run_times
        self._tz = tz
        self._traversal = PublicFeedTraversal(
            session, feed_state_repository, seen_repository,
            tz=tz, max_pages_per_cycle=max_pages_per_cycle, first_run_max_pages=first_run_max_pages,
            retention_days=retention_days, pages_per_cycle_setting=_PAGES_PER_CYCLE_SETTING,
            first_run_pages_setting=_FIRST_RUN_PAGES_SETTING, log=logger, log_name="GeorgiaCollectorJob",
            mark_after_processing=True,
        )
        self._last_run_slot: tuple[date, dt_time] | None = None
        self.status = CollectorJobStatus()

    async def should_run(self, now: datetime) -> bool:
        local_now = now.astimezone(self._tz)
        matched_time = next(
            (
                t
                for t in self._run_times
                if (t.hour, t.minute) == (local_now.hour, local_now.minute)
            ),
            None,
        )
        if matched_time is None:
            return False

        slot = (local_now.date(), matched_time)
        if slot == self._last_run_slot:
            return False

        self._last_run_slot = slot
        return True

    async def run(self, now: datetime | None = None) -> None:
        run_at = now or datetime.now(timezone.utc)
        self.status.last_run_at = run_at

        # Сначала outbox (см. модульный докстрок "G. Failure behavior") —
        # не блокирует остальной цикл, если production всё ещё недоступен.
        await self._flush_outbox()

        if not self._session.is_available():
            logger.info("GeorgiaCollectorJob: circuit breaker open — skipping this cycle")
            last_success = self._feed_state.get().last_successful_poll_at
            await self._report_health({
                "status": "error", "detail": "police.ge circuit breaker open — cycle skipped",
                "mode": "skipped", "pages": 0,
                "last_success_at": last_success.isoformat() if last_success else None,
            }, run_at)
            return

        try:
            monitored = set(await self._production.get_active_plates())
        except ProductionApiError:
            logger.warning("GeorgiaCollectorJob: production API unavailable (get_active_plates)")
            return

        outcome = await self._traversal.run(run_at, lambda entry: self._maybe_submit_entry(entry, monitored))
        self._note_outcome(outcome, run_at)
        await self._report_health(outcome.to_payload(), run_at)
        await self._run_bootstrap(run_at)

    def _note_outcome(self, outcome: TraversalOutcome, run_at: datetime) -> None:
        if outcome.completed:
            self.status.last_success_at = run_at
            return
        self.status.error_count += 1
        self.status.last_error = outcome.detail
        self.status.last_error_at = run_at

    async def _report_health(self, payload: dict, run_at: datetime) -> None:
        """Итог цикла — production (см. reader/public_bot/collector_health.py),
        чтобы о простое или отставании узнал человек. cycle_at — время цикла:
        отчёт, повторённый клиентом после таймаута, production узнаёт и не
        считает вторым. На цикл не влияет никак: сбой отправки только пишется
        в журнал."""
        try:
            await self._production.report_health({**payload, "cycle_at": run_at.isoformat()})
        except Exception as exc:  # noqa: BLE001 — сбой отчёта не должен ломать цикл
            logger.warning("GeorgiaCollectorJob: health report not delivered to production (%s)", exc)

    async def _maybe_submit_entry(self, entry: dict, monitored: set[str]) -> None:
        """См. "DATA MINIMIZATION" в reader/jobs/georgia_public_feed_job.py —
        несовпавшая строка не парсится и никуда не отправляется вовсе, ТОЛЬКО
        мгновенное сравнение нормализованного protocolAuto с множеством
        plates, полученным от production в начале цикла."""
        raw_plate = entry.get("protocolAuto")
        if not raw_plate:
            return
        try:
            normalized_plate = normalize_car_number(raw_plate)
        except FineValidationError:
            return
        if normalized_plate not in monitored:
            return

        try:
            record = parse_published_protocol_entry(entry)
        except FineParseError:
            logger.exception("GeorgiaCollectorJob: не удалось разобрать совпавшую запись feed'а")
            return

        payload = record_to_payload(record)
        # В outbox ДО отправки и до отметки строки увиденной (обход отмечает
        # её после этого вызова, mark_after_processing): если цикл убьют
        # посреди отправки (до трёх попыток по 180 с, когда production
        # медленная) или она упадёт непредвиденной ошибкой, совпадение не
        # потеряется — следующий цикл сначала разбирает outbox, а
        # неотмеченную строку прочитает снова (повтор production отсеет по
        # fingerprint).
        outbox_id = self._outbox.enqueue(KIND_FEED_MATCH, payload)
        try:
            await self._production.submit_feed_match(payload)
        except ProductionApiError:
            logger.warning("GeorgiaCollectorJob: production недоступна, feed-match остаётся в outbox")
            return
        except Exception:
            # Непредвиденный ответ (например, 2xx не в JSON): совпадение уже
            # в outbox, обход продолжается.
            logger.exception("GeorgiaCollectorJob: непредвиденная ошибка отправки, feed-match остаётся в outbox")
            return
        self._outbox.remove(outbox_id)

    async def _run_bootstrap(self, run_at: datetime) -> None:
        """Один one-time historical searchByAuto на КАЖДУЮ pending-плиту,
        ТОТ ЖЕ API, что и старый per-car FineJob bootstrap до появления
        public-feed архитектуры (см. reader/fines/police_ge_session.py::
        search_by_plate + reader/fines/parser.py::parse_search_response)."""
        if not self._session.is_available():
            return
        try:
            pending_plates = await self._production.get_pending_bootstrap_plates()
        except ProductionApiError:
            logger.warning("GeorgiaCollectorJob: production недоступна, bootstrap пропущен в этом цикле")
            return

        for plate in pending_plates:
            if not self._session.is_available():
                break
            try:
                raw = await self._session.search_by_plate(plate)
                records = parse_search_response(raw, car_number=plate)
            except FineProviderError:
                logger.warning("GeorgiaCollectorJob: bootstrap lookup для %s не удался (police.ge)", plate)
                continue
            except FineParseError:
                logger.exception("GeorgiaCollectorJob: bootstrap-ответ для %s не распарсился", plate)
                continue

            payload = {"car_number": plate, "fines": [record_to_payload(r) for r in records]}
            try:
                await self._production.submit_bootstrap_result(payload)
            except ProductionApiError:
                logger.warning(
                    "GeorgiaCollectorJob: production недоступна, bootstrap-result для %s уходит в outbox",
                    plate,
                )
                self._outbox.enqueue(KIND_BOOTSTRAP_RESULT, payload)

    async def _flush_outbox(self) -> None:
        for entry in self._outbox.list_all():
            try:
                if entry.kind == KIND_FEED_MATCH:
                    await self._production.submit_feed_match(entry.payload)
                elif entry.kind == KIND_BOOTSTRAP_RESULT:
                    await self._production.submit_bootstrap_result(entry.payload)
                else:
                    logger.error("GeorgiaCollectorJob: неизвестный outbox kind %r, удаляю", entry.kind)
                    self._outbox.remove(entry.id)
                    continue
                self._outbox.remove(entry.id)
            except Exception as exc:
                # ProductionApiError (production недоступна) или непредвиденный
                # ответ — запись остаётся, попытка засчитывается, остальные
                # записи и сам цикл (обход feed'а, bootstrap) идут дальше.
                if not isinstance(exc, ProductionApiError):
                    logger.warning(
                        "GeorgiaCollectorJob: outbox entry %d (kind=%s) — непредвиденная ошибка %s",
                        entry.id, entry.kind, type(exc).__name__,
                    )
                self._outbox.record_attempt(entry.id)
                attempts_so_far = entry.attempts + 1
                if attempts_so_far >= _STUCK_OUTBOX_ATTEMPTS_WARNING_THRESHOLD:
                    logger.warning(
                        "GeorgiaCollectorJob: outbox entry %d (kind=%s) stuck after %d "
                        "attempts — still retrying, not dropped (see module-level "
                        "_STUCK_OUTBOX_ATTEMPTS_WARNING_THRESHOLD docstring)",
                        entry.id, entry.kind, attempts_so_far,
                    )
                else:
                    logger.info(
                        "GeorgiaCollectorJob: outbox entry %d всё ещё не доставлен (попытка %d)",
                        entry.id, attempts_so_far,
                    )
