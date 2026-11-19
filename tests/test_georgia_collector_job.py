"""Tests for GeorgiaCollectorJob (reader/georgia_collector/collector_job.py)
— the REDNET-side poller/bootstrap loop, see "Georgia remote collector
architecture (REDNET)". Covers: exact wall-clock schedule (same semantics
as FineJob/GeorgiaPublicFineFeedJob), production-API-unavailable handling
(outbox queueing, no lost matches), police.ge-unavailable handling (circuit
breaker gate, error recording via the local feed_state), and the
no-Telegram-import architectural guarantee."""

import asyncio
import logging
import re
import sys
from datetime import date, datetime, timezone
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.fines.provider import FineProviderError
from reader.fines.public_feed_seen_repository import (
    GeorgiaPublicFeedSeenRepository,
)
from reader.fines.public_feed_state_repository import (
    GeorgiaPublicFeedStateRepository,
)
from reader.georgia_collector.__main__ import (
    load_fast_lookahead_pages,
    load_page_limits,
)
from reader.georgia_collector.collector_job import (
    FAST_LOOKAHEAD_PAGES,
    GeorgiaCollectorJob,
)
from reader.georgia_collector.outbox_repository import (
    KIND_FEED_MATCH,
    CollectorOutboxRepository,
)
from reader.georgia_collector.production_client import ProductionApiError

# Async tests run through asyncio_mode = auto (pytest.ini): no module-wide
# asyncio mark, which only made the synchronous tests here warn.

_TBILISI = ZoneInfo("Asia/Tbilisi")
_DEFAULT_RUN_TIMES = [dt_time(9, 0), dt_time(15, 0), dt_time(21, 0), dt_time(23, 55)]


def _utc(hour: int, minute: int = 0, *, day: int = 22) -> datetime:
    local = datetime(2026, 10, day, hour, minute, tzinfo=_TBILISI)
    return local.astimezone(timezone.utc)


def _empty_page() -> dict:
    return {"success": True, "data": {"count": 0, "results": []}}


def _page(entries: list[dict]) -> dict:
    return {"success": True, "data": {"count": len(entries), "results": entries}}


def _entry(*, protocol_no="AB000001", plate="AA001AA", publish_date="2026-10-22", amount=50) -> dict:
    return {
        "protocolAuto": plate,
        "protocolNo": protocol_no,
        "protocolDate": "2026-10-22",
        "activeDate": None,
        "violationDate": "2026-10-22",
        "protocolPlace": "ქუჩა",
        "protocolLaw": "125-1",
        "protocolLawDescription": "აღწერა",
        "protocolAmount": amount,
        "publishDate": publish_date,
        "lastDate": "2026-12-21",
        "remainingDays": 60,
    }


class _FakeSession:
    def __init__(
        self, pages_by_offset: dict[int, dict] | None = None, *, available=True, search_results=None,
        errors: dict[int, Exception] | None = None,
    ):
        self._pages = pages_by_offset or {}
        self._available = available
        self._search_results = search_results or {}
        self._errors = errors or {}
        self.requested_offsets: list[int] = []
        self.search_calls: list[str] = []

    def is_available(self) -> bool:
        return self._available

    async def fetch_published_protocols(self, *, first_result: int) -> dict:
        self.requested_offsets.append(first_result)
        if first_result in self._errors:
            raise self._errors[first_result]
        return self._pages.get(first_result, _empty_page())

    async def search_by_plate(self, plate: str) -> dict:
        self.search_calls.append(plate)
        if plate in self._search_results:
            return self._search_results[plate]
        return {"success": True, "data": {"count": 0, "results": []}}


class _FakeProductionClient:
    def __init__(self, *, active_plates=None, pending_plates=None, fail_plates=False, fail_submit=False):
        self._active_plates = active_plates or []
        self._pending_plates = pending_plates or []
        self._fail_plates = fail_plates
        self._fail_submit = fail_submit
        self.submitted_feed_matches: list[dict] = []
        self.submitted_bootstrap_results: list[dict] = []

    async def get_active_plates(self) -> list[str]:
        if self._fail_plates:
            raise ProductionApiError("production unavailable")
        return self._active_plates

    async def get_pending_bootstrap_plates(self) -> list[str]:
        if self._fail_plates:
            raise ProductionApiError("production unavailable")
        return self._pending_plates

    async def submit_feed_match(self, payload: dict) -> dict:
        if self._fail_submit:
            raise ProductionApiError("production unavailable")
        self.submitted_feed_matches.append(payload)
        return {"status": "ok", "tasks_matched": 1}

    async def submit_bootstrap_result(self, payload: dict) -> dict:
        if self._fail_submit:
            raise ProductionApiError("production unavailable")
        self.submitted_bootstrap_results.append(payload)
        return {"status": "ok"}


def _make_job(
    tmp_path, session, production_client, *, run_times=None, max_pages_per_cycle=30, first_run_max_pages=100,
):
    db_path = tmp_path / "collector.db"
    feed_state_repo = GeorgiaPublicFeedStateRepository(db_path)
    seen_repo = GeorgiaPublicFeedSeenRepository(db_path)
    outbox_repo = CollectorOutboxRepository(db_path)
    job = GeorgiaCollectorJob(
        session, production_client, feed_state_repo, seen_repo, outbox_repo,
        run_times=run_times or _DEFAULT_RUN_TIMES, tz=_TBILISI,
        max_pages_per_cycle=max_pages_per_cycle, first_run_max_pages=first_run_max_pages,
    )
    return job, feed_state_repo, seen_repo, outbox_repo


# ==== schedule — same exact wall-clock semantics as FineJob/
# GeorgiaPublicFineFeedJob ====


@pytest.mark.parametrize("hour,minute", [(9, 0), (15, 0), (21, 0), (23, 55)])
async def test_should_run_true_at_each_scheduled_slot(tmp_path, hour, minute):
    job, *_ = _make_job(tmp_path, _FakeSession(), _FakeProductionClient())
    assert await job.should_run(_utc(hour, minute)) is True


@pytest.mark.parametrize("hour", [0, 3, 6, 12, 18])
async def test_should_run_false_outside_scheduled_slots(tmp_path, hour):
    job, *_ = _make_job(tmp_path, _FakeSession(), _FakeProductionClient())
    assert await job.should_run(_utc(hour)) is False


async def test_should_run_after_23_55_next_slot_is_09_00_next_day(tmp_path):
    job, *_ = _make_job(tmp_path, _FakeSession(), _FakeProductionClient())
    assert await job.should_run(_utc(23, 55)) is True
    assert await job.should_run(_utc(23, 55)) is False
    assert await job.should_run(_utc(9, day=23)) is True


# ==== production API unavailable — no lost matches ====


async def test_production_unavailable_for_plates_skips_cycle_gracefully(tmp_path):
    session = _FakeSession({0: _page([_entry()])})
    production = _FakeProductionClient(fail_plates=True)
    job, _feed_state_repo, _seen, _outbox = _make_job(tmp_path, session, production)

    await job.run(_utc(9))

    # Ни одной страницы feed'а не запрашивалось — мы даже не знаем, какие
    # plates мониторить, запрашивать police.ge бессмысленно и расточительно.
    assert session.requested_offsets == []


async def test_feed_match_submission_failure_queues_to_outbox_not_lost(tmp_path):
    session = _FakeSession({0: _page([_entry()]), 30: _empty_page()})
    production = _FakeProductionClient(active_plates=["AA001AA"], fail_submit=True)
    job, _feed_state, _seen, outbox = _make_job(tmp_path, session, production)

    await job.run(_utc(9))

    assert outbox.count() == 1
    entries = outbox.list_all()
    assert entries[0].kind == KIND_FEED_MATCH
    assert entries[0].payload["car_number"] == "AA001AA"
    assert production.submitted_feed_matches == []  # не "потеряно" — в outbox


async def test_outbox_entry_flushed_on_next_successful_cycle(tmp_path):
    session = _FakeSession({0: _empty_page()})
    production = _FakeProductionClient(active_plates=[])
    job, _feed_state, _seen, outbox = _make_job(tmp_path, session, production)
    outbox.enqueue(KIND_FEED_MATCH, {"car_number": "AA001AA", "fingerprint": "x"})

    await job.run(_utc(9))

    assert outbox.count() == 0
    assert production.submitted_feed_matches == [{"car_number": "AA001AA", "fingerprint": "x"}]


async def test_outbox_entry_still_undeliverable_is_retried_not_dropped(tmp_path):
    session = _FakeSession({0: _empty_page()})
    production = _FakeProductionClient(active_plates=[], fail_submit=True)
    job, _feed_state, _seen, outbox = _make_job(tmp_path, session, production)
    outbox.enqueue(KIND_FEED_MATCH, {"car_number": "AA001AA", "fingerprint": "x"})

    await job.run(_utc(9))

    assert outbox.count() == 1
    assert outbox.list_all()[0].attempts == 1


