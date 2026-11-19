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
CancelledError) is followed by DEEP recovery, not FAST. FAST mode stops on
the SECOND page with a row seen BEFORE this cycle (normally the overlap page
right after the frontier). Rows this cycle already marked reappear when new
publications shift the pages during the traversal (production: bursts of
70–360 rows); stopping on them, or on an overlap page such a burst filled
right after the frontier, would leave new rows below unread for good.

A matched row's payload goes to the local outbox BEFORE it is submitted and
leaves it after production accepted it, so a cycle killed mid-submit loses
nothing: the next cycle flushes the outbox first (production dedups by
fingerprint).

Resumable recovery (an outage longer than one cycle's page cap, ~1.5 days
at 300 pages): a recovery or FAST cycle that hits the cap records where it
stopped (georgia_public_feed_state.recovery_resume_offset/_at, see
_ResumePoint). The next recovery reads the new rows on top as usual until
it reaches rows that attempt already read, jumps to just before where it
stopped, confirms it landed among that attempt's rows (stepping back page
by page if not — a horizon page alone proves nothing) and goes on. So the
gap is caught up over several cycles within the same per-cycle cap,
nothing skipped; until then every cycle ends "recovering". After every
feed poll the collector reports the outcome to production's
/internal/georgia/health (FeedPollOutcome: status, mode, counts, dates —
no plates), which alerts the managers' group (reader/public_bot/
collector_health.py); a report that fails never affects the cycle.

should_run()/run_times/_last_run_slot is the SAME exact-wall-clock pattern
as reader/jobs/fine_job.py::FineJob.should_run()/reader/jobs/
georgia_public_feed_job.py::GeorgiaPublicFineFeedJob.should_run() — an
independent reimplementation (this module cannot import reader/jobs/, see
the "no Telegram/production-DB imports" requirement below), but identical
semantics: exact wall-clock match, no catch-up, no persistence across a
collector restart.

KNOWN TECHNICAL DEBT (see architecture review, task "PR #34 integration
sequence"): _poll_feed() below duplicates GeorgiaPublicFineFeedJob.run()'s
DEEP/FAST frontier+one-overlap-page state machine almost line-for-line
(same deep_mode/is_true_first_run/in_recovery computation, same pending_
overlap/page_has_seen_row tracking, same error/retry/retention handling).
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
    parse_published_protocol_entry,
    parse_search_response,
)
from reader.fines.police_ge_session import PoliceGeSession
from reader.fines.provider import FineProviderError
from reader.fines.public_feed_seen_repository import GeorgiaPublicFeedSeenRepository
from reader.fines.public_feed_state_repository import (
    GeorgiaPublicFeedStateRepository,
    PublicFeedState,
)
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


