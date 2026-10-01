"""Глобальное runtime-состояние инвайтера (см. reader/inviter_admin_bot/) —
ЕДИНСТВЕННЫЙ новый механизм, добавленный ради admin-бота: до него в
reader/inviter/ не было НИКАКОГО способа приостановить автоматические
приглашения, не трогая enabled отдельных аккаунтов и не убивая процесс
(см. аудит перед реализацией — pause/resume механизм отсутствовал
полностью). Одна строка (id=1) — inviter_enabled (см.
InviterWorker.run_one_tick) и last_tick_at (heartbeat для "📊 Статус" —
admin-бот работает ОТДЕЛЬНЫМ процессом от worker'а, поэтому статус нужно
читать из БД, а не из памяти процесса worker'а).

inviter_enabled=False НЕ останавливает сам процесс worker'а (никакого
systemctl stop/start из Telegram-бота, см. задачу) — worker продолжает
тикать (heartbeat продолжает обновляться), просто run_one_tick() ничего
не отправляет, пока флаг не станет снова True."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inviter_runtime_state (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    inviter_enabled  INTEGER NOT NULL DEFAULT 1,
    last_tick_at     TIMESTAMP,
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_ENSURE_ROW = "INSERT OR IGNORE INTO inviter_runtime_state (id, inviter_enabled) VALUES (1, 1)"

_SELECT = "SELECT inviter_enabled, last_tick_at, updated_at FROM inviter_runtime_state WHERE id = 1"

_UPDATE_ENABLED = (
    "UPDATE inviter_runtime_state SET inviter_enabled = :enabled, "
    "updated_at = CURRENT_TIMESTAMP WHERE id = 1"
)

_UPDATE_TICK = (
    "UPDATE inviter_runtime_state SET last_tick_at = :last_tick_at, "
    "updated_at = CURRENT_TIMESTAMP WHERE id = 1"
)


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class InviterRuntimeState:
    inviter_enabled: bool
    last_tick_at: datetime | None
    updated_at: datetime


# Мультикампании (см. reader/inviter/models.py InviteCampaign): admin-бот
# теперь включает/выключает КАЖДУЮ кампанию отдельно
# (invite_campaigns.enabled — worker и так учитывает его, см.
# InviterWorker._enabled_pairs) вместо одного глобального ▶️/⏸. Чтобы
# фактическое состояние не изменилось при переходе, один раз (маркер
# per_campaign_since) глобальная пауза переносится в кампании: если
# inviter_enabled был 0 — все СУЩЕСТВУЮЩИЕ кампании получают enabled=0, а
# inviter_enabled становится 1 (то, что раньше было "глобально на паузе",
# теперь "каждая кампания выключена" — эффективно то же самое). Если
# inviter_enabled был 1 — кампании не трогаются вовсе. Повторные открытия
# ничего не делают (маркер уже задан). inviter_enabled сам по себе
# остаётся и по-прежнему учитывается worker'ом — просто admin-бот его
# больше не переключает.
_MIGRATE_ADD_MARKER = "ALTER TABLE inviter_runtime_state ADD COLUMN per_campaign_since TIMESTAMP"


class InviterRuntimeStateRepository:
    """Единственная строка (id=1) — тот же принцип single-row-settings-
    table, что и у reader/turkey_bot (см. subscription_repository-подобные
    модули), просто ещё проще: здесь буквально два поля."""

    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.execute(_ENSURE_ROW)
        self._migrate_global_pause_to_campaigns()
        self._conn.commit()

    def _migrate_global_pause_to_campaigns(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(inviter_runtime_state)")}
        if "per_campaign_since" not in columns:
            self._conn.execute(_MIGRATE_ADD_MARKER)
        enabled, marker = self._conn.execute(
            "SELECT inviter_enabled, per_campaign_since FROM inviter_runtime_state WHERE id = 1"
        ).fetchone()
        if marker is not None:
            return
        has_campaigns = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='invite_campaigns'"
        ).fetchone()
        if not enabled and has_campaigns:
            self._conn.execute("UPDATE invite_campaigns SET enabled = 0")
        self._conn.execute(
            "UPDATE inviter_runtime_state SET inviter_enabled = 1, "
            "per_campaign_since = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = 1"
        )

    def get(self) -> InviterRuntimeState:
        row = self._conn.execute(_SELECT).fetchone()
        enabled, last_tick_at, updated_at = row
        return InviterRuntimeState(
            inviter_enabled=bool(enabled),
            last_tick_at=_parse_datetime(last_tick_at),
            updated_at=_parse_datetime(updated_at),
        )

    def set_enabled(self, enabled: bool) -> InviterRuntimeState:
        self._conn.execute(_UPDATE_ENABLED, {"enabled": enabled})
        self._conn.commit()
        return self.get()

    def record_tick(self, at: datetime) -> None:
        """Вызывается InviterWorker на КАЖДОМ тике, независимо от
        inviter_enabled — heartbeat должен показывать, что процесс жив,
        даже пока автоприглашения приостановлены (см. модульный докстрок:
        "worker остаётся running")."""
        self._conn.execute(_UPDATE_TICK, {"last_tick_at": at.isoformat()})
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
