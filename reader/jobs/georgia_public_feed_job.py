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
(REDNET)" + её интеграционный review): run()'s DEEP/FAST frontier+one-
overlap-page state machine below is DUPLICATED (not imported) by
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
задачу "DATA MINIMIZATION"). Обычный цикл идёт newest-first со страницы 0;
как только страница содержит ХОТЯ БЫ ОДНУ уже знакомую (seen) запись —
это установленная "граница" с прошлого цикла — он ЗАПРАШИВАЕТ РОВНО ОДНУ
ДОПОЛНИТЕЛЬНУЮ ("overlap") страницу (см. "One final safety adjustment
before merge": защита от сдвига страниц/порядка — newest-first поведение
проверено вживую, но НЕ доказана идеальная долгосрочная стабильность
порядка/backfill), полностью её обрабатывает (её новые строки — НЕ
отбрасываются) и только затем останавливается — без цепочки дальнейших
overlap'ов, если только overlap-страница не оказалась целиком новой (см.
"FAST" ниже). Поэтому стоимость цикла — O(число НОВЫХ записей с прошлого
раза) + 1 страница, а НЕ O(глубина backlog'а за день). Примеры (см.
задачу):
  - граница на странице 1 (ничего нового с прошлого раза) -> 2 запроса
    (сама страница + overlap).
  - 30 новых, граница на странице 2 -> overlap на странице 3 -> 3 запроса.
  - 75 новых, граница на странице 3 -> overlap на странице 4 -> 4 запроса.

РАСПИСАНИЕ (см. задачу "schedule Georgia public feed three times daily" +
корректирующую задачу "IMPORTANT SCHEDULE UPDATE"): ЧЕТЫРЕ ФИКСИРОВАННЫХ
момента суток в сутки (по умолчанию 09:00/15:00/21:00/23:55 Asia/Tbilisi,
см. settings.fine_monitor.public_feed_times/timezone), а НЕ "каждые N
минут от момента запуска" — см. should_run() докстрок. 23:55, а НЕ 00:00,
— намеренно: остаётся частью ТОГО ЖЕ грузинского календарного дня, а не
следующего, и служит end-of-day sweep'ом, подбирающим публикации между
обычным циклом 21:00 и концом дня (минимизирует риск оставить сегодняшние
публикации необработанными на ночь). Типичный (ничего не накопилось сверх
одной страницы) цикл — frontier-страница + 1 overlap-страница = ТИПИЧНЫЙ
baseline ≈ 4 цикла × 2 запроса ≈ 8 запросов/день — НЕ гарантия: если между
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
  Завершается успешно на пустой странице ИЛИ на странице, ВСЕ строки
  которой уже обработаны (DEEP-горизонт, см. _page_already_processed):
  при истинном первом запуске — опубликованы раньше сегодняшнего дня; при
  восстановлении — опубликованы раньше даты самой новой строки последнего
  успешного цикла (но не позже его собственного дня) ЛИБО впервые увидены
  этим циклом или раньше (first_seen_at <= его run_at; строки, отмеченные
  сбойными или прерванными попытками после него, не считаются — "дыра"
  под ними будет дочитана). Дойти до пустой страницы реального feed'а
  (~10 тыс. страниц, ~5–6,3 тыс. публикаций в сутки) невозможно — без
  горизонта DEEP не завершался никогда. Упёрся в max_pages раньше
  горизонта — по-прежнему остаётся в DEEP-режиме и на следующем цикле
  (см. задачу: "Do not silently declare catch-up complete"), но не
  начинает заново с первой страницы: восстановление (или FAST), упёршееся в
  лимит, записывает, докуда дошло (recovery_resume_offset/_at, см.
  _ResumePoint), а следующее, дочитав сверху новые строки до строк той
  попытки, перескакивает к месту её остановки, убеждается, что попало в её
  строки (иначе шаг назад по странице — страница строк, обработанных
  горизонтом, сама по себе ничего не доказывает), и продолжает. Простой
  дольше лимита одного цикла (~1,5 суток при 300 страницах) догоняется за
  несколько циклов в том же лимите, без пропусков.
