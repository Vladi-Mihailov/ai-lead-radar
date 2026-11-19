"""Tests for GeorgiaCollectorJob (reader/georgia_collector/collector_job.py)
— the REDNET-side poller/bootstrap loop, see "Georgia remote collector
architecture (REDNET)". Covers: exact wall-clock schedule (same semantics
as FineJob/GeorgiaPublicFineFeedJob), production-API-unavailable handling
(outbox queueing, no lost matches), police.ge-unavailable handling (circuit
breaker gate, error recording via the local feed_state), and the
no-Telegram-import architectural guarantee."""

import asyncio
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
from reader.georgia_collector.__main__ import load_page_limits
from reader.georgia_collector.collector_job import GeorgiaCollectorJob
from reader.georgia_collector.outbox_repository import (
    KIND_FEED_MATCH,
    CollectorOutboxRepository,
)
from reader.georgia_collector.production_client import ProductionApiError

pytestmark = pytest.mark.asyncio

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
    def __init__(
        self, *, active_plates=None, pending_plates=None, fail_plates=False, fail_submit=False, fail_health=None,
    ):
        self._active_plates = active_plates or []
        self._pending_plates = pending_plates or []
        self._fail_plates = fail_plates
        self._fail_submit = fail_submit
        self._fail_health = fail_health
        self.submitted_feed_matches: list[dict] = []
        self.submitted_bootstrap_results: list[dict] = []
        self.health_reports: list[dict] = []

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

    async def report_health(self, payload: dict) -> dict:
        if self._fail_health is not None:
            raise self._fail_health
        self.health_reports.append(payload)
        return {"status": "ack"}


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
    FAST reaches the frontier and the cycle succeeds."""
    max_pages_per_cycle, _first_run = load_page_limits(env={})
    frontier = _rows("SEEN_", 30, publish_date="2026-10-22")
    pages = {i * 30: _page(_rows(f"P{i:03d}_", 30, publish_date="2026-10-22")) for i in range(120)}
    after = _rows("AFTER_", 30, publish_date="2026-10-22")
    pages[120 * 30] = _page(frontier)
    pages[121 * 30] = _page(after)
    session = _FakeSession(pages)
    production = _FakeProductionClient(active_plates=["AA001AA"])
    job, feed_state_repo, seen_repo, _outbox = _make_job(
        tmp_path, session, production, max_pages_per_cycle=max_pages_per_cycle,
    )
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    _mark_seen_at(seen_repo, frontier + after, seen_at=_utc(15))

    await job.run(_utc(21))

    assert len(session.requested_offsets) == 122
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
    assert second.requested_offsets == [0, 30, 60]

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


async def test_capped_recovery_keeps_what_it_read_as_a_region_and_names_the_setting(tmp_path, caplog):
    import logging

    production = _FakeProductionClient(active_plates=["AA001AA"])
    session = _FakeSession({i * 30: _page(_rows(f"N{i}_", 30, publish_date="2026-10-22")) for i in range(5)})
    job, feed_state_repo, *_ = _make_job(tmp_path, session, production, max_pages_per_cycle=3)
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    feed_state_repo.record_error("ConnectTimeout", at=_utc(21))

    with caplog.at_level(logging.WARNING, logger="reader.georgia_collector.collector_job"):
        await job.run(_utc(23, 55))

    assert session.requested_offsets == [0, 30, 60]
    state = feed_state_repo.get()
    error = state.last_error
    assert "recovery reached the page cap of 3 pages before rows covered by the last successful cycle" in error
    assert "90 rows read so far in 1 region(s) are kept" in error
    assert "GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE" in error
    assert any(r.levelno == logging.WARNING and error in r.getMessage() for r in caplog.records)
    [region] = state.recovery_regions
    assert region.length == 90
    assert region.top == tuple(f"N0_{i:03d}" for i in range(30))
    assert region.bottom == tuple(f"N{p}_{i:03d}" for p in range(3) for i in range(30))


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
    assert "recovery reached the page cap" not in error
    assert feed_state_repo.get().recovery_regions == ()


async def test_fast_cap_has_its_own_message(tmp_path):
    production = _FakeProductionClient(active_plates=["AA001AA"])
    session = _FakeSession({i * 30: _page(_rows(f"N{i}_", 30, publish_date="2026-10-22")) for i in range(3)})
    job, feed_state_repo, *_ = _make_job(tmp_path, session, production, max_pages_per_cycle=2)
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))

    await job.run(_utc(21))

    error = feed_state_repo.get().last_error
    assert error.startswith("safety cap of 2 pages reached before the frontier")
    assert "GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE" in error


async def test_fast_page_of_rows_seen_before_last_success_still_gets_its_overlap_page(tmp_path):
    """The DEEP horizon does not apply in FAST: a page of rows seen before the
    last success is an ordinary frontier, followed by one overlap page."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    old = _rows("OLD_", 30, publish_date="2026-10-22")
    older = _rows("OLDER_", 30, publish_date="2026-10-22")
    session = _FakeSession({0: _page(old), 30: _page(older)})
    job, feed_state_repo, seen_repo, _outbox = _make_job(tmp_path, session, production)
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    _mark_seen_at(seen_repo, old + older, seen_at=_utc(9))

    await job.run(_utc(21))

    assert session.requested_offsets == [0, 30]


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

    def remove(self, protocol_nos: set[str]) -> None:
        self.rows = [row for row in self.rows if row["protocolNo"] not in protocol_nos]

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
    FAST goes on, reads the remaining new rows (and their match) and stops one
    page after rows seen BEFORE this cycle."""
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

    assert feed.requested_offsets == [0, 30, 60, 90, 120, 150, 180, 210]
    assert _submitted(production) == ["DEEP_MATCH"]
    assert feed_state_repo.get().last_error is None

    feed.requested_offsets.clear()
    job3, *_ = _make_job(tmp_path, feed, production)
    await job3.run(_utc(23, 55))

    assert feed.requested_offsets == [0, 30, 60, 90]
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
    the next page holds only new rows. FAST does not stop there — it ends on
    the second page with rows seen before this cycle, so the oldest rows of
    the burst (and their match) are read now, not lost for good."""
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

    assert feed.requested_offsets == [0, 30, 60, 90, 120, 150]
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
        assert feed.requested_offsets == [0, 30, 60], cycle
        assert feed_state_repo.get().last_error is None

    assert all(seen_repo.is_seen(row["protocolNo"]) for row in feed.rows)


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


