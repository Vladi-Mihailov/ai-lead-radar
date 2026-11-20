"""
Тесты GeorgiaPublicFineFeedJob (см. задачу "Georgia fines incremental
public-feed monitoring", ИСПРАВЛЕНО корректирующей задачей "IMPORTANT
CORRECTION BEFORE COMMIT / PR" — исходная date-overlap версия
пересматривала весь предыдущий день на КАЖДОМ 5-минутном цикле, ~6600
запросов/день вместо ожидаемых ~288/день).

Главная проверяемая идея: обычный (FAST) цикл останавливается на первой
"тихой" странице (ни одной новой строки, хотя бы одна уже знакомая по
georgia_public_feed_seen) и FAST_LOOKAHEAD_PAGES тихих страницах за ней —
стоимость O(число новых записей) + 1 + K страниц, а не O(глубина backlog'а
за день).

_FakeFeedSession — фейковая замена PoliceGeSession: отдаёт заранее
заготовленные страницы по firstResult, фиксирует запрошенные offset'ы.
FineMonitoringTaskRepository/DetectedFineRepository/
GeorgiaPublicFeedStateRepository/GeorgiaPublicFeedSeenRepository —
настоящие (SQLite, tmp_path).
"""

import asyncio
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import ParsedFineRecord  # noqa: E402
from reader.fines.provider import FineProvider, FineProviderError  # noqa: E402
from reader.fines.public_feed_seen_repository import (  # noqa: E402
    GeorgiaPublicFeedSeenRepository,
)
from reader.fines.public_feed_state_repository import (  # noqa: E402
    GeorgiaPublicFeedStateRepository,
)
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.jobs.georgia_public_feed_job import (  # noqa: E402
    FAST_LOOKAHEAD_PAGES,
    GeorgiaPublicFineFeedJob,
)

# Async-тесты запускаются через asyncio_mode = auto (pytest.ini): общей
# asyncio-метки модуля нет — из-за неё предупреждали синхронные тесты.

_CHAT_ID = -100999
_USER_ID = 111
_RUN_AT = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
_TODAY = date(2026, 10, 22)
_TBILISI = ZoneInfo("Asia/Tbilisi")
_DEFAULT_RUN_TIMES = [dt_time(9, 0), dt_time(15, 0), dt_time(21, 0), dt_time(23, 55)]


class _FakeCircuitProvider(FineProvider):
    """ТОЛЬКО для FineCheckService.provider_available() (см. job.run() —
    circuit breaker gate ПЕРЕД единым feed-запросом). search_by_plate()
    здесь никогда не должен вызываться самим job'ом."""

    def __init__(self, *, available: bool = True):
        self._available = available
        self.search_by_plate_calls: list[str] = []

    def is_available(self) -> bool:
        return self._available

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        self.search_by_plate_calls.append(plate)
        return []


class _FakeFeedSession:
    def __init__(self, pages_by_offset: dict[int, dict] | None = None, *, errors: dict[int, Exception] | None = None):
        self._pages = pages_by_offset or {}
        self._errors = errors or {}
        self.requested_offsets: list[int] = []

    async def fetch_published_protocols(self, *, first_result: int) -> dict:
        self.requested_offsets.append(first_result)
        if first_result in self._errors:
            raise self._errors[first_result]
        return self._pages.get(first_result, _empty_page())


def _empty_page() -> dict:
    return {"success": True, "data": {"count": 0, "results": []}}


def _entry(
    *, protocol_no="AB000001", plate="AA001AA", publish_date="2026-10-22",
    violation_date="2026-10-22", amount=50,
) -> dict:
    return {
        "protocolAuto": plate,
        "protocolNo": protocol_no,
        "protocolDate": "2026-10-22",
        "activeDate": None,
        "violationDate": violation_date,
        "protocolPlace": "ქუჩა",
        "protocolLaw": "125-1",
        "protocolLawDescription": "აღწერა",
        "protocolAmount": amount,
        "publishDate": publish_date,
        "lastDate": "2026-12-21",
        "remainingDays": 60,
    }


def _page(entries: list[dict], *, count: int | None = None) -> dict:
    return {"success": True, "data": {"count": count if count is not None else len(entries), "results": entries}}


def _make_task_repo(tmp_path) -> FineMonitoringTaskRepository:
    return FineMonitoringTaskRepository(tmp_path / "users.db")


def _make_fine_repo(tmp_path) -> DetectedFineRepository:
    return DetectedFineRepository(tmp_path / "users.db")


def _make_feed_state_repo(tmp_path) -> GeorgiaPublicFeedStateRepository:
    return GeorgiaPublicFeedStateRepository(tmp_path / "users.db")


def _make_seen_repo(tmp_path) -> GeorgiaPublicFeedSeenRepository:
    return GeorgiaPublicFeedSeenRepository(tmp_path / "users.db")


def _make_job(
    session, task_repo, fine_repo, feed_state_repo, seen_repo, *, provider_available=True,
    max_pages_per_cycle=30, first_run_max_pages=100, retention_days=7,
    run_times=None, tz=_TBILISI,
) -> tuple[GeorgiaPublicFineFeedJob, _FakeCircuitProvider]:
    """run_times/tz — только для схемы "schedule Georgia public feed three
    times daily" (см. GeorgiaPublicFineFeedJob.should_run()); тесты
    пагинации/frontier/overlap/dedup ниже вызывают run() напрямую и никогда
    не проходят через should_run(), поэтому значение по умолчанию здесь
    (09:00/15:00/21:00/23:55 Asia/Tbilisi) для них не имеет значения —
    только тесты в разделе "schedule" ниже реально на него опираются."""
    provider = _FakeCircuitProvider(available=provider_available)
    check_service = FineCheckService(provider, task_repo, fine_repo)
    job = GeorgiaPublicFineFeedJob(
        session, task_repo, check_service, feed_state_repo, seen_repo,
        run_times=run_times if run_times is not None else _DEFAULT_RUN_TIMES, tz=tz,
        max_pages_per_cycle=max_pages_per_cycle, first_run_max_pages=first_run_max_pages,
        retention_days=retention_days,
    )
    return job, provider


def _create_active_task(task_repo, car_number: str, *, telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID):
    return task_repo.create(
        car_number=car_number, label=None,
        start_date=date(2026, 10, 1), end_date=date(2026, 11, 1),
        telegram_chat_id=telegram_chat_id, created_by_user_id=created_by_user_id,
    )


def _establish_fast_mode(feed_state_repo, *, at=_RUN_AT - timedelta(minutes=5)):
    """Эмулирует "уже был хотя бы один успешный цикл раньше" — без этого
    job всегда работает в DEEP-режиме (см. _choose mode в run())."""
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=_TODAY, polled_at=at)


def _seed_seen(seen_repo, protocol_nos: list[str], *, publish_date=_TODAY):
    for protocol_no in protocol_nos:
        seen_repo.mark_seen(protocol_no, publish_date=publish_date)


# ==== circuit breaker gate ====


async def test_run_skips_cycle_entirely_when_circuit_breaker_open(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        session = _FakeFeedSession()
        job, _provider = _make_job(
            session, task_repo, fine_repo, feed_state_repo, seen_repo, provider_available=False,
        )

        await job.run(_RUN_AT)

        assert session.requested_offsets == []
        assert feed_state_repo.get().last_successful_poll_at is None
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


# ==== THE core fix: normal cycle is O(new rows), not O(backlog depth) ====


async def test_frontier_on_page1_then_a_quiet_page_and_two_lookahead_pages(tmp_path):
    """Было (тест 1/2 корректирующей задачи "One final safety adjustment
    before merge"): граница на первой странице -> РОВНО ОДНА overlap-
    страница -> 2 запроса. Теперь (FAST_LOOKAHEAD_PAGES): граница на первой
    странице -> тихая страница (целиком уже известная) -> ещё 2 тихие
    страницы look-ahead -> 4 запроса."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        _establish_fast_mode(feed_state_repo)

        known_entries = [_entry(protocol_no=f"KNOWN{i:03d}", plate=f"ZZ{i:03d}ZZ") for i in range(27)]
        for e in known_entries:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        new_entries = [
            _entry(protocol_no="NEW001", plate="AA001AA"),
            _entry(protocol_no="NEW002", plate="ZZ900ZZ"),
            _entry(protocol_no="NEW003", plate="ZZ901ZZ"),
        ]
        page0 = _page(new_entries + known_entries)  # новые впереди — newest-first
        overlap_entries = [_entry(protocol_no=f"OLDER{i:03d}", plate=f"XX{i:03d}XX") for i in range(90)]
        for e in overlap_entries:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        session = _FakeFeedSession({
            0: page0,
            30: _page(overlap_entries[:30]),
            60: _page(overlap_entries[30:60]),
            90: _page(overlap_entries[60:]),
        })
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_RUN_AT)

        assert session.requested_offsets == [0, 30, 60, 90]
        assert len(fine_repo.list_by_car_number("AA001AA")) == 1  # NEW001 совпал
        assert seen_repo.is_seen("NEW001") is True
        assert seen_repo.is_seen("NEW002") is True
        assert seen_repo.is_seen("NEW003") is True
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_frontier_on_page1_unseen_item_on_overlap_page_is_processed(tmp_path):
    """Тест 1/4 корректирующей задачи: граница на странице 1, но на
    следующей странице (2) ЕСТЬ ещё одна новая, несовпавшая строка ДО этого,
    и одна СОВПАВШАЯ строка — обе должны быть обработаны нормально, а не
    отброшены только потому, что это страница после границы. Страница 2 с
    новой строкой — не тихая, поэтому FAST читает и третью (здесь пустую:
    конец списка)."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        _establish_fast_mode(feed_state_repo)

        known_entries = [_entry(protocol_no=f"KNOWN{i:03d}", plate=f"ZZ{i:03d}ZZ") for i in range(29)]
        for e in known_entries:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        page0 = _page([_entry(protocol_no="NEW001", plate="BB002BB")] + known_entries)
        overlap_new_matched = _entry(protocol_no="OVERLAP_NEW", plate="AA001AA")
        overlap_known = [_entry(protocol_no=f"OLDER{i:03d}", plate=f"XX{i:03d}XX") for i in range(29)]
        for e in overlap_known:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        session = _FakeFeedSession({0: page0, 30: _page([overlap_new_matched] + overlap_known)})
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_RUN_AT)

        assert session.requested_offsets == [0, 30, 60]
        # Строка с overlap-страницы — обработана нормально (persist через
        # detected_fines, та же логика уведомления, что и обычно).
        assert len(fine_repo.list_by_car_number("AA001AA")) == 1
        assert seen_repo.is_seen("OVERLAP_NEW") is True
        assert seen_repo.is_seen("NEW001") is True
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_30_unseen_then_quiet_page2_and_two_lookahead_pages_four_requests(tmp_path):
    """Тест 2/3 корректирующей задачи, было 3 запроса (граница + overlap);
    теперь тихая страница 2 и ещё 2 тихие страницы look-ahead -> 4."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        _establish_fast_mode(feed_state_repo)

        page0_entries = [_entry(protocol_no=f"NEW{i:03d}", plate=f"ZZ{i:03d}ZZ") for i in range(30)]
        known_entries = [_entry(protocol_no=f"KNOWN{i:03d}", plate=f"YY{i:03d}YY") for i in range(30)]
        for e in known_entries:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        overlap_entries = [_entry(protocol_no=f"OLDER{i:03d}", plate=f"XX{i:03d}XX") for i in range(60)]
        for e in overlap_entries:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        session = _FakeFeedSession({
            0: _page(page0_entries), 30: _page(known_entries),
            60: _page(overlap_entries[:30]), 90: _page(overlap_entries[30:]),
        })
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_RUN_AT)

        assert session.requested_offsets == [0, 30, 60, 90]
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_empty_overlap_page_does_not_trigger_a_further_request(tmp_path):
    """Тест 5 корректирующей задачи: граница на странице 1, overlap-
    страница (2) ПУСТАЯ (не просто "вся известная", а реально конец
    backlog'а) -> не должно быть никакого третьего запроса (см. задачу:
    "do not make unnecessary requests beyond an empty result")."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        _establish_fast_mode(feed_state_repo)
        known_entries = [_entry(protocol_no=f"KNOWN{i:03d}", plate=f"ZZ{i:03d}ZZ") for i in range(27)]
        for e in known_entries:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        new_entries = [_entry(protocol_no="NEW1", plate="AA001AA")]
        session = _FakeFeedSession({0: _page(new_entries + known_entries), 30: _empty_page()})
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_RUN_AT)

        assert session.requested_offsets == [0, 30]  # НЕ [0, 30, 60]
        assert len(fine_repo.list_by_car_number("AA001AA")) == 1
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_31_unseen_correct_second_page_traversal(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        _establish_fast_mode(feed_state_repo)

        page0_entries = [_entry(protocol_no=f"NEW{i:03d}", plate=f"ZZ{i:03d}ZZ") for i in range(30)]
        page1_new = _entry(protocol_no="NEW030", plate="AA001AA")
        known_entries = [_entry(protocol_no=f"KNOWN{i:03d}", plate=f"YY{i:03d}YY") for i in range(29)]
        for e in known_entries:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        session = _FakeFeedSession({
            0: _page(page0_entries),
            30: _page([page1_new] + known_entries),
            60: _empty_page(),
        })
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_RUN_AT)

        assert session.requested_offsets == [0, 30, 60]
        assert len(fine_repo.list_by_car_number("AA001AA")) == 1
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_75_unseen_frontier_on_page3_quiet_page4_and_lookahead_six_requests(tmp_path):
    """Тест 4 корректирующей задачи (новые строки до страницы 3 -> граница
    на странице 3, см. "IMPORTANT CORRECTION" burst example): было overlap
    на странице 4 -> 4 запроса; теперь тихая страница 4 и ещё 2 тихие
    страницы look-ahead -> 6 запросов."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        _establish_fast_mode(feed_state_repo)

        page0 = [_entry(protocol_no=f"NEW0{i:03d}", plate=f"ZZ{i:03d}ZZ") for i in range(30)]
        page1 = [_entry(protocol_no=f"NEW1{i:03d}", plate=f"YY{i:03d}YY") for i in range(30)]
        page2_new = _entry(protocol_no="NEW2000", plate="AA001AA")
        known = [_entry(protocol_no=f"KNOWN{i:03d}", plate=f"XX{i:03d}XX") for i in range(29)]
        for e in known:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        overlap_entries = [_entry(protocol_no=f"OLDER{i:03d}", plate=f"WW{i:03d}WW") for i in range(90)]
        for e in overlap_entries:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        session = _FakeFeedSession({
            0: _page(page0), 30: _page(page1), 60: _page([page2_new] + known),
            90: _page(overlap_entries[:30]), 120: _page(overlap_entries[30:60]),
            150: _page(overlap_entries[60:]),
        })
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_RUN_AT)

        assert session.requested_offsets == [0, 30, 60, 90, 120, 150]
        assert len(fine_repo.list_by_car_number("AA001AA")) == 1
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_no_24h_rescan_on_typical_normal_cycle(tmp_path):
    """Прямая проверка исправленной request-volume проблемы: ТИПИЧНЫЙ
    цикл (2-3 новых штрафа, наблюдаемая скорость ~670/день, см. audit
    report) — РОВНО 2 запроса (страница + один overlap, см. "One final
    safety adjustment before merge"), НЕ десятки страниц "целого дня назад"."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        _establish_fast_mode(feed_state_repo)
        known_entries = [_entry(protocol_no=f"KNOWN{i:03d}", plate=f"ZZ{i:03d}ZZ") for i in range(28)]
        for e in known_entries:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        new_entries = [_entry(protocol_no="NEW1"), _entry(protocol_no="NEW2")]
        session = _FakeFeedSession({0: _page(new_entries + known_entries)})
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_RUN_AT)

        assert len(session.requested_offsets) == 2
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