async def test_outbox_entry_never_dropped_even_after_many_failed_attempts(tmp_path, caplog):
    """Outbox durability review (PR #34 integration): "bounded retry" means
    bounded BACKOFF/visibility, not "eventually delete the match" — requirement
    G is explicit that the collector "does not lose already fetched matched
    fines." A permanently-undeliverable entry must keep being retried every
    cycle, forever, never silently dropped — only the log level escalates
    (INFO -> WARNING) once it's clearly stuck, so an operator notices."""
    import logging

    session = _FakeSession({0: _empty_page()})
    production = _FakeProductionClient(active_plates=[], fail_submit=True)
    job, _feed_state, _seen, outbox = _make_job(tmp_path, session, production)
    outbox.enqueue(KIND_FEED_MATCH, {"car_number": "AA001AA", "fingerprint": "x"})

    with caplog.at_level(logging.INFO, logger="reader.georgia_collector.collector_job"):
        for _ in range(12):
            await job._flush_outbox()

    # Still exactly one entry, never removed, no matter how many cycles failed.
    assert outbox.count() == 1
    assert outbox.list_all()[0].attempts == 12

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING and "stuck" in r.message]
    assert len(warning_records) >= 1  # escalated once the threshold (10) was crossed
    info_records = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "ещё не доставлен" in r.message
    ]
    assert len(info_records) == 9  # attempts 1..9, before the threshold escalation kicks in


# ==== police.ge unavailable — circuit breaker gate, safe error recording ====


async def test_circuit_breaker_open_skips_cycle_without_destructive_state_change(tmp_path):
    session = _FakeSession(available=False)
    production = _FakeProductionClient(active_plates=["AA001AA"])
    job, feed_state_repo, _seen, _outbox = _make_job(tmp_path, session, production)

    await job.run(_utc(9))

    assert session.requested_offsets == []
    # Ничего не сломано — следующий цикл может восстановиться нормально.
    state = feed_state_repo.get()
    assert state.last_error is None


async def test_transport_failure_records_error_recovers_next_run(tmp_path):
    session = _FakeSession({0: _page([_entry()])}, )

    async def failing_fetch(*, first_result):
        raise FineProviderError("police.ge timeout")

    session.fetch_published_protocols = failing_fetch  # type: ignore[assignment]
    production = _FakeProductionClient(active_plates=["AA001AA"])
    job, feed_state_repo, _seen, _outbox = _make_job(tmp_path, session, production)

    await job.run(_utc(9))

    state = feed_state_repo.get()
    assert state.last_error is not None

    # Следующий цикл восстанавливается нормально (DEEP mode из-за last_error,
    # но завершается успешно на пустой странице).
    session2 = _FakeSession({0: _empty_page()})
    job2, feed_state_repo2, _seen2, _outbox2 = _make_job(tmp_path, session2, production)
    await job2.run(_utc(15))
    state2 = feed_state_repo2.get()
    assert state2.last_error is None


# ==== matched row actually reaches production ====


async def test_matched_entry_submitted_to_production(tmp_path):
    session = _FakeSession({0: _page([_entry(plate="AA001AA")]), 30: _empty_page()})
    production = _FakeProductionClient(active_plates=["AA001AA"])
    job, *_ = _make_job(tmp_path, session, production)

    await job.run(_utc(9))

    assert len(production.submitted_feed_matches) == 1
    assert production.submitted_feed_matches[0]["car_number"] == "AA001AA"
    assert "fingerprint" in production.submitted_feed_matches[0]


async def test_unmatched_entry_never_submitted(tmp_path):
    session = _FakeSession({0: _page([_entry(plate="ZZ999ZZ")]), 30: _empty_page()})
    production = _FakeProductionClient(active_plates=["AA001AA"])
    job, *_ = _make_job(tmp_path, session, production)

    await job.run(_utc(9))

    assert production.submitted_feed_matches == []


async def test_bootstrap_lookup_submits_result_including_zero_fines(tmp_path):
    session = _FakeSession({0: _empty_page()}, search_results={"AA001AA": {"success": True, "data": {"count": 0, "results": []}}})
    production = _FakeProductionClient(active_plates=[], pending_plates=["AA001AA"])
    job, *_ = _make_job(tmp_path, session, production)

    await job.run(_utc(9))

    assert session.search_calls == ["AA001AA"]
    assert len(production.submitted_bootstrap_results) == 1
    assert production.submitted_bootstrap_results[0] == {"car_number": "AA001AA", "fines": []}


# ==== no Telegram / production-DB imports (architectural guarantee) ====


def test_collector_job_module_never_imports_telegram_or_production_db():
    """См. задачу "A. Collector runs on REDNET only": "NO Telegram bot
    token... NO direct production DB access". Проверяем на уровне графа
    импортов, а не только по соглашению."""
    import subprocess
    import sys as _sys

    script = (
        "import sys; import reader.georgia_collector.collector_job; "
        "import reader.georgia_collector.production_client; "
        "import reader.georgia_collector.outbox_repository; "
        "import reader.georgia_collector.check_now_server; "
        "forbidden = ['telethon', 'reader.public_bot', 'reader.jobs', 'reader.users', 'reader.notifications']; "
        "leaked = [m for m in sys.modules if any(m == f or m.startswith(f + '.') for f in forbidden)]; "
        "print(leaked)"
    )
    result = subprocess.run(
        [_sys.executable, "-c", script],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


# ==== DEEP horizon: first run and recovery complete without an empty page
# (mirrors tests/test_georgia_public_feed_job.py) ====


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


async def test_first_run_completes_at_first_page_older_than_today_then_fast(tmp_path):
    today_rows = _rows("T0_", 29, publish_date="2026-10-22")
    session = _FakeSession({
        0: _page(today_rows + [_entry(protocol_no="MATCH001", plate="AA001AA", publish_date="2026-10-22")]),
        30: _page(_rows("T1_", 30, publish_date="2026-10-22")),
        60: _page(_rows("Y0_", 30, publish_date="2026-10-21")),
        90: _page(_rows("Y1_", 30, publish_date="2026-10-21")),
    })
    production = _FakeProductionClient(active_plates=["AA001AA"])
    job, feed_state_repo, _seen, _outbox = _make_job(tmp_path, session, production, first_run_max_pages=10)

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30, 60]
    assert [m["external_fine_id"] for m in production.submitted_feed_matches] == ["MATCH001"]
    state = feed_state_repo.get()
    assert state.last_successful_poll_at == _utc(21)
    assert state.last_error is None

    new_row = _entry(protocol_no="NEW001", plate="ZZ999ZZ", publish_date="2026-10-22")
    session2 = _FakeSession({0: _page([new_row] + today_rows)})
    job2, *_ = _make_job(tmp_path, session2, production)
    await job2.run(_utc(23, 55))

    assert session2.requested_offsets == [0, 30]


async def test_first_run_cap_on_todays_rows_is_an_error_and_next_cycle_stays_deep(tmp_path):
    pages = {
        0: _page(_rows("T0_", 30, publish_date="2026-10-22")),
        30: _page(_rows("T1_", 30, publish_date="2026-10-22")),
        60: _page(_rows("Y0_", 30, publish_date="2026-10-21")),
    }
    production = _FakeProductionClient(active_plates=["AA001AA"])
    session = _FakeSession(pages)
    job, feed_state_repo, _seen, _outbox = _make_job(tmp_path, session, production, first_run_max_pages=2)

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30]
    state = feed_state_repo.get()
    assert state.last_successful_poll_at is None
    assert "safety cap" in state.last_error

    session2 = _FakeSession(pages)
    job2, feed_state_repo2, *_ = _make_job(tmp_path, session2, production, first_run_max_pages=3)
    await job2.run(_utc(23, 55))

    assert session2.requested_offsets == [0, 30, 60]
    assert feed_state_repo2.get().last_error is None


async def test_recovery_stops_at_rows_seen_before_last_success_of_the_same_day(tmp_path):
    production = _FakeProductionClient(active_plates=["AA001AA"])
    earlier = _rows("E0_", 30, publish_date="2026-10-22")
    session = _FakeSession({
        0: _page(_rows("N0_", 30, publish_date="2026-10-22")),
        30: _page(earlier),
        60: _page(_rows("E1_", 30, publish_date="2026-10-22")),
    })
    job, feed_state_repo, seen_repo, _outbox = _make_job(tmp_path, session, production)
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    _mark_seen_at(seen_repo, earlier, seen_at=_utc(9))
    feed_state_repo.record_error("ConnectTimeout", at=_utc(21))

    await job.run(_utc(23, 55))

    assert session.requested_offsets == [0, 30]
    assert feed_state_repo.get().last_error is None


async def test_recovery_reads_through_rows_marked_after_last_success_and_fills_the_gap(tmp_path):
    production = _FakeProductionClient(active_plates=["AA001AA"])
    base = _rows("B0_", 30, publish_date="2026-10-22")
    top = _rows("T0_", 30, publish_date="2026-10-22")
    gap = _rows("G0_", 29, publish_date="2026-10-22") + [
        _entry(protocol_no="GAP_MATCH", plate="AA001AA", publish_date="2026-10-22"),
    ]
    failing = _FakeSession({0: _page(top)}, errors={30: FineProviderError("ConnectTimeout")})
    job1, feed_state_repo, seen_repo, _outbox = _make_job(tmp_path, failing, production)
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    _mark_seen_at(seen_repo, base, seen_at=_utc(9))

    await job1.run(_utc(21))
    assert feed_state_repo.get().last_error is not None
    assert production.submitted_feed_matches == []

    session = _FakeSession({
        0: _page(_rows("N0_", 30, publish_date="2026-10-22")),
        30: _page(top),
        60: _page(gap),
        90: _page(base),
        120: _page(_rows("O0_", 30, publish_date="2026-10-21")),
    })
    job2, feed_state_repo2, *_ = _make_job(tmp_path, session, production)
    await job2.run(_utc(23, 55))

    assert session.requested_offsets == [0, 30, 60, 90]
    assert [m["external_fine_id"] for m in production.submitted_feed_matches] == ["GAP_MATCH"]
    assert feed_state_repo2.get().last_error is None