# ==== recovery over several cycles: an outage longer than one cycle's page
# cap (reader/fines/public_feed_traversal.py; mirrors
# tests/test_georgia_public_feed_job.py) ====


def _with_matches(rows: list[dict], matches: dict[int, str]) -> list[dict]:
    """rows with the monitored plate at the given indexes (protocolNo as given)."""
    rows = list(rows)
    for index, protocol_no in matches.items():
        rows[index] = _entry(protocol_no=protocol_no, plate="AA001AA", publish_date=rows[index]["publishDate"])
    return rows


async def _first_run_before_the_outage(tmp_path, production, feed):
    """Successful first run at 09:00 22.10 over today's E rows and
    yesterday's Y rows: the last success every test below recovers from."""
    job, feed_state_repo, seen_repo, _outbox = _make_job(tmp_path, feed, production, first_run_max_pages=10)
    await job.run(_utc(9))
    assert feed_state_repo.get().last_error is None
    return feed_state_repo, seen_repo


async def _cycle(tmp_path, feed, production, at, *, cap):
    feed.requested_offsets.clear()
    job, *_ = _make_job(tmp_path, feed, production, max_pages_per_cycle=cap)
    await job.run(at)
    return list(feed.requested_offsets)


def _unseen(feed, seen_repo) -> list[str]:
    return [row["protocolNo"] for row in feed.rows if not seen_repo.is_seen(row["protocolNo"])]