# ==== data minimization ====


async def test_unmatched_rows_store_only_protocol_no_and_publish_date(tmp_path):
    """Тест 5 корректирующей задачи."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")  # никогда не совпадёт с нижеследующими
        session = _FakeFeedSession({0: _page([
            _entry(protocol_no="UNRELATED1", plate="ZZ001ZZ"),
        ]), 30: _empty_page()})
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_RUN_AT)

        assert seen_repo.is_seen("UNRELATED1") is True
        assert fine_repo.list_by_car_number("ZZ001ZZ") == []  # никаких деталей штрафа не сохранено
        assert fine_repo.list_by_car_number("AA001AA") == []
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


# ==== restart / dedup ====


async def test_restart_uses_persisted_seen_ids(tmp_path):
    """Тест 6 корректирующей задачи — НОВЫЙ экземпляр job'а (эмуляция
    restart), та же БД (seen_repo/feed_state_repo переживают restart)."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        entry = _entry(protocol_no="AB000001", plate="AA001AA")
        session1 = _FakeFeedSession({0: _page([entry]), 30: _empty_page()})
        job1, _p1 = _make_job(session1, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job1.run(_RUN_AT)  # first run (deep mode) -> completes -> FAST mode established
        assert len(fine_repo.list_by_car_number("AA001AA")) == 1

        # "Restart" — абсолютно новый job/session, ТЕ ЖЕ repo-объекты
        # (тот же файл БД) — уже знает про AB000001 благодаря seen_repo.
        session2 = _FakeFeedSession({0: _page([entry])})
        job2, _p2 = _make_job(session2, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job2.run(_RUN_AT + timedelta(minutes=5))

        # Граница найдена сразу (page0) -> РОВНО одна overlap-страница
        # (page1, пустая по умолчанию у _FakeFeedSession) -> стоп.
        assert session2.requested_offsets == [0, 30]
        assert len(fine_repo.list_by_car_number("AA001AA")) == 1  # не задвоено
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_duplicate_protocol_no_never_processed_twice(tmp_path):
    """Тест 7 корректирующей задачи."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        _establish_fast_mode(feed_state_repo)
        entry = _entry(protocol_no="AB000001", plate="AA001AA")
        # Одна и та же запись "случайно" попадает дважды в один ответ
        # (гипотетическая защита от нестабильной выдачи источника).
        session = _FakeFeedSession({0: _page([entry, entry])})
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_RUN_AT)

        assert len(fine_repo.list_by_car_number("AA001AA")) == 1
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_page_boundary_shift_does_not_lose_a_fine(tmp_path):
    """Тест 8 корректирующей задачи — между циклами новые строки сдвигают
    offset'ы (classic page-boundary shift), identity (protocolNo) решает
    всё, а не позиция внутри страницы."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        old_entries = [_entry(protocol_no=f"OLD{i:03d}", plate=f"ZZ{i:03d}ZZ") for i in range(30)]
        session1 = _FakeFeedSession({0: _page(old_entries), 30: _empty_page()})
        job1, _p1 = _make_job(session1, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job1.run(_RUN_AT)  # first run -> FAST mode established, all OLD* seen

        # Следующий цикл: ОДНА новая запись (наш номер) встаёт В НАЧАЛО —
        # OLD-записи сдвигаются на одну позицию глубже (page-boundary shift).
        new_entry = _entry(protocol_no="NEW001", plate="AA001AA")
        session2 = _FakeFeedSession({0: _page([new_entry] + old_entries[:29])})
        job2, _p2 = _make_job(session2, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job2.run(_RUN_AT + timedelta(minutes=5))

        assert len(fine_repo.list_by_car_number("AA001AA")) == 1  # новая запись НЕ потеряна
        # Граница найдена сразу (page0 содержит OLD-записи) -> + 1 overlap.
        assert session2.requested_offsets == [0, 30]
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


# ==== failure recovery ====


async def test_mid_pagination_failure_does_not_cause_data_loss(tmp_path):
    """Тест 9 корректирующей задачи — сбой на странице 1 НЕ должен
    "похоронить" ненайденные ещё строки навсегда: следующий цикл уходит в
    DEEP (recovery) режим, пока не дойдёт до пустой страницы."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        _establish_fast_mode(feed_state_repo)

        page0 = [_entry(protocol_no=f"P0_{i:03d}", plate=f"ZZ{i:03d}ZZ") for i in range(30)]
        page1_matched = _entry(protocol_no="MISSED001", plate="AA001AA")
        session1 = _FakeFeedSession(
            {0: _page(page0)}, errors={30: FineProviderError("ConnectTimeout")},
        )
        job1, _p1 = _make_job(session1, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job1.run(_RUN_AT)

        assert fine_repo.list_by_car_number("AA001AA") == []  # ещё не дошли до page1
        assert feed_state_repo.get().last_error is not None  # recovery-режим активирован

        # Следующий цикл (recovery/DEEP) — ДОЛЖЕН дойти до page1 и дальше,
        # игнорируя seen-границу (та неполна после сбоя).
        session2 = _FakeFeedSession({
            0: _page(page0),  # page0 снова отдаётся (уже seen -> не задвоится)
            30: _page([page1_matched]),
            60: _empty_page(),
        })
        job2, _p2 = _make_job(session2, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job2.run(_RUN_AT + timedelta(minutes=5))

        assert len(fine_repo.list_by_car_number("AA001AA")) == 1  # найден, не потерян
        assert feed_state_repo.get().last_error is None  # recovery завершён успешно
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


# ==== retention ====


async def test_retention_cleanup_runs_after_successful_cycle_without_losing_frontier(tmp_path):
    """Тест 10 корректирующей задачи — cleanup не должен портить
    возможность находить границу на следующем же цикле."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        _establish_fast_mode(feed_state_repo)
        known_entries = [_entry(protocol_no=f"KNOWN{i:03d}", plate=f"ZZ{i:03d}ZZ") for i in range(30)]
        for e in known_entries:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        session1 = _FakeFeedSession({0: _page(known_entries)})
        job1, _p1 = _make_job(session1, task_repo, fine_repo, feed_state_repo, seen_repo, retention_days=7)
        await job1.run(_RUN_AT)

        # Следующий цикл сразу после cleanup — граница (ЭТИ ЖЕ сегодняшние
        # записи) всё ещё должна распознаваться как "уже известная".
        session2 = _FakeFeedSession({0: _page(known_entries)})
        job2, _p2 = _make_job(session2, task_repo, fine_repo, feed_state_repo, seen_repo, retention_days=7)
        await job2.run(_RUN_AT + timedelta(minutes=5))

        assert session2.requested_offsets == [0, 30]  # граница сразу + 1 overlap
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


# ==== first-run catch-up stays separate from normal polling ====


async def test_first_run_deep_catch_up_remains_separate_from_normal_polling(tmp_path):
    """Тест 11 корректирующей задачи."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        assert feed_state_repo.get().last_successful_poll_at is None  # истинный первый запуск

        deep_entries_p0 = [_entry(protocol_no=f"DEEP0_{i:03d}", plate=f"ZZ{i:03d}ZZ") for i in range(30)]
        deep_entries_p1 = [_entry(protocol_no="MATCHED001", plate="AA001AA")]
        session1 = _FakeFeedSession({0: _page(deep_entries_p0), 30: _page(deep_entries_p1), 60: _empty_page()})
        job1, _p1 = _make_job(session1, task_repo, fine_repo, feed_state_repo, seen_repo, first_run_max_pages=10)

        await job1.run(_RUN_AT)

        # Первый запуск ИГНОРИРУЕТ seen-границу (её ещё нет) — идёт до
        # пустой страницы, а не останавливается на "странице с известными".
        assert session1.requested_offsets == [0, 30, 60]
        assert len(fine_repo.list_by_car_number("AA001AA")) == 1
        assert feed_state_repo.get().last_successful_poll_at == _RUN_AT

        # СЛЕДУЮЩИЙ цикл — уже FAST, не "ещё один первый запуск".
        session2 = _FakeFeedSession({0: _page(deep_entries_p0)})  # все уже seen
        job2, _p2 = _make_job(session2, task_repo, fine_repo, feed_state_repo, seen_repo, first_run_max_pages=10)
        await job2.run(_RUN_AT + timedelta(minutes=5))

        # FAST-режим: граница сразу на page0 + РОВНО 1 overlap — НЕ
        # [0, 30, 60] снова (это был бы ещё один "первый запуск").
        assert session2.requested_offsets == [0, 30]
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_first_run_incomplete_within_cap_stays_in_deep_mode(tmp_path, caplog):
    import logging

    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        # Каждая страница непустая -> пустая страница никогда не наступает
        # в пределах first_run_max_pages=3 (лимит 2 при 2 страницах
        # look-ahead больше не допускается).
        pages = {
            offset: _page([_entry(protocol_no=f"P{offset}_{i}") for i in range(30)]) for offset in (0, 30, 60)
        }
        session = _FakeFeedSession(pages)
        job, _provider = _make_job(
            session, task_repo, fine_repo, feed_state_repo, seen_repo, first_run_max_pages=3,
        )

        with caplog.at_level(logging.WARNING, logger="reader.jobs.georgia_public_feed_job"):
            await job.run(_RUN_AT)

        state = feed_state_repo.get()
        assert state.last_successful_poll_at is None  # первый запуск ещё НЕ завершён
        assert state.last_error is not None
        assert any("safety cap" in r.getMessage() for r in caplog.records)
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


# ==== structural / unchanged-from-before coverage ====


async def test_job_never_calls_search_by_plate(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        session = _FakeFeedSession({0: _page([_entry(plate="AA001AA")]), 30: _empty_page()})
        job, provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_RUN_AT)

        assert provider.search_by_plate_calls == []
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_multiple_tasks_for_same_plate_each_get_persisted(tmp_path):
    """Аудит п.11 / задача "MATCHED ROW": fan-out — ОДИН feed-запрос, но
    КАЖДАЯ задача physical plate получает свою собственную запись."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        task_a = _create_active_task(task_repo, "AA001AA", telegram_chat_id=10, created_by_user_id=10)
        task_b = _create_active_task(task_repo, "AA001AA", telegram_chat_id=20, created_by_user_id=20)
        session = _FakeFeedSession({0: _page([_entry(protocol_no="AB000001", plate="AA001AA")]), 30: _empty_page()})
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_RUN_AT)

        fingerprint = fine_repo.list_by_car_number("AA001AA")[0].fingerprint
        fine_a = fine_repo.get_by_fingerprint(task_a.id, fingerprint)
        fine_b = fine_repo.get_by_fingerprint(task_b.id, fingerprint)
        assert fine_a is not None
        assert fine_b is not None
        assert fine_a.id != fine_b.id
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


# ==== schedule: 09:00/15:00/21:00/23:55 Asia/Tbilisi (см. задачу "schedule
# Georgia public feed three times daily" + корректирующую задачу
# "IMPORTANT SCHEDULE UPDATE": добавлен 23:55 как намеренный end-of-day
# sweep, НЕ 00:00 — 23:55 остаётся частью ТОГО ЖЕ грузинского календарного
# дня) ====
#
# Asia/Tbilisi не наблюдает DST (UTC+4 круглый год) — 09:00/15:00/21:00/
# 23:55 local соответствуют 05:00/11:00/17:00/19:55 UTC, используется ниже
# вместо datetime(..., tzinfo=_TBILISI) только чтобы явно проверить, что
# should_run() делает конвертацию сам (now приходит в UTC, как и из
# Scheduler.run_forever(), см. reader/jobs/scheduler.py), а не полагается
# на то, что now уже в нужной таймзоне.


def _utc(hour: int, minute: int = 0, *, day: int = 22) -> datetime:
    """UTC-момент, соответствующий заданному local-времени Asia/Tbilisi
    (UTC+4, без DST) в день _TODAY (или +N дней, см. day)."""
    local = datetime(2026, 10, day, hour, minute, tzinfo=_TBILISI)
    return local.astimezone(timezone.utc)


@pytest.mark.parametrize("hour,minute", [(9, 0), (15, 0), (21, 0), (23, 55)])
async def test_should_run_true_at_each_scheduled_slot(tmp_path, hour, minute):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        job, _provider = _make_job(_FakeFeedSession(), task_repo, fine_repo, feed_state_repo, seen_repo)
        assert await job.should_run(_utc(hour, minute)) is True
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


@pytest.mark.parametrize("hour", [0, 3, 6, 12, 18])
async def test_should_run_false_outside_scheduled_slots(tmp_path, hour):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        job, _provider = _make_job(_FakeFeedSession(), task_repo, fine_repo, feed_state_repo, seen_repo)
        assert await job.should_run(_utc(hour)) is False
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_should_run_after_23_55_next_slot_is_09_00_next_day(tmp_path):
    """23:55 — последний слот дня (намеренный end-of-day sweep, см.
    задачу "IMPORTANT SCHEDULE UPDATE"), НЕ 00:00 — должен остаться частью
    ТОГО ЖЕ грузинского календарного дня, а следующий запуск — ровно 09:00
    следующего дня, а не 00:00 того же вызова _utc(..., day=23)."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        job, _provider = _make_job(_FakeFeedSession(), task_repo, fine_repo, feed_state_repo, seen_repo)

        # Обычный дневной цикл 21:00 срабатывает штатно...
        assert await job.should_run(_utc(21)) is True
        await job.run(_utc(21))

        # ...и 23:55 того же дня — тоже штатный, отдельный от 21:00 слот.
        assert await job.should_run(_utc(23, 55)) is True
        await job.run(_utc(23, 55))

        # Никаких запусков после 23:55 того же дня — ни на следующей
        # минуте опроса Scheduler'а (та же минута 23:55 — защита от
        # двойного срабатывания), ни на 00:00/03:00/06:00/12:00/18:00
        # следующего дня.
        assert await job.should_run(_utc(23, 55)) is False
        for hour in (0, 3, 6, 12, 18):
            assert await job.should_run(_utc(hour, day=23)) is False

        # Следующий запуск — ровно 09:00 следующего дня.
        assert await job.should_run(_utc(9, day=23)) is True
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_should_run_false_mid_day_after_restart_does_not_fire_unsolicited_run(tmp_path):
    """Рестарт сервиса в 13:00 (между 09:00 и 15:00) не должен сам по себе
    запускать public feed — см. задачу "STARTUP": "Обычный restart сервиса
    НЕ должен сам по себе запускать public feed вне 09:00/15:00/21:00/23:55".
    Новый процесс создаёт НОВЫЙ GeorgiaPublicFineFeedJob с
    `_last_run_slot=None` (см. should_run() докстрок) — это ровно
    воспроизводится здесь свежим job'ом, которому ни разу ещё не задавали
    should_run()."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        fresh_job_after_restart, _provider = _make_job(
            _FakeFeedSession(), task_repo, fine_repo, feed_state_repo, seen_repo,
        )
        assert await fresh_job_after_restart.should_run(_utc(13)) is False
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_missed_slot_does_not_trigger_per_car_fallback_next_scheduled_run_catches_up(tmp_path):
    """Пропущенный слот (процесс был выключен в 21:00) НЕ порождает
    отдельный per-car catch-up — см. задачу "DOWNTIME": "не запускать
    отдельный per-car catch-up storm". should_run() просто никогда не
    вернул True для пропущенной минуты (эмулируется тем, что мы его для
    неё и не вызываем); при СЛЕДУЮЩЕМ обычном запуске (09:00 следующего
    дня) backlog по-прежнему безопасно дочитывается существующей DEEP/
    frontier-логикой run() — ни одного обращения к search_by_plate()."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        # Запись опубликована в день запуска (23.10 по Тбилиси): более старая
        # страница завершила бы DEEP первого запуска сразу (DEEP-горизонт).
        session = _FakeFeedSession({
            0: _page([_entry(plate="AA001AA", publish_date="2026-10-23")]), 30: _empty_page(),
        })
        job, provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        # 21:00 слот "пропущен" — should_run() для него никогда не
        # вызывается (процесс был выключен). Следующий опрос Scheduler'а,
        # который job реально видит, — 09:00 следующего дня.
        assert await job.should_run(_utc(9, day=23)) is True
        await job.run(_utc(9, day=23))

        assert provider.search_by_plate_calls == []
        assert session.requested_offsets == [0, 30]
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


# ==== DEEP-горизонт: первый запуск и восстановление завершаются без пустой страницы ====


def _rows(prefix: str, count: int, *, publish_date: str) -> list[dict]:
    return [
        _entry(protocol_no=f"{prefix}{i:03d}", plate=f"ZZ{i:03d}ZZ", publish_date=publish_date)
        for i in range(count)
    ]


def _mark_seen_at(seen_repo, rows: list[dict], *, seen_at: datetime) -> None:
    for row in rows:
        seen_repo.mark_seen(
            row["protocolNo"], publish_date=date.fromisoformat(row["publishDate"]), seen_at=seen_at,
        )


def _spy_persisted(job) -> list[str]:
    """external_fine_id каждой записи, переданной в persist_feed_match —
    "обработана ровно один раз" нельзя проверить по detected_fines, их
    fingerprint dedup скрыл бы повторную обработку."""
    persisted: list[str] = []
    original = job._check_service.persist_feed_match

    async def spy(task, record):
        persisted.append(record.external_fine_id)
        return await original(task, record)

    job._check_service.persist_feed_match = spy
    return persisted


async def test_first_run_completes_at_first_page_older_than_today_then_fast(tmp_path):
    """До пустой страницы реального feed'а не дойти (~10 тыс. страниц) —
    первый запуск читает только сегодняшние публикации и завершается на
    первой странице, целиком опубликованной раньше сегодняшнего дня, и
    двух страницах look-ahead за ней."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        today_rows = _rows("T0_", 29, publish_date="2026-10-22")
        session = _FakeFeedSession({
            0: _page(today_rows + [_entry(protocol_no="MATCH001", plate="AA001AA", publish_date="2026-10-22")]),
            30: _page(_rows("T1_", 30, publish_date="2026-10-22")),
            60: _page(_rows("Y0_", 30, publish_date="2026-10-21")),
            90: _page(_rows("Y1_", 30, publish_date="2026-10-21")),
            120: _page(_rows("Y2_", 30, publish_date="2026-10-21")),
            150: _page(_rows("Y3_", 30, publish_date="2026-10-21")),
        })
        job, _provider = _make_job(
            session, task_repo, fine_repo, feed_state_repo, seen_repo, first_run_max_pages=10,
        )

        await job.run(_utc(21))

        assert session.requested_offsets == [0, 30, 60, 90, 120]
        assert len(fine_repo.list_by_car_number("AA001AA")) == 1
        state = feed_state_repo.get()
        assert state.last_successful_poll_at == _utc(21)
        assert state.last_error is None

        # Следующий цикл — FAST: граница на page0 + ровно одна overlap-страница.
        new_row = _entry(protocol_no="NEW001", plate="ZZ999ZZ", publish_date="2026-10-22")
        session2 = _FakeFeedSession({0: _page([new_row] + today_rows)})
        job2, _p2 = _make_job(session2, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job2.run(_utc(23, 55))

        assert session2.requested_offsets == [0, 30]
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_first_run_cap_on_todays_rows_is_an_error_and_next_cycle_stays_deep(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        pages = {i * 30: _page(_rows(f"T{i}_", 30, publish_date="2026-10-22")) for i in range(3)}
        pages.update({(3 + i) * 30: _page(_rows(f"Y{i}_", 30, publish_date="2026-10-21")) for i in range(3)})
        session = _FakeFeedSession(pages)
        job, _provider = _make_job(
            session, task_repo, fine_repo, feed_state_repo, seen_repo, first_run_max_pages=3,
        )

        await job.run(_utc(21))

        assert session.requested_offsets == [0, 30, 60]
        state = feed_state_repo.get()
        assert state.last_successful_poll_at is None
        assert "safety cap" in state.last_error

        # Следующий цикл всё ещё DEEP: уже виденные сегодняшние строки его не
        # останавливают (FAST остановился бы на них) — доходит до вчерашних
        # страниц и завершается после первой из них и двух страниц look-ahead.
        session2 = _FakeFeedSession(pages)
        job2, _p2 = _make_job(
            session2, task_repo, fine_repo, feed_state_repo, seen_repo, first_run_max_pages=6,
        )
        await job2.run(_utc(23, 55))

        assert session2.requested_offsets == [0, 30, 60, 90, 120, 150]
        assert feed_state_repo.get().last_error is None
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_recovery_stops_at_rows_seen_before_last_success_of_the_same_day(tmp_path):
    """Строки того же дня, что и последний успех, перечитывать целиком не
    нужно: увиденные ДО него уже обработаны (first_seen_at)."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        feed_state_repo.record_successful_cycle(
            newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15),
        )
        earlier = _rows("E0_", 90, publish_date="2026-10-22")
        _mark_seen_at(seen_repo, earlier, seen_at=_utc(9))
        feed_state_repo.record_error("ConnectTimeout", at=_utc(21))
        session = _FakeFeedSession({
            0: _page(_rows("N0_", 30, publish_date="2026-10-22")),
            30: _page(earlier[:30]),
            60: _page(earlier[30:60]),
            90: _page(earlier[60:]),
            120: _page(_rows("E1_", 30, publish_date="2026-10-22")),
        })
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_utc(23, 55))

        # Три страницы увиденных до успеха строк (одна + 2 look-ahead);
        # остальной день ниже не перечитывается.
        assert session.requested_offsets == [0, 30, 60, 90]
        assert feed_state_repo.get().last_error is None
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_recovery_reads_through_rows_marked_after_last_success_and_fills_the_gap(tmp_path):
    """Сбойная попытка после успеха отметила верхние строки и упала, не
    дойдя до более старых ("дыра"). Восстановление не считает отмеченные
    ею строки обработанными, дочитывает дыру (совпавшая запись обработана
    ровно один раз) и останавливается на строках, увиденных до успеха."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        feed_state_repo.record_successful_cycle(
            newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15),
        )
        base = _rows("B0_", 30, publish_date="2026-10-22")
        _mark_seen_at(seen_repo, base, seen_at=_utc(9))
        top = _rows("T0_", 30, publish_date="2026-10-22")
        gap = _rows("G0_", 29, publish_date="2026-10-22") + [
            _entry(protocol_no="GAP_MATCH", plate="AA001AA", publish_date="2026-10-22"),
        ]

        failing = _FakeFeedSession({0: _page(top)}, errors={30: FineProviderError("ConnectTimeout")})
        job1, _p1 = _make_job(failing, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job1.run(_utc(21))
        assert feed_state_repo.get().last_error is not None
        assert fine_repo.list_by_car_number("AA001AA") == []

        session = _FakeFeedSession({
            0: _page(_rows("N0_", 30, publish_date="2026-10-22")),
            30: _page(top),
            60: _page(gap),
            90: _page(base),
            120: _page(_rows("O0_", 30, publish_date="2026-10-21")),
            150: _page(_rows("O1_", 30, publish_date="2026-10-21")),
        })
        job2, _p2 = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)
        persisted = _spy_persisted(job2)
        await job2.run(_utc(23, 55))

        assert session.requested_offsets == [0, 30, 60, 90, 120, 150]
        assert persisted == ["GAP_MATCH"]
        assert len(fine_repo.list_by_car_number("AA001AA")) == 1
        assert feed_state_repo.get().last_error is None
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_recovery_stops_at_page_older_than_last_success_date_without_seen_rows(tmp_path):
    """Строки старше даты последнего успеха уже обработаны, даже если
    retention успел удалить их из georgia_public_feed_seen."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        feed_state_repo.record_successful_cycle(
            newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(23, 55),
        )
        feed_state_repo.record_error("ConnectTimeout", at=_utc(9, day=23))
        session = _FakeFeedSession({
            0: _page(_rows("N0_", 30, publish_date="2026-10-23")),
            30: _page(_rows("OLD0_", 30, publish_date="2026-10-21")),
            60: _page(_rows("OLD1_", 30, publish_date="2026-10-21")),
            90: _page(_rows("OLD2_", 30, publish_date="2026-10-21")),
        })
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_utc(15, day=23))

        assert session.requested_offsets == [0, 30, 60, 90]
        assert feed_state_repo.get().last_error is None
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_row_without_publish_date_does_not_complete_deep_by_date(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        undated = _entry(protocol_no="NODATE1", plate="ZZ998ZZ", publish_date=None)
        session = _FakeFeedSession({
            0: _page(_rows("Y0_", 29, publish_date="2026-10-21") + [undated]),
            30: _page(_rows("Y1_", 30, publish_date="2026-10-21")),
            60: _page(_rows("Y2_", 30, publish_date="2026-10-21")),
            90: _page(_rows("Y3_", 30, publish_date="2026-10-21")),
        })
        job, _provider = _make_job(
            session, task_repo, fine_repo, feed_state_repo, seen_repo, first_run_max_pages=10,
        )

        await job.run(_utc(21))

        assert session.requested_offsets == [0, 30, 60, 90]
        assert feed_state_repo.get().last_error is None
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_busy_fast_cycle_reaches_the_frontier_under_a_300_page_cap(tmp_path):
    """Самый загруженный цикл (~3,5 тыс. новых строк ≈ 120 страниц): при
    лимите 300 FAST доходит до границы, читает тихую страницу и 2 страницы
    look-ahead (120 + 3), цикл успешен."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _establish_fast_mode(feed_state_repo, at=_utc(15))
        frontier = _rows("SEEN_", 30, publish_date="2026-10-22")
        after = _rows("AFTER_", 30, publish_date="2026-10-22")
        after2 = _rows("AFTER2_", 30, publish_date="2026-10-22")
        _seed_seen(seen_repo, [row["protocolNo"] for row in frontier + after + after2])
        pages = {i * 30: _page(_rows(f"P{i:03d}_", 30, publish_date="2026-10-22")) for i in range(120)}
        pages[120 * 30] = _page(frontier)
        pages[121 * 30] = _page(after)
        pages[122 * 30] = _page(after2)
        session = _FakeFeedSession(pages)
        job, _provider = _make_job(
            session, task_repo, fine_repo, feed_state_repo, seen_repo, max_pages_per_cycle=300,
        )

        await job.run(_utc(21))

        assert len(session.requested_offsets) == 123
        assert feed_state_repo.get().last_error is None
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_recovery_after_two_failed_cycles_fits_the_page_cap(tmp_path):
    """Регрессия "застревания": последний успех поздно вечером 21.10, два
    сбоя 22.10. Только date-горизонт (дата 21.10) требовал бы перечитать
    ВСЕ строки 21.10: 6 + 8 + 1 = 15 страниц > лимита 10 — и так на каждом
    следующем цикле. Строки, увиденные до успеха, завершают восстановление
    на 7-й странице и двух страницах look-ahead — 9 страниц, в пределах
    лимита."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        feed_state_repo.record_successful_cycle(
            newest_seen_publish_date=date(2026, 10, 21), polled_at=_utc(23, 55, day=21),
        )
        yesterday_pages = [_rows(f"Y{i}_", 30, publish_date="2026-10-21") for i in range(8)]
        for rows in yesterday_pages:
            _mark_seen_at(seen_repo, rows, seen_at=_utc(21, day=21))

        for hour in (9, 15):
            failing = _FakeFeedSession(errors={0: FineProviderError("ConnectTimeout")})
            job_failed, _pf = _make_job(
                failing, task_repo, fine_repo, feed_state_repo, seen_repo, max_pages_per_cycle=10,
            )
            await job_failed.run(_utc(hour))
        assert feed_state_repo.get().last_error is not None

        pages = {i * 30: _page(_rows(f"N{i}_", 30, publish_date="2026-10-22")) for i in range(6)}
        for i, rows in enumerate(yesterday_pages):
            pages[(6 + i) * 30] = _page(rows)
        pages[14 * 30] = _page(_rows("D20_", 30, publish_date="2026-10-20"))
        session = _FakeFeedSession(pages)
        job, _provider = _make_job(
            session, task_repo, fine_repo, feed_state_repo, seen_repo, max_pages_per_cycle=10,
        )

        await job.run(_utc(21))

        assert len(session.requested_offsets) == 9
        state = feed_state_repo.get()
        assert state.last_error is None
        assert state.last_successful_poll_at == _utc(21)
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_rows_marked_by_an_earlier_successful_cycle_complete_a_later_recovery(tmp_path):
    """Сквозная проверка first_seen_at = run_at: строки, отмеченные последним
    успешным циклом (first_seen_at == его run_at), завершают последующее
    восстановление; строки более поздней сбойной попытки — нет."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        rows_a = _rows("A0_", 30, publish_date="2026-10-22")
        rows_b = _rows("B0_", 30, publish_date="2026-10-22")
        rows_k = _rows("K0_", 30, publish_date="2026-10-22")
        yesterday = _rows("Y0_", 30, publish_date="2026-10-21")

        first = _FakeFeedSession({0: _page(rows_a), 30: _page(yesterday)})
        job1, _p1 = _make_job(first, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job1.run(_utc(15))
        assert first.requested_offsets == [0, 30, 60]  # look-ahead доходит до конца списка

        second = _FakeFeedSession({0: _page(rows_b), 30: _page(rows_a), 60: _page(yesterday)})
        job2, _p2 = _make_job(second, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job2.run(_utc(21))
        assert second.requested_offsets == [0, 30, 60, 90]  # FAST: тихая страница + look-ahead до пустой

        failing = _FakeFeedSession({0: _page(rows_k)}, errors={30: FineProviderError("ConnectTimeout")})
        job3, _p3 = _make_job(failing, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job3.run(_utc(23, 55))
        assert feed_state_repo.get().last_error is not None

        recovery = _FakeFeedSession({
            0: _page(_rows("C0_", 30, publish_date="2026-10-23")),
            30: _page(rows_k),
            60: _page(rows_b),
            90: _page(rows_a),
            120: _page(yesterday),
        })
        job4, _p4 = _make_job(recovery, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job4.run(_utc(9, day=23))

        # B (отмечены последним успехом) и две страницы look-ahead: A и Y.
        assert recovery.requested_offsets == [0, 30, 60, 90, 120]
        assert feed_state_repo.get().last_error is None
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_interrupted_cycle_is_followed_by_deep_recovery_that_reads_below_the_kill_point(tmp_path):
    """Цикл, убитый посреди обхода (SIGTERM/OOM/CancelledError — мимо всех
    except), оставляет отметку "цикл идёт": следующий цикл — DEEP, а не
    FAST, и дочитывает строки под точкой остановки."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        rows_s = _rows("S0_", 30, publish_date="2026-10-22")
        yesterday = _rows("Y0_", 30, publish_date="2026-10-21")
        first = _FakeFeedSession({0: _page(rows_s), 30: _page(yesterday)})
        job1, _p1 = _make_job(first, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job1.run(_utc(15))
        assert feed_state_repo.get().last_error is None

        k0 = _rows("K0_", 30, publish_date="2026-10-22")
        k1 = _rows("K1_", 30, publish_date="2026-10-22")
        k2 = _rows("K2_", 29, publish_date="2026-10-22") + [
            _entry(protocol_no="LOST_MATCH", plate="AA001AA", publish_date="2026-10-22"),
        ]
        killed = _FakeFeedSession(
            {0: _page(k0), 30: _page(k1), 60: _page(k2), 90: _page(rows_s)},
            errors={30: asyncio.CancelledError()},
        )
        job2, _p2 = _make_job(killed, task_repo, fine_repo, feed_state_repo, seen_repo)
        with pytest.raises(asyncio.CancelledError):
            await job2.run(_utc(21))
        assert feed_state_repo.get().last_error is not None  # отметка "цикл идёт" осталась

        recovery = _FakeFeedSession({
            0: _page(_rows("N0_", 30, publish_date="2026-10-22")),
            30: _page(k0),
            60: _page(k1),
            90: _page(k2),
            120: _page(rows_s),
            150: _page(yesterday),
            180: _page(_rows("Y1_", 30, publish_date="2026-10-21")),
        })
        job3, _p3 = _make_job(recovery, task_repo, fine_repo, feed_state_repo, seen_repo)
        persisted = _spy_persisted(job3)
        await job3.run(_utc(23, 55))

        assert recovery.requested_offsets == [0, 30, 60, 90, 120, 150, 180]
        assert persisted == ["LOST_MATCH"]
        assert feed_state_repo.get().last_error is None
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_recovery_ignores_first_seen_times_when_last_success_is_not_in_the_past(tmp_path, caplog):
    """Успех записан с часами "впереди" (на сутки): по first_seen_at строки
    сбойной попытки и самого восстановления сошли бы за обработанные —
    защита отключает этот критерий, остаётся только дата."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        feed_state_repo.record_successful_cycle(
            newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(21, day=23),
        )
        g0 = _rows("G0_", 30, publish_date="2026-10-22")
        g1 = _rows("G1_", 29, publish_date="2026-10-22") + [
            _entry(protocol_no="SKEW_MATCH", plate="AA001AA", publish_date="2026-10-22"),
        ]
        failing = _FakeFeedSession({0: _page(g0)}, errors={30: FineProviderError("ConnectTimeout")})
        job1, _p1 = _make_job(failing, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job1.run(_utc(21))
        assert feed_state_repo.get().last_error is not None

        recovery = _FakeFeedSession({
            0: _page(_rows("N0_", 30, publish_date="2026-10-22")),
            30: _page(g0),
            60: _page(g1),
            90: _page(_rows("Y0_", 30, publish_date="2026-10-21")),
        })
        job2, _p2 = _make_job(recovery, task_repo, fine_repo, feed_state_repo, seen_repo)
        persisted = _spy_persisted(job2)
        with caplog.at_level(logging.WARNING, logger="reader.jobs.georgia_public_feed_job"):
            await job2.run(_utc(23, 55))

        # Только по дате обработана лишь Y0: look-ahead доходит до конца списка.
        assert recovery.requested_offsets == [0, 30, 60, 90, 120]
        assert persisted == ["SKEW_MATCH"]
        assert any("clock skew" in r.getMessage() for r in caplog.records)
        assert feed_state_repo.get().last_error is None
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_future_dated_row_does_not_move_the_recovery_date_horizon_past_the_success_day(tmp_path):
    """Строка, датированная завтрашним днём, увиденная успешным циклом,
    поднимает newest_seen_publish_date — горизонт восстановления всё равно
    не позже дня самого цикла, сегодняшняя "дыра" дочитывается."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        future_row = _entry(protocol_no="FUT0001", plate="ZZ990ZZ", publish_date="2026-10-23")
        s0_rows = _rows("S0_", 29, publish_date="2026-10-22")
        first = _FakeFeedSession({
            0: _page([future_row] + s0_rows),
            30: _page(_rows("Y0_", 30, publish_date="2026-10-21")),
        })
        job1, _p1 = _make_job(first, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job1.run(_utc(21))
        assert feed_state_repo.get().newest_seen_publish_date == date(2026, 10, 23)

        failing = _FakeFeedSession(errors={0: FineProviderError("ConnectTimeout")})
        job2, _p2 = _make_job(failing, task_repo, fine_repo, feed_state_repo, seen_repo)
        await job2.run(_utc(23, 55))

        g0 = _rows("G0_", 30, publish_date="2026-10-22")
        g1 = _rows("G1_", 29, publish_date="2026-10-22") + [
            _entry(protocol_no="FUT_GAP_MATCH", plate="AA001AA", publish_date="2026-10-22"),
        ]
        recovery = _FakeFeedSession({
            0: _page([future_row] + _rows("H0_", 29, publish_date="2026-10-23")),
            30: _page(g0),
            60: _page(g1),
            90: _page(s0_rows),
        })
        job3, _p3 = _make_job(recovery, task_repo, fine_repo, feed_state_repo, seen_repo)
        persisted = _spy_persisted(job3)
        await job3.run(_utc(9, day=23))

        assert recovery.requested_offsets == [0, 30, 60, 90, 120]
        assert persisted == ["FUT_GAP_MATCH"]
        assert feed_state_repo.get().last_error is None
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_capped_recovery_says_it_cannot_reach_the_last_success_and_names_the_setting(tmp_path, caplog):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
        feed_state_repo.record_error("ConnectTimeout", at=_utc(21))
        session = _FakeFeedSession(
            {i * 30: _page(_rows(f"N{i}_", 30, publish_date="2026-10-22")) for i in range(5)}
        )
        job, _provider = _make_job(
            session, task_repo, fine_repo, feed_state_repo, seen_repo, max_pages_per_cycle=3,
        )
        with caplog.at_level(logging.WARNING, logger="reader.jobs.georgia_public_feed_job"):
            await job.run(_utc(23, 55))

        assert session.requested_offsets == [0, 30, 60]
        error = feed_state_repo.get().last_error
        assert "recovery could not reach rows covered by the last successful cycle within 3 pages" in error
        assert "fine_monitor.public_feed_max_pages_per_cycle" in error
        assert any(r.levelno == logging.WARNING and error in r.getMessage() for r in caplog.records)
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_first_run_cap_has_its_own_message(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        # Лимит первого запуска в 2 страницы при 2 страницах look-ahead больше не допускается.
        session = _FakeFeedSession(
            {i * 30: _page(_rows(f"T{i}_", 30, publish_date="2026-10-22")) for i in range(3)}
        )
        job, _provider = _make_job(
            session, task_repo, fine_repo, feed_state_repo, seen_repo, first_run_max_pages=3,
        )
        await job.run(_utc(21))

        error = feed_state_repo.get().last_error
        assert error.startswith("first-run catch-up: safety cap of 3 pages")
        assert "fine_monitor.public_feed_first_run_max_pages" in error
        assert "recovery could not reach" not in error
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_fast_cap_has_its_own_message(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        # Лимит в 2 страницы при 2 страницах look-ahead больше не допускается.
        _establish_fast_mode(feed_state_repo, at=_utc(15))
        session = _FakeFeedSession(
            {i * 30: _page(_rows(f"N{i}_", 30, publish_date="2026-10-22")) for i in range(4)}
        )
        job, _provider = _make_job(
            session, task_repo, fine_repo, feed_state_repo, seen_repo, max_pages_per_cycle=3,
        )
        await job.run(_utc(21))

        error = feed_state_repo.get().last_error
        assert error.startswith("safety cap of 3 pages reached before the frontier")
        assert "fine_monitor.public_feed_max_pages_per_cycle" in error
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_fast_page_of_rows_seen_before_last_success_still_gets_its_lookahead_pages(tmp_path):
    """DEEP-горизонт в FAST не применяется: страница из строк, увиденных до
    последнего успеха, — обычная тихая страница, после неё страницы
    look-ahead."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
        old = _rows("OLD_", 30, publish_date="2026-10-22")
        older = _rows("OLDER_", 30, publish_date="2026-10-22")
        oldest = _rows("OLDEST_", 30, publish_date="2026-10-22")
        _mark_seen_at(seen_repo, old + older + oldest, seen_at=_utc(9))
        session = _FakeFeedSession({0: _page(old), 30: _page(older), 60: _page(oldest)})
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_utc(21))

        assert session.requested_offsets == [0, 30, 60]
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_match_on_the_page_that_completes_deep_is_still_processed(tmp_path):
    """Страница, дошедшая до DEEP-горизонта, сначала обрабатывается целиком
    (цикл обнаружения не меняется) и только потом засчитывается в остановку
    (здесь look-ahead доходит до конца списка)."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        horizon_page = _rows("Y0_", 29, publish_date="2026-10-21") + [
            _entry(protocol_no="H_MATCH", plate="AA001AA", publish_date="2026-10-21"),
        ]
        session = _FakeFeedSession({
            0: _page(_rows("T0_", 30, publish_date="2026-10-22")),
            30: _page(horizon_page),
        })
        job, _provider = _make_job(
            session, task_repo, fine_repo, feed_state_repo, seen_repo, first_run_max_pages=10,
        )
        persisted = _spy_persisted(job)

        await job.run(_utc(21))

        assert session.requested_offsets == [0, 30, 60]
        assert persisted == ["H_MATCH"]
        assert seen_repo.is_seen("Y0_000")
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_first_run_horizon_uses_the_tbilisi_date_not_utc(tmp_path):
    """01:00 по Тбилиси 23.10 — это 21:00 UTC 22.10: "сегодня" — 23.10."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        session = _FakeFeedSession({
            0: _page(_rows("D23_", 30, publish_date="2026-10-23")),
            30: _page(_rows("D22_", 30, publish_date="2026-10-22")),
            60: _page(_rows("D22B_", 30, publish_date="2026-10-22")),
            90: _page(_rows("D22C_", 30, publish_date="2026-10-22")),
            120: _page(_rows("D22D_", 30, publish_date="2026-10-22")),
        })
        job, _provider = _make_job(
            session, task_repo, fine_repo, feed_state_repo, seen_repo, first_run_max_pages=10,
        )

        await job.run(_utc(1, day=23))

        # Страницы 22.10 обработаны (одна + 2 look-ahead); при "сегодня" по
        # UTC (22.10) обход дочитал бы до конца списка.
        assert session.requested_offsets == [0, 30, 60, 90]
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


# ==== живой feed: публикации между циклами и ВО ВРЕМЯ обхода (зеркало
# tests/test_georgia_collector_job.py) ====


class _LiveFeed:
    """Feed newest-first, меняющийся как настоящий: publish() кладёт строки
    наверх; строки, опубликованные, пока цикл ещё читает, сдвигают все
    следующие страницы вниз, и следующая страница повторяет строки, уже
    прочитанные этим циклом (в production — пачки по 70–360 строк в циклы
    15:00/21:00)."""

    def __init__(self, rows: list[dict] | None = None):
        self.rows: list[dict] = list(rows or [])
        self.requested_offsets: list[int] = []
        self._served = 0
        self._published_after: dict[int, list[dict]] = {}
        self._fail_at: dict[int, BaseException] = {}

    def publish(self, rows: list[dict]) -> None:
        self.rows[:0] = rows

    def publish_after_requests(self, count: int, rows: list[dict]) -> None:
        """rows появятся наверху сразу после следующих count запросов."""
        self._published_after[self._served + count - 1] = rows

    def fail_after_requests(self, count: int, exc: BaseException) -> None:
        """Запрос после следующих count запросов бросит exc."""
        self._fail_at[self._served + count] = exc

    async def fetch_published_protocols(self, *, first_result: int) -> dict:
        number = self._served
        self._served += 1
        self.requested_offsets.append(first_result)
        if number in self._fail_at:
            raise self._fail_at.pop(number)
        entries = self.rows[first_result:first_result + 30]
        if number in self._published_after:
            self.publish(self._published_after.pop(number))
        return _page(entries) if entries else _empty_page()


async def test_fast_reads_new_rows_below_a_page_it_already_read_when_publications_shift_the_pages(tmp_path):
    """60 строк, опубликованных после 2-й страницы, сдвигают список на две
    страницы: страницы 3–4 повторяют строки, уже отмеченные этим циклом. Это
    не граница — FAST идёт дальше, дочитывает новые строки (и их совпадение)
    и останавливается после страницы строк, увиденных ДО этого цикла, и
    страниц look-ahead."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        feed = _LiveFeed(
            _rows("OLD", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"),
        )
        job1, _p1 = _make_job(feed, task_repo, fine_repo, feed_state_repo, seen_repo, first_run_max_pages=10)
        await job1.run(_utc(15))
        assert feed_state_repo.get().last_error is None

        feed.publish(_rows("NEW", 119, publish_date="2026-10-22") + [
            _entry(protocol_no="DEEP_MATCH", plate="AA001AA", publish_date="2026-10-22"),
        ])
        feed.publish_after_requests(2, _rows("BURST", 60, publish_date="2026-10-22"))
        feed.requested_offsets.clear()
        job2, _p2 = _make_job(feed, task_repo, fine_repo, feed_state_repo, seen_repo)
        persisted = _spy_persisted(job2)
        await job2.run(_utc(21))

        assert feed.requested_offsets == [0, 30, 60, 90, 120, 150, 180, 210, 240]
        assert persisted == ["DEEP_MATCH"]
        assert feed_state_repo.get().last_error is None

        feed.requested_offsets.clear()
        job3, _p3 = _make_job(feed, task_repo, fine_repo, feed_state_repo, seen_repo)
        persisted3 = _spy_persisted(job3)
        await job3.run(_utc(23, 55))

        assert feed.requested_offsets == [0, 30, 60, 90, 120]
        assert all(seen_repo.is_seen(row["protocolNo"]) for row in feed.rows)
        assert persisted3 == []
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_interrupted_cycle_then_recovery_with_shifting_pages_reads_every_row_once(tmp_path):
    """Сквозной сценарий: первый запуск, цикл, убитый после двух страниц,
    восстановление, во время которого наверху публикуются 45 строк, затем
    FAST. Каждая опубликованная строка увидена, каждое совпадение обработано
    ровно один раз."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        persisted: list[str] = []
        feed = _LiveFeed(_rows("Y0_", 30, publish_date="2026-10-21"))
        feed.publish(_rows("A", 45, publish_date="2026-10-22"))
        job1, _p1 = _make_job(feed, task_repo, fine_repo, feed_state_repo, seen_repo)
        spy1 = _spy_persisted(job1)
        await job1.run(_utc(9))
        persisted += spy1
        assert feed_state_repo.get().last_error is None

        feed.publish(_rows("B", 89, publish_date="2026-10-22") + [
            _entry(protocol_no="MATCH_BELOW_KILL", plate="AA001AA", publish_date="2026-10-22"),
        ])
        feed.fail_after_requests(2, asyncio.CancelledError())
        job2, _p2 = _make_job(feed, task_repo, fine_repo, feed_state_repo, seen_repo)
        spy2 = _spy_persisted(job2)
        with pytest.raises(asyncio.CancelledError):
            await job2.run(_utc(15))
        persisted += spy2
        assert feed_state_repo.get().last_error is not None

        feed.publish(_rows("C", 60, publish_date="2026-10-22"))
        feed.publish_after_requests(
            1,
            [_entry(protocol_no="MATCH_IN_BURST_TOP", plate="AA001AA")]
            + _rows("D", 43, publish_date="2026-10-22")
            + [_entry(protocol_no="MATCH_IN_BURST_BOTTOM", plate="AA001AA")],
        )
        job3, _p3 = _make_job(feed, task_repo, fine_repo, feed_state_repo, seen_repo)
        spy3 = _spy_persisted(job3)
        await job3.run(_utc(21))
        persisted += spy3
        assert feed_state_repo.get().last_error is None
        assert sorted(persisted) == ["MATCH_BELOW_KILL", "MATCH_IN_BURST_BOTTOM"]

        job4, _p4 = _make_job(feed, task_repo, fine_repo, feed_state_repo, seen_repo)
        spy4 = _spy_persisted(job4)
        await job4.run(_utc(23, 55))
        persisted += spy4

        assert feed_state_repo.get().last_error is None
        assert all(seen_repo.is_seen(row["protocolNo"]) for row in feed.rows)
        assert sorted(persisted) == ["MATCH_BELOW_KILL", "MATCH_IN_BURST_BOTTOM", "MATCH_IN_BURST_TOP"]
        assert len(fine_repo.list_by_car_number("AA001AA")) == 3
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


async def test_fast_keeps_reading_when_a_burst_fills_the_overlap_page_right_after_the_frontier(tmp_path):
    """Граница на странице 0, сразу после неё опубликованы 150 строк:
    следующая страница целиком новая. FAST на ней не останавливается — он
    заканчивает только после тихой страницы и страниц look-ahead, так что
    самые старые строки пачки (и их совпадение) читаются сейчас, а не
    теряются навсегда. Страница, где пачка снова показывает старые строки
    границы (смещение 150), не тихая: этот цикл их уже прочитал."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        feed = _LiveFeed(
            _rows("OLD", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"),
        )
        job1, _p1 = _make_job(feed, task_repo, fine_repo, feed_state_repo, seen_repo, first_run_max_pages=10)
        await job1.run(_utc(15))

        feed.publish(_rows("NEW", 10, publish_date="2026-10-22"))
        burst = _rows("B", 150, publish_date="2026-10-22")
        burst[115] = _entry(protocol_no="BURST_MATCH", plate="AA001AA", publish_date="2026-10-22")
        feed.publish_after_requests(1, burst)
        feed.requested_offsets.clear()
        job2, _p2 = _make_job(feed, task_repo, fine_repo, feed_state_repo, seen_repo)
        persisted = _spy_persisted(job2)
        await job2.run(_utc(21))

        assert feed.requested_offsets == [0, 30, 60, 90, 120, 150, 180, 210, 240]
        assert persisted == ["BURST_MATCH"]

        job3, _p3 = _make_job(feed, task_repo, fine_repo, feed_state_repo, seen_repo)
        persisted3 = _spy_persisted(job3)
        await job3.run(_utc(23, 55))

        assert all(seen_repo.is_seen(row["protocolNo"]) for row in feed.rows)
        assert persisted3 == []
        assert feed_state_repo.get().last_error is None
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


# ==== FAST-граница: тихая страница и FAST_LOOKAHEAD_PAGES за ней (зеркало
# tests/test_georgia_collector_job.py). Только run() и фейки — те же тесты
# выполнятся и против общего обхода PR #57 после его rebase на это
# изменение и упадут, если вернётся старая остановка на "второй странице с
# увиденной строкой". Номера протоколов синтетические. ====

_FEED_JOB_LOGGER = "reader.jobs.georgia_public_feed_job"


def _receipts(series: str, first: int, count: int, *, publish_date: str = "2026-10-22") -> list[dict]:
    """count синтетических квитанций одной серии ('ავ' или 'კვ'), номера по
    убыванию от first; их номера машин никогда не отслеживаются."""
    return [
        _entry(protocol_no=f"{series}{first - i:09d}", plate=f"ZZ{i % 1000:03d}ZZ", publish_date=publish_date)
        for i in range(count)
    ]


def _receipt_match(series: str, number: int, *, publish_date: str = "2026-10-22") -> dict:
    return _entry(protocol_no=f"{series}{number:09d}", plate="AA001AA", publish_date=publish_date)


def _feed_pages(rows: list[dict]) -> dict[int, dict]:
    """rows (newest first) — страницы police.ge по 30; data.count = все строки."""
    return {
        offset: {"success": True, "data": {"count": len(rows), "results": rows[offset:offset + 30]}}
        for offset in range(0, len(rows), 30)
    }


def _protocols(rows: list[dict]) -> list[str]:
    return [row["protocolNo"] for row in rows]


def _poll_fields(caplog) -> dict[str, str]:
    """Поля key=value последней итоговой строки ("finished: pages=...") job'а."""
    lines = [
        record.getMessage() for record in caplog.records
        if record.name == _FEED_JOB_LOGGER and "finished: pages=" in record.getMessage()
    ]
    assert lines, "нет итоговой строки цикла"
    return dict(re.findall(r"(\w+)=([^,\s]+)", lines[-1]))


def _counters(caplog) -> dict[str, int]:
    fields = _poll_fields(caplog)
    return {key: int(fields[key]) for key in ("new_rows", "seen_among_new", "quiet_pages", "lookahead_new")}


def _old_rows_on_two_pages_above_the_boundary(old: list[dict], new: list[dict]) -> list[dict]:
    """95 новых строк: старые строки над новыми на страницах 0 и 1, целиком
    новая страница 2, граница на странице 3 (7 новых), с 4-й — тихие."""
    assert len(new) == 95
    return (
        new[0:10] + old[:1] + new[10:29]
        + new[29:49] + old[1:2] + new[49:58]
        + new[58:88]
        + new[88:95] + old[2:25]
        + old[25:]
    )


class _CountingLiveFeed(_LiveFeed):
    """_LiveFeed, у которого data.count — весь список в момент запроса, как у
    police.ge (проба первой страницы 19.11: count = все опубликованные)."""

    async def fetch_published_protocols(self, *, first_result: int) -> dict:
        total = len(self.rows)
        page = await super().fetch_published_protocols(first_result=first_result)
        page["data"]["count"] = total
        return page


@pytest.fixture
def feed_repos(tmp_path):
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    _create_active_task(task_repo, "AA001AA")
    try:
        yield task_repo, fine_repo, feed_state_repo, seen_repo
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()


def _feed_job(feed_repos, session, **kwargs):
    """Job и список external_fine_id всех записей, переданных им в persist_feed_match."""
    task_repo, fine_repo, feed_state_repo, seen_repo = feed_repos
    job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo, **kwargs)
    return job, _spy_persisted(job)


def _fast_job_over(feed_repos, rows, old_rows, **kwargs):
    """Job, чей следующий цикл — FAST по неизменному списку: последний успех
    в 15:00 по Тбилиси видел old_rows."""
    _task, _fine, feed_state_repo, seen_repo = feed_repos
    session = _FakeFeedSession(_feed_pages(rows))
    job, persisted = _feed_job(feed_repos, session, **kwargs)
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    _mark_seen_at(seen_repo, old_rows, seen_at=_utc(15))
    return job, session, persisted


async def _first_run(feed_repos, feed):
    """Успешный DEEP первый запуск в 09:00 по сегодняшним 150 "старым"
    строкам (и странице вчерашних): все следующие циклы в этих тестах — FAST."""
    job, persisted = _feed_job(feed_repos, feed, first_run_max_pages=10)
    await job.run(_utc(9))
    assert feed_repos[2].get().last_error is None
    feed.requested_offsets.clear()
    return job, persisted


def _old_feed_rows() -> tuple[list[dict], list[dict]]:
    return _receipts("ავ", 5999, 150), _receipts("ავ", 3999, 30, publish_date="2026-10-21")


def test_fast_lookahead_is_two_pages_by_default_and_matches_the_collector():
    """K = 2 (данные — в докстроке FAST_LOOKAHEAD_PAGES), одинаково у обоих
    близнецов; сценарии ниже закрепляют его поведением: строки на 1-й и 2-й
    странице под тихой читаются, на 3-й — нет."""
    from reader.georgia_collector import collector_job

    assert FAST_LOOKAHEAD_PAGES == collector_job.FAST_LOOKAHEAD_PAGES == 2


@pytest.mark.parametrize("lookahead, max_pages, first_run, valid", [
    (0, 30, 100, False), (1, 2, 100, True), (2, 3, 100, True), (3, 3, 100, False), (4, 3, 100, False),
    (2, 30, 2, False), (2, 30, 3, True),
])
def test_feed_job_takes_a_lookahead_from_one_to_below_the_page_cap(
    feed_repos, lookahead, max_pages, first_run, valid,
):
    """K < 1 — нет look-ahead; K не меньше любого из лимитов страниц — каждый
    цикл такого рода упирался бы в лимит (и FAST, и DEEP читают ещё K)."""
    task_repo, fine_repo, feed_state_repo, seen_repo = feed_repos
    check_service = FineCheckService(_FakeCircuitProvider(), task_repo, fine_repo)

    def build():
        return GeorgiaPublicFineFeedJob(
            _FakeFeedSession(), task_repo, check_service, feed_state_repo, seen_repo,
            run_times=_DEFAULT_RUN_TIMES, tz=_TBILISI,
            max_pages_per_cycle=max_pages, first_run_max_pages=first_run, fast_lookahead_pages=lookahead,
        )

    if valid:
        build()
    else:
        with pytest.raises(ValueError):
            build()


@pytest.mark.parametrize("old_on_page0", [1, 3], ids=["one_old_row", "several_old_rows"])
async def test_fast_reads_past_old_rows_shown_among_new_rows(feed_repos, caplog, old_on_page0):
    """Старые строки среди новых на странице (переопубликованная квитанция —
    production, 19.11), одна или несколько: FAST на них не останавливается,
    дочитывает до настоящей границы, тихой страницы и 2 страниц look-ahead и
    считает старые строки, стоявшие над новыми (не хвост страницы-границы)."""
    caplog.set_level(logging.INFO, logger=_FEED_JOB_LOGGER)
    old = _receipts("ავ", 5999, 150)
    on_page0 = 30 - old_on_page0
    new = _receipts("ავ", 9999, on_page0 + 40)
    new[on_page0 + 15] = _receipt_match("ავ", 9000)   # страница 1
    new[on_page0 + 35] = _receipt_match("კვ", 120)    # страница 2, граница
    if old_on_page0 == 1:
        page0 = new[:10] + old[:1] + new[10:on_page0]
    else:
        page0 = new[:5] + old[:1] + new[5:8] + old[1:3] + new[8:on_page0]
    rows = page0 + new[on_page0:] + old[old_on_page0:]
    job, session, persisted = _fast_job_over(feed_repos, rows, old)

    await job.run(_utc(21))

    # страница 0 (старые среди новых), 1 (новые), 2 (10 новых + граница),
    # 3–5: тихая страница и 2 страницы look-ahead.
    assert session.requested_offsets == [0, 30, 60, 90, 120, 150]
    assert sorted(persisted) == ["ავ000009000", "კვ000000120"]
    assert _counters(caplog) == {
        "new_rows": on_page0 + 40, "seen_among_new": old_on_page0, "quiet_pages": 3, "lookahead_new": 0,
    }
    assert feed_repos[2].get().last_error is None


async def test_fast_does_not_stop_on_old_rows_on_two_pages_above_the_real_boundary(feed_repos, caplog):
    """Случай, в котором старое правило теряло строки: старые строки над
    новыми на двух разных страницах выше настоящей границы. Остановка на
    "второй странице с увиденной строкой" заканчивала обход на странице 1, и
    страницы 2–3 не читались никогда. Теперь FAST дочитывает до границы и её
    look-ahead; каждое совпадение обработано один раз."""
    caplog.set_level(logging.INFO, logger=_FEED_JOB_LOGGER)
    _task, fine_repo, _state, seen_repo = feed_repos
    old = _receipts("ავ", 5999, 150)
    new = _receipts("ავ", 9999, 95)
    new[70] = _receipt_match("ავ", 9100)   # страница 2
    new[90] = _receipt_match("კვ", 130)    # страница 3, граница
    rows = _old_rows_on_two_pages_above_the_boundary(old, new)
    job, session, persisted = _fast_job_over(feed_repos, rows, old)

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30, 60, 90, 120, 150, 180]
    assert sorted(persisted) == ["ავ000009100", "კვ000000130"]
    assert _counters(caplog) == {"new_rows": 95, "seen_among_new": 2, "quiet_pages": 3, "lookahead_new": 0}
    assert all(seen_repo.is_seen(p) for p in _protocols(new))

    # Следующий цикл: 30 новых сверху, дальше прочитанные выше страницы тихие.
    session2 = _FakeFeedSession(_feed_pages(_receipts("ავ", 19999, 30) + rows))
    job2, persisted2 = _feed_job(feed_repos, session2)
    await job2.run(_utc(23, 55))
    assert session2.requested_offsets == [0, 30, 60, 90]
    assert persisted2 == []
    assert len(fine_repo.list_by_car_number("AA001AA")) == 2


@pytest.mark.parametrize("lookahead_page", [1, 2])
async def test_fast_reads_rows_inserted_within_the_lookahead_below_a_quiet_page(feed_repos, caplog, lookahead_page):
    """Новые строки под целиком старой (тихой) страницей, на 1-й или 2-й
    странице look-ahead: FAST сбрасывает счёт, читает их (и совпадение) и
    останавливается только после новой тихой страницы и 2 страниц look-ahead.
    lookahead_new их считает — свидетельство вставки под границей."""
    caplog.set_level(logging.INFO, logger=_FEED_JOB_LOGGER)
    seen_repo = feed_repos[3]
    old = _receipts("ავ", 5999, 240)
    new = _receipts("ავ", 9999, 12)
    inserted = [_receipt_match("ავ", 1999)] + _receipts("კვ", 199, 2)
    at = 28 + 30 * lookahead_page   # 10 строк от начала страницы look-ahead
    rows = new + old[:at] + inserted + old[at:]
    job, session, persisted = _fast_job_over(feed_repos, rows, old)

    await job.run(_utc(21))

    # страница 0: новые + граница; 1 — тихая; страница look-ahead со
    # вставкой сбрасывает счёт; затем тихая страница и 2 страницы look-ahead.
    assert session.requested_offsets == [i * 30 for i in range(lookahead_page + 5)]
    assert persisted == ["ავ000001999"]
    assert all(seen_repo.is_seen(p) for p in _protocols(inserted))
    assert _counters(caplog) == {
        "new_rows": 15, "seen_among_new": 10, "quiet_pages": lookahead_page + 3, "lookahead_new": 3,
    }
    assert feed_repos[2].get().last_error is None


async def test_fast_stops_two_pages_below_a_quiet_page_and_misses_rows_inserted_further_down(feed_repos, caplog):
    """Задокументированное ограничение: новые строки, вставленные глубже 2
    страниц look-ahead под первой тихой страницей, не читаются — ни этим
    циклом, ни следующими. В production не наблюдалось (список упорядочен
    по публикации); показала бы это диагностика data.count — первая страница
    выросла больше, чем прочитано новых строк. В журнале job'а нет ни одного
    номера квитанции или машины."""
    caplog.set_level(logging.INFO, logger=_FEED_JOB_LOGGER)
    feed_state_repo, seen_repo = feed_repos[2], feed_repos[3]
    old, yesterday = _old_feed_rows()
    feed = _CountingLiveFeed(old + yesterday)
    job, persisted = await _first_run(feed_repos, feed)
    assert (_poll_fields(caplog)["reported_count"], _poll_fields(caplog)["reported_count_delta"]) == ("180", "n/a")

    feed.publish(_receipts("ავ", 9999, 12))
    inserted = [_receipt_match("ავ", 1999)] + _receipts("კვ", 199, 2)
    feed.rows[130:130] = inserted   # страница 4: под тихой страницей 1 и look-ahead 2–3
    await job.run(_utc(15))   # тот же объект job'а, как в production

    assert feed.requested_offsets == [0, 30, 60, 90]
    assert persisted == []
    assert not any(seen_repo.is_seen(p) for p in _protocols(inserted))
    state = feed_state_repo.get()
    assert (state.last_error, state.last_successful_poll_at) == (None, _utc(15))
    assert _counters(caplog) == {"new_rows": 12, "seen_among_new": 0, "quiet_pages": 3, "lookahead_new": 0}
    # С первой страницы прошлого цикла добавилось 15 строк, прочитано 12: 3 вне досягаемости.
    assert (_poll_fields(caplog)["reported_count"], _poll_fields(caplog)["reported_count_delta"]) == ("195", "+15")

    feed.publish(_receipts("ავ", 19999, 30))
    feed.requested_offsets.clear()
    await job.run(_utc(21))

    assert feed.requested_offsets == [0, 30, 60, 90]
    assert not any(seen_repo.is_seen(p) for p in _protocols(inserted))
    assert persisted == []
    fields = _poll_fields(caplog)
    assert (fields["new_rows"], fields["reported_count"], fields["reported_count_delta"]) == ("30", "225", "+30")
    named = set(_protocols(feed.rows)) | {row["protocolAuto"] for row in feed.rows}
    assert not [
        record for record in caplog.records
        if record.name == _FEED_JOB_LOGGER and any(value in record.getMessage() for value in named)
    ]


async def test_old_receipt_re_shown_among_new_rows_is_not_processed_again(feed_repos, caplog):
    """Квитанция, увиденная (и совпавшая) раньше, снова показана наверху
    среди новых (переопубликована или обновлена): повторно не
    обрабатывается, считается в seen_among_new, а FAST дочитывает до
    границы и нового совпадения рядом с ней."""
    caplog.set_level(logging.INFO, logger=_FEED_JOB_LOGGER)
    fine_repo, seen_repo = feed_repos[1], feed_repos[3]
    old, yesterday = _old_feed_rows()
    old[40] = _receipt_match("ავ", 5000)
    feed = _LiveFeed(old + yesterday)
    job, persisted = await _first_run(feed_repos, feed)
    assert persisted == ["ავ000005000"]

    new = _receipts("ავ", 9999, 50)
    new[45] = _receipt_match("ავ", 9500)   # страница 1
    feed.rows.remove(old[40])
    feed.publish(new[:7] + [old[40]] + new[7:])
    await job.run(_utc(15))

    # страница 0: 29 новых + переопубликованная квитанция; страница 1: 21
    # новая + граница; 2–4: тихая страница и 2 страницы look-ahead.
    assert feed.requested_offsets == [0, 30, 60, 90, 120]
    assert persisted == ["ავ000005000", "ავ000009500"]
    assert len(fine_repo.list_by_car_number("AA001AA")) == 2
    assert _counters(caplog) == {"new_rows": 50, "seen_among_new": 1, "quiet_pages": 3, "lookahead_new": 0}
    assert all(seen_repo.is_seen(p) for p in _protocols(feed.rows))


async def test_rows_a_burst_re_shows_in_the_same_cycle_are_not_processed_twice_or_quiet(feed_repos, caplog):
    """90 строк, опубликованных после 4-й страницы, снова показывают три уже
    прочитанные этим циклом страницы: те же квитанции (одна — совпадение)
    приходят дважды за цикл. Повторно они не обрабатываются и увиденными до
    цикла не считаются, так что эти три страницы — не тихая страница с
    look-ahead: FAST дочитывает новые строки под ними (ещё одно совпадение)
    и настоящую границу."""
    caplog.set_level(logging.INFO, logger=_FEED_JOB_LOGGER)
    seen_repo = feed_repos[3]
    old, yesterday = _old_feed_rows()
    feed = _LiveFeed(old + yesterday)
    job, persisted = await _first_run(feed_repos, feed)

    new = _receipts("ავ", 9999, 150)
    new[45] = _receipt_match("ავ", 9500)   # страница 1, снова на странице 5
    new[130] = _receipt_match("კვ", 150)   # страница 7, под повторами
    feed.publish(new)
    feed.publish_after_requests(4, _receipts("ავ", 19999, 90))
    await job.run(_utc(15))

    # страницы 0–3 новые; пачка снова показывает new[30:120] на страницах
    # 4–6; страница 7 новая; 8–10: тихая страница и 2 страницы look-ahead.
    assert feed.requested_offsets == [i * 30 for i in range(11)]
    assert sorted(persisted) == ["ავ000009500", "კვ000000150"]
    assert _counters(caplog) == {"new_rows": 150, "seen_among_new": 0, "quiet_pages": 3, "lookahead_new": 0}

    # Пачку наверху читает следующий цикл; каждая квитанция — ровно один раз.
    feed.requested_offsets.clear()
    await job.run(_utc(21))
    assert feed.requested_offsets == [0, 30, 60, 90, 120, 150]
    assert all(seen_repo.is_seen(p) for p in _protocols(feed.rows))
    assert sorted(persisted) == ["ავ000009500", "კვ000000150"]


async def test_a_burst_during_the_lookahead_does_not_use_it_up(feed_repos, caplog):
    """60 строк, опубликованных сразу после первой тихой страницы, сдвигают
    список на две страницы: следующие две страницы снова показывают строки,
    уже прочитанные этим циклом (его новые, границу, тихую страницу). Это не
    страницы look-ahead — FAST всё равно читает две свежие страницы под
    тихой и находит вставленные там строки."""
    caplog.set_level(logging.INFO, logger=_FEED_JOB_LOGGER)
    seen_repo = feed_repos[3]
    old, yesterday = _old_feed_rows()
    feed = _LiveFeed(old + yesterday)
    job, persisted = await _first_run(feed_repos, feed)

    feed.publish(_receipts("ავ", 9999, 12))
    inserted = [_receipt_match("ავ", 1999)] + _receipts("კვ", 199, 2)
    feed.rows[102:102] = inserted   # вторая страница look-ahead, до пачки
    feed.publish_after_requests(2, _receipts("ავ", 19999, 60))
    await job.run(_utc(15))

    # страница 0: новые + граница; 1 — тихая; 2–3 снова показывают 0–1;
    # 4 — look-ahead; 5 — вставка (сброс); 6–8.
    assert feed.requested_offsets == [i * 30 for i in range(9)]
    assert persisted == ["ავ000001999"]
    assert _counters(caplog) == {"new_rows": 15, "seen_among_new": 12, "quiet_pages": 5, "lookahead_new": 3}
    assert all(seen_repo.is_seen(p) for p in _protocols(inserted))


@pytest.mark.parametrize(
    "kill_at, pages_read", [(120, 4), (210, 7)], ids=["above_the_boundary", "in_the_lookahead"],
)
async def test_fast_killed_mid_cycle_is_followed_by_a_deep_recovery_that_loses_nothing(
    feed_repos, kill_at, pages_read,
):
    """FAST-цикл, убитый (CancelledError) посреди новых строк между старыми
    строками над ними и границей или во время look-ahead: отметка "цикл
    идёт" остаётся, успех не записан. После рестарта — DEEP-восстановление:
    оно читает сквозь страницы, отмеченные убитой попыткой (для FAST это
    были бы тихие страницы), до строк, увиденных последним успехом, и каждое
    совпадение обработано ровно один раз."""
    _task, fine_repo, feed_state_repo, seen_repo = feed_repos
    old = _receipts("ავ", 5999, 150)
    new = _receipts("ავ", 9999, 155)
    new[130] = _receipt_match("ავ", 9300)   # страница 4
    rows = (
        new[0:10] + old[:1] + new[10:29]
        + new[29:49] + old[1:2] + new[49:58]
        + new[58:148]                          # страницы 2–4
        + new[148:155] + old[2:25]             # страница 5: граница
        + old[25:]                             # страницы 6–...: тихие
    )
    session = _FakeFeedSession(_feed_pages(rows), errors={kill_at: asyncio.CancelledError()})
    job, persisted = _feed_job(feed_repos, session)
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    _mark_seen_at(seen_repo, old, seen_at=_utc(15))

    with pytest.raises(asyncio.CancelledError):
        await job.run(_utc(21))
    assert len(session.requested_offsets) == pages_read + 1
    state = feed_state_repo.get()
    assert state.last_error.startswith("cycle in progress")
    assert state.last_successful_poll_at == _utc(15)

    top = _receipts("ავ", 19999, 30)
    top[3] = _receipt_match("კვ", 160)
    recovery = _FakeFeedSession(_feed_pages(top + rows))
    job2, persisted2 = _feed_job(feed_repos, recovery)   # рестарт
    await job2.run(_utc(23, 55))

    # DEEP читает до первой страницы строк, увиденных последним успехом
    # (смещение 210), и двух страниц look-ahead за ней и останавливается
    # (270); FAST остановился бы на страницах убитой попытки (смещение 90).
    # Закреплён только конец: как восстановление проходит страницы,
    # прочитанные прошлой попыткой, — не предмет этого правила.
    assert recovery.requested_offsets[-1] == max(recovery.requested_offsets) == 270
    assert sorted(persisted + persisted2) == ["ავ000009300", "კვ000000160"]
    assert len(fine_repo.list_by_car_number("AA001AA")) == 2
    assert all(seen_repo.is_seen(p) for p in _protocols(top + new))
    state = feed_state_repo.get()
    assert (state.last_error, state.last_successful_poll_at) == (None, _utc(23, 55))


async def test_fast_cap_before_the_boundary_is_an_error_and_the_next_cycle_reads_the_rest(feed_repos):
    """Старые строки над новыми на страницах 0 и 1, лимит 3 страницы: старое
    правило объявляло успех на странице 1 и ниже не читало. Теперь раньше
    наступает лимит — ошибка, успех не записан, ничего, из-за чего следующий
    цикл пропустил бы строки, не сохранено, — и следующий цикл (DEEP) читает
    сверху сквозь строки той попытки до границы."""
    _task, _fine, feed_state_repo, seen_repo = feed_repos
    old = _receipts("ავ", 5999, 150)
    new = _receipts("ავ", 9999, 95)
    new[70] = _receipt_match("ავ", 9100)   # страница 2
    new[90] = _receipt_match("კვ", 130)    # страница 3, граница
    rows = _old_rows_on_two_pages_above_the_boundary(old, new)
    job, session, persisted = _fast_job_over(feed_repos, rows, old, max_pages_per_cycle=3)

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30, 60]
    state = feed_state_repo.get()
    assert (state.last_successful_poll_at, state.newest_seen_publish_date) == (_utc(15), date(2026, 10, 22))
    assert state.last_error.startswith("safety cap of 3 pages reached before the frontier (already-seen rows)")
    assert "fine_monitor.public_feed_max_pages_per_cycle" in state.last_error
    assert persisted == ["ავ000009100"]

    session2 = _FakeFeedSession(_feed_pages(rows))
    job2, persisted2 = _feed_job(feed_repos, session2)
    await job2.run(_utc(23, 55))

    # DEEP заканчивает через две страницы look-ahead после первой страницы
    # строк, увиденных последним успехом (120): на 180.
    assert session2.requested_offsets[-1] == max(session2.requested_offsets) == 180
    assert persisted2 == ["კვ000000130"]
    assert all(seen_repo.is_seen(p) for p in _protocols(new))
    state = feed_state_repo.get()
    assert (state.last_error, state.last_successful_poll_at) == (None, _utc(23, 55))


