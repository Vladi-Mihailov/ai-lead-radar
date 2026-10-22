"""Тесты GeorgiaPlateBootstrapRepository (см. задачу "Georgia fines
incremental public-feed monitoring") — per-PHYSICAL-plate bootstrap state,
НЕ per-task/per-subscription."""

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.plate_bootstrap_repository import (  # noqa: E402
    STATUS_COMPLETED,
    STATUS_PENDING,
    GeorgiaPlateBootstrapRepository,
)


def test_get_returns_none_for_unknown_plate(tmp_path):
    repo = GeorgiaPlateBootstrapRepository(tmp_path / "users.db")
    try:
        assert repo.get("AA001AA") is None
        assert repo.is_completed("AA001AA") is False
    finally:
        repo.close()


def test_ensure_pending_creates_row_with_pending_status(tmp_path):
    repo = GeorgiaPlateBootstrapRepository(tmp_path / "users.db")
    try:
        state = repo.ensure_pending("AA001AA")
        assert state.car_number == "AA001AA"
        assert state.bootstrap_status == STATUS_PENDING
        assert state.bootstrap_completed_at is None
        assert state.is_completed is False
    finally:
        repo.close()


def test_ensure_pending_is_idempotent_and_never_downgrades_completed(tmp_path):
    repo = GeorgiaPlateBootstrapRepository(tmp_path / "users.db")
    try:
        repo.ensure_pending("AA001AA")
        completed_at = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
        repo.mark_completed("AA001AA", completed_at=completed_at)
        assert repo.is_completed("AA001AA") is True

        # Вторая подписка на ту же машину (см. докстрок модуля) — НЕ
        # должна откатить уже completed-строку на pending.
        state = repo.ensure_pending("AA001AA")
        assert state.bootstrap_status == STATUS_COMPLETED
        assert state.bootstrap_completed_at == completed_at
    finally:
        repo.close()


def test_mark_completed_sets_status_and_timestamp(tmp_path):
    repo = GeorgiaPlateBootstrapRepository(tmp_path / "users.db")
    try:
        repo.ensure_pending("AA001AA")
        completed_at = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)

        repo.mark_completed("AA001AA", completed_at=completed_at)

        state = repo.get("AA001AA")
        assert state.bootstrap_status == STATUS_COMPLETED
        assert state.bootstrap_completed_at == completed_at
        assert repo.is_completed("AA001AA") is True
    finally:
        repo.close()


def test_list_pending_returns_only_pending_plates(tmp_path):
    repo = GeorgiaPlateBootstrapRepository(tmp_path / "users.db")
    try:
        repo.ensure_pending("AA001AA")
        repo.ensure_pending("BB002BB")
        repo.mark_completed("BB002BB", completed_at=datetime.now(timezone.utc))
        repo.ensure_pending("CC003CC")

        pending = repo.list_pending()

        assert sorted(pending) == ["AA001AA", "CC003CC"]
    finally:
        repo.close()


def test_state_persists_across_repository_instances(tmp_path):
    """Restart-safety — тот же файл БД, новый экземпляр репозитория (см.
    задачу "Correctness > minimum requests": состояние не живёт только в
    памяти процесса)."""
    db_path = tmp_path / "users.db"
    repo1 = GeorgiaPlateBootstrapRepository(db_path)
    try:
        repo1.ensure_pending("AA001AA")
        repo1.mark_completed("AA001AA", completed_at=datetime.now(timezone.utc))
    finally:
        repo1.close()

    repo2 = GeorgiaPlateBootstrapRepository(db_path)
    try:
        assert repo2.is_completed("AA001AA") is True
    finally:
        repo2.close()
