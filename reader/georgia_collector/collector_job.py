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

DEEP mode (true first run, or recovery after a failed or interrupted cycle)
completes at an empty page OR at a page whose rows are all already processed
(see _page_already_processed): on the true first run, rows published before
today; in recovery, rows published before the newest publish date of the
last successful cycle (capped at that cycle's own day) OR first seen by that
cycle or earlier (first_seen_at <= its run_at; rows marked by failed or
interrupted attempts after it don't count, so a gap below them is still
read). The real feed has ~10k pages (~5–6.3k publications a day), so an
empty page is unreachable — without this horizon DEEP never completed and
re-read max_pages pages every cycle. Each cycle first records "cycle in
progress" as last_error, so a cycle killed midway (SIGTERM, OOM,
CancelledError) is followed by DEEP recovery, not FAST. FAST mode stops
only on a QUIET page (no unseen row, at least one row seen before this
cycle that this cycle had not read yet) followed by FAST_LOOKAHEAD_PAGES
more quiet pages; a page with an unseen row resets the count, so an old
row re-shown above new rows (a re-published receipt — production,
2026-11-19) no longer ends the traversal early. Rows this cycle already
marked, and rows seen before it that a burst re-shows on a later page,
reappear when new publications shift the pages during the traversal
(production: bursts of 70–360 rows); they neither make a page quiet nor
reset the count — stopping on them would leave new rows below unread for
good. Per-cycle counters (seen_among_new, quiet_pages, lookahead_new) and
the page-0 data.count with its delta since the previous cycle go to the
"feed poll finished" line, without protocol numbers.

A matched row's payload goes to the local outbox BEFORE it is submitted and
leaves it after production accepted it, so a cycle killed mid-submit loses
nothing: the next cycle flushes the outbox first (production dedups by
fingerprint).

should_run()/run_times/_last_run_slot is the SAME exact-wall-clock pattern
as reader/jobs/fine_job.py::FineJob.should_run()/reader/jobs/
georgia_public_feed_job.py::GeorgiaPublicFineFeedJob.should_run() — an
independent reimplementation (this module cannot import reader/jobs/, see
the "no Telegram/production-DB imports" requirement below), but identical
semantics: exact wall-clock match, no catch-up, no persistence across a
collector restart.