async def test_fast_cap_during_the_lookahead_is_an_error_not_a_success(feed_repos):
    """Лимит обрывает цикл после первой тихой страницы, до конца look-ahead:
    граница не подтверждена — успех не записан, ошибка называет look-ahead.
    Следующий цикл — DEEP: строки той попытки не покрыты последним успехом,
    поэтому он читает их снова (вместе с опубликованными после) и
    останавливается на строках, увиденных последним успехом."""
    _task, _fine, feed_state_repo, seen_repo = feed_repos
    old = _receipts("ავ", 5999, 150)
    new = _receipts("ავ", 9999, 12)
    new[4] = _receipt_match("ავ", 9400)
    rows = new + old
    job, session, persisted = _fast_job_over(feed_repos, rows, old, max_pages_per_cycle=3)

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30, 60]
    state = feed_state_repo.get()
    assert (state.last_successful_poll_at, state.newest_seen_publish_date) == (_utc(15), date(2026, 10, 22))
    assert state.last_error.startswith(
        "safety cap of 3 pages reached before the frontier was confirmed "
        "(look-ahead: 1 of 2 pages read after a page without new rows)"
    )
    assert "fine_monitor.public_feed_max_pages_per_cycle" in state.last_error
    assert persisted == ["ავ000009400"]

    top = _receipts("ავ", 19999, 30)
    top[7] = _receipt_match("კვ", 170)
    session2 = _FakeFeedSession(_feed_pages(top + rows))
    job2, persisted2 = _feed_job(feed_repos, session2)
    await job2.run(_utc(23, 55))

    # DEEP: новые сверху, страница той попытки (не покрыта последним
    # успехом), первая страница, покрытая им (60), и две страницы look-ahead
    # (120); FAST остановился бы на 90.
    assert session2.requested_offsets[-1] == max(session2.requested_offsets) == 120
    assert persisted2 == ["კვ000000170"]
    assert all(seen_repo.is_seen(p) for p in _protocols(top + new))
    state = feed_state_repo.get()
    assert (state.last_error, state.last_successful_poll_at) == (None, _utc(23, 55))