_LATER_SLOTS = [_utc(h, m, day=d) for d in (23, 24, 25) for h, m in ((9, 0), (15, 0), (21, 0), (23, 55))]


async def _cycles_until_success(tmp_path, feed, production, feed_state_repo, seen_repo, *, cap, slots=_LATER_SLOTS):
    """Runs cycles until one succeeds; checks on the way that none reads
    more pages than the cap and that a success never leaves a row unread."""
    for number, at in enumerate(slots, start=1):
        present = [row["protocolNo"] for row in feed.rows]
        offsets = await _cycle(tmp_path, feed, production, at, cap=cap)
        assert len(offsets) <= cap
        if feed_state_repo.get().last_error is None:
            assert [p for p in present if not seen_repo.is_seen(p)] == []
            return number
    raise AssertionError("recovery did not complete")


async def test_outage_longer_than_the_cap_is_caught_up_over_several_cycles(tmp_path):
    """300 rows (10 pages) since the last success. FAST hits its cap and keeps
    what it read as a region; each recovery reads the new rows on top, finds
    the region's first rows by their numbers, jumps into its last rows, then
    to its very last row, and reads on from there. Each match is submitted
    once and the success clears the regions."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("E", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    feed_state_repo, seen_repo = await _first_run_before_the_outage(tmp_path, production, feed)
    feed.publish(_with_matches(
        _rows("G", 300, publish_date="2026-10-22"), {0: "M_TOP", 150: "M_MIDDLE", 299: "M_BOTTOM"},
    ))

    assert await _cycle(tmp_path, feed, production, _utc(15), cap=4) == [0, 30, 60, 90]
    [region] = feed_state_repo.get().recovery_regions
    assert (region.length, region.top[0], region.bottom[0], region.bottom[-1]) == (120, "M_TOP", "M_TOP", "G119")

    feed.publish(_rows("N", 30, publish_date="2026-10-22"))
    # N on top; G000 found on page 30 — its last rows are on the page too, so
    # straight to the region's last row (page 120) and on.
    assert await _cycle(tmp_path, feed, production, _utc(21), cap=6) == [0, 30, 120, 150, 180, 210]
    [region] = feed_state_repo.get().recovery_regions
    assert (region.length, region.top[0], region.bottom[-1]) == (240, "N000", "G209")
    # N000 on page 0; a jump into the region's last rows (page 120), then to its last row (210).
    assert await _cycle(tmp_path, feed, production, _utc(23, 55), cap=6) == [0, 120, 210, 240, 270, 300]
    assert await _cycle(tmp_path, feed, production, _utc(9, day=23), cap=6) == [0, 180, 300, 330]

    state = feed_state_repo.get()
    assert state.last_error is None
    assert state.last_successful_poll_at == _utc(9, day=23)
    assert state.recovery_regions == ()
    assert _unseen(feed, seen_repo) == []
    assert sorted(_submitted(production)) == ["M_BOTTOM", "M_MIDDLE", "M_TOP"]
    assert [r["status"] for r in production.health_reports] == ["ok", "recovering", "recovering", "recovering", "ok"]
    capped = production.health_reports[2]
    assert capped["mode"] == "recovery"
    assert capped["pages"] == 6
    assert (capped["regions"], capped["backlog_rows_read"]) == (1, 240)
    assert capped["last_success_at"] == _utc(9).isoformat()
    assert capped["oldest_publish_date"] == "2026-10-22"
    assert capped["horizon_date"] == "2026-10-22"
    assert production.health_reports[1]["mode"] == "fast"


async def test_burst_during_the_step_after_a_jump_does_not_skip_rows(tmp_path):
    """Review repro (61df2c5 lost rows here): 75 rows published during the
    capped attempt, 20 more right after the resumed attempt's jump page.
    The jump is only trusted once the region's last rows are found by their
    numbers; when the landing page has none, the recovery reads on in order."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("E", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    feed_state_repo, seen_repo = await _first_run_before_the_outage(tmp_path, production, feed)
    feed.publish(_with_matches(_rows("G", 300, publish_date="2026-10-22"), {120: "M_LOST"}))
    feed.publish_after_requests(2, _rows("B", 75, publish_date="2026-10-22"))
    assert await _cycle(tmp_path, feed, production, _utc(15), cap=6) == [0, 30, 60, 90, 120, 150]
    assert len(feed_state_repo.get().recovery_regions) == 1
    feed.publish(_rows("N", 30, publish_date="2026-10-22"))
    present = [row["protocolNo"] for row in feed.rows]
    feed.publish_after_requests(5, _rows("B2_", 20, publish_date="2026-10-22"))

    await _cycle(tmp_path, feed, production, _utc(21), cap=30)
    state = feed_state_repo.get()
    assert state.last_error is None
    assert [p for p in present if not seen_repo.is_seen(p)] == []
    assert "M_LOST" in _submitted(production)

    await _cycle(tmp_path, feed, production, _utc(23, 55), cap=30)
    assert _unseen(feed, seen_repo) == []
    assert _submitted(production) == ["M_LOST"]


