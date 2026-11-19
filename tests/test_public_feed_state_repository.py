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
    ReadRegion,
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


# ==== участки восстановления (recovery_regions) ====


def _region(prefix: str, length: int) -> ReadRegion:
    return ReadRegion(
        top=tuple(f"{prefix}{i:03d}" for i in range(3)),
        bottom=tuple(f"{prefix}{i:03d}" for i in range(length - 3, length)),
        length=length,
        updated_at="2026-10-22T11:00:00+00:00",
    )


def test_recovery_regions_are_recorded_in_order_kept_by_errors_and_cleared_by_success(tmp_path):
    repo = GeorgiaPublicFeedStateRepository(tmp_path / "users.db")
    try:
        assert repo.get().recovery_regions == ()
        capped_at = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)

        repo.record_recovery_regions([_region("N", 120), _region("G", 9000)])
        repo.record_error("recovery reached the page cap", at=capped_at)
        state = repo.get()
        assert state.recovery_regions == (_region("N", 120), _region("G", 9000))
        assert state.last_error == "recovery reached the page cap"

        repo.record_error("ConnectTimeout", at=capped_at.replace(hour=17))
        assert repo.get().recovery_regions == (_region("N", 120), _region("G", 9000))

        repo.record_successful_cycle(newest_seen_publish_date=date(2026, 10, 22), polled_at=capped_at.replace(hour=19))
        assert repo.get().recovery_regions == ()
    finally:
        repo.close()


def test_recording_no_regions_clears_them_and_leaves_the_error(tmp_path):
    repo = GeorgiaPublicFeedStateRepository(tmp_path / "users.db")
    try:
        repo.record_error("cycle in progress", at=datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc))
        repo.record_recovery_regions([_region("N", 120)])
        repo.record_recovery_regions([])
        state = repo.get()
        assert state.recovery_regions == ()
        assert state.last_error == "cycle in progress"
    finally:
        repo.close()


def test_unreadable_or_incomplete_regions_are_ignored(tmp_path):
    """Испорченная запись (ручная правка, старая версия) не роняет обход:
    такие участки просто не используются — восстановление читает по
    порядку."""
    import sqlite3

    db_path = tmp_path / "users.db"
    repo = GeorgiaPublicFeedStateRepository(db_path)
    try:
        for raw in (
            "not json",
            '{"top": ["A"]}',
            '[{"top": [], "bottom": ["B"], "length": 5}, {"top": ["T"], "bottom": ["B"], "length": 0}, 7, '
            '{"top": ["T"], "bottom": ["B"], "length": 12, "updated_at": "x"}]',
        ):
            conn = sqlite3.connect(db_path)
            conn.execute("UPDATE georgia_public_feed_state SET recovery_regions = ? WHERE id = 1", (raw,))
            conn.commit()
            conn.close()
            regions = repo.get().recovery_regions
            assert all(region.top and region.bottom and region.length > 0 for region in regions)
        assert repo.get().recovery_regions == (ReadRegion(top=("T",), bottom=("B",), length=12, updated_at="x"),)
    finally:
        repo.close()


def _old_table(db_path) -> None:
    import sqlite3

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


def test_existing_table_without_the_new_column_is_upgraded_and_keeps_its_data(tmp_path):
    """REDNET/production уже имеют таблицу первого выпуска: колонка
    добавляется при открытии, ни одно значение не меняется."""
    db_path = tmp_path / "collector.db"
    _old_table(db_path)

    repo = GeorgiaPublicFeedStateRepository(db_path)
    try:
        state = repo.get()
        assert state.newest_seen_publish_date == date(2026, 11, 18)
        assert state.last_successful_poll_at == datetime(2026, 11, 18, 19, 55, tzinfo=timezone.utc)
        assert state.last_error == "cycle in progress"
        assert state.recovery_regions == ()
        repo.record_recovery_regions([_region("G", 300)])
    finally:
        repo.close()

    reopened = GeorgiaPublicFeedStateRepository(db_path)
    try:
        assert reopened.get().recovery_regions == (_region("G", 300),)
    finally:
        reopened.close()


def test_column_added_by_another_process_between_the_check_and_the_alter_is_not_an_error(tmp_path, monkeypatch):
    """Коллектор и его check-now сервер открывают базу одновременно: второй
    процесс мог добавить колонку после того, как этот её не нашёл."""
    import sqlite3

    from reader.fines import public_feed_state_repository as module

    db_path = tmp_path / "collector.db"
    _old_table(db_path)
    real_connect = sqlite3.connect

    class _RacingConnection:
        """PRAGMA table_info видит старую схему; перед ALTER колонку
        добавляет "другой процесс"."""

        def __init__(self, path, **kwargs):
            self._conn = real_connect(path, **kwargs)

        def execute(self, sql, *args):
            if sql.startswith("ALTER TABLE"):
                other = real_connect(db_path)
                other.execute("ALTER TABLE georgia_public_feed_state ADD COLUMN recovery_regions TEXT")
                other.commit()
                other.close()
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(module.sqlite3, "connect", _RacingConnection)
    repo = GeorgiaPublicFeedStateRepository(db_path)
    try:
        assert repo.get().last_error == "cycle in progress"
        assert repo.get().recovery_regions == ()
    finally:
        repo.close()