@pytest.mark.parametrize("lookahead_pages, read", [(1, False), (3, True)])
async def test_lookahead_parameter_sets_how_far_below_the_quiet_page_fast_reads(feed_repos, lookahead_pages, read):
    """fast_lookahead_pages (как GEORGIA_COLLECTOR_FAST_LOOKAHEAD_PAGES у
    коллектора): строки, вставленные на второй странице под первой тихой,
    читаются при K = 3 и не читаются при K = 1."""
    task_repo, fine_repo, feed_state_repo, seen_repo = feed_repos
    old = _receipts("ავ", 5999, 240)
    rows = _receipts("ავ", 9999, 12) + old[:88] + [_receipt_match("ავ", 1999)] + old[88:]
    check_service = FineCheckService(_FakeCircuitProvider(), task_repo, fine_repo)
    job = GeorgiaPublicFineFeedJob(
        _FakeFeedSession(_feed_pages(rows)), task_repo, check_service, feed_state_repo, seen_repo,
        run_times=_DEFAULT_RUN_TIMES, tz=_TBILISI, fast_lookahead_pages=lookahead_pages,
    )
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    _mark_seen_at(seen_repo, old, seen_at=_utc(15))

    await job.run(_utc(21))

    assert seen_repo.is_seen("ავ000001999") is read
    assert len(fine_repo.list_by_car_number("AA001AA")) == (1 if read else 0)
    assert feed_state_repo.get().last_error is None


