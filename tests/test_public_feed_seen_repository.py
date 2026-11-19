"""Тесты GeorgiaPublicFeedSeenRepository (см. корректирующую задачу
"IMPORTANT CORRECTION BEFORE COMMIT / PR" — "PERSIST RECENT SOURCE IDS")
— bounded feed-traversal index, НЕ detected_fines."""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.public_feed_seen_repository import (  # noqa: E402
    GeorgiaPublicFeedSeenRepository,
)


def test_unknown_protocol_no_is_not_seen(tmp_path):
    repo = GeorgiaPublicFeedSeenRepository(tmp_path / "users.db")
    try:
        assert repo.is_seen("AB123456") is False
    finally:
        repo.close()


def test_mark_seen_makes_it_seen(tmp_path):
    repo = GeorgiaPublicFeedSeenRepository(tmp_path / "users.db")
    try:
        repo.mark_seen("AB123456", publish_date=date(2026, 10, 22))

        assert repo.is_seen("AB123456") is True
    finally:
        repo.close()


def test_mark_seen_is_idempotent(tmp_path):
    repo = GeorgiaPublicFeedSeenRepository(tmp_path / "users.db")
    try:
        repo.mark_seen("AB123456", publish_date=date(2026, 10, 22))
        repo.mark_seen("AB123456", publish_date=date(2026, 10, 22))  # не должно упасть/задвоить

        assert repo.count() == 1
    finally:
        repo.close()


def test_cleanup_removes_only_rows_older_than_cutoff(tmp_path):
    repo = GeorgiaPublicFeedSeenRepository(tmp_path / "users.db")
    try:
        repo.mark_seen("OLD1", publish_date=date(2026, 10, 1))
        repo.mark_seen("OLD2", publish_date=date(2026, 10, 10))
        repo.mark_seen("RECENT", publish_date=date(2026, 10, 20))

        removed = repo.cleanup_older_than(date(2026, 10, 15))

        assert removed == 2
        assert repo.is_seen("OLD1") is False
        assert repo.is_seen("OLD2") is False
        assert repo.is_seen("RECENT") is True
    finally:
        repo.close()


def test_cleanup_does_not_remove_current_frontier(tmp_path):
    """Тест 10 задачи: "retention cleanup does not remove IDs still needed
    for current frontier" — свежая запись (сегодня) переживает cleanup с
    разумным retention (7 дней)."""
    repo = GeorgiaPublicFeedSeenRepository(tmp_path / "users.db")
    try:
        today = date(2026, 10, 22)
        repo.mark_seen("AB123456", publish_date=today)

        repo.cleanup_older_than(today - timedelta(days=7))

        assert repo.is_seen("AB123456") is True
    finally:
        repo.close()


def test_state_persists_across_repository_instances(tmp_path):
    db_path = tmp_path / "users.db"
    repo1 = GeorgiaPublicFeedSeenRepository(db_path)
    try:
        repo1.mark_seen("AB123456", publish_date=date(2026, 10, 22))
    finally:
        repo1.close()

    repo2 = GeorgiaPublicFeedSeenRepository(db_path)
    try:
        assert repo2.is_seen("AB123456") is True
    finally:
        repo2.close()


# ==== first_seen_at / seen_at_or_before (DEEP-горизонт восстановления) ====


def _first_seen_at(repo, protocol_no: str) -> str:
    return repo._conn.execute(
        "SELECT first_seen_at FROM georgia_public_feed_seen WHERE protocol_no = ?", (protocol_no,),
    ).fetchone()[0]


def test_mark_seen_with_seen_at_stores_utc_in_current_timestamp_format(tmp_path):
    repo = GeorgiaPublicFeedSeenRepository(tmp_path / "users.db")
    try:
        tbilisi = ZoneInfo("Asia/Tbilisi")
        repo.mark_seen(
            "AB123456", publish_date=date(2026, 10, 22),
            seen_at=datetime(2026, 10, 22, 21, 0, 5, 750000, tzinfo=tbilisi),
        )

        assert _first_seen_at(repo, "AB123456") == "2026-10-22 17:00:05"
    finally:
        repo.close()


