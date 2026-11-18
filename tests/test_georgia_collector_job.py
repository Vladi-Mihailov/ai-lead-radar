"""Tests for GeorgiaCollectorJob (reader/georgia_collector/collector_job.py)
— the REDNET-side poller/bootstrap loop, see "Georgia remote collector
architecture (REDNET)". Covers: exact wall-clock schedule (same semantics
as FineJob/GeorgiaPublicFineFeedJob), production-API-unavailable handling
(outbox queueing, no lost matches), police.ge-unavailable handling (circuit
breaker gate, error recording via the local feed_state), and the
no-Telegram-import architectural guarantee."""

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
    FAST reaches the frontier and the cycle succeeds."""
    max_pages_per_cycle, _first_run = load_page_limits(env={})
    frontier = _rows("SEEN_", 30, publish_date="2026-10-22")
    pages = {i * 30: _page(_rows(f"P{i:03d}_", 30, publish_date="2026-10-22")) for i in range(120)}
    pages[120 * 30] = _page(frontier)
    pages[121 * 30] = _page(_rows("AFTER_", 30, publish_date="2026-10-22"))
    session = _FakeSession(pages)
    production = _FakeProductionClient(active_plates=["AA001AA"])
    job, feed_state_repo, seen_repo, _outbox = _make_job(
        tmp_path, session, production, max_pages_per_cycle=max_pages_per_cycle,
    )
    feed_state_repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=_utc(15))
    _mark_seen_at(seen_repo, frontier, seen_at=_utc(15))

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
    """End-to-end check of first_seen_at = run_at: rows marked by the first
    successful cycle complete a later recovery; rows of the most recent
    successful cycle (first_seen_at == its run_at) do not."""
    production = _FakeProductionClient(active_plates=["AA001AA"])
    rows_a = _rows("A0_", 30, publish_date="2026-10-22")
    rows_b = _rows("B0_", 30, publish_date="2026-10-22")
    yesterday = _rows("Y0_", 30, publish_date="2026-10-21")

    first = _FakeSession({0: _page(rows_a), 30: _page(yesterday)})
    job1, feed_state_repo, *_ = _make_job(tmp_path, first, production)
    await job1.run(_utc(15))
    assert first.requested_offsets == [0, 30]

    second = _FakeSession({0: _page(rows_b), 30: _page(rows_a), 60: _page(yesterday)})
    job2, *_ = _make_job(tmp_path, second, production)
    await job2.run(_utc(21))
    assert second.requested_offsets == [0, 30, 60]

    failing = _FakeSession(errors={0: FineProviderError("ConnectTimeout")})
    job3, *_ = _make_job(tmp_path, failing, production)
    await job3.run(_utc(23, 55))
    assert feed_state_repo.get().last_error is not None

    recovery = _FakeSession({
        0: _page(_rows("C0_", 30, publish_date="2026-10-23")),
        30: _page(rows_b),
        60: _page(rows_a),
        90: _page(yesterday),
    })
    job4, feed_state_repo4, *_ = _make_job(tmp_path, recovery, production)
    await job4.run(_utc(9, day=23))

    assert recovery.requested_offsets == [0, 30, 60]
    assert feed_state_repo4.get().last_error is None