- "FAST" (обычный установившийся режим, после хотя бы одного успешного
  DEEP-прохода И без свежей ошибки) — останавливается на ВТОРОЙ странице,
  содержащей уже известную (seen) запись, увиденную ДО этого цикла (обычно
  это сразу следующая, overlap-страница; см. выше про защиту от сдвига
  страниц). Строки, уже отмеченные этим же циклом, границей не считаются:
  они появляются снова, когда новые публикации сдвигают страницы во время
  обхода (в production — пачки по 70–360 строк), и граница на них — как и
  страница, целиком занятая такой пачкой сразу после границы, — оставила бы
  новые строки под ними непрочитанными навсегда.

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
    parse_published_protocol_entry,
)
from reader.fines.police_ge_session import PoliceGeSession
from reader.fines.provider import FineProviderError
from reader.fines.public_feed_seen_repository import GeorgiaPublicFeedSeenRepository
from reader.fines.public_feed_state_repository import (
    GeorgiaPublicFeedStateRepository,
    PublicFeedState,
)
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.fines.validation import FineValidationError, normalize_car_number
from reader.jobs.base import Job

logger = logging.getLogger(__name__)

_PAGE_SIZE = 30

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
    ):
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

        # Продолжение восстановления, которое прошлый цикл не уложил в лимит
        # страниц (простой дольше ~1,5 суток: отставание больше лимита одного
        # цикла): без него каждая попытка начинала бы с первой страницы и,
        # пока сверху прибавляются новые публикации, не дошла бы до горизонта
        # никогда. См. _ResumePoint.
        resume = (
            _resume_point(feed_state, last_success=processed_before_time, run_at=run_at)
            if deep_mode and processed_before_time is not None else None
        )

        matched_count = 0
        pages_fetched = 0
        newest_seen_this_cycle: date | None = None
        oldest_seen_this_cycle: date | None = None
        completed = False
        # "One page of overlap after frontier" (см. корректирующую задачу
        # "One final safety adjustment before merge") — после страницы, на
        # которой НАЙДЕНА установленная граница (page_has_seen_row), FAST-
        # режим не останавливается немедленно: следующие страницы тоже
        # полностью обрабатываются (их новые строки — НЕ отбрасываются), и
        # цикл останавливается на ВТОРОЙ странице со строкой, увиденной до
        # этого цикла, — обычно это сразу следующая (overlap) страница. Если
        # пачка публикаций сдвинула список сразу после границы и следующая
        # страница целиком новая, обход продолжается, иначе самые старые
        # строки пачки остались бы непрочитанными навсегда.
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
                    logger.exception("GeorgiaPublicFineFeedJob: некорректный формат ответа feed'а")
                    self._record_error(str(exc), at=run_at)
                    return

                if not raw_results and not aligning:
                    # Пустая страница — конец релевантного backlog'а,
                    # достигнутый ДО overlap'а или ВО ВРЕМЯ него (см. задачу:
                    # "do not make unnecessary requests beyond an empty
                    # result") — в обоих случаях останавливаемся немедленно.
                    # После перескока (aligning) — не конец, а перелёт за
                    # конец feed'а: см. выравнивание ниже.
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

                    # Впервые видим эту конкретную запись feed'а (неважно,
                    # совпадёт ли она с отслеживаемым номером, и неважно,
                    # является ли эта страница overlap'ом — см. задачу:
                    # "All unseen rows found on the overlap page must be
                    # handled normally") — отмечаем ДО попытки сопоставления,
                    # см. GeorgiaPublicFeedSeenRepository докстрок.
                    self._seen.mark_seen(
                        protocol_no, publish_date=entry_date or run_at.date(), seen_at=run_at,
                    )
                    marked_this_cycle.add(protocol_no)
                    matched_count += await self._maybe_process_entry(entry, monitored)

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
                            "GeorgiaPublicFineFeedJob: recovery aligned at offset %d after jumping to %d",
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
                    # DEEP дошёл до уже обработанных строк (см. DEEP-горизонт) —
                    # цикл завершён честно, а не упором в лимит страниц.
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
                    # Установленная граница с прошлого цикла найдена — не
                    # останавливаемся немедленно (см. задачу про защиту от
                    # сдвига страниц/порядка), а запрашиваем ещё страницу.
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
                                "GeorgiaPublicFineFeedJob: recovery resumes — jump from offset %d to %d "
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
            logger.exception("GeorgiaPublicFineFeedJob: ошибка обращения к police.ge")
            return
        except Exception as exc:  # любая иная ошибка тоже не должна "засчитать" цикл
            self._record_error(str(exc), at=run_at)
            logger.exception("GeorgiaPublicFineFeedJob: неожиданная ошибка")
            return

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
            logger.warning(
                "GeorgiaPublicFineFeedJob: %s (matched=%d this attempt)", message, matched_count,
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
            "retention_removed=%d",
            pages_fetched, matched_count, deep_mode, removed,
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
