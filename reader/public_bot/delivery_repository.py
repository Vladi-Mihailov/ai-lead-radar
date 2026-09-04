"""ClientFineDeliveryRepository — client_fine_deliveries поверх SQLite.

Отдельно от DetectedFineRepository.notification_sent_at (тот флаг означает
"оператор уведомлён", см. reader/fines/notification_coordinator.py) — здесь
трекается доставка КОНКРЕТНОМУ получателю в рамках одной подписки
(reader.public_bot.models.FineMonitoringSubscription), независимо и
идемпотентно: у одной delegated-подписки может быть до ДВУХ независимых
получателей одного и того же detected_fine — 'owner' (реальный владелец
машины) и 'trusted_operator' (кто поставил её на мониторинг за него), см.
design report про trusted-operator delegated flow. Для обычной (не-
delegated) подписки существует только recipient_role='owner'. Каждая пара
(detected_fine_id, subscription_id, recipient_role) ретраится сама по
себе, не влияя на остальные и не влияя на операторский notification_sent_at.
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from reader.public_bot.models import ClientFineDelivery

RecipientRole = Literal["owner", "trusted_operator"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS client_fine_deliveries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_fine_id  INTEGER NOT NULL REFERENCES detected_fines(id),
    subscription_id   INTEGER NOT NULL REFERENCES fine_monitoring_subscriptions(id),
    recipient_role    TEXT NOT NULL DEFAULT 'owner',
    delivered_at      TIMESTAMP,
    last_attempt_at   TIMESTAMP,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(detected_fine_id, subscription_id, recipient_role)
)
"""

_SELECT_FIELDS = (
    "id, detected_fine_id, subscription_id, recipient_role, "
    "delivered_at, last_attempt_at, attempt_count"
)

_SELECT_BY_KEY = f"""
    SELECT {_SELECT_FIELDS} FROM client_fine_deliveries
    WHERE detected_fine_id = ? AND subscription_id = ? AND recipient_role = ?
"""

# INSERT ... ON CONFLICT — тот же приём, что и UserRepository._UPSERT_*:
# первая попытка доставки создаёт строку с attempt_count=1, каждая
# следующая (после неудачи) увеличивает счётчик и сдвигает last_attempt_at,
# не создавая вторую строку — UNIQUE(detected_fine_id, subscription_id,
# recipient_role) гарантирует это и на уровне схемы, не только приложения.
_RECORD_ATTEMPT = """
INSERT INTO client_fine_deliveries (detected_fine_id, subscription_id, recipient_role, last_attempt_at, attempt_count)
VALUES (:detected_fine_id, :subscription_id, :recipient_role, CURRENT_TIMESTAMP, 1)
ON CONFLICT(detected_fine_id, subscription_id, recipient_role) DO UPDATE SET
    last_attempt_at = CURRENT_TIMESTAMP,
    attempt_count = attempt_count + 1
"""

_MARK_DELIVERED = """
UPDATE client_fine_deliveries
SET delivered_at = CURRENT_TIMESTAMP
WHERE detected_fine_id = :detected_fine_id AND subscription_id = :subscription_id
  AND recipient_role = :recipient_role
"""


