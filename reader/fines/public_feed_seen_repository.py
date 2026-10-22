"""Bounded feed-traversal index для GeorgiaPublicFineFeedJob (см. задачу
"Georgia fines incremental public-feed monitoring" — "PERSIST RECENT
SOURCE IDS" correction). detected_fines fingerprint dedup решает
корректность для СОВПАВШИХ (matched) штрафов, но НЕ даёт дешёвого способа
понять "докуда мы уже долистали" для НЕСОВПАВШИХ (unmatched) строк —
подавляющее большинство строк feed'а (чужие по всей стране номера), о
которых мы сознательно НЕ храним ничего, кроме этого.

ТОЛЬКО protocol_no/publish_date/first_seen_at — НИКАКИХ других полей (см.
задачу "DATA MINIMIZATION" docstring GeorgiaPublicFineFeedJob): ни
protocolAuto/plate, ни сумма, ни место, ни статья, ни описание нарушения —
это НЕ detected_fines и не должно использоваться как источник каких-либо
деталей штрафа, только как "видели ли мы уже эту запись feed'а".

Ограничено по времени (см. retention_days в GeorgiaPublicFineFeedJob) —
при наблюдаемой скорости публикации (~670/день, см. audit report) даже
7 дней хранения — это ~4700 строк, тривиально для SQLite."""

import sqlite3
from datetime import date
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS georgia_public_feed_seen (
    protocol_no   TEXT PRIMARY KEY,
    publish_date  TEXT NOT NULL,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_georgia_public_feed_seen_publish_date
    ON georgia_public_feed_seen (publish_date)
"""

_SELECT_IS_SEEN = "SELECT 1 FROM georgia_public_feed_seen WHERE protocol_no = ?"

_MARK_SEEN = (
    "INSERT OR IGNORE INTO georgia_public_feed_seen (protocol_no, publish_date) "
    "VALUES (:protocol_no, :publish_date)"
)

_DELETE_OLDER_THAN = "DELETE FROM georgia_public_feed_seen WHERE publish_date < ?"

_COUNT = "SELECT COUNT(*) FROM georgia_public_feed_seen"


class GeorgiaPublicFeedSeenRepository:
    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.execute(_INDEX)
        self._conn.commit()

    def is_seen(self, protocol_no: str) -> bool:
        return self._conn.execute(_SELECT_IS_SEEN, (protocol_no,)).fetchone() is not None

    def mark_seen(self, protocol_no: str, *, publish_date: date) -> None:
        """Идемпотентно (INSERT OR IGNORE) — вызывается для КАЖДОЙ строки
        feed'а, встреченной впервые (см. GeorgiaPublicFineFeedJob), вне
        зависимости от того, совпала она с отслеживаемым номером или нет —
        это единственное, что делает "frontier"-детектирование вообще
        возможным (если бы отмечались только совпавшие строки, почти
        каждая страница выглядела бы "полностью новой" навсегда)."""
        self._conn.execute(
            _MARK_SEEN, {"protocol_no": protocol_no, "publish_date": publish_date.isoformat()},
        )
        self._conn.commit()

    def cleanup_older_than(self, cutoff: date) -> int:
        """Retention (см. задачу) — удаляет строки СТРОГО старше cutoff.
        Текущий "frontier" всегда среди самых свежих publish_date
        (сегодня/вчера), поэтому при разумном retention_days (см.
        GeorgiaPublicFineFeedJob, по умолчанию 7 дней) это НИКОГДА не
        затрагивает записи, ещё нужные для определения "уже видели ли мы
        этот день"."""
        cursor = self._conn.execute(_DELETE_OLDER_THAN, (cutoff.isoformat(),))
        self._conn.commit()
        return cursor.rowcount

    def count(self) -> int:
        return self._conn.execute(_COUNT).fetchone()[0]

    def close(self) -> None:
        self._conn.close()
