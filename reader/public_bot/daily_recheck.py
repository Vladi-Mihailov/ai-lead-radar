"""Ежедневная вечерняя перепроверка известных штрафов Грузии (см. задачу
"восстановить ежедневную перепроверку существующих штрафов через поиск
автомобиля на police.ge").

Плановый сбор REDNET ищет только НОВЫЕ публикации; уже известные штрафы до
этого перепроверялись только вручную, поэтому исчезновение оплаченного
штрафа из базы полиции бот замечал с опозданием на недели. Здесь — один
проход в день по номерам, у которых есть штрафы в базе полиции (🔴/🟡/🟢 и
те, что не найдены один раз и ждут подтверждения), через тот же
FineCheckService.check_plate_for_tasks(), что и "🔄 Проверить авто со
штрафами": тот же searchByAuto через REDNET (AutoFailoverCheckNowProvider),
тот же false-zero guard, те же detected_fines/статусы/уведомления. Новые
штрафы, найденные по пути, уведомляются как обычно; известные — нет
(дедуп по fingerprint и по квитанции, см. check_service.py/
delivery_service.py).

Бережность к police.ge и к REDNET (1 vCPU / ~1 ГБ):
- окно [daily_recheck_time, daily_recheck_cutoff_time) по Asia/Tbilisi —
  по умолчанию 23:00–23:45, между циклами коллектора 21:00 и 23:55;
- строго последовательно: пауза между номерами, длинная пауза после каждой
  пачки, не больше daily_recheck_max_plates номеров за проход;
- ответ police.ge 403/429 или открытый circuit breaker REDNET — остановка
  на сегодня; daily_recheck_max_consecutive_errors ошибок подряд — тоже;
- один массовый проход на процесс (BatchCheckGate) — с ручным "🔄
  Проверить авто" и "fine check-all" не пересекается.

Прогресс — в БД (georgia_daily_recheck_runs/_items): после рестарта
процесса тот же день продолжается с непроверенных номеров, второй запуск
того же дня не начнётся; номера, до которых не дошли (остановка/окно
закончилось), завтра окажутся в начале очереди — сортировка по давности
последней успешной проверки; номера, на которых вчерашний проход упал, —
в конце (два "вечно падающих" номера не останавливают весь проход).
Прогон, оставшийся 'running' после рестарта вне окна, закрывается как
прерванный.

Этот модуль сам номеров в журнал не пишет (только счётчики); проверка
номера идёт через FineCheckService, чья существующая INFO-строка "Georgia
fine check plate=..." пишет номер так же, как при ручной проверке (см.
аудит утечек номеров — меняется отдельным решением).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from reader.fines.batch_gate import BatchCheckGate
from reader.fines.models import CheckResult, FineCarDebtGroup, FineMonitoringTask
from reader.fines.suppression import is_fine_output_suppressed

logger = logging.getLogger(__name__)

RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_STOPPED = "stopped"
RUN_POSTPONED = "postponed"

ITEM_PENDING = "pending"
ITEM_OK = "ok"
ITEM_ERROR = "error"
ITEM_SKIPPED = "skipped"

STOP_BLOCKED = "blocked"
STOP_UNAVAILABLE = "unavailable"
STOP_ERRORS = "errors"
STOP_CUTOFF = "cutoff"
STOP_BUSY = "busy"
STOP_INTERRUPTED = "interrupted"

_GATE_HOLDER = "daily_recheck"
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS georgia_daily_recheck_runs (
        run_date      TEXT PRIMARY KEY,
        status        TEXT NOT NULL,
        started_at    TIMESTAMP NOT NULL,
        heartbeat_at  TIMESTAMP NOT NULL,
        finished_at   TIMESTAMP,
        total         INTEGER NOT NULL DEFAULT 0,
        checked       INTEGER NOT NULL DEFAULT 0,
        failed        INTEGER NOT NULL DEFAULT 0,
        stop_reason   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS georgia_daily_recheck_items (
        run_date    TEXT NOT NULL,
        position    INTEGER NOT NULL,
        car_number  TEXT NOT NULL,
        state       TEXT NOT NULL DEFAULT 'pending',
        checked_at  TIMESTAMP,
        PRIMARY KEY (run_date, car_number)
    )
    """,
)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _fmt(value: datetime) -> str:
    return _naive_utc(value).strftime(_TIMESTAMP_FORMAT)