@dataclass(frozen=True)
class FeedPollOutcome:
    """Итог обхода feed'а одного цикла для отчёта production (см. run(),
    ProductionApiClient.report_health) — только режим, счётчики и даты, без
    номеров и квитанций. status: "ok" — цикл завершён; "recovering" — упёрся
    в лимит страниц, следующий цикл продолжит с того же места; "error"."""

    status: str
    detail: str
    mode: str
    pages: int
    last_success_at: datetime | None
    resume_offset: int | None = None
    oldest_publish_date: date | None = None
    horizon_date: date | None = None

    def to_payload(self) -> dict:
        return {
            "status": self.status,
            "detail": self.detail,
            "mode": self.mode,
            "pages": self.pages,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "resume_offset": self.resume_offset,
            "oldest_publish_date": self.oldest_publish_date.isoformat() if self.oldest_publish_date else None,
            "horizon_date": self.horizon_date.isoformat() if self.horizon_date else None,
        }


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
            await self._report_health({"status": "error", "detail": "police.ge circuit breaker open — cycle skipped"})
            return

        try:
            monitored = set(await self._production.get_active_plates())
        except ProductionApiError:
            logger.warning("GeorgiaCollectorJob: production API unavailable (get_active_plates)")
            return

        outcome = await self._poll_feed(monitored, run_at)
        await self._report_health(outcome.to_payload())
        await self._run_bootstrap(run_at)

    async def _report_health(self, payload: dict) -> None:
        """Итог цикла — production (см. reader/public_bot/collector_health.py),
        чтобы о простое или отставании узнал человек. На цикл не влияет
        никак: сбой отправки только пишется в журнал."""
        try:
            await self._production.report_health(payload)
        except Exception as exc:  # noqa: BLE001 — сбой отчёта не должен ломать цикл
            logger.warning("GeorgiaCollectorJob: health report not delivered to production (%s)", exc)

    async def _poll_feed(self, monitored: set[str], run_at: datetime) -> FeedPollOutcome:
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

        mode = "first_run" if is_true_first_run else "recovery" if deep_mode else "fast"
        # Продолжение восстановления, которое прошлый цикл не уложил в лимит
        # страниц (простой дольше ~1,5 суток: отставание больше лимита одного
        # цикла): без него каждая попытка начинала бы с первой страницы и,
        # пока сверху прибавляются новые публикации, не дошла бы до горизонта
        # никогда. См. _ResumePoint.
        resume = (
            _resume_point(feed_state, last_success=processed_before_time, run_at=run_at)
            if deep_mode and processed_before_time is not None else None
        )

        pages_fetched = 0
        newest_seen_this_cycle: date | None = None
        oldest_seen_this_cycle: date | None = None
        completed = False
        pending_overlap = False
        # Отмеченные этим циклом: пока идёт обход, наверху feed'а появляются
        # новые публикации, страницы сдвигаются вниз, и следующая страница
        # снова показывает уже прочитанные строки (в production — пачки по
        # 70–360 строк в циклы 15:00/21:00). Это не граница FAST — граница
        # только строка, увиденная до этого цикла, иначе FAST остановился бы
        # на повторе и не дочитал бы новые строки под ним.
        marked_this_cycle: set[str] = set()
        next_offset = 0
        deepest_offset = 0
        # Перескок (см. _ResumePoint): jump_offset — страница, к которой
        # перескочили; top_last_offset — последняя страница перед перескоком;
        # aligning — место, где остановилась прошлая попытка, ещё не найдено;
        # jump_page_terminal — страница перескока пустая или целиком
        # обработанная (после выравнивания цикл на ней и завершается).
        resume_handled = False
        jump_offset = 0
        top_last_offset = 0
        aligning = False
        jump_page_terminal = False

        def outcome(status: str, detail: str, *, resume_offset: int | None = None) -> FeedPollOutcome:
            return FeedPollOutcome(
                status=status, detail=detail, mode=mode, pages=pages_fetched,
                last_success_at=run_at if status == "ok" else feed_state.last_successful_poll_at,
                resume_offset=resume_offset, oldest_publish_date=oldest_seen_this_cycle,
                horizon_date=processed_before_date,
            )

        # Отметка "цикл идёт": если процесс остановят посреди цикла (SIGTERM,
        # OOM, CancelledError — их не ловит ни один except ниже), last_error
        # останется, и следующий цикл пойдёт DEEP-восстановлением, а не FAST
        # (FAST остановился бы на строках прерванного цикла и не вернулся бы
        # к недочитанным под ними). Успех её снимает, ошибка перезаписывает.
        self._feed_state.record_error(_CYCLE_IN_PROGRESS, at=run_at)

        try:
            while pages_fetched < max_pages:
                first_result = next_offset
                raw = await self._session.fetch_published_protocols(first_result=first_result)
                pages_fetched += 1
                deepest_offset = max(deepest_offset, first_result)

                try:
                    raw_results = extract_published_protocols_results(raw)
                except FineParseError as exc:
                    self._record_error(str(exc), at=run_at)
                    return outcome("error", str(exc))

                if not raw_results and not aligning:
                    completed = True
                    break

                page_has_seen_row = False
                for entry in raw_results:
                    entry_date = _parse_publish_date(entry.get("publishDate"))
                    if entry_date is not None:
                        if newest_seen_this_cycle is None or entry_date > newest_seen_this_cycle:
                            newest_seen_this_cycle = entry_date
                        if oldest_seen_this_cycle is None or entry_date < oldest_seen_this_cycle:
                            oldest_seen_this_cycle = entry_date

                    protocol_no = entry.get("protocolNo")
                    if not protocol_no:
                        continue

                    if self._seen.is_seen(protocol_no):
                        if protocol_no not in marked_this_cycle:
                            page_has_seen_row = True
                        continue

                    self._seen.mark_seen(protocol_no, publish_date=entry_date or run_at.date(), seen_at=run_at)
                    marked_this_cycle.add(protocol_no)
                    await self._maybe_submit_entry(entry, monitored)

                if aligning:
                    # После перескока страница должна содержать строку,
                    # прочитанную прошлой попыткой: иначе перескочили дальше,
                    # чем она дошла (пачка публикаций во время неё сдвинула её
                    # страницы, строки исчезли сверху) — шаг назад, пока такая
                    # строка не найдётся или пока между перескоком и
                    # страницами до него не останется непрочитанного. Страница
                    # строк, обработанных горизонтом, сама по себе этого не
                    # доказывает: между ними и прошлой попыткой может быть дыра.
                    if first_result == jump_offset:
                        jump_page_terminal = not raw_results or _page_already_processed(
                            raw_results, self._seen,
                            published_before=processed_before_date, seen_at_or_before=processed_before_time,
                        )
                    if (
                        _first_row_read_by(raw_results, self._seen, resume) is not None
                        or first_result - _PAGE_SIZE <= top_last_offset
                    ):
                        aligning = False
                        logger.info(
                            "GeorgiaCollectorJob: recovery aligned at offset %d after jumping to %d",
                            first_result, jump_offset,
                        )
                        if jump_page_terminal:
                            completed = True
                            break
                        next_offset = jump_offset + _PAGE_SIZE
                    else:
                        next_offset = first_result - _PAGE_SIZE
                    continue

                if deep_mode and _page_already_processed(
                    raw_results, self._seen,
                    published_before=processed_before_date, seen_at_or_before=processed_before_time,
                ):
                    # DEEP дошёл до уже обработанных строк — цикл завершён
                    # честно, а не упором в лимит страниц.
                    completed = True
                    break

                if not deep_mode and page_has_seen_row:
                    if pending_overlap:
                        # Вторая страница со строками, увиденными до этого
                        # цикла: ниже — только уже обработанное. Страница
                        # без них (пачка публикаций сдвинула список сразу
                        # после границы) обход не завершает.
                        completed = True
                        break
                    pending_overlap = True

                if resume is not None and not resume_handled:
                    row_index = _first_row_read_by(raw_results, self._seen, resume)
                    if row_index is not None:
                        # Дошли сверху до строк прошлой попытки: дальше подряд
                        # лежит прочитанное ею — перескок к её концу минус
                        # страница (с выравниванием выше).
                        resume_handled = True
                        target = (first_result + row_index + resume.offset - _PAGE_SIZE) // _PAGE_SIZE * _PAGE_SIZE
                        if target > first_result + _PAGE_SIZE:
                            logger.info(
                                "GeorgiaCollectorJob: recovery resumes — jump from offset %d to %d "
                                "(the previous attempt read %d rows from the top)",
                                first_result, target, resume.offset,
                            )
                            jump_offset = target
                            top_last_offset = first_result
                            aligning = True
                            next_offset = target
                            continue

                next_offset = first_result + _PAGE_SIZE
        except FineProviderError as exc:
            self._record_error(str(exc), at=run_at)
            return outcome("error", str(exc))
        except Exception as exc:
            self._record_error(str(exc), at=run_at)
            logger.exception("GeorgiaCollectorJob: unexpected error during feed poll")
            return outcome("error", str(exc))

        if not completed:
            # Лимит страниц: от верха feed'а до deepest_offset всё прочитано
            # подряд (с перескоком — только после выравнивания), поэтому
            # следующее восстановление продолжит отсюда, а не с первой
            # страницы. Не выровнялись — прежняя отметка остаётся.
            resume_offset = None
            if not is_true_first_run and not aligning:
                resume_offset = deepest_offset + _PAGE_SIZE
                self._feed_state.record_recovery_progress(offset=resume_offset, at=run_at)
            message = _safety_cap_message(
                max_pages, is_true_first_run=is_true_first_run, deep_mode=deep_mode, resume_offset=resume_offset,
            )
            logger.warning("GeorgiaCollectorJob: %s", message)
            self._record_error(message, at=run_at)
            return outcome(
                "error" if is_true_first_run else "recovering", message,
                resume_offset=resume_offset if resume_offset is not None else feed_state.recovery_resume_offset,
            )

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
        return outcome("ok", f"feed poll finished: pages={pages_fetched}, mode={mode}")

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


