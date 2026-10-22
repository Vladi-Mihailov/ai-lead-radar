"""Тесты GeorgiaPublicFeedSeenRepository (см. корректирующую задачу
"IMPORTANT CORRECTION BEFORE COMMIT / PR" — "PERSIST RECENT SOURCE IDS")
— bounded feed-traversal index, НЕ detected_fines."""

import sys
from datetime import date, timedelta
from pathlib import Path

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