def _row_to_delivery(row) -> ClientFineDelivery:
    id_, detected_fine_id, subscription_id, recipient_role, delivered_at, last_attempt_at, attempt_count = row
    return ClientFineDelivery(
        id=id_,
        detected_fine_id=detected_fine_id,
        subscription_id=subscription_id,
        recipient_role=recipient_role,
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
        self._migrate_recipient_role_if_needed()
        self._conn.commit()

    def _migrate_recipient_role_if_needed(self) -> None:
        """Существующие (Stage 1) базы имеют UNIQUE(detected_fine_id,
        subscription_id) БЕЗ recipient_role — production ни разу не
        писал в эту таблицу (доставка ещё не была реализована), но
        безопасная миграция всё равно нужна для любой среды, где эта
        таблица уже была создана Stage 1 кодом. SQLite не может изменить
        существующий UNIQUE-constraint через ALTER TABLE — та же "12-step"
        процедура пересоздания таблицы, что и в
        FineSubscriptionRepository, с сохранением id."""
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(client_fine_deliveries)")
        }
        if "recipient_role" in columns:
            return

        with self._conn:
            self._conn.execute(
                "ALTER TABLE client_fine_deliveries RENAME TO client_fine_deliveries_pre_recipient_role"
            )
            self._conn.execute(_SCHEMA)
            self._conn.execute(
                """
                INSERT INTO client_fine_deliveries (
                    id, detected_fine_id, subscription_id, recipient_role,
                    delivered_at, last_attempt_at, attempt_count
                )
                SELECT
                    id, detected_fine_id, subscription_id, 'owner',
                    delivered_at, last_attempt_at, attempt_count
                FROM client_fine_deliveries_pre_recipient_role
                """
            )
            self._conn.execute("DROP TABLE client_fine_deliveries_pre_recipient_role")
            self._conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) "
                "SELECT 'client_fine_deliveries', COALESCE(MAX(id), 0) "
                "FROM client_fine_deliveries "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM sqlite_sequence WHERE name = 'client_fine_deliveries'"
                ")"
            )
            self._conn.execute(
                "UPDATE sqlite_sequence SET seq = ("
                "  SELECT COALESCE(MAX(id), 0) FROM client_fine_deliveries"
                ") WHERE name = 'client_fine_deliveries'"
            )

    def get(
        self, detected_fine_id: int, subscription_id: int, recipient_role: RecipientRole = "owner",
    ) -> ClientFineDelivery | None:
        row = self._conn.execute(
            _SELECT_BY_KEY, (detected_fine_id, subscription_id, recipient_role)
        ).fetchone()
        return _row_to_delivery(row) if row else None

    def record_attempt(
        self, detected_fine_id: int, subscription_id: int, recipient_role: RecipientRole = "owner",
    ) -> ClientFineDelivery:
        """Идемпотентно фиксирует попытку доставки — вызывать ПЕРЕД
        реальной отправкой сообщения получателю, чтобы иметь attempt_count/
        last_attempt_at даже если сама отправка упадёт. Повторный вызов для
        той же тройки (detected_fine_id, subscription_id, recipient_role)
        не создаёт вторую строку — только увеличивает attempt_count
        существующей; 'owner' и 'trusted_operator' одной и той же подписки
        трекаются полностью независимо."""
        self._conn.execute(
            _RECORD_ATTEMPT,
            {
                "detected_fine_id": detected_fine_id,
                "subscription_id": subscription_id,
                "recipient_role": recipient_role,
            },
        )
        self._conn.commit()

        delivery = self.get(detected_fine_id, subscription_id, recipient_role)
        if delivery is None:
            raise RuntimeError("Не удалось прочитать только что записанную попытку доставки")
        return delivery

    def mark_delivered(
        self, detected_fine_id: int, subscription_id: int, recipient_role: RecipientRole = "owner",
    ) -> None:
        """Идемпотентно — повторный вызов для уже доставленной тройки
        ничего не ломает (просто переустанавливает тот же delivered_at).
        Ничего не делает (без ошибки), если record_attempt() для этой
        тройки ещё не вызывался — строки просто не существует, обновлять
        нечего."""
        self._conn.execute(
            _MARK_DELIVERED,
            {
                "detected_fine_id": detected_fine_id,
                "subscription_id": subscription_id,
                "recipient_role": recipient_role,
            },
        )
        self._conn.commit()

    def is_delivered(
        self, detected_fine_id: int, subscription_id: int, recipient_role: RecipientRole = "owner",
    ) -> bool:
        delivery = self.get(detected_fine_id, subscription_id, recipient_role)
        return delivery is not None and delivery.delivered_at is not None

    def close(self) -> None:
        self._conn.close()