# ==== защита от ложного успеха: пустая или неполная страница до data.count
# и look-ahead DEEP-горизонта (зеркало tests/test_georgia_collector_job.py) ====


def _raw_page(rows: list[dict], *, count: int | None) -> dict:
    """Страница police.ge с данным data.count (None — без count вовсе)."""
    data: dict = {"results": rows}
    if count is not None:
        data["count"] = count
    return {"success": True, "data": data}


def _job_over_pages(feed_repos, pages, old_rows, *, deep, **kwargs):
    """Job над неизменными страницами: последний успех в 15:00 видел
    old_rows; с deep — следующая попытка упала в 17:00, и цикл будет DEEP."""
    _task, _fine, feed_state_repo, seen_repo = feed_repos
    session = _FakeFeedSession(pages)
    job, persisted = _feed_job(feed_repos, session, **kwargs)
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    _mark_seen_at(seen_repo, old_rows, seen_at=_utc(15))
    if deep:
        feed_state_repo.record_error("ConnectTimeout", at=_utc(17))
    return job, session, persisted


@pytest.mark.parametrize("deep", [False, True], ids=["fast", "deep"])
@pytest.mark.parametrize("rows_on_page", [0, 20], ids=["empty_page", "short_page"])
@pytest.mark.parametrize("bad_count", ["total", "zero"])
async def test_an_incomplete_page_before_the_end_of_the_list_is_an_error_not_the_end(
    feed_repos, deep, rows_on_page, bad_count,
):
    """Случайный {success: true, results: []} — или неполная страница —
    посреди списка (по data.count дальше ещё есть строки): не конец списка, а
    ошибка — даже если сама страница сообщает count 0 (решает наибольший
    count цикла). Успех не записан, следующий цикл (DEEP) дочитывает строки ниже,
    вместе с совпадением среди них."""
    _task, fine_repo, feed_state_repo, seen_repo = feed_repos
    old = _receipts("ავ", 5999, 150)
    new = _receipts("ავ", 9999, 90)
    new[65] = _receipt_match("კვ", 190)   # страница 2
    rows = new + old
    pages = _feed_pages(rows)
    pages[60] = _raw_page(new[60:60 + rows_on_page], count=len(rows) if bad_count == "total" else 0)
    job, session, persisted = _job_over_pages(feed_repos, pages, old, deep=deep)

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30, 60]
    state = feed_state_repo.get()
    assert state.last_successful_poll_at == _utc(15)
    page = "an empty page" if rows_on_page == 0 else "20 of 30 rows"
    assert state.last_error.startswith(f"police.ge returned {page} at offset 60, although the list reports 240 rows")
    assert persisted == []

    session2 = _FakeFeedSession(_feed_pages(rows))
    job2, persisted2 = _feed_job(feed_repos, session2)
    await job2.run(_utc(23, 55))

    assert session2.requested_offsets[-1] == max(session2.requested_offsets) == 150
    assert persisted2 == ["კვ000000190"]
    assert len(fine_repo.list_by_car_number("AA001AA")) == 1
    assert all(seen_repo.is_seen(p) for p in _protocols(new))
    assert (feed_state_repo.get().last_error, feed_state_repo.get().last_successful_poll_at) == (None, _utc(23, 55))


