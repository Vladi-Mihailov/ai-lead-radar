"""GeorgiaPublicFineFeedJob — см. задачу "Georgia fines incremental
public-feed monitoring" (production architecture change после PR #30
"back off when police.ge is unavailable"), ИСПРАВЛЕНО по корректирующей
задаче "IMPORTANT CORRECTION BEFORE COMMIT / PR" (request-volume
contradiction: исходная версия пересканировала весь overlap-день заново
КАЖДЫЙ 5-минутный цикл — ~6600 запросов/день вместо ожидаемых ~288/день),
И ЕЩЁ РАЗ ИСПРАВЛЕНО по задаче "schedule Georgia public feed three times
daily" + корректирующей задаче "IMPORTANT SCHEDULE UPDATE" (добавлен
23:55 как намеренный end-of-day sweep) — расписание сменилось с "каждые N
минут от момента запуска" на ЧЕТЫРЕ фиксированных момента суток
(09:00/15:00/21:00/23:55 Asia/Tbilisi по умолчанию, см.
settings.fine_monitor.public_feed_times/timezone), тем же способом, что и
FineJob.run_times/tz (см. reader/jobs/fine_job.py::should_run).

KNOWN TECHNICAL DEBT (см. задачу "Georgia remote collector architecture
(REDNET)" + её интеграционный review): run()'s DEEP/FAST frontier+look-
ahead state machine below is DUPLICATED (not imported) by
reader/georgia_collector/collector_job.py::GeorgiaCollectorJob._poll_feed()
for the REDNET collector process. Extraction into a shared module was
deliberately reviewed and rejected — this class is already-merged,
production-tested code, and the collector package is architecturally
forbidden from importing reader/jobs/ at all (its own "no Telegram/
production-DB imports" invariant); the two also genuinely differ in what
happens to a newly-matched row (synchronous local persist here vs. async
remote submit-with-outbox-fallback there), which would force extra
parameterization into any shared abstraction for no functional gain.
**Collector and in-process feed job must be kept behaviorally
synchronized** — any future change to this pagination/frontier/overlap/
DEEP-recovery logic MUST be mirrored by hand in collector_job.py's
_poll_feed() (see the identical note there).

Заменяет per-car scheduled police.ge-опрос (FineJob/ArchiveFineJob на
bootstrap-completed машинах, см. reader/fines/plate_bootstrap_repository.py)
ОДНИМ периодическим обходом публичного feed'а "Receipts of fines
published" (police.ge/protocol/index.php?url=protocols, см. audit report)
— "Do NOT continuously search every plate individually".

КЛЮЧЕВАЯ ИДЕЯ ("NORMAL POLL ALGORITHM", см. корректирующую задачу): вместо
date-based overlap-окна (которое требовало пересматривать целый день на
каждом цикле) — ПЕРСИСТЕНТНЫЙ "seen"-индекс protocolNo (см.
GeorgiaPublicFeedSeenRepository, НЕ detected_fines — только
protocol_no/publish_date, НИКАКИХ деталей штрафа для несовпавших строк, см.
задачу "DATA MINIMIZATION"). Обычный цикл идёт newest-first со страницы 0
и обрабатывает каждую страницу целиком (новые строки любой страницы — НЕ
отбрасываются); граница с прошлого цикла — первая "тихая" страница (ни
одной новой строки, хотя бы одна строка, увиденная до этого цикла), но
цикл останавливается, только прочитав за ней ещё FAST_LOOKAHEAD_PAGES
тихих страниц (look-ahead, см. "FAST" ниже): новая строка на любой из них
сбрасывает счёт. Поэтому стоимость цикла — O(число НОВЫХ записей с
прошлого раза) + 1 + K страниц, а НЕ O(глубина backlog'а за день). Примеры
(K = 2):
  - ничего нового с прошлого раза -> 3 запроса (тихая страница + 2).
  - 30 новых, граница ровно по краю первой страницы -> 4 запроса.
  - 75 новых (на третьей странице — 15 новых и 15 старых) -> 6 запросов.

РАСПИСАНИЕ (см. задачу "schedule Georgia public feed three times daily" +
корректирующую задачу "IMPORTANT SCHEDULE UPDATE"): ЧЕТЫРЕ ФИКСИРОВАННЫХ
момента суток в сутки (по умолчанию 09:00/15:00/21:00/23:55 Asia/Tbilisi,
см. settings.fine_monitor.public_feed_times/timezone), а НЕ "каждые N
минут от момента запуска" — см. should_run() докстрок. 23:55, а НЕ 00:00,
— намеренно: остаётся частью ТОГО ЖЕ грузинского календарного дня, а не
следующего, и служит end-of-day sweep'ом, подбирающим публикации между
обычным циклом 21:00 и концом дня (минимизирует риск оставить сегодняшние
публикации необработанными на ночь). Типичный (ничего не накопилось сверх
одной страницы) цикл — страница-граница + тихая страница + K = ТИПИЧНЫЙ
baseline ≈ 4 цикла × 4 запроса ≈ 16 запросов/день — НЕ гарантия: если между
циклами накопилось больше 30 новых записей, pagination сама сделает больше
запросов (см. примеры выше), а первый-в-жизни DEEP catch-up (или любое
восстановление после сбоя) может потребовать заметно больше страниц — это
одноразовое/recovery поведение, не входящее в типичный ежедневный baseline
(см. "ОДНОРАЗОВЫЙ CUTOVER CATCH-UP" и safety cap ниже — их семантика этой
задачей не меняется).

РЕЖИМЫ (см. _choose_mode):
- "DEEP" (первый запуск ЛИБО восстановление после сбоя предыдущего
  цикла, см. ниже) — обходит страницы ИГНОРИРУЯ seen-границу (мало ли
  что именно она отражает после сбоя/никогда не запускалась), жёстко
  ограничен max_pages (first_run_max_pages для истинного первого запуска,
  max_pages_per_cycle для восстановления после сбоя — обычно меньше).
  Завершается успешно в настоящем конце списка ИЛИ после 1 +
  FAST_LOOKAHEAD_PAGES страниц подряд, ВСЕ строки которых уже обработаны
  (DEEP-горизонт, см. _page_already_processed; страница из одних повторов
  счёт не меняет, необработанная строка его сбрасывает): при истинном
  первом запуске — опубликованы раньше сегодняшнего дня; при
  восстановлении — опубликованы раньше даты самой новой строки последнего
  успешного цикла (но не позже его собственного дня) ЛИБО впервые увидены
  этим циклом или раньше (first_seen_at <= его run_at; строки, отмеченные
  сбойными или прерванными попытками после него, не считаются — "дыра"
  под ними будет дочитана). Одной обработанной страницы мало: её даёт и
  блок из 30+ переопубликованных старых строк над новыми. Дойти до конца
  реального feed'а (~10 тыс. страниц, ~5–6,3 тыс. публикаций в сутки)
  невозможно — без горизонта DEEP не завершался никогда. Упёрся в
  max_pages раньше горизонта — по-прежнему остаётся в DEEP-режиме и на
  следующем цикле (см. задачу: "Do not silently declare catch-up complete").
- В любом режиме пустая или неполная страница завершает обход, только если
  это настоящий конец списка (её строки доходят до data.count); раньше —
  это сбой источника и ошибка: принять её за конец значило бы потерять
  всё ниже. Без data.count пустая страница, как раньше, считается концом
  (с предупреждением).
- "FAST" (обычный установившийся режим, после хотя бы одного успешного
  DEEP-прохода И без свежей ошибки) — останавливается только на "тихой"
  странице (ни одной новой строки, хотя бы одна строка, увиденная ДО этого
  цикла и ещё не прочитанная им), за которой прочитаны ещё
  FAST_LOOKAHEAD_PAGES тихих страниц; страница с новой строкой сбрасывает
  счёт. Старая строка над новыми (переопубликованная квитанция — production,
  19.11.2026) обход больше не завершает: раньше хватало второй страницы с
  такой строкой, и новые строки между ней и настоящей границей не читались
  никогда. Строки, уже отмеченные этим же циклом, и строки, увиденные до
  него, но уже прочитанные им, появляются снова, когда новые публикации
  сдвигают страницы во время обхода (в production — пачки по 70–360
  строк): страницу тихой они не делают и счёт не сбрасывают — остановка на
  них оставила бы новые строки ниже непрочитанными навсегда. Счётчики
  цикла (seen_among_new, quiet_pages, lookahead_new) и data.count первой
  страницы с разницей от прошлого цикла — в итоговой строке журнала, без
  номеров протоколов.

ВОССТАНОВЛЕНИЕ ПОСЛЕ СБОЯ (см. задачу "ATOMICITY": "mid-pagination
failure does not cause data loss"): ЛЮБОЕ исключение в середине цикла
выставляет last_error (см. GeorgiaPublicFeedStateRepository); прерванный
цикл (SIGTERM, OOM, CancelledError) оставляет отметку "цикл идёт",
записанную перед первой страницей, — пока last_error не сброшен,
СЛЕДУЮЩИЙ цикл автоматически работает в DEEP-режиме (не доверяя
FAST-границе, которая могла не покрыть то, что сбойный цикл не успел
обработать), пока не дойдёт до пустой страницы или DEEP-горизонта. Строки,
реально обработанные ДО сбоя (mark_seen/persist_feed_match — обе
операции идемпотентны и коммитятся по одной), не теряются и не
переобрабатываются как ошибка — detected_fines fingerprint dedup и
georgia_public_feed_seen PRIMARY KEY делают повторную обработку безопасной.

protocolNo НЕ считается глобально монотонным (см. audit report, риск №5)
— обход никогда не полагается на его порядок, только на seen/unseen per
protocolNo, на пустоту страницы и на DEEP-горизонт (даты публикации и
first_seen_at)."""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from reader.fines.check_service import FineCheckService
from reader.fines.models import FineMonitoringTask
from reader.fines.parser import (
    FineParseError,
    extract_published_protocols_results,
    extract_reported_count,
    parse_published_protocol_entry,
)
from reader.fines.police_ge_session import PoliceGeSession
from reader.fines.provider import FineProviderError
from reader.fines.public_feed_seen_repository import GeorgiaPublicFeedSeenRepository
from reader.fines.public_feed_state_repository import GeorgiaPublicFeedStateRepository
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.fines.validation import FineValidationError, normalize_car_number
from reader.jobs.base import Job