async def test_recovery_stops_at_page_older_than_last_success_date_without_seen_rows(tmp_path):
    production = _FakeProductionClient(active_plates=["AA001AA"])
    session = _FakeSession({
        0: _page(_rows("N0_", 30, publish_date="2026-10-23")),
        30: _page(_rows("OLD0_", 30, publish_date="2026-10-21")),
        60: _page(_rows("OLD1_", 30, publish_date="2026-10-21")),
    })
    job, feed_state_repo, _seen, _outbox = _make_job(tmp_path, session, production)
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(23, 55))
    feed_state_repo.record_error("ConnectTimeout", at=_utc(9, day=23))

    await job.run(_utc(15, day=23))

    assert session.requested_offsets == [0, 30]
    assert feed_state_repo.get().last_error is None


async def test_row_without_publish_date_does_not_complete_deep_by_date(tmp_path):
    undated = _entry(protocol_no="NODATE1", plate="ZZ998ZZ", publish_date=None)
    session = _FakeSession({
        0: _page(_rows("Y0_", 29, publish_date="2026-10-21") + [undated]),
        30: _page(_rows("Y1_", 30, publish_date="2026-10-21")),
    })
    production = _FakeProductionClient(active_plates=["AA001AA"])
    job, feed_state_repo, _seen, _outbox = _make_job(tmp_path, session, production, first_run_max_pages=10)

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30]
    assert feed_state_repo.get().last_error is None


async def test_busy_fast_cycle_reaches_the_frontier_under_the_default_cap(tmp_path):
    """The busiest cycle (~3.5k new rows ≈ 120 pages) used to hit the old
    implicit 30/100-page caps; with the collector default (load_page_limits)
    FAST reaches the frontier, reads its look-ahead pages and the cycle
    succeeds (120 + 1 quiet page + 2 look-ahead pages)."""
    max_pages_per_cycle, _first_run = load_page_limits(env={})
    frontier = _rows("SEEN_", 30, publish_date="2026-10-22")
    pages = {i * 30: _page(_rows(f"P{i:03d}_", 30, publish_date="2026-10-22")) for i in range(120)}
    after = _rows("AFTER_", 30, publish_date="2026-10-22")
    after2 = _rows("AFTER2_", 30, publish_date="2026-10-22")
    pages[120 * 30] = _page(frontier)
    pages[121 * 30] = _page(after)
    pages[122 * 30] = _page(after2)
    session = _FakeSession(pages)
    production = _FakeProductionClient(active_plates=["AA001AA"])
    job, feed_state_repo, seen_repo, _outbox = _make_job(
        tmp_path, session, production, max_pages_per_cycle=max_pages_per_cycle,
    )
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    _mark_seen_at(seen_repo, frontier + after + after2, seen_at=_utc(15))

    await job.run(_utc(21))

    assert len(session.requested_offsets) == 123
    assert feed_state_repo.get().last_error is None


async def test_recovery_after_two_failed_cycles_fits_the_page_cap(tmp_path):
    """Regression for the "stuck recovery": last success late on 21.10, two
    failed cycles on 22.10. A date-only horizon (21.10) would need to re-read
    ALL rows of 21.10 (6 + 8 + 1 = 15 pages > cap 10, worse every cycle);
    rows seen before the success complete recovery on page 7."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    yesterday_pages = [_rows(f"Y{i}_", 30, publish_date="2026-10-21") for i in range(8)]
    failing = _FakeSession(errors={0: FineProviderError("ConnectTimeout")})
    job_failed, feed_state_repo, seen_repo, _outbox = _make_job(
        tmp_path, failing, production, max_pages_per_cycle=10,
    )
    feed_state_repo.record_successful_cycle(
        newest_seen_publish_date=date(2026, 10, 21), polled_at=_utc(23, 55, day=21),
    )
    for rows in yesterday_pages:
        _mark_seen_at(seen_repo, rows, seen_at=_utc(21, day=21))
    await job_failed.run(_utc(9))
    await job_failed.run(_utc(15))
    assert feed_state_repo.get().last_error is not None

    pages = {i * 30: _page(_rows(f"N{i}_", 30, publish_date="2026-10-22")) for i in range(6)}
    for i, rows in enumerate(yesterday_pages):
        pages[(6 + i) * 30] = _page(rows)
    pages[14 * 30] = _page(_rows("D20_", 30, publish_date="2026-10-20"))
    session = _FakeSession(pages)
    job, feed_state_repo2, *_ = _make_job(tmp_path, session, production, max_pages_per_cycle=10)

    await job.run(_utc(21))

    assert len(session.requested_offsets) == 7
    state = feed_state_repo2.get()
    assert state.last_error is None
    assert state.last_successful_poll_at == _utc(21)


async def test_rows_marked_by_an_earlier_successful_cycle_complete_a_later_recovery(tmp_path):
    """End-to-end check of first_seen_at = run_at: rows marked by the last
    successful cycle (first_seen_at == its run_at) complete a later
    recovery; rows of a later failed attempt do not."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    rows_a = _rows("A0_", 30, publish_date="2026-10-22")
    rows_b = _rows("B0_", 30, publish_date="2026-10-22")
    rows_k = _rows("K0_", 30, publish_date="2026-10-22")
    yesterday = _rows("Y0_", 30, publish_date="2026-10-21")

    first = _FakeSession({0: _page(rows_a), 30: _page(yesterday)})
    job1, feed_state_repo, *_ = _make_job(tmp_path, first, production)
    await job1.run(_utc(15))
    assert first.requested_offsets == [0, 30]

    second = _FakeSession({0: _page(rows_b), 30: _page(rows_a), 60: _page(yesterday)})
    job2, *_ = _make_job(tmp_path, second, production)
    await job2.run(_utc(21))
    assert second.requested_offsets == [0, 30, 60, 90]  # FAST: quiet page + look-ahead up to the empty page

    failing = _FakeSession({0: _page(rows_k)}, errors={30: FineProviderError("ConnectTimeout")})
    job3, *_ = _make_job(tmp_path, failing, production)
    await job3.run(_utc(23, 55))
    assert feed_state_repo.get().last_error is not None

    recovery = _FakeSession({
        0: _page(_rows("C0_", 30, publish_date="2026-10-23")),
        30: _page(rows_k),
        60: _page(rows_b),
        90: _page(rows_a),
        120: _page(yesterday),
    })
    job4, feed_state_repo4, *_ = _make_job(tmp_path, recovery, production)
    await job4.run(_utc(9, day=23))

    assert recovery.requested_offsets == [0, 30, 60]
    assert feed_state_repo4.get().last_error is None


def _submitted(production) -> list[str]:
    return [m["external_fine_id"] for m in production.submitted_feed_matches]


