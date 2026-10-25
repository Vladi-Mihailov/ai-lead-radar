"""Tests for GeorgiaCollectorJob (reader/georgia_collector/collector_job.py)
— the REDNET-side poller/bootstrap loop, see "Georgia remote collector
architecture (REDNET)". Covers: exact wall-clock schedule (same semantics
as FineJob/GeorgiaPublicFineFeedJob), production-API-unavailable handling
(outbox queueing, no lost matches), police.ge-unavailable handling (circuit
breaker gate, error recording via the local feed_state), and the
no-Telegram-import architectural guarantee."""

import sys
from datetime import datetime, timezone
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
    def __init__(self, pages_by_offset: dict[int, dict] | None = None, *, available=True, search_results=None):
        self._pages = pages_by_offset or {}
        self._available = available
        self._search_results = search_results or {}
        self.requested_offsets: list[int] = []
        self.search_calls: list[str] = []

    def is_available(self) -> bool:
        return self._available

    async def fetch_published_protocols(self, *, first_result: int) -> dict:
        self.requested_offsets.append(first_result)
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


def _make_job(tmp_path, session, production_client, *, run_times=None):
    db_path = tmp_path / "collector.db"
    feed_state_repo = GeorgiaPublicFeedStateRepository(db_path)
    seen_repo = GeorgiaPublicFeedSeenRepository(db_path)
    outbox_repo = CollectorOutboxRepository(db_path)
    job = GeorgiaCollectorJob(
        session, production_client, feed_state_repo, seen_repo, outbox_repo,
        run_times=run_times or _DEFAULT_RUN_TIMES, tz=_TBILISI,
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
