"""Tests for CollectorOutboxRepository (reader/georgia_collector/
outbox_repository.py) — see "Georgia remote collector architecture
(REDNET)" integration review, section "Outbox durability". These exercise
the repository directly (not through GeorgiaCollectorJob, see
test_georgia_collector_job.py for that) specifically to prove durability
across what a real process restart looks like: closing one instance and
opening a FRESH one against the same SQLite file, not just re-reading the
same long-lived Python object."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.georgia_collector.outbox_repository import (
    KIND_BOOTSTRAP_RESULT,
    KIND_FEED_MATCH,
    CollectorOutboxRepository,
)


def test_queued_entry_survives_process_restart(tmp_path):
    db_path = tmp_path / "collector.db"

    repo = CollectorOutboxRepository(db_path)
    repo.enqueue(KIND_FEED_MATCH, {"car_number": "AA001AA", "fingerprint": "abc"})
    repo.close()

    # Simulates a real restart: a brand new repository instance, not the
    # same Python object, opened fresh against the same file on disk.
    restarted = CollectorOutboxRepository(db_path)
    try:
        entries = restarted.list_all()
        assert len(entries) == 1
        assert entries[0].kind == KIND_FEED_MATCH
        assert entries[0].payload == {"car_number": "AA001AA", "fingerprint": "abc"}
        assert entries[0].attempts == 0
    finally:
        restarted.close()


def test_removed_entry_stays_removed_after_restart(tmp_path):
    db_path = tmp_path / "collector.db"

    repo = CollectorOutboxRepository(db_path)
    repo.enqueue(KIND_FEED_MATCH, {"car_number": "AA001AA"})
    entry_id = repo.list_all()[0].id
    repo.remove(entry_id)
    repo.close()

    restarted = CollectorOutboxRepository(db_path)
    try:
        assert restarted.count() == 0
    finally:
        restarted.close()


def test_attempt_count_persists_across_restart(tmp_path):
    db_path = tmp_path / "collector.db"

    repo = CollectorOutboxRepository(db_path)
    repo.enqueue(KIND_BOOTSTRAP_RESULT, {"car_number": "AA001AA", "fines": []})
    entry_id = repo.list_all()[0].id
    repo.record_attempt(entry_id)
    repo.record_attempt(entry_id)
    repo.close()

    restarted = CollectorOutboxRepository(db_path)
    try:
        entry = restarted.list_all()[0]
        assert entry.attempts == 2
    finally:
        restarted.close()


def test_multiple_entries_independently_tracked_and_ordered(tmp_path):
    db_path = tmp_path / "collector.db"

    repo = CollectorOutboxRepository(db_path)
    repo.enqueue(KIND_FEED_MATCH, {"car_number": "AA001AA"})
    repo.enqueue(KIND_BOOTSTRAP_RESULT, {"car_number": "BB002BB", "fines": []})
    repo.close()

    restarted = CollectorOutboxRepository(db_path)
    try:
        entries = restarted.list_all()
        assert [e.payload["car_number"] for e in entries] == ["AA001AA", "BB002BB"]
        restarted.remove(entries[0].id)
    finally:
        restarted.close()

    reopened_again = CollectorOutboxRepository(db_path)
    try:
        remaining = reopened_again.list_all()
        assert len(remaining) == 1
        assert remaining[0].payload["car_number"] == "BB002BB"
    finally:
        reopened_again.close()