def _parse_ts(value: str | None) -> datetime | None:
    return _naive_utc(datetime.fromisoformat(value)) if value else None


@dataclass(frozen=True)
class DailyRecheckRun:
    run_date: date
    status: str
    started_at: datetime
    heartbeat_at: datetime
    finished_at: datetime | None
    total: int
    checked: int
    failed: int
    stop_reason: str | None


def _row_to_run(row) -> DailyRecheckRun:
    return DailyRecheckRun(
        run_date=date.fromisoformat(row[0]),
        status=row[1],
        started_at=_parse_ts(row[2]),
        heartbeat_at=_parse_ts(row[3]),
        finished_at=_parse_ts(row[4]),
        total=row[5],
        checked=row[6],
        failed=row[7],
        stop_reason=row[8],
    )


_RUN_FIELDS = "run_date, status, started_at, heartbeat_at, finished_at, total, checked, failed, stop_reason"


class DailyRecheckRepository:
    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        for statement in _SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get_run(self, run_date: date) -> DailyRecheckRun | None:
        row = self._conn.execute(
            f"SELECT {_RUN_FIELDS} FROM georgia_daily_recheck_runs WHERE run_date = ?", (run_date.isoformat(),),
        ).fetchone()
        return _row_to_run(row) if row else None

    def latest_run(self) -> DailyRecheckRun | None:
        row = self._conn.execute(
            f"SELECT {_RUN_FIELDS} FROM georgia_daily_recheck_runs ORDER BY run_date DESC LIMIT 1",
        ).fetchone()
        return _row_to_run(row) if row else None

    def create_run(self, run_date: date, plates: list[str], *, now: datetime) -> bool:
        """Новый прогон дня вместе со списком номеров — одной транзакцией.
        False — прогон этого дня уже есть (PRIMARY KEY run_date): второй
        запуск того же задания не начнётся даже при гонке."""
        status = RUN_RUNNING if plates else RUN_COMPLETED
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO georgia_daily_recheck_runs "
                    "(run_date, status, started_at, heartbeat_at, finished_at, total) VALUES (?, ?, ?, ?, ?, ?)",
                    (run_date.isoformat(), status, _fmt(now), _fmt(now), None if plates else _fmt(now), len(plates)),
                )
                self._conn.executemany(
                    "INSERT INTO georgia_daily_recheck_items (run_date, position, car_number) VALUES (?, ?, ?)",
                    [(run_date.isoformat(), position, car) for position, car in enumerate(plates)],
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def take_over_stale_run(self, run_date: date, *, now: datetime, stale_after: timedelta) -> bool:
        """Прогон дня остался 'running' без живого исполнителя (процесс
        перезапустили посреди прохода) — продолжить его можно, только если
        heartbeat старше stale_after. Обновление условное: второй
        претендент проиграет."""
        threshold = _fmt(_naive_utc(now) - stale_after)
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE georgia_daily_recheck_runs SET heartbeat_at = ? "
                "WHERE run_date = ? AND status = ? AND heartbeat_at <= ?",
                (_fmt(now), run_date.isoformat(), RUN_RUNNING, threshold),
            )
        return cursor.rowcount == 1

    def close_stale_running(self, *, now: datetime, stale_after: timedelta, keep_run_date: date | None) -> int:
        """Прогоны, оставшиеся 'running' без живого исполнителя (процесс
        перезапустили, а окно уже закончилось), — 'postponed'/interrupted,
        чтобы статистика не показывала "идёт" до следующего вечера.
        keep_run_date — сегодняшний прогон во время окна не трогаем: его
        продолжает take_over_stale_run."""
        threshold = _fmt(_naive_utc(now) - stale_after)
        query = (
            "UPDATE georgia_daily_recheck_runs SET status = ?, stop_reason = ?, finished_at = ? "
            "WHERE status = ? AND heartbeat_at <= ?"
        )
        params: list = [RUN_POSTPONED, STOP_INTERRUPTED, _fmt(now), RUN_RUNNING, threshold]
        if keep_run_date is not None:
            query += " AND run_date != ?"
            params.append(keep_run_date.isoformat())
        with self._conn:
            cursor = self._conn.execute(query, params)
        return cursor.rowcount

    def failed_plates_of_previous_run(self, before: date) -> set[str]:
        """Номера, на которых упал последний прогон ДО before."""
        row = self._conn.execute(
            "SELECT run_date FROM georgia_daily_recheck_runs WHERE run_date < ? ORDER BY run_date DESC LIMIT 1",
            (before.isoformat(),),
        ).fetchone()
        if row is None:
            return set()
        rows = self._conn.execute(
            "SELECT car_number FROM georgia_daily_recheck_items WHERE run_date = ? AND state = ?",
            (row[0], ITEM_ERROR),
        ).fetchall()
        return {r[0] for r in rows}

    def heartbeat(self, run_date: date, *, now: datetime) -> None:
        self._conn.execute(
            "UPDATE georgia_daily_recheck_runs SET heartbeat_at = ? WHERE run_date = ?",
            (_fmt(now), run_date.isoformat()),
        )
        self._conn.commit()

    def pending_plates(self, run_date: date) -> list[str]:
        rows = self._conn.execute(
            "SELECT car_number FROM georgia_daily_recheck_items WHERE run_date = ? AND state = ? ORDER BY position",
            (run_date.isoformat(), ITEM_PENDING),
        ).fetchall()
        return [row[0] for row in rows]

    def mark_item(self, run_date: date, car_number: str, *, state: str, now: datetime) -> None:
        counter = {ITEM_OK: "checked", ITEM_ERROR: "failed"}.get(state)
        with self._conn:
            self._conn.execute(
                "UPDATE georgia_daily_recheck_items SET state = ?, checked_at = ? "
                "WHERE run_date = ? AND car_number = ?",
                (state, _fmt(now), run_date.isoformat(), car_number),
            )
            if counter is not None:
                self._conn.execute(
                    f"UPDATE georgia_daily_recheck_runs SET {counter} = {counter} + 1, heartbeat_at = ? "
                    "WHERE run_date = ?",
                    (_fmt(now), run_date.isoformat()),
                )

    def finish_run(self, run_date: date, *, status: str, now: datetime, stop_reason: str | None = None) -> None:
        self._conn.execute(
            "UPDATE georgia_daily_recheck_runs SET status = ?, finished_at = ?, heartbeat_at = ?, stop_reason = ? "
            "WHERE run_date = ?",
            (status, _fmt(now), _fmt(now), stop_reason, run_date.isoformat()),
        )
        self._conn.commit()