async def test_interrupted_cycle_is_followed_by_deep_recovery_that_reads_below_the_kill_point(tmp_path):
    """A cycle killed midway (SIGTERM/OOM/CancelledError — past every except)
    leaves the "cycle in progress" marker: the next cycle runs DEEP, not
    FAST, and reads the rows below the kill point."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    rows_s = _rows("S0_", 30, publish_date="2026-10-22")
    yesterday = _rows("Y0_", 30, publish_date="2026-10-21")
    first = _FakeSession({0: _page(rows_s), 30: _page(yesterday)})
    job1, feed_state_repo, *_ = _make_job(tmp_path, first, production)
    await job1.run(_utc(15))
    assert feed_state_repo.get().last_error is None

    k0 = _rows("K0_", 30, publish_date="2026-10-22")
    k1 = _rows("K1_", 30, publish_date="2026-10-22")
    k2 = _rows("K2_", 29, publish_date="2026-10-22") + [
        _entry(protocol_no="LOST_MATCH", plate="AA001AA", publish_date="2026-10-22"),
    ]
    killed = _FakeSession(
        {0: _page(k0), 30: _page(k1), 60: _page(k2), 90: _page(rows_s)},
        errors={30: asyncio.CancelledError()},
    )
    job2, *_ = _make_job(tmp_path, killed, production)
    with pytest.raises(asyncio.CancelledError):
        await job2.run(_utc(21))
    assert feed_state_repo.get().last_error is not None

    recovery = _FakeSession({
        0: _page(_rows("N0_", 30, publish_date="2026-10-22")),
        30: _page(k0),
        60: _page(k1),
        90: _page(k2),
        120: _page(rows_s),
        150: _page(yesterday),
    })
    job3, feed_state_repo3, *_ = _make_job(tmp_path, recovery, production)
    await job3.run(_utc(23, 55))

    assert recovery.requested_offsets == [0, 30, 60, 90, 120]
    assert _submitted(production) == ["LOST_MATCH"]
    assert feed_state_repo3.get().last_error is None


async def test_recovery_ignores_first_seen_times_when_last_success_is_not_in_the_past(tmp_path, caplog):
    """A success recorded with the clock a day ahead would make rows of the
    failed attempt and of the recovery itself look processed; the guard drops
    the first-seen criterion and keeps the date one."""
    import logging

    production = _FakeProductionClient(active_plates=["AA001AA"])
    g0 = _rows("G0_", 30, publish_date="2026-10-22")
    g1 = _rows("G1_", 29, publish_date="2026-10-22") + [
        _entry(protocol_no="SKEW_MATCH", plate="AA001AA", publish_date="2026-10-22"),
    ]
    failing = _FakeSession({0: _page(g0)}, errors={30: FineProviderError("ConnectTimeout")})
    job1, feed_state_repo, *_ = _make_job(tmp_path, failing, production)
    feed_state_repo.record_successful_cycle(
        newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(21, day=23),
    )
    await job1.run(_utc(21))
    assert feed_state_repo.get().last_error is not None

    recovery = _FakeSession({
        0: _page(_rows("N0_", 30, publish_date="2026-10-22")),
        30: _page(g0),
        60: _page(g1),
        90: _page(_rows("Y0_", 30, publish_date="2026-10-21")),
    })
    job2, feed_state_repo2, *_ = _make_job(tmp_path, recovery, production)
    with caplog.at_level(logging.WARNING, logger="reader.georgia_collector.collector_job"):
        await job2.run(_utc(23, 55))

    assert recovery.requested_offsets == [0, 30, 60, 90]
    assert _submitted(production) == ["SKEW_MATCH"]
    assert any("clock skew" in r.getMessage() for r in caplog.records)
    assert feed_state_repo2.get().last_error is None


async def test_future_dated_row_does_not_move_the_recovery_date_horizon_past_the_success_day(tmp_path):
    production = _FakeProductionClient(active_plates=["AA001AA"])
    future_row = _entry(protocol_no="FUT0001", plate="ZZ990ZZ", publish_date="2026-10-23")
    s0_rows = _rows("S0_", 29, publish_date="2026-10-22")
    first = _FakeSession({
        0: _page([future_row] + s0_rows),
        30: _page(_rows("Y0_", 30, publish_date="2026-10-21")),
    })
    job1, feed_state_repo, *_ = _make_job(tmp_path, first, production)
    await job1.run(_utc(21))
    assert feed_state_repo.get().newest_seen_publish_date == date(2026, 10, 23)

    failing = _FakeSession(errors={0: FineProviderError("ConnectTimeout")})
    job2, *_ = _make_job(tmp_path, failing, production)
    await job2.run(_utc(23, 55))

    g0 = _rows("G0_", 30, publish_date="2026-10-22")
    g1 = _rows("G1_", 29, publish_date="2026-10-22") + [
        _entry(protocol_no="FUT_GAP_MATCH", plate="AA001AA", publish_date="2026-10-22"),
    ]
    recovery = _FakeSession({
        0: _page([future_row] + _rows("H0_", 29, publish_date="2026-10-23")),
        30: _page(g0),
        60: _page(g1),
        90: _page(s0_rows),
    })
    job3, feed_state_repo3, *_ = _make_job(tmp_path, recovery, production)
    await job3.run(_utc(9, day=23))

    assert recovery.requested_offsets == [0, 30, 60, 90]
    assert _submitted(production) == ["FUT_GAP_MATCH"]
    assert feed_state_repo3.get().last_error is None


async def test_capped_recovery_says_it_cannot_reach_the_last_success_and_names_the_setting(tmp_path, caplog):
    import logging

    production = _FakeProductionClient(active_plates=["AA001AA"])
    session = _FakeSession({i * 30: _page(_rows(f"N{i}_", 30, publish_date="2026-10-22")) for i in range(5)})
    job, feed_state_repo, *_ = _make_job(tmp_path, session, production, max_pages_per_cycle=3)
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    feed_state_repo.record_error("ConnectTimeout", at=_utc(21))

    with caplog.at_level(logging.WARNING, logger="reader.georgia_collector.collector_job"):
        await job.run(_utc(23, 55))

    assert session.requested_offsets == [0, 30, 60]
    error = feed_state_repo.get().last_error
    assert "recovery could not reach rows covered by the last successful cycle within 3 pages" in error
    assert "GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE" in error
    assert any(r.levelno == logging.WARNING and error in r.getMessage() for r in caplog.records)


async def test_first_run_cap_has_its_own_message(tmp_path):
    production = _FakeProductionClient(active_plates=["AA001AA"])
    session = _FakeSession({
        0: _page(_rows("T0_", 30, publish_date="2026-10-22")),
        30: _page(_rows("T1_", 30, publish_date="2026-10-22")),
    })
    job, feed_state_repo, *_ = _make_job(tmp_path, session, production, first_run_max_pages=2)

    await job.run(_utc(21))

    error = feed_state_repo.get().last_error
    assert error.startswith("first-run catch-up: safety cap of 2 pages")
    assert "GEORGIA_COLLECTOR_FIRST_RUN_MAX_PAGES" in error
    assert "recovery could not reach" not in error


async def test_fast_cap_has_its_own_message(tmp_path):
    # A cap of 2 pages is no longer allowed with the 2 look-ahead pages.
    production = _FakeProductionClient(active_plates=["AA001AA"])
    session = _FakeSession({i * 30: _page(_rows(f"N{i}_", 30, publish_date="2026-10-22")) for i in range(4)})
    job, feed_state_repo, *_ = _make_job(tmp_path, session, production, max_pages_per_cycle=3)
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))

    await job.run(_utc(21))

    error = feed_state_repo.get().last_error
    assert error.startswith("safety cap of 3 pages reached before the frontier")
    assert "GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE" in error


async def test_fast_page_of_rows_seen_before_last_success_still_gets_its_lookahead_pages(tmp_path):
    """The DEEP horizon does not apply in FAST: a page of rows seen before the
    last success is an ordinary quiet page, followed by the look-ahead pages."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    old = _rows("OLD_", 30, publish_date="2026-10-22")
    older = _rows("OLDER_", 30, publish_date="2026-10-22")
    oldest = _rows("OLDEST_", 30, publish_date="2026-10-22")
    session = _FakeSession({0: _page(old), 30: _page(older), 60: _page(oldest)})
    job, feed_state_repo, seen_repo, _outbox = _make_job(tmp_path, session, production)
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    _mark_seen_at(seen_repo, old + older + oldest, seen_at=_utc(9))

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30, 60]


async def test_match_on_the_page_that_completes_deep_is_still_processed(tmp_path):
    """The page that completes DEEP is processed in full first (the detection
    loop is unchanged); only then does it stop the traversal."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    horizon_page = _rows("Y0_", 29, publish_date="2026-10-21") + [
        _entry(protocol_no="H_MATCH", plate="AA001AA", publish_date="2026-10-21"),
    ]
    session = _FakeSession({0: _page(_rows("T0_", 30, publish_date="2026-10-22")), 30: _page(horizon_page)})
    job, _feed_state, seen_repo, _outbox = _make_job(tmp_path, session, production, first_run_max_pages=10)

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30]
    assert _submitted(production) == ["H_MATCH"]
    assert seen_repo.is_seen("Y0_000")


async def test_first_run_horizon_uses_the_tbilisi_date_not_utc(tmp_path):
    """01:00 Tbilisi on 23.10 is 21:00 UTC on 22.10: "today" is 23.10."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    session = _FakeSession({
        0: _page(_rows("D23_", 30, publish_date="2026-10-23")),
        30: _page(_rows("D22_", 30, publish_date="2026-10-22")),
        60: _page(_rows("D22B_", 30, publish_date="2026-10-22")),
    })
    job, *_ = _make_job(tmp_path, session, production, first_run_max_pages=10)

    await job.run(_utc(1, day=23))

    assert session.requested_offsets == [0, 30]


# ==== live feed: publications between cycles and DURING a traversal
# (mirrors tests/test_georgia_public_feed_job.py) ====


class _LiveFeed:
    """A newest-first feed that changes like the real one: publish() puts
    rows on top; rows published while a cycle is still reading shift every
    later page down, so the next page repeats rows this cycle already read
    (production: bursts of 70–360 rows in the 15:00/21:00 cycles)."""

    def __init__(self, rows: list[dict] | None = None):
        self.rows: list[dict] = list(rows or [])
        self.requested_offsets: list[int] = []
        self._served = 0
        self._published_after: dict[int, list[dict]] = {}
        self._fail_at: dict[int, BaseException] = {}

    def publish(self, rows: list[dict]) -> None:
        self.rows[:0] = rows

    def publish_after_requests(self, count: int, rows: list[dict]) -> None:
        """rows appear on top right after the next `count` requests."""
        self._published_after[self._served + count - 1] = rows

    def fail_after_requests(self, count: int, exc: BaseException) -> None:
        """The request after the next `count` ones raises exc."""
        self._fail_at[self._served + count] = exc

    def is_available(self) -> bool:
        return True

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

    async def search_by_plate(self, plate: str) -> dict:
        return {"success": True, "data": {"count": 0, "results": []}}


