"""Singleton cursor/state для GeorgiaPublicFineFeedJob (см. задачу
"Georgia fines incremental public-feed monitoring" — "ADD PERSISTENT FEED
STATE / CURSOR"). detected_fines fingerprint dedup уже достаточен для
MATCHED штрафов, но НЕ является курсором обхода самого feed'а: unmatched
строки сознательно нигде не сохраняются (см. задачу "DATA MINIMIZATION"),
поэтому "докуда долистали в прошлый раз" нужно хранить отдельно, иначе
каждый цикл пришлось бы листать весь backlog (311k+ строк) с нуля.

ОДНА строка (id=1) — тот же singleton-паттерн, что и
reader/inviter/runtime_state_repository.py::InviterRuntimeStateRepository.

publishDate у police.ge — ТОЛЬКО дата, без времени (см. audit report) —
newest_seen_publish_date сознательно date, а НЕ datetime: сравнение "уже
видели этот день" внутри одного календарного дня принципиально не может
быть точнее дня средствами самого feed'а. Корректность опирается на
detected_fines fingerprint dedup (см. GeorgiaPublicFineFeedJob) — ЭТОТ
курсор только ограничивает, СКОЛЬКО страниц нужно перечитать в начале
каждого цикла (оптимизация, не источник истины, см. задачу: "Correctness
> minimum requests").

last_successful_poll_at обновляется ТОЛЬКО когда ВЕСЬ цикл (все нужные
страницы в пределах overlap-окна) завершился без ошибки И без упора в
safety cap до того, как окно было покрыто — иначе курсор остаётся
нетронутым, и следующий цикл просто повторяет то же (или более широкое)
окно заново, что безопасно благодаря fingerprint dedup (см. задачу
"FEED CURSOR": "If poll fails halfway: do not advance cursor beyond
unprocessed data")."""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS georgia_public_feed_state (
    id                       INTEGER PRIMARY KEY CHECK (id = 1),
    newest_seen_publish_date TEXT,
    last_successful_poll_at  TIMESTAMP,
    last_completed_at        TIMESTAMP,
    last_error               TEXT,
    last_error_at            TIMESTAMP
)
"""

_ENSURE_ROW = "INSERT OR IGNORE INTO georgia_public_feed_state (id) VALUES (1)"

_SELECT = (
    "SELECT newest_seen_publish_date, last_successful_poll_at, last_completed_at, "
    "last_error, last_error_at FROM georgia_public_feed_state WHERE id = 1"
)

_RECORD_SUCCESS = """
UPDATE georgia_public_feed_state
SET newest_seen_publish_date = :newest_seen_publish_date,
    last_successful_poll_at = :polled_at,
    last_completed_at = :polled_at,
    last_error = NULL,
    last_error_at = NULL
WHERE id = 1
"""

_RECORD_ERROR = """
UPDATE georgia_public_feed_state SET last_error = :message, last_error_at = :at WHERE id = 1
"""


@dataclass(frozen=True)
class PublicFeedState:
    newest_seen_publish_date: date | None
    last_successful_poll_at: datetime | None
    last_completed_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None


def _row_to_state(row) -> PublicFeedState:
    newest, polled_at, completed_at, error, error_at = row
    return PublicFeedState(
        newest_seen_publish_date=date.fromisoformat(newest) if newest else None,
        last_successful_poll_at=datetime.fromisoformat(polled_at) if polled_at else None,
        last_completed_at=datetime.fromisoformat(completed_at) if completed_at else None,
        last_error=error,
        last_error_at=datetime.fromisoformat(error_at) if error_at else None,
    )


class GeorgiaPublicFeedStateRepository:
    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.execute(_ENSURE_ROW)
        self._conn.commit()

    def get(self) -> PublicFeedState:
        row = self._conn.execute(_SELECT).fetchone()
        return _row_to_state(row)

    def record_successful_cycle(self, *, newest_seen_publish_date: date, polled_at: datetime) -> None:
        self._conn.execute(_RECORD_SUCCESS, {
            "newest_seen_publish_date": newest_seen_publish_date.isoformat(),
            "polled_at": polled_at.isoformat(),
        })
        self._conn.commit()

    def record_error(self, message: str, *, at: datetime) -> None:
        self._conn.execute(_RECORD_ERROR, {"message": message, "at": at.isoformat()})
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
