"""
Тесты GeorgiaPublicFineFeedJob (см. задачу "Georgia fines incremental
public-feed monitoring", ИСПРАВЛЕНО корректирующей задачей "IMPORTANT
CORRECTION BEFORE COMMIT / PR" — исходная date-overlap версия
пересматривала весь предыдущий день на КАЖДОМ 5-минутном цикле, ~6600
запросов/день вместо ожидаемых ~288/день).

Главная проверяемая идея: обычный (FAST) цикл останавливается, как только
страница содержит ХОТЯ БЫ ОДНУ уже знакомую (georgia_public_feed_seen)
запись — стоимость O(число новых записей), а не O(глубина backlog'а за
день).

_FakeFeedSession — фейковая замена PoliceGeSession: отдаёт заранее
заготовленные страницы по firstResult, фиксирует запрошенные offset'ы.
FineMonitoringTaskRepository/DetectedFineRepository/
GeorgiaPublicFeedStateRepository/GeorgiaPublicFeedSeenRepository —
настоящие (SQLite, tmp_path).
"""

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
from reader.jobs.georgia_public_feed_job import GeorgiaPublicFineFeedJob  # noqa: E402

pytestmark = pytest.mark.asyncio

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


async def test_frontier_on_page1_plus_overlap_entirely_seen_exactly_two_requests(tmp_path):
    """Тест 1/2 корректирующей задачи ("One final safety adjustment before
    merge"): граница найдена на самой первой странице (типичный "тихий"
    цикл) -> РОВНО ОДНА дополнительная overlap-страница (целиком уже
    известная) -> 2 запроса, НЕ 1."""
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
        overlap_entries = [_entry(protocol_no=f"OLDER{i:03d}", plate=f"XX{i:03d}XX") for i in range(30)]
        for e in overlap_entries:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        session = _FakeFeedSession({0: page0, 30: _page(overlap_entries)})
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_RUN_AT)

        assert session.requested_offsets == [0, 30]
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
    overlap-странице (2) ЕСТЬ ещё одна новая, несовпавшая строка ДО этого,
    и одна СОВПАВШАЯ строка — обе должны быть обработаны нормально, а не
    отброшены только потому, что это "overlap"."""
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

        assert session.requested_offsets == [0, 30]
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


async def test_30_unseen_then_frontier_on_page2_overlap_on_page3_three_requests(tmp_path):
    """Тест 2/3 корректирующей задачи."""
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
        overlap_entries = [_entry(protocol_no=f"OLDER{i:03d}", plate=f"XX{i:03d}XX") for i in range(30)]
        for e in overlap_entries:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        session = _FakeFeedSession({
            0: _page(page0_entries), 30: _page(known_entries), 60: _page(overlap_entries),
        })
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_RUN_AT)

        assert session.requested_offsets == [0, 30, 60]
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


async def test_75_unseen_frontier_on_page3_overlap_on_page4_four_requests(tmp_path):
    """Тест 4 корректирующей задачи (75 новых -> граница на странице 3 ->
    overlap на странице 4 -> 4 запроса, см. "IMPORTANT CORRECTION" burst
    example)."""
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
        overlap_entries = [_entry(protocol_no=f"OLDER{i:03d}", plate=f"WW{i:03d}WW") for i in range(30)]
        for e in overlap_entries:
            seen_repo.mark_seen(e["protocolNo"], publish_date=_TODAY)
        session = _FakeFeedSession({
            0: _page(page0), 30: _page(page1), 60: _page([page2_new] + known),
            90: _page(overlap_entries),
        })
        job, _provider = _make_job(session, task_repo, fine_repo, feed_state_repo, seen_repo)

        await job.run(_RUN_AT)

        assert session.requested_offsets == [0, 30, 60, 90]
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
        # в пределах first_run_max_pages=2.
        pages = {offset: _page([_entry(protocol_no=f"P{offset}_{i}") for i in range(30)]) for offset in (0, 30)}
        session = _FakeFeedSession(pages)
        job, _provider = _make_job(
            session, task_repo, fine_repo, feed_state_repo, seen_repo, first_run_max_pages=2,
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
        session = _FakeFeedSession({0: _page([_entry(plate="AA001AA")]), 30: _empty_page()})
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