@dataclass(frozen=True)
class _ResumePoint:
    """Где остановилась попытка (восстановление или FAST), упёршаяся в лимит
    страниц, — см. GeorgiaPublicFeedStateRepository: offset — first_result
    сразу за последней прочитанной ею страницей, until — её run_at, after —
    последний успешный цикл. Строки, впервые отмеченные в (after, until],
    прочитаны продолжаемым восстановлением, и от верха feed'а в момент той
    попытки до offset всё прочитано подряд (страницы сдвигаются только
    публикациями сверху, см. marked_this_cycle). Поэтому следующая попытка,
    дойдя сверху до первой такой строки (позиция Z), перескакивает к
    Z + offset минус страница и выравнивается (см. aligning в обходе), а
    упёршись в лимит снова, записывает свою отметку."""

    offset: int
    after: datetime
    until: datetime


def _resume_point(feed_state: PublicFeedState, *, last_success: datetime, run_at: datetime) -> _ResumePoint | None:
    """None — продолжать нечего: отметки нет, она не от этого восстановления
    (не позже последнего успеха) или из будущего (часы) — обход идёт подряд
    с первой страницы, как раньше."""
    offset, until = feed_state.recovery_resume_offset, feed_state.recovery_resume_at
    if not offset or until is None:
        return None
    until = _as_utc(until)
    if not (
        last_success.replace(microsecond=0) < until.replace(microsecond=0)
        < _as_utc(run_at).replace(microsecond=0)
    ):
        return None
    return _ResumePoint(offset=offset, after=last_success, until=until)