KNOWN TECHNICAL DEBT (see architecture review, task "PR #34 integration
sequence"): _poll_feed() below duplicates GeorgiaPublicFineFeedJob.run()'s
DEEP/FAST frontier+look-ahead state machine almost line-for-line
(same deep_mode/is_true_first_run/in_recovery computation, same quiet-page/
look-ahead tracking, same error/retry/retention handling).
This was reviewed deliberately for extraction into a shared module and
rejected: GeorgiaPublicFineFeedJob is already-merged, production-tested
code living in reader/jobs/, which this package is architecturally
forbidden from importing (see the module-level "no Telegram/production-DB
imports" invariant, enforced by test) — any shared module would have to
live elsewhere and be imported by BOTH sites, meaning the extraction would
itself modify the already-stable in-process job's internals for zero
functional gain. The two also genuinely differ in what happens to a newly-
matched row (synchronous local persist vs. async remote submit with
outbox-fallback-on-failure), which would force an extra parameterization
axis into any shared abstraction, trading real duplication for added
indirection. Decision: leave both implementations independent.
**Collector and in-process feed job must be kept behaviorally
synchronized** — any future change to the pagination/frontier/overlap/
DEEP-recovery semantics in one MUST be mirrored in the other by hand (see
the identical note in georgia_public_feed_job.py's module docstring).

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
    extract_reported_count,
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

FAST_LOOKAHEAD_PAGES = 2
"""K: how many more quiet pages FAST reads after the first quiet page (a
page without unseen rows that shows at least one row seen before this
cycle and not yet read by it) before it stops; a page with unseen rows
among them resets the count. Overridden by GEORGIA_COLLECTOR_FAST_LOOKAHEAD_PAGES (see
__main__.load_fast_lookahead_pages); mirrored in
reader/jobs/georgia_public_feed_job.py.

Data behind K = 2 (production, 2026-11-19):
- the only clean FAST observation (17:00 UTC: 40 pages read, 1197 new
  rows, so 1–3 old ones) had its out-of-place old row directly above the
  boundary: the old "second page with a seen row" stop ended one page
  above the boundary and missed nothing only by luck;
- new rows numbered below numbers already seen (1–7 rows in 10 of 32
  cycles, 63 once): the shallow number-space overlaps are <= 5 rows;
- a page-0 probe showed the feed ordered by publication, not by protocol
  number (the series interleave), with today's rows as one contiguous
  block on top; the anomaly seen is an old row re-shown above new rows.
  Deep insertion of new rows below the boundary was not observed; if the
  data.count delta ever exceeds the new rows read, revisit (a DEEP trigger
  or a daily sweep, ~190–210 pages a day).
K = 2 covers ~60 rows beyond the first quiet page for at most +2 page
requests per ordinary FAST cycle (~+8 a day at four cycles), well inside
the 300-page cap."""

# Отметка "цикл идёт" в georgia_public_feed_state.last_error (см. отметку
# перед циклом страниц): если она осталась, цикл был прерван.
_CYCLE_IN_PROGRESS = (
    "cycle in progress (if this stays, the cycle was interrupted "
    "and the next one runs deep recovery)"
)

# Настройки лимита страниц, которые называет _safety_cap_message() —
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


@dataclass
class _FeedCycleCounters:
    """Per-cycle feed diagnostics for the log, never protocol numbers or
    plates (mirrored in reader/jobs/georgia_public_feed_job.py):
    - new_rows: rows this cycle saw for the first time;
    - seen_among_new: rows seen before this cycle standing above a new row
      on the same page (an old row re-shown among new ones). The old rows
      under the last new row of the boundary page are not counted — every
      cycle has them;
    - quiet_pages: pages with no new row and at least one row seen before
      this cycle that it had not read yet;
    - lookahead_new: FAST only — new rows on pages after a quiet page,
      i.e. rows inserted below a boundary that the look-ahead found;
    - reported_count: data.count of page 0 (the feed total), and
      reported_count_delta: its change since the previous cycle's page 0 in
      this process (n/a after a restart). A delta above new_rows hints at
      rows added out of reach; a single cycle can also differ by rows a
      burst pushed into the previous cycle's pages."""

    new_rows: int = 0
    seen_among_new: int = 0
    quiet_pages: int = 0
    lookahead_new: int = 0
    reported_count: int | None = None
    reported_count_delta: int | None = None

    def describe(self) -> str:
        reported = "n/a" if self.reported_count is None else str(self.reported_count)
        delta = "n/a" if self.reported_count_delta is None else f"{self.reported_count_delta:+d}"
        return (
            f"new_rows={self.new_rows}, seen_among_new={self.seen_among_new}, "
            f"quiet_pages={self.quiet_pages}, lookahead_new={self.lookahead_new}, "
            f"reported_count={reported}, reported_count_delta={delta}"
        )


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
        fast_lookahead_pages: int = FAST_LOOKAHEAD_PAGES,
    ):
        if fast_lookahead_pages < 1:
            raise ValueError("fast_lookahead_pages must be >= 1")
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
        self._fast_lookahead_pages = fast_lookahead_pages
        self._last_run_slot: tuple[date, dt_time] | None = None
        # data.count первой страницы прошлого цикла этого процесса — только
        # для журнала (см. _poll_feed), в БД не хранится.
        self._previous_reported_count: int | None = None
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
        # DEEP-горизонт (см. _page_already_processed): DEEP завершается на
        # странице, ВСЕ строки которой уже обработаны, — ниже по feed'у
        # (newest-first) лежат только такие. До пустой страницы не дойти:
        # в feed'е ~10 тыс. страниц (~5–6,3 тыс. публикаций в сутки), без
        # горизонта DEEP не завершался никогда и каждый цикл перечитывал
        # max_pages страниц заново.
        # - Истинный первый запуск — только сегодняшние публикации: более
        #   ранние штрафы отслеживаемых номеров находит bootstrap-поиск при
        #   добавлении номера.
        # - Восстановление — строки, опубликованные раньше даты самой новой
        #   строки последнего успешного цикла (но не позже дня самого цикла:
        #   строка, датированная будущим, горизонт не сдвигает), ЛИБО впервые
        #   увиденные этим циклом или раньше (first_seen_at <= его run_at —
        #   всё, что он отметил, обработано). Отмеченные позже (в т.ч.
        #   сбойной или прерванной попыткой над недочитанной "дырой") не
        #   считаются — дыра будет дочитана. Весь последний день заново не
        #   перечитывается, иначе восстановление после двух сбоев подряд не
        #   помещалось бы в лимит страниц.
        if is_true_first_run:
            processed_before_date = run_at.astimezone(self._tz).date()
            processed_before_time = None
        else:
            last_success = _as_utc(feed_state.last_successful_poll_at)
            success_day = last_success.astimezone(self._tz).date()
            newest = feed_state.newest_seen_publish_date
            processed_before_date = success_day if newest is None else min(newest, success_day)
            processed_before_time = last_success
            if deep_mode and last_success.replace(microsecond=0) >= _as_utc(run_at).replace(microsecond=0):
                # Успех записан с часами "впереди": по first_seen_at строки
                # этого и прошлых сбойных циклов сошли бы за обработанные.
                logger.warning(
                    "GeorgiaCollectorJob: last successful cycle %s is not earlier than this cycle %s "
                    "(clock skew?) — recovery ignores first-seen times this cycle",
                    last_success.isoformat(), run_at.isoformat(),
                )
                processed_before_time = None

        pages_fetched = 0
        newest_seen_this_cycle: date | None = None
        completed = False
        # Отмеченные этим циклом: пока идёт обход, наверху feed'а появляются
        # новые публикации, страницы сдвигаются вниз, и следующая страница
        # снова показывает уже прочитанные строки (в production — пачки по
        # 70–360 строк в циклы 15:00/21:00). Это не граница FAST — граница
        # только строка, увиденная до этого цикла, иначе FAST остановился бы
        # на повторе и не дочитал бы новые строки под ним.
        marked_this_cycle: set[str] = set()
        # Строки, увиденные до этого цикла, которые он уже прочитал: такая
        # строка, снова показанная ниже после сдвига пачкой, страницу "тихой"
        # не делает — иначе пачка во время look-ahead израсходовала бы его на
        # уже прочитанные строки, не заглянув под тихую страницу.
        seen_before_read: set[str] = set()
        # FAST-граница (см. FAST_LOOKAHEAD_PAGES): тихих страниц подряд с
        # последней страницы с новыми строками; обход завершается, когда их
        # 1 + K. Все страницы, включая look-ahead, обрабатываются целиком (их
        # новые строки — НЕ отбрасываются). Отдельная старая строка над
        # новыми (переопубликованная квитанция) обход больше не завершает.
        quiet_run = 0
        counters = _FeedCycleCounters()

        # Отметка "цикл идёт": если процесс остановят посреди цикла (SIGTERM,
        # OOM, CancelledError — их не ловит ни один except ниже), last_error
        # останется, и следующий цикл пойдёт DEEP-восстановлением, а не FAST
        # (FAST остановился бы на строках прерванного цикла и не вернулся бы
        # к недочитанным под ними). Успех её снимает, ошибка перезаписывает.
        self._feed_state.record_error(_CYCLE_IN_PROGRESS, at=run_at)

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

                if page_index == 0:
                    # Только для журнала: data.count — всего строк в списке
                    # (проба 19.11: 298 570), разница с прошлым циклом —
                    # сколько добавилось с его первой страницы.
                    counters.reported_count = extract_reported_count(raw)
                    if counters.reported_count is not None and self._previous_reported_count is not None:
                        counters.reported_count_delta = counters.reported_count - self._previous_reported_count
                    self._previous_reported_count = counters.reported_count

                if not raw_results:
                    completed = True
                    break

                page_new = 0
                page_quiet_rows = 0
                seen_above_new = 0
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
                        if protocol_no not in marked_this_cycle and protocol_no not in seen_before_read:
                            seen_before_read.add(protocol_no)
                            page_quiet_rows += 1
                            seen_above_new += 1
                        continue

                    self._seen.mark_seen(protocol_no, publish_date=entry_date or run_at.date(), seen_at=run_at)
                    marked_this_cycle.add(protocol_no)
                    page_new += 1
                    # Старые строки над новой на этой странице (не хвост
                    # страницы-границы под последней новой строкой).
                    counters.seen_among_new += seen_above_new
                    seen_above_new = 0
                    await self._maybe_submit_entry(entry, monitored)

                if page_new:
                    if not deep_mode and counters.quiet_pages:
                        # Новые строки ниже тихой страницы — вставка под
                        # границей: без look-ahead их бы не прочитали.
                        counters.lookahead_new += page_new
                    quiet_run = 0
                elif page_quiet_rows:
                    counters.quiet_pages += 1
                    quiet_run += 1

                if deep_mode and _page_already_processed(
                    raw_results, self._seen,
                    published_before=processed_before_date, seen_at_or_before=processed_before_time,
                ):
                    # DEEP дошёл до уже обработанных строк — цикл завершён
                    # честно, а не упором в лимит страниц.
                    completed = True
                    break

                if not deep_mode and quiet_run > self._fast_lookahead_pages:
                    # Тихая страница и K тихих за ней: ниже — только уже
                    # обработанное. Страница из одних повторов (пачка
                    # сдвинула список) счёт не меняет.
                    completed = True
                    break
            else:
                completed = False
        except FineProviderError as exc:
            self._record_error(str(exc), at=run_at)
            return
        except Exception as exc:
            self._record_error(str(exc), at=run_at)
            logger.exception("GeorgiaCollectorJob: unexpected error during feed poll")
            return

        counters.new_rows = len(marked_this_cycle)
        if not completed:
            message = _safety_cap_message(
                max_pages, is_true_first_run=is_true_first_run, deep_mode=deep_mode,
                lookahead_pages_read=quiet_run - 1 if not deep_mode and quiet_run else None,
                lookahead_pages=self._fast_lookahead_pages,
            )
            logger.warning("GeorgiaCollectorJob: %s (pages=%d, %s)", message, pages_fetched, counters.describe())
            self._record_error(message, at=run_at)
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
            "GeorgiaCollectorJob feed poll finished: pages=%d, deep_mode=%s, %s",
            pages_fetched, deep_mode, counters.describe(),
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
        # В outbox ДО отправки: строка уже отмечена увиденной, и если цикл
        # убьют посреди отправки (до трёх попыток по 180 с, когда production
        # медленная) или она упадёт непредвиденной ошибкой, совпадение не
        # потеряется — следующий цикл сначала разбирает outbox (повтор
        # production отсеет по fingerprint).
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


def _page_already_processed(
    entries: list[dict],
    seen: GeorgiaPublicFeedSeenRepository,
    *,
    published_before: date | None,
    seen_at_or_before: datetime | None,
) -> bool:
    """DEEP-горизонт (см. _poll_feed()): КАЖДАЯ строка страницы опубликована
    раньше published_before ЛИБО впервые увидена не позже seen_at_or_before
    (последним успешным циклом или раньше). Строка без даты публикации и
    без такой отметки — не обработанная: обход продолжается."""
    if not entries:
        return False
    for entry in entries:
        entry_date = _parse_publish_date(entry.get("publishDate"))
        if published_before is not None and entry_date is not None and entry_date < published_before:
            continue
        protocol_no = entry.get("protocolNo")
        if (
            seen_at_or_before is not None
            and protocol_no
            and seen.seen_at_or_before(protocol_no, seen_at_or_before)
        ):
            continue
        return False
    return True


def _as_utc(value: datetime) -> datetime:
    """Naive datetime считается UTC (как и first_seen_at в
    GeorgiaPublicFeedSeenRepository)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _safety_cap_message(
    max_pages: int,
    *,
    is_true_first_run: bool,
    deep_mode: bool,
    lookahead_pages_read: int | None = None,
    lookahead_pages: int = FAST_LOOKAHEAD_PAGES,
) -> str:
    """Свой текст для каждого режима, с именем настройки лимита.
    Восстановление, не дошедшее до строк последнего успеха за max_pages
    страниц, само уже не завершится: новые строки только отодвигают их.
    lookahead_pages_read — FAST упёрся в лимит после тихой страницы, до
    конца look-ahead: граница не подтверждена, это тоже не успех."""
    if is_true_first_run:
        return (
            f"first-run catch-up: safety cap of {max_pages} pages reached before rows published "
            f"before today; retrying in deep mode next cycle (raise {_FIRST_RUN_PAGES_SETTING} "
            "if this repeats)"
        )
    if deep_mode:
        return (
            "recovery could not reach rows covered by the last successful cycle within "
            f"{max_pages} pages; it keeps failing until {_PAGES_PER_CYCLE_SETTING} is raised"
        )
    if lookahead_pages_read is not None:
        return (
            f"safety cap of {max_pages} pages reached before the frontier was confirmed "
            f"(look-ahead: {lookahead_pages_read} of {lookahead_pages} pages read after a page "
            "without new rows); the next cycle runs deep recovery, which needs even more pages "
            f"— raise {_PAGES_PER_CYCLE_SETTING}"
        )
    return (
        f"safety cap of {max_pages} pages reached before the frontier (already-seen rows); "
        "the next cycle runs deep recovery, which needs even more pages — raise "
        f"{_PAGES_PER_CYCLE_SETTING}"
    )