logger = logging.getLogger(__name__)

_PAGE_SIZE = 30

FAST_LOOKAHEAD_PAGES = 2
"""K: how many more quiet pages FAST reads after the first quiet page (a
page without unseen rows that shows at least one row seen before this
cycle and not yet read by it) before it stops; a page with unseen rows
among them resets the count. DEEP (first run and recovery) ends the same
way: on 1 + K consecutive pages whose rows are all already processed (its
horizon), so a block of re-shown old rows cannot end it early either. Mirrors reader/georgia_collector/collector_job.py::
FAST_LOOKAHEAD_PAGES (the collector's GEORGIA_COLLECTOR_FAST_LOOKAHEAD_PAGES
overrides it there; this job has no setting for it).

Data behind K = 2 (production, 2026-11-19):
- both FAST cycles that day had one old row re-shown above new rows
  (17:00 UTC: 1197 of the 1200 rows read were new, the old row directly
  above the boundary page; 19:55 UTC: 238 of 240). The old "second page
  with a seen row" stop happened to end on the real boundary both times;
  a second such row on another page above it would have ended the
  traversal early;
- new rows numbered below numbers already seen (1–7 rows in 10 of 32
  cycles, 63 once): the shallow number-space overlaps are <= 5 rows;
- a page-0 probe showed the feed ordered by publication, not by protocol
  number (the series interleave), with the day's rows as one contiguous
  block on top; at 19:55 every new number was above every old one, so the
  re-shown row is a re-published or updated receipt, not a number-ordering
  effect. Deep insertion of new rows below the boundary was not observed;
  if the data.count delta ever exceeds the new rows read, revisit (a DEEP
  trigger or a daily sweep, ~190–210 pages a day).
K = 2 looks up to ~60 rows below the first quiet page: it counts pages,
and one fresh old row makes a page quiet, so pages a burst partly repeats
cover less. Cost, measured in simulation: +1.94 pages per FAST cycle
without bursts and +2.68 with them (repeat pages are neutral), about +10
requests a day (~5 % of the daily total), plus K pages per DEEP cycle
(first run or recovery), well inside the 300-page cap. K must stay below
both page caps, max_pages_per_cycle and first_run_max_pages (the jobs
refuse it otherwise, the collector already at start-up): every cycle would
end at the cap.

Expected one-off: a true first run that finds its page 0 already older
than today (no row published today yet) sees only 1 + K pages; shifted by
the next cycle's new rows they can give one quiet page fewer than 1 + K, so
that FAST cycle reads on into older rows no cycle has seen (they count as
new) up to the page cap and records an error; the DEEP recovery after it
completes normally. A first run that finds any row from today is not
affected: it sees at least 2 + K pages."""