async def test_fast_reads_new_rows_below_a_page_it_already_read_when_publications_shift_the_pages(tmp_path):
    """60 rows published after the 2nd page shift the list by two pages: pages
    3–4 repeat rows this cycle already marked. They are not the frontier —
    FAST goes on, reads the remaining new rows (and their match) and stops
    after a page of rows seen BEFORE this cycle plus the look-ahead pages."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("OLD", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    job1, feed_state_repo, seen_repo, _outbox = _make_job(tmp_path, feed, production, first_run_max_pages=10)
    await job1.run(_utc(15))
    assert feed_state_repo.get().last_error is None

    feed.publish(_rows("NEW", 119, publish_date="2026-10-22") + [
        _entry(protocol_no="DEEP_MATCH", plate="AA001AA", publish_date="2026-10-22"),
    ])
    feed.publish_after_requests(2, _rows("BURST", 60, publish_date="2026-10-22"))
    feed.requested_offsets.clear()
    job2, *_ = _make_job(tmp_path, feed, production)
    await job2.run(_utc(21))

    assert feed.requested_offsets == [0, 30, 60, 90, 120, 150, 180, 210, 240]
    assert _submitted(production) == ["DEEP_MATCH"]
    assert feed_state_repo.get().last_error is None

    feed.requested_offsets.clear()
    job3, *_ = _make_job(tmp_path, feed, production)
    await job3.run(_utc(23, 55))

    assert feed.requested_offsets == [0, 30, 60, 90, 120]
    assert all(seen_repo.is_seen(row["protocolNo"]) for row in feed.rows)
    assert _submitted(production) == ["DEEP_MATCH"]


async def test_interrupted_cycle_then_recovery_with_shifting_pages_reads_every_row_once(tmp_path):
    """End to end: a first run, a cycle killed after two pages, a recovery
    during which 45 rows are published on top, then FAST. Every published
    row ends up seen and every match is submitted exactly once."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("Y0_", 30, publish_date="2026-10-21"))
    feed.publish(_rows("A", 45, publish_date="2026-10-22"))
    job1, feed_state_repo, seen_repo, _outbox = _make_job(tmp_path, feed, production)
    await job1.run(_utc(9))
    assert feed_state_repo.get().last_error is None

    feed.publish(_rows("B", 89, publish_date="2026-10-22") + [
        _entry(protocol_no="MATCH_BELOW_KILL", plate="AA001AA", publish_date="2026-10-22"),
    ])
    feed.fail_after_requests(2, asyncio.CancelledError())
    job2, *_ = _make_job(tmp_path, feed, production)
    with pytest.raises(asyncio.CancelledError):
        await job2.run(_utc(15))
    assert feed_state_repo.get().last_error is not None

    feed.publish(_rows("C", 60, publish_date="2026-10-22"))
    feed.publish_after_requests(1, [_entry(protocol_no="MATCH_IN_BURST_TOP", plate="AA001AA")]
                                + _rows("D", 43, publish_date="2026-10-22")
                                + [_entry(protocol_no="MATCH_IN_BURST_BOTTOM", plate="AA001AA")])
    job3, *_ = _make_job(tmp_path, feed, production)
    await job3.run(_utc(21))
    assert feed_state_repo.get().last_error is None
    assert sorted(_submitted(production)) == ["MATCH_BELOW_KILL", "MATCH_IN_BURST_BOTTOM"]

    job4, *_ = _make_job(tmp_path, feed, production)
    await job4.run(_utc(23, 55))

    assert feed_state_repo.get().last_error is None
    assert all(seen_repo.is_seen(row["protocolNo"]) for row in feed.rows)
    assert sorted(_submitted(production)) == ["MATCH_BELOW_KILL", "MATCH_IN_BURST_BOTTOM", "MATCH_IN_BURST_TOP"]


async def test_fast_keeps_reading_when_a_burst_fills_the_overlap_page_right_after_the_frontier(tmp_path):
    """The frontier is on page 0 and 150 rows are published right after it:
    the next page holds only new rows. FAST does not stop there — it ends
    only after a quiet page and the look-ahead pages, so the oldest rows of
    the burst (and their match) are read now, not lost for good. The page
    where the burst re-shows the frontier's old rows (offset 150) is not
    quiet: this cycle has read those rows already."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("OLD", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    job1, feed_state_repo, seen_repo, _outbox = _make_job(tmp_path, feed, production, first_run_max_pages=10)
    await job1.run(_utc(15))

    feed.publish(_rows("NEW", 10, publish_date="2026-10-22"))
    burst = _rows("B", 150, publish_date="2026-10-22")
    burst[115] = _entry(protocol_no="BURST_MATCH", plate="AA001AA", publish_date="2026-10-22")
    feed.publish_after_requests(1, burst)
    feed.requested_offsets.clear()
    job2, *_ = _make_job(tmp_path, feed, production)
    await job2.run(_utc(21))

    assert feed.requested_offsets == [0, 30, 60, 90, 120, 150, 180, 210, 240]
    assert _submitted(production) == ["BURST_MATCH"]

    job3, *_ = _make_job(tmp_path, feed, production)
    await job3.run(_utc(23, 55))

    assert all(seen_repo.is_seen(row["protocolNo"]) for row in feed.rows)
    assert _submitted(production) == ["BURST_MATCH"]
    assert feed_state_repo.get().last_error is None


async def test_one_job_instance_runs_consecutive_cycles_like_production(tmp_path):
    """Production keeps one job object for every cycle: nothing from one
    cycle (rows it marked) may change how the next one stops."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("OLD", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    job, feed_state_repo, seen_repo, _outbox = _make_job(tmp_path, feed, production, first_run_max_pages=10)
    await job.run(_utc(9))

    for cycle, run_at in enumerate((_utc(15), _utc(21), _utc(23, 55))):
        feed.publish(_rows(f"C{cycle}_", 45, publish_date="2026-10-22"))
        feed.requested_offsets.clear()
        await job.run(run_at)
        # 45 new rows (pages 0–1), a quiet page, two look-ahead pages.
        assert feed.requested_offsets == [0, 30, 60, 90, 120], cycle
        assert feed_state_repo.get().last_error is None

    assert all(seen_repo.is_seen(row["protocolNo"]) for row in feed.rows)


# ==== FAST boundary: a quiet page plus FAST_LOOKAHEAD_PAGES more (mirrors
# tests/test_georgia_public_feed_job.py). Only run() and the fakes are used,
# so the same tests hold for the shared traversal of PR #57 once it is
# rebased onto this change — and fail if the old "second page with a seen
# row" stop comes back. Protocol numbers are synthetic. ====

_COLLECTOR_LOGGER = "reader.georgia_collector.collector_job"


def _receipts(series: str, first: int, count: int, *, publish_date: str = "2026-10-22") -> list[dict]:
    """count synthetic receipts of one protocol series ('ავ' or 'კვ'),
    numbers descending from first; their plates are never monitored."""
    return [
        _entry(protocol_no=f"{series}{first - i:09d}", plate=f"ZZ{i % 1000:03d}ZZ", publish_date=publish_date)
        for i in range(count)
    ]


def _receipt_match(series: str, number: int, *, publish_date: str = "2026-10-22") -> dict:
    return _entry(protocol_no=f"{series}{number:09d}", plate="AA001AA", publish_date=publish_date)


def _feed_pages(rows: list[dict]) -> dict[int, dict]:
    """rows (newest first) as police.ge pages of 30; data.count = all rows."""
    return {
        offset: {"success": True, "data": {"count": len(rows), "results": rows[offset:offset + 30]}}
        for offset in range(0, len(rows), 30)
    }


def _protocols(rows: list[dict]) -> list[str]:
    return [row["protocolNo"] for row in rows]


def _poll_fields(caplog) -> dict[str, str]:
    """key=value fields of the job's last "feed poll finished" line."""
    lines = [
        record.getMessage() for record in caplog.records
        if record.name == _COLLECTOR_LOGGER and "finished: pages=" in record.getMessage()
    ]
    assert lines, "no feed poll finished line"
    return dict(re.findall(r"(\w+)=([^,\s]+)", lines[-1]))


def _counters(caplog) -> dict[str, int]:
    fields = _poll_fields(caplog)
    return {key: int(fields[key]) for key in ("new_rows", "seen_among_new", "quiet_pages", "lookahead_new")}


def _fast_job_over(tmp_path, rows, old_rows, production, **kwargs):
    """A job whose next cycle is FAST over a fixed feed: the last success at
    15:00 Tbilisi saw old_rows."""
    session = _FakeSession(_feed_pages(rows))
    job, feed_state_repo, seen_repo, _outbox = _make_job(tmp_path, session, production, **kwargs)
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    _mark_seen_at(seen_repo, old_rows, seen_at=_utc(15))
    return job, session, feed_state_repo, seen_repo


def _old_rows_on_two_pages_above_the_boundary(old: list[dict], new: list[dict]) -> list[dict]:
    """95 new rows: re-shown old rows above new ones on pages 0 and 1, all-new
    page 2, the boundary on page 3 (7 new rows), quiet pages from page 4."""
    assert len(new) == 95
    return (
        new[0:10] + old[:1] + new[10:29]
        + new[29:49] + old[1:2] + new[49:58]
        + new[58:88]
        + new[88:95] + old[2:25]
        + old[25:]
    )


