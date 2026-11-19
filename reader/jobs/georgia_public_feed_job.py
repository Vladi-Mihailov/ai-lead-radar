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

ОБХОД СПИСКА (режимы FAST/DEEP, DEEP-горизонт, восстановление в несколько
циклов после долгого простоя) — reader/fines/public_feed_traversal.py::
PublicFeedTraversal, общий с REDNET-коллектором (reader/georgia_collector/
collector_job.py): раньше этот код был продублирован построчно и
синхронизировался вручную. Здесь — только то, что делается с новой
строкой (сопоставление с отслеживаемыми номерами и запись совпадения, см.
_maybe_process_entry), расписание и статистика запусков.

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
  которой уже обработаны (DEEP-горизонт, см. page_already_processed):
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
  начинает заново с первой страницы: прочитанное записывается участками
  (номера первых и последних строк и их число, см. public_feed_traversal),
  и следующее восстановление, дочитав сверху новые строки до начала
  участка, перескакивает к его концу, находит его по номерам и продолжает
  подряд. Простой дольше лимита одного цикла догоняется за несколько
  циклов в том же лимите, без пропусков.
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
first_seen_at), а восстановление после долгого простоя — на точные номера
первых и последних строк прочитанных участков."""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from reader.fines.check_service import FineCheckService
from reader.fines.models import FineMonitoringTask
from reader.fines.parser import FineParseError, parse_published_protocol_entry
from reader.fines.police_ge_session import PoliceGeSession
from reader.fines.public_feed_seen_repository import GeorgiaPublicFeedSeenRepository
from reader.fines.public_feed_state_repository import GeorgiaPublicFeedStateRepository
from reader.fines.public_feed_traversal import PublicFeedTraversal
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.fines.validation import FineValidationError, normalize_car_number
from reader.jobs.base import Job

logger = logging.getLogger(__name__)

# Настройки лимита страниц, которые называют сообщения об упоре в лимит —
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
        self._traversal = PublicFeedTraversal(
            session, feed_state_repository, seen_repository,
            tz=tz, max_pages_per_cycle=max_pages_per_cycle, first_run_max_pages=first_run_max_pages,
            retention_days=retention_days, pages_per_cycle_setting=_PAGES_PER_CYCLE_SETTING,
            first_run_pages_setting=_FIRST_RUN_PAGES_SETTING, log=logger, log_name="GeorgiaPublicFineFeedJob",
        )
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
        matched_count = 0

        async def on_new_row(entry: dict) -> None:
            nonlocal matched_count
            matched_count += await self._maybe_process_entry(entry, monitored)

        outcome = await self._traversal.run(run_at, on_new_row)
        if outcome.completed:
            self.status.last_success_at = run_at
            logger.info(
                "GeorgiaPublicFineFeedJob finished: pages=%d, matched=%d, mode=%s",
                outcome.pages, matched_count, outcome.mode,
            )
            return
        self.status.error_count += 1
        self.status.last_error = outcome.detail
        self.status.last_error_at = run_at
        logger.warning(
            "GeorgiaPublicFineFeedJob: cycle not completed (%s, matched=%d this attempt)",
            outcome.status, matched_count,
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
