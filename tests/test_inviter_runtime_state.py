"""Тесты reader/inviter/runtime_state_repository.py — единственная новая
таблица, добавленная ради reader/inviter_admin_bot/ (глобальная пауза +
heartbeat, см. модульный докстрок)."""

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter.runtime_state_repository import (
    InviterRuntimeStateRepository,
)


def test_default_state_is_enabled_with_no_heartbeat(tmp_path):
    repo = InviterRuntimeStateRepository(tmp_path / "inviter.db")
    try:
        state = repo.get()
        assert state.inviter_enabled is True
        assert state.last_tick_at is None
    finally:
        repo.close()


def test_set_enabled_false_then_true(tmp_path):
    repo = InviterRuntimeStateRepository(tmp_path / "inviter.db")
    try:
        disabled = repo.set_enabled(False)
        assert disabled.inviter_enabled is False
        assert repo.get().inviter_enabled is False

        enabled = repo.set_enabled(True)
        assert enabled.inviter_enabled is True
    finally:
        repo.close()


def test_record_tick_updates_heartbeat_independent_of_enabled(tmp_path):
    repo = InviterRuntimeStateRepository(tmp_path / "inviter.db")
    try:
        repo.set_enabled(False)
        now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
        repo.record_tick(now)

        state = repo.get()
        assert state.inviter_enabled is False
        assert state.last_tick_at == now
    finally:
        repo.close()


def test_state_persists_across_repository_reopen(tmp_path):
    db_path = tmp_path / "inviter.db"
    repo1 = InviterRuntimeStateRepository(db_path)
    try:
        repo1.set_enabled(False)
        repo1.record_tick(datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc))
    finally:
        repo1.close()

    repo2 = InviterRuntimeStateRepository(db_path)
    try:
        state = repo2.get()
        assert state.inviter_enabled is False
        assert state.last_tick_at == datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
    finally:
        repo2.close()


def test_reopen_is_idempotent_does_not_duplicate_row(tmp_path):
    """INSERT OR IGNORE — открытие репозитория дважды не должно создавать
    вторую строку id=1 (PRIMARY KEY CHECK гарантирует это на уровне схемы,
    но проверяем поведение, а не полагаемся только на ограничение)."""
    db_path = tmp_path / "inviter.db"
    InviterRuntimeStateRepository(db_path).close()
    repo = InviterRuntimeStateRepository(db_path)
    try:
        state = repo.get()
        assert state.inviter_enabled is True
    finally:
        repo.close()