class _CountingLiveFeed(_LiveFeed):
    """_LiveFeed whose data.count is the whole list when the request comes,
    as on police.ge (page-0 probe, 19.11: count = all published rows)."""

    async def fetch_published_protocols(self, *, first_result: int) -> dict:
        total = len(self.rows)
        page = await super().fetch_published_protocols(first_result=first_result)
        page["data"]["count"] = total
        return page


async def _first_run(tmp_path, production, feed):
    """Successful DEEP first run at 09:00 over today's 150 'old' rows (and a
    page of yesterday's): every later cycle in these tests is FAST."""
    job, feed_state_repo, seen_repo, _outbox = _make_job(tmp_path, feed, production, first_run_max_pages=10)
    await job.run(_utc(9))
    assert feed_state_repo.get().last_error is None
    feed.requested_offsets.clear()
    return job, feed_state_repo, seen_repo


def _old_feed_rows() -> tuple[list[dict], list[dict]]:
    return _receipts("ავ", 5999, 150), _receipts("ავ", 3999, 30, publish_date="2026-10-21")


def test_fast_lookahead_is_two_pages_by_default():
    """K = 2 (the data is in FAST_LOOKAHEAD_PAGES' docstring); the scenarios
    below pin it in behaviour: rows on the 1st and 2nd page below the quiet
    page are read, rows on the 3rd are not."""
    assert FAST_LOOKAHEAD_PAGES == 2
    assert load_fast_lookahead_pages(env={}) == 2


@pytest.mark.parametrize("lookahead, max_pages, valid", [
    (0, 30, False), (1, 2, True), (2, 3, True), (3, 3, False), (4, 3, False),
])
def test_collector_job_takes_a_lookahead_from_one_to_below_the_page_cap(tmp_path, lookahead, max_pages, valid):
    """K < 1 makes no look-ahead; K >= the page cap makes every FAST cycle
    end at the cap."""
    db_path = tmp_path / "collector.db"

    def build():
        return GeorgiaCollectorJob(
            _FakeSession(), _FakeProductionClient(), GeorgiaPublicFeedStateRepository(db_path),
            GeorgiaPublicFeedSeenRepository(db_path), CollectorOutboxRepository(db_path),
            run_times=_DEFAULT_RUN_TIMES, tz=_TBILISI,
            max_pages_per_cycle=max_pages, fast_lookahead_pages=lookahead,
        )

    if valid:
        build()
    else:
        with pytest.raises(ValueError):
            build()


@pytest.mark.parametrize("old_on_page0", [1, 3], ids=["one_old_row", "several_old_rows"])
async def test_fast_reads_past_old_rows_shown_among_new_rows(tmp_path, caplog, old_on_page0):
    """Old rows re-shown among new ones on a page (a re-published receipt —
    production, 19.11), one or several: FAST does not stop there; it reads to
    the real boundary, a quiet page and two look-ahead pages, and counts the
    old rows that stood above new ones (not the boundary page's old tail)."""
    caplog.set_level(logging.INFO, logger=_COLLECTOR_LOGGER)
    production = _FakeProductionClient(active_plates=["AA001AA"])
    old = _receipts("ავ", 5999, 150)
    on_page0 = 30 - old_on_page0
    new = _receipts("ავ", 9999, on_page0 + 40)
    new[on_page0 + 15] = _receipt_match("ავ", 9000)   # page 1
    new[on_page0 + 35] = _receipt_match("კვ", 120)    # page 2, the boundary
    if old_on_page0 == 1:
        page0 = new[:10] + old[:1] + new[10:on_page0]
    else:
        page0 = new[:5] + old[:1] + new[5:8] + old[1:3] + new[8:on_page0]
    rows = page0 + new[on_page0:] + old[old_on_page0:]
    job, session, feed_state_repo, _seen = _fast_job_over(tmp_path, rows, old, production)

    await job.run(_utc(21))

    # page 0 (old rows among new), page 1 (new), page 2 (10 new + the
    # boundary), pages 3–5: the quiet page and two look-ahead pages.
    assert session.requested_offsets == [0, 30, 60, 90, 120, 150]
    assert sorted(_submitted(production)) == ["ავ000009000", "კვ000000120"]
    assert _counters(caplog) == {
        "new_rows": on_page0 + 40, "seen_among_new": old_on_page0, "quiet_pages": 3, "lookahead_new": 0,
    }
    assert feed_state_repo.get().last_error is None


async def test_fast_does_not_stop_on_old_rows_on_two_pages_above_the_real_boundary(tmp_path, caplog):
    """The case the old rule lost rows in: re-shown old rows on two different
    pages above the real boundary. The old "second page with a seen row" stop
    ended on page 1 and pages 2–3 were never read, now or later. FAST now
    reads to the boundary and its look-ahead; each match is submitted once."""
    caplog.set_level(logging.INFO, logger=_COLLECTOR_LOGGER)
    production = _FakeProductionClient(active_plates=["AA001AA"])
    old = _receipts("ავ", 5999, 150)
    new = _receipts("ავ", 9999, 95)
    new[70] = _receipt_match("ავ", 9100)   # page 2
    new[90] = _receipt_match("კვ", 130)    # page 3, the boundary
    rows = _old_rows_on_two_pages_above_the_boundary(old, new)
    job, session, feed_state_repo, seen_repo = _fast_job_over(tmp_path, rows, old, production)

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30, 60, 90, 120, 150, 180]
    assert sorted(_submitted(production)) == ["ავ000009100", "კვ000000130"]
    assert _counters(caplog) == {"new_rows": 95, "seen_among_new": 2, "quiet_pages": 3, "lookahead_new": 0}
    assert all(seen_repo.is_seen(p) for p in _protocols(new))
    assert feed_state_repo.get().last_error is None

    # The next cycle: 30 new rows on top, then the pages read above are quiet.
    session2 = _FakeSession(_feed_pages(_receipts("ავ", 19999, 30) + rows))
    job2, *_ = _make_job(tmp_path, session2, production)
    await job2.run(_utc(23, 55))
    assert session2.requested_offsets == [0, 30, 60, 90]
    assert sorted(_submitted(production)) == ["ავ000009100", "კვ000000130"]


@pytest.mark.parametrize("lookahead_page", [1, 2])
async def test_fast_reads_rows_inserted_within_the_lookahead_below_a_quiet_page(tmp_path, caplog, lookahead_page):
    """New rows below a fully old (quiet) page, on the first or the second
    look-ahead page: FAST resets, reads them (and their match) and stops only
    after a new quiet page and two look-ahead pages. lookahead_new counts
    them — the evidence of an insertion below a boundary."""
    caplog.set_level(logging.INFO, logger=_COLLECTOR_LOGGER)
    production = _FakeProductionClient(active_plates=["AA001AA"])
    old = _receipts("ავ", 5999, 240)
    new = _receipts("ავ", 9999, 12)
    inserted = [_receipt_match("ავ", 1999)] + _receipts("კვ", 199, 2)
    at = 28 + 30 * lookahead_page   # 10 rows into the look-ahead page
    rows = new + old[:at] + inserted + old[at:]
    job, session, feed_state_repo, seen_repo = _fast_job_over(tmp_path, rows, old, production)

    await job.run(_utc(21))

    # page 0: new rows + the boundary; page 1 quiet; the look-ahead page with
    # the inserted rows resets; then a quiet page and two look-ahead pages.
    assert session.requested_offsets == [i * 30 for i in range(lookahead_page + 5)]
    assert _submitted(production) == ["ავ000001999"]
    assert all(seen_repo.is_seen(p) for p in _protocols(inserted))
    assert _counters(caplog) == {
        "new_rows": 15, "seen_among_new": 10, "quiet_pages": lookahead_page + 3, "lookahead_new": 3,
    }
    assert feed_state_repo.get().last_error is None


async def test_fast_stops_two_pages_below_a_quiet_page_and_misses_rows_inserted_further_down(tmp_path, caplog):
    """Documented limitation: new rows inserted more than two look-ahead
    pages below the first quiet page are not read, by this cycle or later
    ones. Not observed in production (the feed is ordered by publication);
    the count diagnostic would show it — page 0's data.count grows by more
    than the new rows read. Nothing in the log names a receipt or a plate."""
    caplog.set_level(logging.INFO, logger=_COLLECTOR_LOGGER)
    production = _FakeProductionClient(active_plates=["AA001AA"])
    old, yesterday = _old_feed_rows()
    feed = _CountingLiveFeed(old + yesterday)
    job, feed_state_repo, seen_repo = await _first_run(tmp_path, production, feed)
    assert (_poll_fields(caplog)["reported_count"], _poll_fields(caplog)["reported_count_delta"]) == ("180", "n/a")

    feed.publish(_receipts("ავ", 9999, 12))
    inserted = [_receipt_match("ავ", 1999)] + _receipts("კვ", 199, 2)
    feed.rows[130:130] = inserted   # page 4: below the quiet page 1 and look-ahead pages 2–3
    await job.run(_utc(15))   # the same job object, as in production

    assert feed.requested_offsets == [0, 30, 60, 90]
    assert _submitted(production) == []
    assert not any(seen_repo.is_seen(p) for p in _protocols(inserted))
    state = feed_state_repo.get()
    assert (state.last_error, state.last_successful_poll_at) == (None, _utc(15))
    assert _counters(caplog) == {"new_rows": 12, "seen_among_new": 0, "quiet_pages": 3, "lookahead_new": 0}
    # 15 rows more since the previous cycle's page 0, 12 of them read: 3 out of reach.
    assert (_poll_fields(caplog)["reported_count"], _poll_fields(caplog)["reported_count_delta"]) == ("195", "+15")

    feed.publish(_receipts("ავ", 19999, 30))
    feed.requested_offsets.clear()
    await job.run(_utc(21))

    assert feed.requested_offsets == [0, 30, 60, 90]
    assert not any(seen_repo.is_seen(p) for p in _protocols(inserted))
    assert _submitted(production) == []
    fields = _poll_fields(caplog)
    assert (fields["new_rows"], fields["reported_count"], fields["reported_count_delta"]) == ("30", "225", "+30")
    named = set(_protocols(feed.rows)) | {row["protocolAuto"] for row in feed.rows}
    assert not [
        record for record in caplog.records
        if record.name == _COLLECTOR_LOGGER and any(value in record.getMessage() for value in named)
    ]


