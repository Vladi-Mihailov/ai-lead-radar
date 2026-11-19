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


# ==== отметка продолжения восстановления (recovery_resume_offset/_at) ====


def test_recovery_progress_is_recorded_kept_by_errors_and_cleared_by_success(tmp_path):
    repo = GeorgiaPublicFeedStateRepository(tmp_path / "users.db")
    try:
        assert repo.get().recovery_resume_offset is None
        assert repo.get().recovery_resume_at is None
        capped_at = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)

        repo.record_recovery_progress(offset=9000, at=capped_at)
        repo.record_error("recovery reached the page cap", at=capped_at)
        state = repo.get()
        assert state.recovery_resume_offset == 9000
        assert state.recovery_resume_at == capped_at

        repo.record_error("ConnectTimeout", at=capped_at.replace(hour=17))
        assert repo.get().recovery_resume_offset == 9000

        repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=capped_at.replace(hour=19))
        state = repo.get()
        assert state.recovery_resume_offset is None
        assert state.recovery_resume_at is None
    finally:
        repo.close()


def test_existing_table_without_the_new_columns_is_upgraded_and_keeps_its_data(tmp_path):
    """REDNET/production уже имеют таблицу первого выпуска: колонки
    добавляются при открытии, ни одно значение не меняется."""
    import sqlite3

    db_path = tmp_path / "collector.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE georgia_public_feed_state ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), newest_seen_publish_date TEXT, "
        "last_successful_poll_at TIMESTAMP, last_completed_at TIMESTAMP, "
        "last_error TEXT, last_error_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO georgia_public_feed_state VALUES (1, '2026-11-18', "
        "'2026-11-18T19:55:00+00:00', '2026-11-18T19:55:00+00:00', 'cycle in progress', "
        "'2026-11-19T05:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    repo = GeorgiaPublicFeedStateRepository(db_path)
    try:
        state = repo.get()
        assert state.newest_seen_publish_date == date(2026, 11, 18)
        assert state.last_successful_poll_at == datetime(2026, 11, 18, 19, 55, tzinfo=timezone.utc)
        assert state.last_error == "cycle in progress"
        assert state.recovery_resume_offset is None
        repo.record_recovery_progress(offset=300, at=datetime(2026, 11, 19, 5, 0, tzinfo=timezone.utc))
        assert repo.get().recovery_resume_offset == 300
    finally:
        repo.close()

    reopened = GeorgiaPublicFeedStateRepository(db_path)
    try:
        assert reopened.get().recovery_resume_offset == 300
    finally:
        reopened.close()