def _first_row_read_by(
    entries: list[dict], seen: GeorgiaPublicFeedSeenRepository, resume: _ResumePoint | None,
) -> int | None:
    """Индекс первой строки страницы, уже прочитанной продолжаемым
    восстановлением (см. _ResumePoint); None — таких нет."""
    if resume is None:
        return None
    for index, entry in enumerate(entries):
        protocol_no = entry.get("protocolNo")
        if protocol_no and seen.seen_between(protocol_no, after=resume.after, until=resume.until):
            return index
    return None


def _safety_cap_message(
    max_pages: int, *, is_true_first_run: bool, deep_mode: bool, resume_offset: int | None,
) -> str:
    """Свой текст для каждого режима, с именем настройки лимита. Упёршиеся
    в лимит восстановление и FAST следующий цикл продолжает с resume_offset
    (см. _ResumePoint); None — место, где остановилась прошлая попытка, в
    этом цикле не нашлось, и её отметка остаётся."""
    if is_true_first_run:
        return (
            f"first-run catch-up: safety cap of {max_pages} pages reached before rows published "
            f"before today; retrying in deep mode next cycle (raise {_FIRST_RUN_PAGES_SETTING} "
            "if this repeats)"
        )
    if resume_offset is None:
        return (
            f"recovery reached the page cap of {max_pages} pages while looking for where the previous "
            "attempt stopped; the next cycle continues from that attempt's position (raise "
            f"{_PAGES_PER_CYCLE_SETTING} if this repeats)"
        )
    if deep_mode:
        return (
            f"recovery reached the page cap of {max_pages} pages before rows covered by the last "
            f"successful cycle; the next cycle continues from where this one stopped (offset "
            f"{resume_offset}), nothing is skipped (raise {_PAGES_PER_CYCLE_SETTING} to catch up "
            "in fewer cycles)"
        )
    return (
        f"safety cap of {max_pages} pages reached before the frontier (already-seen rows); the next "
        f"cycle runs deep recovery and continues from where this one stopped (offset {resume_offset}) "
        f"(raise {_PAGES_PER_CYCLE_SETTING} to catch up in fewer cycles)"
    )