# Отметка "цикл идёт" в georgia_public_feed_state.last_error (см. отметку
# перед циклом страниц): если она осталась, цикл был прерван.
_CYCLE_IN_PROGRESS = (
    "cycle in progress (if this stays, the cycle was interrupted "
    "and the next one runs deep recovery)"
)

# Настройки лимита страниц, которые называет _safety_cap_message() —
# ключи config.yaml (см. reader/settings.py).
_FIRST_RUN_PAGES_SETTING = "fine_monitor.public_feed_first_run_max_pages"
_PAGES_PER_CYCLE_SETTING = "fine_monitor.public_feed_max_pages_per_cycle"


@dataclass
class GeorgiaPublicFineFeedJobStatus:
    """Статистика запусков — только для наблюдаемости (аналог
    reader.jobs.fine_job.FineJobStatus), не влияет на логику."""

    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    error_count: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None


@dataclass
class _FeedCycleCounters:
    """Per-cycle feed diagnostics for the log, never protocol numbers or
    plates (mirrors reader/georgia_collector/collector_job.py):
    - new_rows: rows this cycle saw for the first time;
    - seen_among_new: rows seen before this cycle standing above a new row
      on the same page (an old row re-shown among new ones). The old rows
      under the last new row of the boundary page are not counted — every
      cycle has them. DEEP cycles count it too;
    - quiet_pages: pages with no new row and at least one row seen before
      this cycle that it had not read yet. DEEP cycles count them too (there
      rows marked by failed attempts make pages quiet as well);
    - lookahead_new: FAST only — every new row on a page after the cycle's
      first quiet page, also after a look-ahead reset: rows inserted below a
      boundary that the look-ahead found (or a burst deeper than that page);
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


class GeorgiaPublicFineFeedJob(Job):
    name = "georgia_public_fine_feed"

    def __init__(
        self,
        session: PoliceGeSession,
        task_repository: FineMonitoringTaskRepository,
        check_service: FineCheckService,
        feed_state_repository: GeorgiaPublicFeedStateRepository,
        seen_repository: GeorgiaPublicFeedSeenRepository,
        *,
        run_times: list[dt_time],
        tz: ZoneInfo,
        max_pages_per_cycle: int = 30,
        first_run_max_pages: int = 100,
        retention_days: int = 7,
        fast_lookahead_pages: int = FAST_LOOKAHEAD_PAGES,
    ):
        if not 1 <= fast_lookahead_pages < min(max_pages_per_cycle, first_run_max_pages):
            # K не меньше лимита — каждый цикл упирался бы в лимит: и FAST, и
            # DEEP читают после первой тихой/обработанной страницы ещё K.
            raise ValueError(
                "fast_lookahead_pages must be >= 1 and below max_pages_per_cycle and first_run_max_pages"
            )
        self._session = session
        self._task_repository = task_repository
        self._check_service = check_service
        self._feed_state = feed_state_repository
        self._seen = seen_repository
        self._run_times = run_times
        self._tz = tz
        self._max_pages_per_cycle = max_pages_per_cycle
        self._first_run_max_pages = first_run_max_pages
        self._retention_days = retention_days
        self._fast_lookahead_pages = fast_lookahead_pages
        # data.count первой страницы прошлого цикла этого процесса — только
        # для журнала (см. run()), в БД не хранится.
        self._previous_reported_count: int | None = None
        # Только в памяти, тот же приём, что и reader/jobs/fine_job.py::
        # FineJob._last_run_slot — какой именно (день, время-слот) из
        # run_times уже отработан, НЕ источник истины про DEEP/FAST режим
        # (это georgia_public_feed_state, см. run() ниже).
        self._last_run_slot: tuple[date, dt_time] | None = None
        self.status = GeorgiaPublicFineFeedJobStatus()

    async def should_run(self, now: datetime) -> bool:
        """Срабатывает ТОЛЬКО при точном совпадении часа и минуты с одним
        из run_times (в self._tz) — см. задачу "schedule Georgia public
        feed three times daily" + reader/jobs/fine_job.py::FineJob.
        should_run (тот же код, тот же осознанный компромисс):

        - `_last_run_slot` — только в памяти этого объекта. После рестарта
          процесса создаётся новый GeorgiaPublicFineFeedJob с
          `_last_run_slot=None`, поэтому обычный restart НЕ запускает feed
          вне 09:00/15:00/21:00/23:55 (если рестарт пришёлся ровно на минуту
          одного из run_times — job запустится, как и запустился бы без
          рестарта; это не "лишний" запуск).
        - Пропущенный слот (процесс не опрашивал should_run() в нужную
          минуту — был выключен/завис) просто пропускается навсегда,
          никакого отдельного per-car catch-up здесь не создаётся — при
          СЛЕДУЮЩЕМ обычном запуске backlog безопасно дочитывают уже
          существующие seen/frontier/pagination/DEEP recovery/fingerprint
          dedupe (см. run() ниже) — их семантика этой задачей не менялась.
        """
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
            # Уже отработали именно этот слот сегодня — не запускать снова
            # при следующем опросе Scheduler'а в течение той же минуты.
            return False

        self._last_run_slot = slot
        return True

    async def run(self, now: datetime | None = None) -> None:
        run_at = now or datetime.now(timezone.utc)
        self.status.last_run_at = run_at

        if not self._check_service.provider_available():
            # Circuit breaker открыт (см. задачу "FAILURE BEHAVIOR":
            # "respect circuit breaker; do not run thousands of fallback
            # per-car checks") — ни одного feed-запроса, state не трогаем
            # вовсе (следующий цикл просто повторит попытку).
            logger.info(
                "GeorgiaPublicFineFeedJob: police_ge circuit breaker open — skipping this cycle"
            )
            return

        monitored = self._build_monitored_plate_map()
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
                    "GeorgiaPublicFineFeedJob: last successful cycle %s is not earlier than this "
                    "cycle %s (clock skew?) — recovery ignores first-seen times this cycle",
                    last_success.isoformat(), run_at.isoformat(),
                )
                processed_before_time = None

        matched_count = 0
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
        # DEEP-горизонт с тем же look-ahead: обработанных страниц подряд
        # (см. _page_already_processed); DEEP завершается, когда их 1 + K.
        horizon_run = 0
        # data.count последней страницы, где он был (за обход только растёт):
        # отличает настоящий конец списка от пустого/неполного ответа посреди
        # него. None — источник его не сообщил: пустая страница, как раньше,
        # считается концом (с предупреждением).
        latest_count: int | None = None
        warned_no_count = False
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
                    logger.exception("GeorgiaPublicFineFeedJob: некорректный формат ответа feed'а")
                    self._record_error(str(exc), at=run_at)
                    return

                page_count = extract_reported_count(raw)
                if page_count is not None:
                    latest_count = page_count
                if page_index == 0:
                    # Только для журнала: data.count — всего строк в списке
                    # (проба 19.11: 298 570), разница с прошлым циклом —
                    # сколько добавилось с его первой страницы.
                    counters.reported_count = page_count
                    if counters.reported_count is not None and self._previous_reported_count is not None:
                        counters.reported_count_delta = counters.reported_count - self._previous_reported_count
                    self._previous_reported_count = counters.reported_count

                if len(raw_results) < _PAGE_SIZE:
                    # Пустая или неполная страница — конец списка, только если
                    # строк до её конца не меньше data.count. Иначе это сбой
                    # источника посреди списка ({success: true, results: []}):
                    # всё ниже было бы потеряно, а неполная страница молча
                    # пропустила бы остаток своих 30 мест, — ошибка, без
                    # успеха; следующий цикл пойдёт DEEP.
                    if latest_count is None:
                        if not warned_no_count:
                            warned_no_count = True
                            logger.warning(
                                "GeorgiaPublicFineFeedJob: police.ge reports no data.count — an empty page is taken as "
                                "the end of the list, as before (a transient one cannot be told apart)"
                            )
                    elif first_result + len(raw_results) < latest_count:
                        message = _incomplete_page_message(first_result, len(raw_results), latest_count)
                        logger.warning("GeorgiaPublicFineFeedJob: %s", message)
                        self._record_error(message, at=run_at)
                        return
                    if not raw_results:
                        # Настоящий конец списка — дальше не запрашиваем ("do
                        # not make unnecessary requests beyond an empty result").
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

                    # Впервые видим эту конкретную запись feed'а (неважно,
                    # совпадёт ли она с отслеживаемым номером, и неважно,
                    # является ли эта страница look-ahead'ом — см. задачу:
                    # "All unseen rows found on the overlap page must be
                    # handled normally") — отмечаем ДО попытки сопоставления,
                    # см. GeorgiaPublicFeedSeenRepository докстрок.
                    self._seen.mark_seen(
                        protocol_no, publish_date=entry_date or run_at.date(), seen_at=run_at,
                    )
                    marked_this_cycle.add(protocol_no)
                    page_new += 1
                    # Старые строки над новой на этой странице (не хвост
                    # страницы-границы под последней новой строкой).
                    counters.seen_among_new += seen_above_new
                    seen_above_new = 0
                    matched_count += await self._maybe_process_entry(entry, monitored)

                if page_new:
                    if not deep_mode and counters.quiet_pages:
                        # Новые строки ниже тихой страницы — вставка под
                        # границей: без look-ahead их бы не прочитали.
                        counters.lookahead_new += page_new
                    quiet_run = 0
                elif page_quiet_rows:
                    counters.quiet_pages += 1
                    quiet_run += 1

                if deep_mode:
                    if page_new or page_quiet_rows:
                        # DEEP-горизонт с тем же look-ahead, что у FAST: 1 + K
                        # страниц подряд, все строки которых уже обработаны.
                        # Необработанная строка сбрасывает счёт; страница из
                        # одних повторов (пачка сдвинула список) его не
                        # меняет. Иначе блок из 30+ переопубликованных старых
                        # строк над новыми завершал DEEP, и новые строки под
                        # ним терялись.
                        if _page_already_processed(
                            raw_results, self._seen,
                            published_before=processed_before_date, seen_at_or_before=processed_before_time,
                        ):
                            horizon_run += 1
                        else:
                            horizon_run = 0
                    if horizon_run > self._fast_lookahead_pages:
                        # DEEP дошёл до уже обработанных строк — цикл завершён
                        # честно, а не упором в лимит страниц.
                        completed = True
                        break
                elif quiet_run > self._fast_lookahead_pages:
                    # Тихая страница и K тихих за ней: ниже — только уже
                    # обработанное. Страница из одних повторов (пачка
                    # сдвинула список) счёт не меняет.
                    completed = True
                    break
            else:
                completed = False
        except FineProviderError as exc:
            self._record_error(str(exc), at=run_at)
            logger.exception("GeorgiaPublicFineFeedJob: ошибка обращения к police.ge")
            return
        except Exception as exc:  # любая иная ошибка тоже не должна "засчитать" цикл
            self._record_error(str(exc), at=run_at)
            logger.exception("GeorgiaPublicFineFeedJob: неожиданная ошибка")
            return

        counters.new_rows = len(marked_this_cycle)
        if not completed:
            lookahead_run = horizon_run if deep_mode else quiet_run
            message = _safety_cap_message(
                max_pages, is_true_first_run=is_true_first_run, deep_mode=deep_mode,
                lookahead_pages_read=lookahead_run - 1 if lookahead_run else None,
                lookahead_pages=self._fast_lookahead_pages,
            )
            logger.warning(
                "GeorgiaPublicFineFeedJob: %s (matched=%d this attempt, pages=%d, %s)",
                message, matched_count, pages_fetched, counters.describe(),
            )
            self._record_error(message, at=run_at)
            return

        if newest_seen_this_cycle is not None:
            self._feed_state.record_successful_cycle(
                newest_seen_publish_date=newest_seen_this_cycle, polled_at=run_at,
            )
        else:
            # Пустая страница на offset=0 (ничего нового на всём feed'е с
            # прошлого раза, включая саму первую страницу) — всё ещё
            # УСПЕШНЫЙ цикл, просто нет свежей даты для наблюдаемости.
            self._feed_state.record_successful_cycle(
                newest_seen_publish_date=feed_state.newest_seen_publish_date or run_at.date(),
                polled_at=run_at,
            )
        removed = self._seen.cleanup_older_than(run_at.date() - timedelta(days=self._retention_days))
        self.status.last_success_at = run_at
        logger.info(
            "GeorgiaPublicFineFeedJob finished: pages=%d, matched=%d, deep_mode=%s, "
            "retention_removed=%d, %s",
            pages_fetched, matched_count, deep_mode, removed, counters.describe(),
        )

    def _build_monitored_plate_map(self) -> dict[str, list[FineMonitoringTask]]:
        """car_number (УЖЕ нормализован — см. normalize_car_number()
        докстрок, хранится нормализованным везде в проекте) -> список
        задач этого физического номера (см. задачу "MATCH ALGORITHM":
        "load monitored physical plates from DB once"; "multiple
        subscriptions for same physical plate" — см. production
        O528HY702, 2 активные задачи одного номера)."""
        monitored: dict[str, list[FineMonitoringTask]] = {}
        for task in self._task_repository.list_active():
            monitored.setdefault(task.car_number, []).append(task)
        return monitored

    async def _maybe_process_entry(
        self, entry: dict, monitored: dict[str, list[FineMonitoringTask]],
    ) -> int:
        """True-путь ("matched") — ЕДИНСТВЕННОЕ место, где сырая запись
        feed'а превращается в ParsedFineRecord (см. задачу "DATA
        MINIMIZATION": несовпавшая строка не парсится и не логируется
        индивидуально вовсе — только мгновенное сравнение нормализованного
        protocolAuto со множеством в памяти; её protocolNo/publishDate уже
        отмечены в georgia_public_feed_seen вызывающим кодом ДО этого
        вызова — больше ничего из неё не сохраняется). Возвращает 1, если
        строка была обработана (совпала хотя бы с одной задачей), иначе 0."""
        raw_plate = entry.get("protocolAuto")
        if not raw_plate:
            return 0

        try:
            normalized_plate = normalize_car_number(raw_plate)
        except FineValidationError:
            # Не наш формат (или мусор) — отбрасываем молча, тот же приём,
            # что и везде в проекте для некорректного пользовательского
            # ввода, здесь — для некорректной сторонней записи.
            return 0

        tasks = monitored.get(normalized_plate)
        if not tasks:
            return 0

        try:
            record = parse_published_protocol_entry(entry)
        except FineParseError:
            logger.exception(
                "GeorgiaPublicFineFeedJob: не удалось разобрать совпавшую запись feed'а"
            )
            return 0

        # Fan-out (см. задачу "MATCHED ROW" + аудит п.11: "multiple
        # subscriptions for same physical plate... correct fan-out
        # semantics") — ОДНА уже полученная запись, persist ОТДЕЛЬНО для
        # КАЖДОЙ задачи этого физического номера (тот же принцип, что и у
        # FineCheckService.check_plate_for_tasks для обычного поиска).
        for task in tasks:
            await self._check_service.persist_feed_match(task, record)

        return 1

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
    """DEEP-горизонт (см. run()): КАЖДАЯ строка страницы опубликована
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