async def test_older_attempt_rows_below_an_unread_gap_are_not_taken_for_the_region(tmp_path):
    """Review repro (61df2c5 lost rows here): T1 reads G[0:120) and stops at
    the cap; 90 rows (one match) come before T2, whose top part alone uses
    its cap (70 more rows published meanwhile). T3 must read the 30 H rows
    between T2's region and T1's — not jump over them because rows below
    were read by some attempt."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("E", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    feed_state_repo, seen_repo = await _first_run_before_the_outage(tmp_path, production, feed)
    feed.publish(_rows("G", 300, publish_date="2026-10-22"))
    assert await _cycle(tmp_path, feed, production, _utc(15), cap=4) == [0, 30, 60, 90]
    feed.publish(_with_matches(_rows("H", 90, publish_date="2026-10-22"), {60: "M_LOST"}))
    feed.publish_after_requests(2, _rows("B", 70, publish_date="2026-10-22"))
    assert await _cycle(tmp_path, feed, production, _utc(21), cap=4) == [0, 30, 60, 90]
    upper, lower = feed_state_repo.get().recovery_regions
    assert (upper.top[0], lower.top[0], lower.length) == ("H000", "G000", 120)
    feed.publish(_rows("N", 30, publish_date="2026-10-22"))

    await _cycle(tmp_path, feed, production, _utc(23, 55), cap=30)

    state = feed_state_repo.get()
    assert state.last_error is None
    assert state.recovery_regions == ()
    assert _unseen(feed, seen_repo) == []
    assert _submitted(production) == ["M_LOST"]


async def test_killed_attempt_keeps_the_pages_it_read_and_the_next_one_goes_on_from_there(tmp_path):
    """Regions are written after every page: an attempt killed mid-recovery
    (SIGTERM, OOM) leaves what it read, and the next attempt continues after
    it instead of starting over; matches are submitted once."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("E", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    feed_state_repo, seen_repo = await _first_run_before_the_outage(tmp_path, production, feed)
    feed.publish(_with_matches(_rows("G", 300, publish_date="2026-10-22"), {140: "M_KILLED", 200: "M_AFTER"}))
    assert await _cycle(tmp_path, feed, production, _utc(15), cap=4) == [0, 30, 60, 90]
    feed.publish(_rows("N", 30, publish_date="2026-10-22"))

    feed.fail_after_requests(4, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _cycle(tmp_path, feed, production, _utc(21), cap=6)
    assert feed.requested_offsets == [0, 30, 120, 150, 180]
    state = feed_state_repo.get()
    assert state.last_error.startswith("cycle in progress")
    [region] = state.recovery_regions
    assert (region.length, region.top[0], region.bottom[-1]) == (180, "N000", "G149")
    assert _submitted(production) == ["M_KILLED"]

    feed.publish(_rows("N2_", 30, publish_date="2026-10-22"))
    assert await _cycle(tmp_path, feed, production, _utc(23, 55), cap=6) == [0, 30, 120, 180, 210, 240]
    assert await _cycle(tmp_path, feed, production, _utc(9, day=23), cap=6) == [0, 150, 240, 270, 300, 330]
    assert await _cycle(tmp_path, feed, production, _utc(15, day=23), cap=6) == [0, 210, 330, 360]

    assert feed_state_repo.get().last_error is None
    assert _unseen(feed, seen_repo) == []
    assert sorted(_submitted(production)) == ["M_AFTER", "M_KILLED"]


async def test_error_mid_recovery_keeps_the_progress(tmp_path):
    """A police.ge error in the middle of a recovery: the rows read before it
    stay in the region, the next cycle goes on after them."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("E", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    feed_state_repo, seen_repo = await _first_run_before_the_outage(tmp_path, production, feed)
    feed.publish(_with_matches(_rows("G", 300, publish_date="2026-10-22"), {200: "M_AFTER_ERROR"}))
    assert await _cycle(tmp_path, feed, production, _utc(15), cap=4) == [0, 30, 60, 90]

    feed.fail_after_requests(4, FineProviderError("ConnectTimeout"))
    # G000 on page 0 with the region's last rows: straight to its last row (90), on to 150, error at 180.
    assert await _cycle(tmp_path, feed, production, _utc(21), cap=10) == [0, 90, 120, 150, 180]
    state = feed_state_repo.get()
    assert state.last_error == "ConnectTimeout"
    [region] = state.recovery_regions
    assert (region.length, region.bottom[-1]) == (180, "G179")
    assert production.health_reports[-1]["status"] == "error"
    assert production.health_reports[-1]["backlog_rows_read"] == 180
    assert _submitted(production) == []

    assert await _cycles_until_success(tmp_path, feed, production, feed_state_repo, seen_repo, cap=10) == 1
    assert _unseen(feed, seen_repo) == []
    assert _submitted(production) == ["M_AFTER_ERROR"]


async def test_cap_hit_while_looking_for_the_region_end_keeps_the_regions_valid(tmp_path, monkeypatch):
    """The cap ends the cycle during the search for the region's last rows:
    what is written is the rows read in order plus the region (all read), so
    the next cycle simply searches again."""
    from reader.fines import public_feed_traversal

    monkeypatch.setattr(public_feed_traversal, "BOTTOM_ANCHOR_ROWS", 30)
    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("E", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    feed_state_repo, seen_repo = await _first_run_before_the_outage(tmp_path, production, feed)
    feed.publish(_with_matches(_rows("G", 360, publish_date="2026-10-22"), {250: "M_BELOW"}))
    assert await _cycle(tmp_path, feed, production, _utc(15), cap=8) == [0, 30, 60, 90, 120, 150, 180, 210]
    feed.publish(_rows("N", 30, publish_date="2026-10-22"))
    # 45 rows published right after the region's first rows were found: the
    # search page (240) now shows rows above its last ones — the cap ends the
    # cycle before they are found.
    feed.publish_after_requests(2, _rows("B", 45, publish_date="2026-10-22"))

    assert await _cycle(tmp_path, feed, production, _utc(21), cap=3) == [0, 30, 240]
    state = feed_state_repo.get()
    [region] = state.recovery_regions
    assert (region.length, region.top[0], region.bottom[-1]) == (270, "N000", "G239")
    assert production.health_reports[-1]["status"] == "recovering"

    assert await _cycles_until_success(tmp_path, feed, production, feed_state_repo, seen_repo, cap=8) == 1
    assert _unseen(feed, seen_repo) == []
    assert _submitted(production) == ["M_BELOW"]


async def test_long_region_with_short_anchor_lists_jumps_twice_and_corrects_its_length(tmp_path, monkeypatch):
    """With few end-anchor rows (BOTTOM_ANCHOR_ROWS=60) a long region is
    crossed in two jumps: into its last rows, then to its very last row.
    Rows counted twice by the previous cycle (a burst re-showed rows above
    the ones it had read) made the region look longer; the jump lands short
    of the end, reads on, and the measured length replaces the inflated one."""
    from reader.fines import public_feed_traversal

    monkeypatch.setattr(public_feed_traversal, "BOTTOM_ANCHOR_ROWS", 60)
    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("E", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    feed_state_repo, seen_repo = await _first_run_before_the_outage(tmp_path, production, feed)
    feed.publish(_with_matches(_rows("G", 600, publish_date="2026-10-22"), {500: "M_DEEP"}))
    # 80 rows published while the first attempt reads: 20 of them land below
    # its position and are counted although they sit above its first rows.
    feed.publish_after_requests(1, _rows("B", 80, publish_date="2026-10-22"))
    assert await _cycle(tmp_path, feed, production, _utc(15), cap=12) == [i * 30 for i in range(12)]
    [region] = feed_state_repo.get().recovery_regions
    assert region.length == 330  # G000..G279 + 50 burst rows that sit above G000
    assert region.bottom == tuple(f"G{i:03d}" for i in range(220, 280))

    # B on top, G000 on page 60; the jump overshoots the end (unseen rows on
    # page 360), one step back finds the last rows (330), then on in order.
    assert await _cycle(tmp_path, feed, production, _utc(21), cap=12) == [
        0, 30, 60, 360, 330, 360, 390, 420, 450, 480, 510, 540,
    ]
    [region] = feed_state_repo.get().recovery_regions
    assert region.length == 80 + 490  # B000..B079 + G000..G489: no inflated rows left
    assert await _cycles_until_success(tmp_path, feed, production, feed_state_repo, seen_repo, cap=12) == 1
    assert _unseen(feed, seen_repo) == []
    assert _submitted(production) == ["M_DEEP"]


async def test_region_whose_last_rows_vanished_is_read_in_order_and_still_makes_progress(tmp_path, monkeypatch):
    """The region's last 30 rows (all its end anchors) disappear: no jump
    can be confirmed, so the region is read in order from its first page —
    counted again, so the capped cycle still saves progress — and the
    recovery completes."""
    from reader.fines import public_feed_traversal

    monkeypatch.setattr(public_feed_traversal, "BOTTOM_ANCHOR_ROWS", 30)
    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("E", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    feed_state_repo, seen_repo = await _first_run_before_the_outage(tmp_path, production, feed)
    rows = _with_matches(_rows("G", 300, publish_date="2026-10-22"), {200: "M_AFTER"})
    feed.publish(rows)
    assert await _cycle(tmp_path, feed, production, _utc(15), cap=6) == [0, 30, 60, 90, 120, 150]
    feed.remove({row["protocolNo"] for row in rows[150:180]})

    assert await _cycles_until_success(tmp_path, feed, production, feed_state_repo, seen_repo, cap=8) <= 3
    assert _unseen(feed, seen_repo) == []
    assert _submitted(production) == ["M_AFTER"]


async def test_rows_vanished_inside_the_region_never_hide_rows_below_it(tmp_path):
    """40 rows inside the capped attempt's region disappear before the next
    cycle, so the region is shorter than its length says: the landing page
    after the jump to its last row has none of its rows, the recovery reads
    on in order from where it found the region's last rows, and nothing
    below is skipped."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("E", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    feed_state_repo, seen_repo = await _first_run_before_the_outage(tmp_path, production, feed)
    gap = _with_matches(_rows("G", 300, publish_date="2026-10-22"), {125: "M_AFTER_GAP"})
    feed.publish(gap)
    assert await _cycle(tmp_path, feed, production, _utc(15), cap=4) == [0, 30, 60, 90]
    feed.remove({row["protocolNo"] for row in gap[40:80]})
    feed.publish(_rows("N", 30, publish_date="2026-10-22"))

    assert await _cycle(tmp_path, feed, production, _utc(21), cap=6) == [0, 30, 120, 60, 90, 120]
    assert await _cycles_until_success(tmp_path, feed, production, feed_state_repo, seen_repo, cap=6) <= 3
    assert _unseen(feed, seen_repo) == []
    assert _submitted(production) == ["M_AFTER_GAP"]


async def test_jump_past_the_end_of_the_feed_still_reads_the_rows_between(tmp_path):
    """90 of the region's 120 rows vanish, so the jump into its last rows
    lands past the end of the feed. An empty page is a sign of overshooting,
    not the end of the cycle: the recovery jumps back and reads the 30 rows
    in between (with a match)."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("Y0_", 30, publish_date="2026-10-21"))
    job0, feed_state_repo, seen_repo, _outbox = _make_job(tmp_path, feed, production, first_run_max_pages=10)
    await job0.run(_utc(9))
    gap = _with_matches(_rows("G", 150, publish_date="2026-10-22"), {135: "M_HIDDEN"})
    feed.publish(gap)
    assert await _cycle(tmp_path, feed, production, _utc(15), cap=4) == [0, 30, 60, 90]
    feed.remove({row["protocolNo"] for row in gap[30:120]})

    assert await _cycles_until_success(tmp_path, feed, production, feed_state_repo, seen_repo, cap=6) == 1
    assert _submitted(production) == ["M_HIDDEN"]
    assert _unseen(feed, seen_repo) == []


async def test_next_region_right_below_is_recognised_although_the_last_end_anchor_sits_elsewhere(tmp_path):
    """The last end-anchor of a region can be a row that sits elsewhere
    (rows a burst pushed into view are counted last), so its end is not
    marked. The next region, starting right below it, is still recognised by
    its first rows — not taken for rows re-shown by a burst — and skipped."""
    from reader.fines.public_feed_state_repository import ReadRegion

    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("E", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    feed_state_repo, seen_repo = await _first_run_before_the_outage(tmp_path, production, feed)
    rows = _rows("G", 300, publish_date="2026-10-22")
    feed.publish(rows)
    _mark_seen_at(seen_repo, rows, seen_at=_utc(15))  # read by earlier, capped attempts
    names = [row["protocolNo"] for row in rows]
    feed_state_repo.record_recovery_regions([
        ReadRegion(top=tuple(names[:30]), bottom=(*names[:60], "ELSEWHERE"), length=61, updated_at="x"),
        ReadRegion(top=tuple(names[60:90]), bottom=tuple(names[240:]), length=240, updated_at="x"),
    ])
    feed_state_repo.record_error("ConnectTimeout", at=_utc(15))

    # page 60 has no row of the first region's end: back to 30, on to 60 —
    # G060 starts the second region: a jump into its last rows (270), done.
    assert await _cycle(tmp_path, feed, production, _utc(21), cap=10) == [0, 60, 30, 60, 270, 300]
    assert feed_state_repo.get().last_error is None


async def test_regions_whose_rows_are_gone_are_harmless(tmp_path):
    """Regions naming rows that are not on the feed (left by an old version,
    or every row of them vanished): the recovery reads in order to the
    horizon, as without regions, and the success clears them."""
    from reader.fines.public_feed_state_repository import ReadRegion

    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("E", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    feed_state_repo, seen_repo = await _first_run_before_the_outage(tmp_path, production, feed)
    feed.publish(_with_matches(_rows("G", 120, publish_date="2026-10-22"), {45: "M_IN_ORDER"}))
    feed_state_repo.record_recovery_regions([
        ReadRegion(top=("GONE_TOP",), bottom=("GONE_BOTTOM",), length=5000, updated_at="2026-10-22T08:00:00+00:00"),
    ])
    feed_state_repo.record_error("ConnectTimeout", at=_utc(15))

    assert await _cycle(tmp_path, feed, production, _utc(21), cap=10) == [0, 30, 60, 90, 120]
    state = feed_state_repo.get()
    assert state.last_error is None
    assert state.recovery_regions == ()
    assert _submitted(production) == ["M_IN_ORDER"]


# ==== health report to production ====


async def test_health_report_failure_never_affects_the_cycle(tmp_path):
    for failure in (ProductionApiError("production unavailable"), RuntimeError("boom")):
        production = _FakeProductionClient(active_plates=["AA001AA"], pending_plates=["BB002BB"], fail_health=failure)
        session = _FakeSession({0: _page(_rows("Y0_", 30, publish_date="2026-10-21"))})
        workdir = tmp_path / type(failure).__name__
        workdir.mkdir()
        job, feed_state_repo, *_ = _make_job(workdir, session, production)

        await job.run(_utc(21))

        state = feed_state_repo.get()
        assert state.last_error is None
        assert state.last_successful_poll_at == _utc(21)
        assert session.search_calls == ["BB002BB"]


async def test_health_reports_say_error_for_a_failed_or_skipped_cycle_with_the_last_success(tmp_path):
    production = _FakeProductionClient(active_plates=["AA001AA"])
    first_run = _FakeSession({0: _page(_rows("Y0_", 30, publish_date="2026-10-21"))})
    job0, *_ = _make_job(tmp_path, first_run, production)
    await job0.run(_utc(15))
    failing = _FakeSession(errors={0: FineProviderError("ConnectTimeout")})
    job, *_ = _make_job(tmp_path, failing, production)
    await job.run(_utc(21))
    unavailable = _FakeSession(available=False)
    job2, *_ = _make_job(tmp_path, unavailable, production)
    await job2.run(_utc(23, 55))

    _ok, failed, skipped = production.health_reports
    assert (failed["status"], failed["mode"], failed["detail"]) == ("error", "fast", "ConnectTimeout")
    assert failed["last_success_at"] == _utc(15).isoformat()
    assert failed["cycle_at"] == _utc(21).isoformat()
    assert (skipped["status"], skipped["mode"]) == ("error", "skipped")
    assert "circuit breaker" in skipped["detail"]
    assert skipped["last_success_at"] == _utc(15).isoformat()
    assert skipped["cycle_at"] == _utc(23, 55).isoformat()


async def test_first_run_reports_are_errors_without_regions_or_a_last_success(tmp_path):
    production = _FakeProductionClient(active_plates=["AA001AA"])
    session = _FakeSession({i * 30: _page(_rows(f"T{i}_", 30, publish_date="2026-10-22")) for i in range(3)})
    job, feed_state_repo, *_ = _make_job(tmp_path, session, production, first_run_max_pages=2)
    await job.run(_utc(21))
    unavailable = _FakeSession(available=False)
    job2, *_ = _make_job(tmp_path, unavailable, production)
    await job2.run(_utc(23, 55))

    capped, skipped = production.health_reports
    assert (capped["status"], capped["mode"], capped["regions"]) == ("error", "first_run", 0)
    assert capped["last_success_at"] is None
    assert skipped["last_success_at"] is None
    assert feed_state_repo.get().recovery_regions == ()


async def test_health_reports_never_carry_plates_or_protocol_numbers(tmp_path):
    import json
    import re

    production = _FakeProductionClient(active_plates=["AA001AA"])
    feed = _LiveFeed(_rows("E", 60, publish_date="2026-10-22") + _rows("Y0_", 30, publish_date="2026-10-21"))
    await _first_run_before_the_outage(tmp_path, production, feed)
    feed.publish(_with_matches(_rows("G", 300, publish_date="2026-10-22"), {10: "M_ROW"}))
    await _cycle(tmp_path, feed, production, _utc(15), cap=4)
    feed.fail_after_requests(2, FineProviderError("ConnectTimeout"))
    await _cycle(tmp_path, feed, production, _utc(21), cap=6)

    assert len(production.health_reports) == 3
    for report in production.health_reports:
        text = json.dumps(report)
        assert not re.search(r"AA001AA|ZZ\d{3}ZZ|M_ROW|\bG\d{3}\b|\bE\d{3}\b", text), text