@pytest.mark.parametrize("deep", [False, True], ids=["fast", "first_run"])
async def test_the_real_end_of_the_list_still_ends_the_traversal_as_a_success(feed_repos, deep):
    """Неполная последняя страница и пустая страница на data.count — настоящий
    конец списка: цикл успешен (ещё до конца своего look-ahead)."""
    feed_state_repo = feed_repos[2]
    if deep:
        rows = _receipts("ავ", 9999, 45)   # первый запуск: только сегодняшние строки
        session = _FakeFeedSession({**_feed_pages(rows), 60: _raw_page([], count=len(rows))})
        job, _persisted = _feed_job(feed_repos, session, first_run_max_pages=10)
    else:
        old = _receipts("ავ", 5999, 40)
        rows = _receipts("ავ", 9999, 12) + old
        job, session, _persisted = _job_over_pages(
            feed_repos, {**_feed_pages(rows), 60: _raw_page([], count=len(rows))}, old, deep=False,
        )

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30, 60]
    state = feed_state_repo.get()
    assert (state.last_error, state.last_successful_poll_at) == (None, _utc(21))


async def test_without_data_count_an_empty_page_still_ends_the_list_with_a_warning(feed_repos, caplog):
    """police.ge не сообщил data.count: пустую страницу не отличить от конца,
    поэтому она, как раньше, завершает обход — с предупреждением без чисел."""
    caplog.set_level(logging.WARNING, logger=_FEED_JOB_LOGGER)
    feed_state_repo = feed_repos[2]
    old = _receipts("ავ", 5999, 18)
    rows = _receipts("ავ", 9999, 12) + old
    job, session, _persisted = _job_over_pages(
        feed_repos, {0: _raw_page(rows, count=None), 30: _raw_page([], count=None)}, old, deep=False,
    )

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30]
    assert (feed_state_repo.get().last_error, feed_state_repo.get().last_successful_poll_at) == (None, _utc(21))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "data.count" in r.getMessage()]
    assert len(warnings) == 1
    assert not re.search(r"\d", warnings[0])


