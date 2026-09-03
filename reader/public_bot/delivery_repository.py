"""ClientFineDeliveryRepository — client_fine_deliveries поверх SQLite.

Отдельно от DetectedFineRepository.notification_sent_at (тот флаг означает
"оператор уведомлён", см. reader/fines/notification_coordinator.py) — здесь
трекается доставка КОНКРЕТНОМУ подписчику
(reader.public_bot.models.FineMonitoringSubscription), независимо и
идемпотентно: у одного detected_fine может быть 0..N доставок (по числу
активных подписчиков на момент обнаружения штрафа), каждая ретраится сама
по себе, не влияя на остальные и не влияя на операторский notification_sent_at.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from reader.public_bot.models import ClientFineDelivery

_SCHEMA = """
CREATE TABLE IF NOT EXISTS client_fine_deliveries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_fine_id  INTEGER NOT NULL REFERENCES detected_fines(id),
    subscription_id   INTEGER NOT NULL REFERENCES fine_monitoring_subscriptions(id),
    delivered_at      TIMESTAMP,
    last_attempt_at   TIMESTAMP,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(detected_fine_id, subscription_id)
)
"""

_SELECT_FIELDS = "id, detected_fine_id, subscription_id, delivered_at, last_attempt_at, attempt_count"

_SELECT_BY_PAIR = f"""
    SELECT {_SELECT_FIELDS} FROM client_fine_deliveries
    WHERE detected_fine_id = ? AND subscription_id = ?
"""

# INSERT ... ON CONFLICT — тот же приём, что и UserRepository._UPSERT_*:
# первая попытка доставки создаёт строку с attempt_count=1, каждая
# следующая (после неудачи) увеличивает счётчик и сдвигает last_attempt_at,
# не создавая вторую строку — UNIQUE(detected_fine_id, subscription_id)
# гарантирует это и на уровне схемы, не только приложения.
_RECORD_ATTEMPT = """
INSERT INTO client_fine_deliveries (detected_fine_id, subscription_id, last_attempt_at, attempt_count)
VALUES (:detected_fine_id, :subscription_id, CURRENT_TIMESTAMP, 1)
ON CONFLICT(detected_fine_id, subscription_id) DO UPDATE SET
    last_attempt_at = CURRENT_TIMESTAMP,
    attempt_count = attempt_count + 1
"""

_MARK_DELIVERED = """
UPDATE client_fine_deliveries
SET delivered_at = CURRENT_TIMESTAMP
WHERE detected_fine_id = :detected_fine_id AND subscription_id = :subscription_id
"""


def _row_to_delivery(row) -> ClientFineDelivery:
    id_, detected_fine_id, subscription_id, delivered_at, last_attempt_at, attempt_count = row
    return ClientFineDelivery(
        id=id_,
        detected_fine_id=detected_fine_id,
        subscription_id=subscription_id,
        delivered_at=datetime.fromisoformat(delivered_at) if delivered_at else None,
        last_attempt_at=datetime.fromisoformat(last_attempt_at) if last_attempt_at else None,
        attempt_count=attempt_count,
    )


class ClientFineDeliveryRepository:
    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def get(self, detected_fine_id: int, subscription_id: int) -> ClientFineDelivery | None:
        row = self._conn.execute(_SELECT_BY_PAIR, (detected_fine_id, subscription_id)).fetchone()
        return _row_to_delivery(row) if row else None

    def record_attempt(self, detected_fine_id: int, subscription_id: int) -> ClientFineDelivery:
        """Идемпотентно фиксирует попытку доставки — вызывать ПЕРЕД
        реальной отправкой сообщения клиенту, чтобы иметь attempt_count/
        last_attempt_at даже если сама отправка упадёт. Повторный вызов для
        той же пары (detected_fine_id, subscription_id) не создаёт вторую
        строку — только увеличивает attempt_count существующей."""
        self._conn.execute(
            _RECORD_ATTEMPT,
            {"detected_fine_id": detected_fine_id, "subscription_id": subscription_id},
        )
        self._conn.commit()

        delivery = self.get(detected_fine_id, subscription_id)
        if delivery is None:
            raise RuntimeError("Не удалось прочитать только что записанную попытку доставки")
        return delivery

    def mark_delivered(self, detected_fine_id: int, subscription_id: int) -> None:
        """Идемпотентно — повторный вызов для уже доставленной пары ничего
        не ломает (просто переустанавливает тот же delivered_at). Ничего не
        делает (без ошибки), если record_attempt() для этой пары ещё не
        вызывался — строки просто не существует, обновлять нечего."""
        self._conn.execute(
            _MARK_DELIVERED,
            {"detected_fine_id": detected_fine_id, "subscription_id": subscription_id},
        )
        self._conn.commit()

    def is_delivered(self, detected_fine_id: int, subscription_id: int) -> bool:
        delivery = self.get(detected_fine_id, subscription_id)
        return delivery is not None and delivery.delivered_at is not None

    def close(self) -> None:
        self._conn.close()
