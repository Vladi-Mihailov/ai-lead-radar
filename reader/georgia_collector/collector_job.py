"""REDNET-side public-feed poller — the collector half of "Georgia remote
collector architecture (REDNET)". Deliberately mirrors (does NOT import —
reader/jobs/georgia_public_feed_job.py::GeorgiaPublicFineFeedJob.run() is
intentionally left untouched and keeps running as today's in-process
fallback) the same DEEP/FAST frontier+one-overlap-page algorithm, retargeted
at two things that differ here: (1) the monitored-plate set comes from
production's API each cycle, not a local repository; (2) a matched row is
POSTed to production instead of persisted locally — a POST failure (after
the production client's own bounded retries) queues the payload in the
local outbox (outbox_repository.py) instead of aborting the cycle, so a
production-side outage never loses an already-fetched match.

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
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from reader.fines.collector_payload import record_to_payload
from reader.fines.parser import (
    FineParseError,
    extract_published_protocols_results,
    parse_published_protocol_entry,
    parse_search_response,
)
from reader.fines.police_ge_session import PoliceGeSession
from reader.fines.provider import FineProviderError
from reader.fines.public_feed_seen_repository import GeorgiaPublicFeedSeenRepository
from reader.fines.public_feed_state_repository import GeorgiaPublicFeedStateRepository
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

_PAGE_SIZE = 30


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
        self._max_pages_per_cycle = max_pages_per_cycle
        self._first_run_max_pages = first_run_max_pages
        self._retention_days = retention_days
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
            return

        try:
            monitored = set(await self._production.get_active_plates())
        except ProductionApiError:
            logger.warning("GeorgiaCollectorJob: production API unavailable (get_active_plates)")
            return

        await self._poll_feed(monitored, run_at)
        await self._run_bootstrap(run_at)

    async def _poll_feed(self, monitored: set[str], run_at: datetime) -> None:
        feed_state = self._feed_state.get()
        is_true_first_run = feed_state.last_successful_poll_at is None
        in_recovery = feed_state.last_error is not None
        deep_mode = is_true_first_run or in_recovery
        max_pages = self._first_run_max_pages if is_true_first_run else self._max_pages_per_cycle

        pages_fetched = 0
        newest_seen_this_cycle: date | None = None
        completed = False
        pending_overlap = False

        try:
            for page_index in range(max_pages):
                first_result = page_index * _PAGE_SIZE
                raw = await self._session.fetch_published_protocols(first_result=first_result)
                pages_fetched += 1

                try:
                    raw_results = extract_published_protocols_results(raw)
                except FineParseError as exc:
                    self._record_error(str(exc), at=run_at)
                    return

                if not raw_results:
                    completed = True
                    break

                page_has_seen_row = False
                for entry in raw_results:
                    entry_date = _parse_publish_date(entry.get("publishDate"))
                    if entry_date is not None and (
                        newest_seen_this_cycle is None or entry_date > newest_seen_this_cycle
                    ):
                        newest_seen_this_cycle = entry_date

                    protocol_no = entry.get("protocolNo")
                    if not protocol_no:
                        continue

                    if self._seen.is_seen(protocol_no):
                        page_has_seen_row = True
                        continue

                    self._seen.mark_seen(protocol_no, publish_date=entry_date or run_at.date())
                    await self._maybe_submit_entry(entry, monitored)

                if pending_overlap:
                    completed = True
                    break

                if not deep_mode and page_has_seen_row:
                    pending_overlap = True
                    continue
            else:
                completed = False
        except FineProviderError as exc:
            self._record_error(str(exc), at=run_at)
            return
        except Exception as exc:
            self._record_error(str(exc), at=run_at)
            logger.exception("GeorgiaCollectorJob: unexpected error during feed poll")
            return

        if not completed:
            self._record_error("safety cap reached before an empty page", at=run_at)
            return

        if newest_seen_this_cycle is not None:
            self._feed_state.record_successful_cycle(
                newest_seen_publish_date=newest_seen_this_cycle, polled_at=run_at,
            )
        else:
            self._feed_state.record_successful_cycle(
                newest_seen_publish_date=feed_state.newest_seen_publish_date or run_at.date(),
                polled_at=run_at,
            )
        self._seen.cleanup_older_than(run_at.date() - timedelta(days=self._retention_days))
        self.status.last_success_at = run_at
        logger.info(
            "GeorgiaCollectorJob feed poll finished: pages=%d, deep_mode=%s", pages_fetched, deep_mode,
        )

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
        try:
            await self._production.submit_feed_match(payload)
        except ProductionApiError:
            logger.warning(
                "GeorgiaCollectorJob: production недоступна, feed-match для %s уходит в outbox",
                normalized_plate,
            )
            self._outbox.enqueue(KIND_FEED_MATCH, payload)

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
            except ProductionApiError:
                self._outbox.record_attempt(entry.id)
                logger.info(
                    "GeorgiaCollectorJob: outbox entry %d всё ещё не доставлен (попытка %d)",
                    entry.id, entry.attempts + 1,
                )

    def _record_error(self, message: str, *, at: datetime) -> None:
        self.status.error_count += 1
        self.status.last_error = message
        self.status.last_error_at = at
        self._feed_state.record_error(message, at=at)


def _parse_publish_date(value) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