@pytest.mark.parametrize("pages, offset, reported", [
    # Список вырос за обход: решает наибольший count, а не первой страницы.
    ({0: 60, 30: 90, 60: 120}, 60, 120),
    # У пустой страницы count нет — берётся последний из прежних.
    ({0: 300, 30: None}, 30, 300),
], ids=["grown_count", "earlier_count"])
async def test_the_largest_data_count_decides_whether_a_page_ends_the_list(feed_repos, pages, offset, reported):
    feed_state_repo = feed_repos[2]
    new = _receipts("ავ", 9999, 60)
    raw_pages = {
        first: _raw_page(new[first:first + 30] if first < offset else [], count=count)
        for first, count in pages.items()
    }
    job, session, _persisted = _job_over_pages(feed_repos, raw_pages, [], deep=False)

    await job.run(_utc(21))

    assert session.requested_offsets[-1] == offset
    state = feed_state_repo.get()
    assert state.last_successful_poll_at == _utc(15)
    assert state.last_error.startswith(
        f"police.ge returned an empty page at offset {offset}, although the list reports {reported} rows"
    )


@pytest.mark.parametrize("block", [30, 60])
async def test_deep_reads_past_a_block_of_re_shown_old_rows(feed_repos, block):
    """Блок старых квитанций, снова показанных наверху, занимает целые
    страницы, которые DEEP-горизонт принимает. Одна (или две) такие страницы
    больше не завершают восстановление: новые строки под блоком читаются (с
    совпадением), а DEEP останавливается только после 1 + K страниц
    обработанных строк."""
    feed_state_repo, seen_repo = feed_repos[2], feed_repos[3]
    old = _receipts("ავ", 5999, 240)
    below = _receipts("ავ", 8999, 30)
    below[10] = _receipt_match("კვ", 180)
    rows = _receipts("ავ", 9999, 30) + old[:block] + below + old[block:]
    job, session, persisted = _job_over_pages(feed_repos, _feed_pages(rows), old, deep=True)

    await job.run(_utc(21))

    # страница 0 новая; блок (1 или 2 страницы); новые строки под ним; затем
    # 3 страницы обработанных строк.
    assert session.requested_offsets == [i * 30 for i in range(block // 30 + 5)]
    assert persisted == ["კვ000000180"]
    assert all(seen_repo.is_seen(p) for p in _protocols(below))
    assert (feed_state_repo.get().last_error, feed_state_repo.get().last_successful_poll_at) == (None, _utc(21))


async def test_deep_cap_during_the_lookahead_is_an_error_not_a_success(feed_repos):
    feed_state_repo = feed_repos[2]
    old = _receipts("ავ", 5999, 150)
    rows = _receipts("ავ", 9999, 30) + old
    job, session, _persisted = _job_over_pages(
        feed_repos, _feed_pages(rows), old, deep=True, max_pages_per_cycle=3,
    )

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30, 60]
    state = feed_state_repo.get()
    assert state.last_successful_poll_at == _utc(15)
    assert state.last_error.startswith(
        "recovery reached rows covered by the last successful cycle, but the safety cap of 3 pages came "
        "during the look-ahead (look-ahead: 1 of 2 pages read)"
    )
    assert "fine_monitor.public_feed_max_pages_per_cycle" in state.last_error

    session2 = _FakeFeedSession(_feed_pages(rows))
    job2, _persisted2 = _feed_job(feed_repos, session2)
    await job2.run(_utc(23, 55))

    # Страница 0 той попытки не покрыта последним успехом (её строки отмечены
    # после него): затем три обработанные страницы, конец на 90.
    assert session2.requested_offsets[-1] == max(session2.requested_offsets) == 90
    assert feed_state_repo.get().last_error is None


async def test_first_run_reads_past_a_page_of_older_rows_followed_by_todays_rows(feed_repos):
    """Горизонт первого запуска (строки, опубликованные раньше сегодняшнего
    дня) с тем же look-ahead: одна страница вчерашних строк, за которой снова
    сегодняшние (с совпадением), его не завершает; три вчерашние подряд —
    завершают."""
    feed_state_repo = feed_repos[2]
    today = _receipts("ავ", 9999, 60)
    today[45] = _receipt_match("კვ", 170)   # страница 2
    yesterday = _receipts("ავ", 5999, 120, publish_date="2026-10-21")
    rows = today[:30] + yesterday[:30] + today[30:] + yesterday[30:]
    session = _FakeFeedSession(_feed_pages(rows))
    job, persisted = _feed_job(feed_repos, session, first_run_max_pages=10)

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30, 60, 90, 120, 150]
    assert persisted == ["კვ000000170"]
    assert feed_state_repo.get().last_successful_poll_at == _utc(21)