def _incomplete_page_message(first_result: int, rows: int, reported_count: int) -> str:
    """Пустая или неполная страница до data.count — сбой источника, а не
    конец списка (см. проверку в цикле страниц). Без номеров протоколов."""
    page = "an empty page" if rows == 0 else f"{rows} of {_PAGE_SIZE} rows"
    return (
        f"police.ge returned {page} at offset {first_result}, although the list reports "
        f"{reported_count} rows; not taken as the end of the list — the next cycle runs deep recovery"
    )


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
    lookahead_pages_read — лимит наступил во время look-ahead (после первой
    тихой страницы у FAST, после первой уже обработанной у DEEP): граница не
    подтверждена, это тоже не успех."""
    lookahead = f"look-ahead: {lookahead_pages_read} of {lookahead_pages} pages read"
    if is_true_first_run:
        if lookahead_pages_read is not None:
            return (
                f"first-run catch-up: safety cap of {max_pages} pages reached during the look-ahead "
                f"after rows published before today ({lookahead}); retrying in deep mode next cycle "
                f"(raise {_FIRST_RUN_PAGES_SETTING} if this repeats)"
            )
        return (
            f"first-run catch-up: safety cap of {max_pages} pages reached before rows published "
            f"before today; retrying in deep mode next cycle (raise {_FIRST_RUN_PAGES_SETTING} "
            "if this repeats)"
        )
    if deep_mode:
        if lookahead_pages_read is not None:
            return (
                "recovery reached rows covered by the last successful cycle, but the safety cap of "
                f"{max_pages} pages came during the look-ahead ({lookahead}); it keeps failing until "
                f"{_PAGES_PER_CYCLE_SETTING} is raised"
            )
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