class _TaskRepositoryLike(Protocol):
    def list_debt_car_groups(self) -> list[FineCarDebtGroup]: ...

    def list_by_car_number(self, car_number: str) -> list[FineMonitoringTask]: ...


class _CheckServiceLike(Protocol):
    async def check_plate_for_tasks(
        self, car_number: str, tasks: list[FineMonitoringTask], *, refresh_page: bool = False,
    ) -> dict[int, CheckResult]: ...


class _StatusTrackerLike(Protocol):
    def listed_car_numbers(self) -> list[str]: ...

    def recently_gone_car_numbers(self) -> list[str]: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DailyRecheckService:
    def __init__(
        self,
        repository: DailyRecheckRepository,
        task_repository: _TaskRepositoryLike,
        check_service: _CheckServiceLike,
        status_tracker: _StatusTrackerLike,
        *,
        gate: BatchCheckGate,
        tz: ZoneInfo,
        start_time: time = time(23, 0),
        cutoff_time: time = time(23, 45),
        inter_car_delay_seconds: float = 8.0,
        batch_size: int = 10,
        batch_pause_seconds: float = 60.0,
        max_plates: int = 200,
        max_consecutive_errors: int = 2,
        busy_wait_seconds: float = 30.0,
        stale_after: timedelta = timedelta(minutes=5),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = _utc_now,
    ):
        if cutoff_time <= start_time:
            raise ValueError("daily_recheck_cutoff_time должен быть позже daily_recheck_time (в пределах одних суток)")
        if batch_size < 1 or max_plates < 1 or max_consecutive_errors < 1:
            raise ValueError("daily_recheck_batch_size/max_plates/max_consecutive_errors должны быть >= 1")
        self._repository = repository
        self._tasks = task_repository
        self._check_service = check_service
        self._statuses = status_tracker
        self._gate = gate
        self._tz = tz
        self._start_time = start_time
        self._cutoff_time = cutoff_time
        self._inter_car_delay_seconds = inter_car_delay_seconds
        self._batch_size = batch_size
        self._batch_pause_seconds = batch_pause_seconds
        self._max_plates = max_plates
        self._max_consecutive_errors = max_consecutive_errors
        self._busy_wait_seconds = busy_wait_seconds
        self._stale_after = stale_after
        self._sleep = sleep
        self._clock = clock
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def latest_run(self) -> DailyRecheckRun | None:
        return self._repository.latest_run()

    def in_window(self, now: datetime) -> bool:
        local_time = now.astimezone(self._tz).time()
        return self._start_time <= local_time < self._cutoff_time

    def select_plates(self, *, today: date | None = None) -> list[str]:
        """Номера с известными штрафами в базе полиции: положительный снимок
        долга (как у "🔄 Проверить авто со штрафами"), номера с не-⚪
        статусами (в т.ч. найденные feed'ом после последней проверки и
        оплаченные, но ещё видимые на police.ge) и номера, у которых штраф
        стал ⚪ в последние дни (ложное ⚪ сменится на "снова найден").
        Сначала — те, что дольше всего не проверялись; номера, на которых
        упал вчерашний проход, — в конце."""
        last_checked: dict[str, datetime | None] = {}
        for group in self._tasks.list_debt_car_groups():
            last_checked[group.car_number] = _naive_utc(group.checked_at)
        for car_number in [*self._statuses.listed_car_numbers(), *self._statuses.recently_gone_car_numbers()]:
            if car_number in last_checked:
                continue
            stamps = [
                _naive_utc(task.last_successful_checked_at)
                for task in self._tasks.list_by_car_number(car_number)
                if task.last_successful_checked_at is not None
            ]
            last_checked[car_number] = max(stamps) if stamps else None
        failed_yesterday = self._repository.failed_plates_of_previous_run(
            today or self._clock().astimezone(self._tz).date(),
        )
        plates = [car for car in last_checked if not is_fine_output_suppressed(car)]
        plates.sort(key=lambda car: (car in failed_yesterday, last_checked[car] or datetime.min, car))
        return plates[: self._max_plates]

    def _tasks_for_plate(self, car_number: str) -> list[FineMonitoringTask]:
        """Активные задачи номера и задачи с положительным снимком долга
        (как у "🔄 Проверить авто со штрафами": иначе снимок неактивной
        задачи так и остался бы старым и держал машину в статистике). Если
        таких нет — одна самая свежая задача с известным снимком, иначе
        самая новая."""
        tasks = self._tasks.list_by_car_number(car_number)
        selected = [
            task for task in tasks
            if task.status == "active" or (task.last_successful_total_amount or 0) > 0
        ]
        if selected:
            return selected
        with_snapshot = [task for task in tasks if task.last_successful_checked_at is not None]
        if with_snapshot:
            return [max(with_snapshot, key=lambda task: (_naive_utc(task.last_successful_checked_at), task.id))]
        return [max(tasks, key=lambda task: task.id)] if tasks else []

    async def tick(self, now: datetime | None = None) -> DailyRecheckRun | None:
        """Вызывается фоновым циклом раз в ~30 с. Вне окна и после
        завершённого/остановленного прогона дня — ничего не делает (вторая
        попытка в тот же день не начинается)."""
        now = now or self._clock()
        if self._running:
            return None
        in_window = self.in_window(now)
        run_date = now.astimezone(self._tz).date()
        if self._repository.close_stale_running(
            now=now, stale_after=self._stale_after, keep_run_date=run_date if in_window else None,
        ):
            logger.info("Georgia daily recheck: an interrupted run was closed")
        if not in_window:
            return None
        existing = self._repository.get_run(run_date)
        if existing is None:
            plates = self.select_plates(today=run_date)
            if not self._repository.create_run(run_date, plates, now=now):
                return None
            if not plates:
                logger.info("Georgia daily recheck: nothing to check (run_date=%s)", run_date)
                return self._repository.get_run(run_date)
            logger.info("Georgia daily recheck started: run_date=%s plates=%d", run_date, len(plates))
        elif existing.status == RUN_RUNNING:
            if not self._repository.take_over_stale_run(run_date, now=now, stale_after=self._stale_after):
                return None
            logger.info(
                "Georgia daily recheck resumed after interruption: run_date=%s pending=%d",
                run_date, len(self._repository.pending_plates(run_date)),
            )
        else:
            return None
        return await self._execute(run_date)

    async def _execute(self, run_date: date) -> DailyRecheckRun | None:
        self._running = True
        acquired = False
        try:
            while not self._gate.try_acquire(_GATE_HOLDER):
                # Идёт ручной массовый проход — ждём его окончания, но не
                # дольше окна (police.ge не получает двух проходов сразу).
                now = self._clock()
                if not self.in_window(now):
                    return self._finish(run_date, RUN_POSTPONED, STOP_BUSY)
                self._repository.heartbeat(run_date, now=now)
                await self._sleep(self._busy_wait_seconds)
            acquired = True

            consecutive_errors = 0
            since_pause = 0
            first = True
            for car_number in self._repository.pending_plates(run_date):
                if not first:
                    if since_pause >= self._batch_size:
                        await self._sleep(self._batch_pause_seconds)
                        since_pause = 0
                    else:
                        await self._sleep(self._inter_car_delay_seconds)
                if not self.in_window(self._clock()):
                    return self._finish(run_date, RUN_POSTPONED, STOP_CUTOFF)

                tasks = self._tasks_for_plate(car_number)
                if not tasks:
                    self._repository.mark_item(run_date, car_number, state=ITEM_SKIPPED, now=self._clock())
                    continue

                # Первый номер прохода заодно перечитывает страницу police.ge
                # (дата "Date of update of information") — один GET в день.
                try:
                    results = await self._check_service.check_plate_for_tasks(
                        car_number, tasks, refresh_page=first,
                    )
                except Exception:
                    logger.exception("Georgia daily recheck: unexpected error while checking a plate")
                    results = None
                first = False
                since_pause += 1

                state, stop_reason = self._classify(results)
                self._repository.mark_item(run_date, car_number, state=state, now=self._clock())
                if stop_reason is not None:
                    return self._finish(run_date, RUN_STOPPED, stop_reason)
                if state == ITEM_ERROR:
                    consecutive_errors += 1
                    if consecutive_errors >= self._max_consecutive_errors:
                        return self._finish(run_date, RUN_STOPPED, STOP_ERRORS)
                else:
                    consecutive_errors = 0

            return self._finish(run_date, RUN_COMPLETED, None)
        finally:
            if acquired:
                self._gate.release(_GATE_HOLDER)
            self._running = False

    @staticmethod
    def _classify(results: dict[int, CheckResult] | None) -> tuple[str, str | None]:
        """(состояние позиции, причина остановки прохода или None)."""
        if results is None:
            return ITEM_ERROR, None
        values = list(results.values())
        errors = [result for result in values if result.status == "error"]
        if errors:
            if any(result.provider_blocked for result in errors):
                return ITEM_ERROR, STOP_BLOCKED
            if any(result.provider_unavailable for result in errors):
                return ITEM_ERROR, STOP_UNAVAILABLE
            return ITEM_ERROR, None
        if values and all(result.status == "suppressed" for result in values):
            return ITEM_SKIPPED, None
        return ITEM_OK, None

    def _finish(self, run_date: date, status: str, stop_reason: str | None) -> DailyRecheckRun | None:
        self._repository.finish_run(run_date, status=status, now=self._clock(), stop_reason=stop_reason)
        run = self._repository.get_run(run_date)
        if run is not None:
            log = logger.warning if status == RUN_STOPPED else logger.info
            log(
                "Georgia daily recheck %s: run_date=%s checked=%d failed=%d total=%d reason=%s",
                status, run_date, run.checked, run.failed, run.total, stop_reason,
            )
        return run


async def run_daily_recheck_loop(service: DailyRecheckService, *, poll_interval_seconds: float = 30.0) -> None:
    """Фоновый цикл процесса бота (см. reader/public_bot/main.py) — ошибка
    одного тика не останавливает цикл."""
    while True:
        try:
            await service.tick()
        except Exception:
            logger.exception("Georgia daily recheck: тик завершился с ошибкой")
        await asyncio.sleep(poll_interval_seconds)