async def test_old_receipt_re_shown_among_new_rows_is_not_processed_again(tmp_path, caplog):
    """A receipt an earlier cycle saw (and submitted) is re-shown on top
    among new rows (re-publication or update): it is not submitted again, it
    is counted in seen_among_new, and FAST reads on to the boundary and the
    new match next to it."""
    caplog.set_level(logging.INFO, logger=_COLLECTOR_LOGGER)
    production = _FakeProductionClient(active_plates=["AA001AA"])
    old, yesterday = _old_feed_rows()
    old[40] = _receipt_match("ავ", 5000)
    feed = _LiveFeed(old + yesterday)
    job, _feed_state, seen_repo = await _first_run(tmp_path, production, feed)
    assert _submitted(production) == ["ავ000005000"]

    new = _receipts("ავ", 9999, 50)
    new[45] = _receipt_match("ავ", 9500)   # page 1
    feed.rows.remove(old[40])
    feed.publish(new[:7] + [old[40]] + new[7:])
    await job.run(_utc(15))

    # page 0: 29 new rows + the re-shown receipt; page 1: 21 new + the
    # boundary; pages 2–4: the quiet page and two look-ahead pages.
    assert feed.requested_offsets == [0, 30, 60, 90, 120]
    assert _submitted(production) == ["ავ000005000", "ავ000009500"]
    assert _counters(caplog) == {"new_rows": 50, "seen_among_new": 1, "quiet_pages": 3, "lookahead_new": 0}
    assert all(seen_repo.is_seen(p) for p in _protocols(feed.rows))


async def test_rows_a_burst_re_shows_in_the_same_cycle_are_not_processed_twice_or_quiet(tmp_path, caplog):
    """90 rows published after the 4th page re-show three pages this cycle
    has read: the same receipts (one of them a match) come twice in one
    cycle. They are not submitted twice and not counted as seen before, so
    those three pages are not a quiet page plus look-ahead: FAST reads on to
    the new rows below them (another match) and the real boundary."""
    caplog.set_level(logging.INFO, logger=_COLLECTOR_LOGGER)
    production = _FakeProductionClient(active_plates=["AA001AA"])
    old, yesterday = _old_feed_rows()
    feed = _LiveFeed(old + yesterday)
    job, _feed_state, seen_repo = await _first_run(tmp_path, production, feed)

    new = _receipts("ავ", 9999, 150)
    new[45] = _receipt_match("ავ", 9500)   # page 1, re-shown on page 5
    new[130] = _receipt_match("კვ", 150)   # page 7, below the re-shown pages
    feed.publish(new)
    feed.publish_after_requests(4, _receipts("ავ", 19999, 90))
    await job.run(_utc(15))

    # pages 0–3 new; the burst re-shows new[30:120] on pages 4–6; page 7 new;
    # pages 8–10: the quiet page and two look-ahead pages.
    assert feed.requested_offsets == [i * 30 for i in range(11)]
    assert sorted(_submitted(production)) == ["ავ000009500", "კვ000000150"]
    assert _counters(caplog) == {"new_rows": 150, "seen_among_new": 0, "quiet_pages": 3, "lookahead_new": 0}

    # The burst on top is read by the next cycle; every receipt exactly once.
    feed.requested_offsets.clear()
    await job.run(_utc(21))
    assert feed.requested_offsets == [0, 30, 60, 90, 120, 150]
    assert all(seen_repo.is_seen(p) for p in _protocols(feed.rows))
    assert sorted(_submitted(production)) == ["ავ000009500", "კვ000000150"]


async def test_a_burst_during_the_lookahead_does_not_use_it_up(tmp_path, caplog):
    """60 rows published right after the first quiet page shift the list by
    two pages: the next two pages re-show rows this cycle has already read
    (its new rows, the boundary, the quiet page). They are not look-ahead
    pages — FAST still reads two fresh pages below the quiet page and finds
    the rows inserted there."""
    caplog.set_level(logging.INFO, logger=_COLLECTOR_LOGGER)
    production = _FakeProductionClient(active_plates=["AA001AA"])
    old, yesterday = _old_feed_rows()
    feed = _LiveFeed(old + yesterday)
    job, _feed_state, seen_repo = await _first_run(tmp_path, production, feed)

    feed.publish(_receipts("ავ", 9999, 12))
    inserted = [_receipt_match("ავ", 1999)] + _receipts("კვ", 199, 2)
    feed.rows[102:102] = inserted   # the second look-ahead page, before the burst
    feed.publish_after_requests(2, _receipts("ავ", 19999, 60))
    await job.run(_utc(15))

    # page 0: new rows + the boundary; page 1 quiet; pages 2–3 re-show pages
    # 0–1; page 4 look-ahead; page 5 the inserted rows (reset); pages 6–8.
    assert feed.requested_offsets == [i * 30 for i in range(9)]
    assert _submitted(production) == ["ავ000001999"]
    assert _counters(caplog) == {"new_rows": 15, "seen_among_new": 12, "quiet_pages": 5, "lookahead_new": 3}
    assert all(seen_repo.is_seen(p) for p in _protocols(inserted))


@pytest.mark.parametrize(
    "kill_at, pages_read", [(120, 4), (210, 7)], ids=["above_the_boundary", "in_the_lookahead"],
)
async def test_fast_killed_mid_cycle_is_followed_by_a_deep_recovery_that_loses_nothing(
    tmp_path, kill_at, pages_read,
):
    """A FAST cycle killed (CancelledError) while reading new rows between
    re-shown old rows and the boundary, or during its look-ahead: the
    "cycle in progress" marker stays and no success is recorded. The
    restarted job runs DEEP recovery: it reads through the pages the killed
    attempt marked (for FAST they would be quiet pages) down to rows the last
    success had seen, and every match is submitted exactly once."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    old = _receipts("ავ", 5999, 150)
    new = _receipts("ავ", 9999, 155)
    new[130] = _receipt_match("ავ", 9300)   # page 4
    rows = (
        new[0:10] + old[:1] + new[10:29]
        + new[29:49] + old[1:2] + new[49:58]
        + new[58:148]                          # pages 2–4
        + new[148:155] + old[2:25]             # page 5: the boundary
        + old[25:]                             # pages 6–...: quiet
    )
    session = _FakeSession(_feed_pages(rows), errors={kill_at: asyncio.CancelledError()})
    job, feed_state_repo, seen_repo, _outbox = _make_job(tmp_path, session, production)
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
    recovery = _FakeSession(_feed_pages(top + rows))
    job2, feed_state_repo2, *_ = _make_job(tmp_path, recovery, production)   # restart
    await job2.run(_utc(23, 55))

    # DEEP reads down to the first page of rows the last success had seen
    # (offset 210) and stops there; FAST would have stopped on the killed
    # attempt's pages (offset 90). Only the end is pinned: how a recovery
    # crosses pages an earlier attempt read is not this rule's business.
    assert recovery.requested_offsets[-1] == max(recovery.requested_offsets) == 210
    assert sorted(_submitted(production)) == ["ავ000009300", "კვ000000160"]
    assert all(seen_repo.is_seen(p) for p in _protocols(top + new))
    state = feed_state_repo2.get()
    assert (state.last_error, state.last_successful_poll_at) == (None, _utc(23, 55))


async def test_fast_cap_before_the_boundary_is_an_error_and_the_next_cycle_reads_the_rest(tmp_path):
    """Re-shown old rows on pages 0 and 1 and a cap of 3 pages: the old rule
    declared success on page 1 and never read below. Now the cap comes
    first — an error, no success recorded, nothing stored that makes the
    next cycle skip rows — and the next cycle (DEEP) reads from the top
    through the capped attempt's rows to the boundary."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    old = _receipts("ავ", 5999, 150)
    new = _receipts("ავ", 9999, 95)
    new[70] = _receipt_match("ავ", 9100)   # page 2
    new[90] = _receipt_match("კვ", 130)    # page 3, the boundary
    rows = _old_rows_on_two_pages_above_the_boundary(old, new)
    job, session, feed_state_repo, seen_repo = _fast_job_over(
        tmp_path, rows, old, production, max_pages_per_cycle=3,
    )

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30, 60]
    state = feed_state_repo.get()
    assert (state.last_successful_poll_at, state.newest_seen_publish_date) == (_utc(15), date(2026, 10, 22))
    assert state.last_error.startswith("safety cap of 3 pages reached before the frontier (already-seen rows)")
    assert "GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE" in state.last_error
    assert _submitted(production) == ["ავ000009100"]

    session2 = _FakeSession(_feed_pages(rows))
    job2, feed_state_repo2, *_ = _make_job(tmp_path, session2, production)
    await job2.run(_utc(23, 55))

    # DEEP ends on the first page of rows the last success had seen (120).
    assert session2.requested_offsets[-1] == max(session2.requested_offsets) == 120
    assert sorted(_submitted(production)) == ["ავ000009100", "კვ000000130"]
    assert all(seen_repo.is_seen(p) for p in _protocols(new))
    state = feed_state_repo2.get()
    assert (state.last_error, state.last_successful_poll_at) == (None, _utc(23, 55))