async def test_first_run_cap_during_the_lookahead_is_an_error_not_a_success(feed_repos):
    feed_state_repo = feed_repos[2]
    rows = _receipts("ავ", 9999, 30) + _receipts("ავ", 5999, 90, publish_date="2026-10-21")
    session = _FakeFeedSession(_feed_pages(rows))
    job, _persisted = _feed_job(feed_repos, session, first_run_max_pages=3)

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30, 60]
    state = feed_state_repo.get()
    assert state.last_successful_poll_at is None
    assert state.last_error.startswith(
        "first-run catch-up: safety cap of 3 pages reached during the look-ahead after rows published "
        "before today (look-ahead: 1 of 2 pages read)"
    )
    assert "fine_monitor.public_feed_first_run_max_pages" in state.last_error


async def test_deep_burst_repeats_do_not_count_as_horizon_pages(feed_repos):
    """30 строк, опубликованных сразу после первой обработанной страницы
    DEEP, снова показывают её: повтор нейтрален, поэтому DEEP всё равно
    читает две свежие обработанные страницы — а вставленные под ними строки
    сбрасывают look-ahead и читаются."""
    feed_state_repo, seen_repo = feed_repos[2], feed_repos[3]
    old, yesterday = _old_feed_rows()
    inserted = [_receipt_match("ავ", 1999)] + _receipts("კვ", 199, 2)
    feed = _CountingLiveFeed(_receipts("ავ", 9999, 12) + old[:90] + inserted + old[90:] + yesterday)
    job, persisted = _feed_job(feed_repos, feed)
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    _mark_seen_at(seen_repo, old + yesterday, seen_at=_utc(15))
    feed_state_repo.record_error("ConnectTimeout", at=_utc(17))
    feed.publish_after_requests(2, _receipts("ავ", 19999, 30))

    await job.run(_utc(21))

    # 0 новые; 30 обработанная; 60 повторяет 30 (нейтральна); 90 обработанная;
    # 120 вставка (сброс); 150–210 три обработанные страницы.
    assert feed.requested_offsets == [i * 30 for i in range(8)]
    assert persisted == ["ავ000001999"]
    assert feed_state_repo.get().last_error is None


async def test_one_job_instance_runs_consecutive_cycles_like_production(tmp_path):
    """В production один объект job'а на все циклы: ничто из одного цикла
    (отмеченные им строки) не должно менять, где остановится следующий."""
    task_repo = _make_task_repo(tmp_path)
    fine_repo = _make_fine_repo(tmp_path)
    feed_state_repo = _make_feed_state_repo(tmp_path)
    seen_repo = _make_seen_repo(tmp_path)
    try:
        _create_active_task(task_repo, "AA001AA")
        feed = _LiveFeed(
            _rows("OLD", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"),
        )
        job, _provider = _make_job(feed, task_repo, fine_repo, feed_state_repo, seen_repo, first_run_max_pages=10)
        await job.run(_utc(9))

        for cycle, run_at in enumerate((_utc(15), _utc(21), _utc(23, 55))):
            feed.publish(_rows(f"C{cycle}_", 45, publish_date="2026-10-22"))
            feed.requested_offsets.clear()
            await job.run(run_at)
            # 45 новых строк (страницы 0–1), тихая страница, 2 страницы look-ahead.
            assert feed.requested_offsets == [0, 30, 60, 90, 120], cycle
            assert feed_state_repo.get().last_error is None

        assert all(seen_repo.is_seen(row["protocolNo"]) for row in feed.rows)
    finally:
        task_repo.close()
        fine_repo.close()
        feed_state_repo.close()
        seen_repo.close()
