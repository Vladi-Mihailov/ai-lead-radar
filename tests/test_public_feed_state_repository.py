"""Тесты GeorgiaPublicFeedStateRepository (см. задачу "Georgia fines
incremental public-feed monitoring", "ADD PERSISTENT FEED STATE /
CURSOR") — singleton cursor-состояние для GeorgiaPublicFineFeedJob."""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.public_feed_state_repository import (  # noqa: E402
    GeorgiaPublicFeedStateRepository,
)


def test_initial_state_is_empty(tmp_path):
    repo = GeorgiaPublicFeedStateRepository(tmp_path / "users.db")
    try:
        state = repo.get()
        assert state.newest_seen_publish_date is None
        assert state.last_successful_poll_at is None
        assert state.last_completed_at is None
        assert state.last_error is None
    finally:
        repo.close()


def test_record_successful_cycle_updates_state(tmp_path):
    repo = GeorgiaPublicFeedStateRepository(tmp_path / "users.db")
    try:
        polled_at = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
        repo.record_successful_cycle(
            newest_seen_publish_date=date(2026, 10, 22), polled_at=polled_at,
        )

        state = repo.get()
        assert state.newest_seen_publish_date == date(2026, 10, 22)
        assert state.last_successful_poll_at == polled_at
        assert state.last_completed_at == polled_at
        assert state.last_error is None
    finally:
        repo.close()


def test_record_error_does_not_clear_newest_seen_publish_date(tmp_path):
    """"If poll fails halfway: do not advance cursor beyond unprocessed
    data" — обратное тоже верно: ошибка НЕ должна случайно стереть уже
    достоверно известный курсор прошлого успешного цикла."""
    repo = GeorgiaPublicFeedStateRepository(tmp_path / "users.db")
    try:
        polled_at = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
        repo.record_successful_cycle(
            newest_seen_publish_date=date(2026, 10, 22), polled_at=polled_at,
        )

        error_at = datetime(2026, 10, 22, 10, 5, tzinfo=timezone.utc)
        repo.record_error("ConnectTimeout", at=error_at)

        state = repo.get()
        assert state.newest_seen_publish_date == date(2026, 10, 22)
        assert state.last_successful_poll_at == polled_at
        assert state.last_error == "ConnectTimeout"
        assert state.last_error_at == error_at
    finally:
        repo.close()


def test_successful_cycle_clears_previous_error(tmp_path):
    repo = GeorgiaPublicFeedStateRepository(tmp_path / "users.db")
    try:
        repo.record_error("ConnectTimeout", at=datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc))

        repo.record_successful_cycle(
            newest_seen_publish_date=date(2026, 10, 22),
            polled_at=datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc),
        )

        state = repo.get()
        assert state.last_error is None
        assert state.last_error_at is None
    finally:
        repo.close()


def test_state_persists_across_repository_instances(tmp_path):
    db_path = tmp_path / "users.db"
    repo1 = GeorgiaPublicFeedStateRepository(db_path)
    try:
        repo1.record_successful_cycle(
            newest_seen_publish_date=date(2026, 10, 22),
            polled_at=datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc),
        )
    finally:
        repo1.close()

    repo2 = GeorgiaPublicFeedStateRepository(db_path)
    try:
        assert repo2.get().newest_seen_publish_date == date(2026, 10, 22)
    finally:
        repo2.close()