async def test_fast_cap_during_the_lookahead_is_an_error_not_a_success(tmp_path):
    """The cap ends the cycle after the first quiet page, before the
    look-ahead is complete: the boundary is not confirmed, so no success is
    recorded and the error names the look-ahead. The next cycle runs DEEP:
    the capped attempt's rows are not covered by the last success, so it
    reads them again (with the rows published since) and stops at rows the
    last success had seen."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    old = _receipts("ავ", 5999, 150)
    new = _receipts("ავ", 9999, 12)
    new[4] = _receipt_match("ავ", 9400)
    rows = new + old
    job, session, feed_state_repo, seen_repo = _fast_job_over(
        tmp_path, rows, old, production, max_pages_per_cycle=3,
    )

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30, 60]
    state = feed_state_repo.get()
    assert (state.last_successful_poll_at, state.newest_seen_publish_date) == (_utc(15), date(2026, 10, 22))
    assert state.last_error.startswith(
        "safety cap of 3 pages reached before the frontier was confirmed "
        "(look-ahead: 1 of 2 pages read after a page without new rows)"
    )
    assert "GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE" in state.last_error
    assert _submitted(production) == ["ავ000009400"]

    top = _receipts("ავ", 19999, 30)
    top[7] = _receipt_match("კვ", 170)
    session2 = _FakeSession(_feed_pages(top + rows))
    job2, feed_state_repo2, *_ = _make_job(tmp_path, session2, production)
    await job2.run(_utc(23, 55))

    # DEEP: the new rows on top, the capped attempt's page (not covered by
    # the last success), then the first page the last success covered (60);
    # FAST would have gone on to 90.
    assert session2.requested_offsets[-1] == max(session2.requested_offsets) == 60
    assert sorted(_submitted(production)) == ["ავ000009400", "კვ000000170"]
    assert all(seen_repo.is_seen(p) for p in _protocols(top + new))
    state = feed_state_repo2.get()
    assert (state.last_error, state.last_successful_poll_at) == (None, _utc(23, 55))


@pytest.mark.parametrize("setting, read", [("1", False), ("3", True)])
async def test_lookahead_setting_sets_how_far_below_the_quiet_page_fast_reads(tmp_path, setting, read):
    """GEORGIA_COLLECTOR_FAST_LOOKAHEAD_PAGES: rows inserted on the second
    page below the first quiet page are read with K = 3, not with K = 1."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    old = _receipts("ავ", 5999, 240)
    rows = _receipts("ავ", 9999, 12) + old[:88] + [_receipt_match("ავ", 1999)] + old[88:]
    db_path = tmp_path / "collector.db"
    feed_state_repo = GeorgiaPublicFeedStateRepository(db_path)
    seen_repo = GeorgiaPublicFeedSeenRepository(db_path)
    job = GeorgiaCollectorJob(
        _FakeSession(_feed_pages(rows)), production, feed_state_repo, seen_repo, CollectorOutboxRepository(db_path),
        run_times=_DEFAULT_RUN_TIMES, tz=_TBILISI,
        fast_lookahead_pages=load_fast_lookahead_pages(env={"GEORGIA_COLLECTOR_FAST_LOOKAHEAD_PAGES": setting}),
    )
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    _mark_seen_at(seen_repo, old, seen_at=_utc(15))

    await job.run(_utc(21))

    assert seen_repo.is_seen("ავ000001999") is read
    assert _submitted(production) == (["ავ000001999"] if read else [])
    assert feed_state_repo.get().last_error is None


class _SubmitFails(_FakeProductionClient):
    """Production whose first feed-match submit dies with `exc` (a kill —
    CancelledError — or an unexpected error)."""

    def __init__(self, exc: BaseException, **kwargs):
        super().__init__(**kwargs)
        self._exc = exc

    async def submit_feed_match(self, payload: dict) -> dict:
        if self._exc is not None:
            exc, self._exc = self._exc, None
            raise exc
        return await super().submit_feed_match(payload)


@pytest.mark.parametrize("exc", [asyncio.CancelledError(), ValueError("unexpected response")])
async def test_match_is_not_lost_when_the_cycle_dies_during_its_submit(tmp_path, exc):
    """The payload is queued BEFORE the submit: a kill (or an unexpected
    error) mid-submit leaves the row seen but the match in the outbox, and
    the next cycle delivers it first — exactly once."""
    production = _SubmitFails(exc, active_plates=["AA001AA"])
    match = _entry(protocol_no="MATCH_IN_FLIGHT", plate="AA001AA", publish_date="2026-10-22")
    session = _FakeSession({0: _page(_rows("T0_", 29, publish_date="2026-10-22") + [match]),
                            30: _page(_rows("Y0_", 30, publish_date="2026-10-21"))})
    job, feed_state_repo, seen_repo, outbox_repo = _make_job(tmp_path, session, production, first_run_max_pages=10)

    if isinstance(exc, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await job.run(_utc(21))
        assert feed_state_repo.get().last_error is not None  # "cycle in progress" stays
    else:
        await job.run(_utc(21))
        assert feed_state_repo.get().last_error is None  # the traversal went on
    assert seen_repo.is_seen("MATCH_IN_FLIGHT")
    assert [entry.payload["external_fine_id"] for entry in outbox_repo.list_all()] == ["MATCH_IN_FLIGHT"]

    job2, *_ = _make_job(tmp_path, session, production, first_run_max_pages=10)
    await job2.run(_utc(23, 55))

    assert _submitted(production) == ["MATCH_IN_FLIGHT"]
    assert outbox_repo.list_all() == []


async def test_submitted_match_leaves_nothing_in_the_outbox(tmp_path):
    production = _FakeProductionClient(active_plates=["AA001AA"])
    match = _entry(protocol_no="MATCH_OK", plate="AA001AA", publish_date="2026-10-22")
    session = _FakeSession({0: _page([match]), 30: _page(_rows("Y0_", 30, publish_date="2026-10-21"))})
    job, _feed_state, _seen, outbox_repo = _make_job(tmp_path, session, production, first_run_max_pages=10)

    await job.run(_utc(21))

    assert _submitted(production) == ["MATCH_OK"]
    assert outbox_repo.list_all() == []


class _UnparseableAnswers(_FakeProductionClient):
    """Production accepts feed matches but answers `failures` times in a row
    with a body the client cannot parse (not a ProductionApiError)."""

    def __init__(self, failures: int, **kwargs):
        super().__init__(**kwargs)
        self._failures = failures

    async def submit_feed_match(self, payload: dict) -> dict:
        if self._failures > 0:
            self._failures -= 1
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return await super().submit_feed_match(payload)


async def test_a_recurring_unexpected_submit_error_never_stops_the_feed_poll(tmp_path):
    """The queued match keeps failing with an unexpected error on the next
    flushes: each cycle still reads the feed and runs bootstrap, the attempts
    are counted, and the match goes out once production answers normally."""
    production = _UnparseableAnswers(3, active_plates=["AA001AA"], pending_plates=["BB002BB"])
    feed = _LiveFeed(_rows("Y0_", 30, publish_date="2026-10-21"))
    feed.publish(_rows("A", 20, publish_date="2026-10-22") + [
        _entry(protocol_no="MATCH_BAD_ANSWER", plate="AA001AA", publish_date="2026-10-22"),
    ])
    job, feed_state_repo, seen_repo, outbox_repo = _make_job(tmp_path, feed, production, first_run_max_pages=10)

    await job.run(_utc(9))
    for cycle, run_at in enumerate((_utc(15), _utc(21))):
        feed.publish(_rows(f"N{cycle}_", 10, publish_date="2026-10-22"))
        feed.requested_offsets.clear()
        await job.run(run_at)
        assert feed.requested_offsets, cycle
        assert feed_state_repo.get().last_error is None
    assert [entry.attempts for entry in outbox_repo.list_all()] == [2]
    assert len(production.submitted_bootstrap_results) == 3

    await job.run(_utc(23, 55))

    assert outbox_repo.list_all() == []
    assert _submitted(production) == ["MATCH_BAD_ANSWER"]
    assert all(seen_repo.is_seen(row["protocolNo"]) for row in feed.rows)