def test_naive_seen_at_is_treated_as_utc(tmp_path):
    repo = GeorgiaPublicFeedSeenRepository(tmp_path / "users.db")
    try:
        repo.mark_seen("AB123456", publish_date=date(2026, 10, 22), seen_at=datetime(2026, 10, 22, 17, 0, 5))

        assert _first_seen_at(repo, "AB123456") == "2026-10-22 17:00:05"
        assert repo.seen_at_or_before("AB123456", datetime(2026, 10, 22, 17, 0, 5, tzinfo=timezone.utc)) is True
        assert repo.seen_at_or_before("AB123456", datetime(2026, 10, 22, 17, 0, 4, tzinfo=timezone.utc)) is False
    finally:
        repo.close()


def test_seen_at_or_before_includes_the_same_moment_and_is_false_for_unknown_rows(tmp_path):
    repo = GeorgiaPublicFeedSeenRepository(tmp_path / "users.db")
    try:
        at = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)
        repo.mark_seen("AB123456", publish_date=date(2026, 10, 22), seen_at=at)

        # Отмечена циклом с run_at == at — этот цикл её обработал.
        assert repo.seen_at_or_before("AB123456", at) is True
        assert repo.seen_at_or_before("AB123456", at + timedelta(seconds=1)) is True
        assert repo.seen_at_or_before("AB123456", at - timedelta(seconds=1)) is False
        assert repo.seen_at_or_before("UNKNOWN1", at + timedelta(days=1)) is False
    finally:
        repo.close()


def test_repeated_mark_seen_keeps_the_first_seen_time(tmp_path):
    repo = GeorgiaPublicFeedSeenRepository(tmp_path / "users.db")
    try:
        first = datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc)
        repo.mark_seen("AB123456", publish_date=date(2026, 10, 22), seen_at=first)
        repo.mark_seen("AB123456", publish_date=date(2026, 10, 22), seen_at=first + timedelta(hours=6))

        assert _first_seen_at(repo, "AB123456") == "2026-10-22 05:00:00"
        assert repo.count() == 1
    finally:
        repo.close()


def test_rows_with_default_current_timestamp_compare_correctly(tmp_path):
    """Строки, записанные до этой правки (first_seen_at = CURRENT_TIMESTAMP),
    сравниваются с моментом цикла в том же формате."""
    repo = GeorgiaPublicFeedSeenRepository(tmp_path / "users.db")
    try:
        repo.mark_seen("AB123456", publish_date=date(2026, 10, 22))
        now = datetime.now(timezone.utc)

        assert repo.seen_at_or_before("AB123456", now + timedelta(minutes=5)) is True
        assert repo.seen_at_or_before("AB123456", now - timedelta(minutes=5)) is False
    finally:
        repo.close()


def test_seen_between_is_after_exclusive_until_inclusive_to_the_second(tmp_path):
    """Восстановление в несколько циклов: строки, отмеченные после последнего
    успеха (after) и не позже цикла, упёршегося в лимит (until)."""
    repo = GeorgiaPublicFeedSeenRepository(tmp_path / "users.db")
    try:
        success = datetime(2026, 10, 22, 5, 0, 0, 400000, tzinfo=timezone.utc)
        capped = datetime(2026, 10, 22, 11, 0, 0, 900000, tzinfo=timezone.utc)
        repo.mark_seen("BY_SUCCESS", publish_date=date(2026, 10, 22), seen_at=success)
        repo.mark_seen("BY_CAPPED", publish_date=date(2026, 10, 22), seen_at=capped)
        repo.mark_seen("LATER", publish_date=date(2026, 10, 22), seen_at=capped + timedelta(seconds=1))

        assert repo.seen_between("BY_SUCCESS", after=success, until=capped) is False
        assert repo.seen_between("BY_CAPPED", after=success, until=capped) is True
        assert repo.seen_between("LATER", after=success, until=capped) is False
        assert repo.seen_between("UNKNOWN1", after=success, until=capped) is False
    finally:
        repo.close()
