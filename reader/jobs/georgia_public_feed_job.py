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
overlap'ов. Поэтому стоимость цикла — O(число НОВЫХ записей с прошлого
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
  Завершается успешно ТОЛЬКО на пустой странице; упёрся в max_pages —
  остаётся в DEEP-режиме и на следующем цикле (см. задачу: "Do not
  silently declare catch-up complete").
- "FAST" (обычный установившийся режим, после хотя бы одного успешного
  DEEP-прохода И без свежей ошибки) — останавливается через ОДНУ
  дополнительную (overlap) страницу ПОСЛЕ первой страницы, содержащей уже
  известную (seen) запись (см. выше про защиту от сдвига страниц).

ВОССТАНОВЛЕНИЕ ПОСЛЕ СБОЯ (см. задачу "ATOMICITY": "mid-pagination
failure does not cause data loss"): ЛЮБОЕ исключение в середине цикла
выставляет last_error (см. GeorgiaPublicFeedStateRepository) — пока он не
сброшен, СЛЕДУЮЩИЙ цикл автоматически работает в DEEP-режиме (не доверяя
FAST-границе, которая могла не покрыть то, что сбойный цикл не успел
обработать), пока не пройдёт полностью до пустой страницы. Строки,
реально обработанные ДО сбоя (mark_seen/persist_feed_match — обе
операции идемпотентны и коммитятся по одной), не теряются и не
переобрабатываются как ошибка — detected_fines fingerprint dedup и
georgia_public_feed_seen PRIMARY KEY делают повторную обработку безопасной.

protocolNo НЕ считается глобально монотонным (см. audit report, риск №5)
— обход никогда не полагается на его порядок, только на seen/unseen per
protocolNo и на пустоту страницы."""

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
from reader.fines.public_feed_state_repository import GeorgiaPublicFeedStateRepository
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.fines.validation import FineValidationError, normalize_car_number
from reader.jobs.base import Job

logger = logging.getLogger(__name__)

_PAGE_SIZE = 30


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

        matched_count = 0
        pages_fetched = 0
        newest_seen_this_cycle: date | None = None
        completed = False
        # "One page of overlap after frontier" (см. корректирующую задачу
        # "One final safety adjustment before merge") — после страницы, на
        # которой НАЙДЕНА установленная граница (page_has_seen_row), FAST-
        # режим не останавливается немедленно: сначала запрашивается РОВНО
        # ОДНА дополнительная страница (на случай небольшого сдвига
        # порядка/страницы, см. докстрок класса), она тоже полностью
        # обрабатывается (её новые строки — НЕ отбрасываются), и ТОЛЬКО
        # после неё цикл останавливается — без цепочки дальнейших overlap'ов
        # и без повторной проверки page_has_seen_row на ней самой.
        pending_overlap = False

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

                if not raw_results:
                    # Пустая страница — конец релевантного backlog'а,
                    # достигнутый ДО overlap'а или ВО ВРЕМЯ него (см. задачу:
                    # "do not make unnecessary requests beyond an empty
                    # result") — в обоих случаях останавливаемся немедленно.
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

                    # Впервые видим эту конкретную запись feed'а (неважно,
                    # совпадёт ли она с отслеживаемым номером, и неважно,
                    # является ли эта страница overlap'ом — см. задачу:
                    # "All unseen rows found on the overlap page must be
                    # handled normally") — отмечаем ДО попытки сопоставления,
                    # см. GeorgiaPublicFeedSeenRepository докстрок.
                    self._seen.mark_seen(
                        protocol_no, publish_date=entry_date or run_at.date(),
                    )
                    matched_count += await self._maybe_process_entry(entry, monitored)

                if pending_overlap:
                    # Это и была единственная overlap-страница — хватит,
                    # независимо от того, что на ней нашлось (см. задачу:
                    # никакой цепочки дальнейших overlap'ов).
                    completed = True
                    break

                if not deep_mode and page_has_seen_row:
                    # Установленная граница с прошлого цикла найдена — не
                    # останавливаемся немедленно (см. задачу про защиту от
                    # сдвига страниц/порядка), а запрашиваем ровно ОДНУ
                    # дополнительную страницу на следующей итерации.
                    pending_overlap = True
                    continue
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

        if not completed:
            logger.warning(
                "GeorgiaPublicFineFeedJob: reached safety cap (%d pages, deep_mode=%s) before "
                "reaching an empty page — %s scan INCOMPLETE, will retry in deep mode next cycle "
                "(matched=%d this attempt)",
                max_pages, deep_mode, "first-run catch-up" if is_true_first_run else "recovery",
                matched_count,
            )
            self._record_error(
                "safety cap reached before an empty page (see WARNING log)", at=run_at,
            )
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
