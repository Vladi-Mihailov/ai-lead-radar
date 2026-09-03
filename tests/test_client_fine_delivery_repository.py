"""
Тесты ClientFineDeliveryRepository — идемпотентная доставка обнаруженного
штрафа конкретному клиентскому подписчику (@GEShtrafbot foundation, см.
design report). Только сама таблица/репозиторий.
"""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402

_CHAT_ID = -100999
_USER_ID = 111


def _make_task(db_path, car_number="B957MA09") -> int:
    task_repo = FineMonitoringTaskRepository(db_path)
    try:
        task = task_repo.create(
            car_number=car_number, label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        return task.id
    finally:
        task_repo.close()


def _make_detected_fine(db_path, task_id, *, fingerprint="fp-1") -> int:
    fine_repo = DetectedFineRepository(db_path)
    try:
        fine = fine_repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            external_fine_id="AB123456", fingerprint=fingerprint,
            penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
            delivered_status="Не вручено", raw_data='{"protocolNo": "AB123456"}',
        )
        return fine.id
    finally:
        fine_repo.close()


def _make_subscription(db_path, task_id, *, telegram_user_id=1) -> int:
    sub_repo = FineSubscriptionRepository(db_path)
    try:
        sub = sub_repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id,
            telegram_username=f"user{telegram_user_id}",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )
        return sub.id
    finally:
        sub_repo.close()


def test_get_returns_none_when_no_delivery_exists(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    fine_id = _make_detected_fine(db_path, task_id)
    sub_id = _make_subscription(db_path, task_id)

    repo = ClientFineDeliveryRepository(db_path)
    try:
        assert repo.get(fine_id, sub_id) is None
        assert repo.is_delivered(fine_id, sub_id) is False
    finally:
        repo.close()


def test_record_attempt_creates_row_with_attempt_count_one(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    fine_id = _make_detected_fine(db_path, task_id)
    sub_id = _make_subscription(db_path, task_id)

    repo = ClientFineDeliveryRepository(db_path)
    try:
        delivery = repo.record_attempt(fine_id, sub_id)

        assert delivery.detected_fine_id == fine_id
        assert delivery.subscription_id == sub_id
        assert delivery.attempt_count == 1
        assert delivery.last_attempt_at is not None
        assert delivery.delivered_at is None
    finally:
        repo.close()


def test_record_attempt_is_idempotent_and_increments_counter_without_new_row(tmp_path):
    """Дедуп: повторные попытки (например, после сетевой ошибки отправки)
    не создают вторую строку — UNIQUE(detected_fine_id, subscription_id)."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    fine_id = _make_detected_fine(db_path, task_id)
    sub_id = _make_subscription(db_path, task_id)

    repo = ClientFineDeliveryRepository(db_path)
    try:
        first = repo.record_attempt(fine_id, sub_id)
        second = repo.record_attempt(fine_id, sub_id)
        third = repo.record_attempt(fine_id, sub_id)

        assert first.id == second.id == third.id  # одна и та же строка
        assert third.attempt_count == 3

        rows = repo._conn.execute(
            "SELECT COUNT(*) FROM client_fine_deliveries WHERE detected_fine_id = ? AND subscription_id = ?",
            (fine_id, sub_id),
        ).fetchone()
        assert rows[0] == 1
    finally:
        repo.close()


def test_mark_delivered_then_is_delivered_true(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    fine_id = _make_detected_fine(db_path, task_id)
    sub_id = _make_subscription(db_path, task_id)

    repo = ClientFineDeliveryRepository(db_path)
    try:
        repo.record_attempt(fine_id, sub_id)
        assert repo.is_delivered(fine_id, sub_id) is False

        repo.mark_delivered(fine_id, sub_id)

        assert repo.is_delivered(fine_id, sub_id) is True
        assert repo.get(fine_id, sub_id).delivered_at is not None
    finally:
        repo.close()


def test_mark_delivered_is_idempotent(tmp_path):
    """Повторный mark_delivered() для уже доставленной пары не ломается и
    не создаёт вторую строку."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    fine_id = _make_detected_fine(db_path, task_id)
    sub_id = _make_subscription(db_path, task_id)

    repo = ClientFineDeliveryRepository(db_path)
    try:
        repo.record_attempt(fine_id, sub_id)
        repo.mark_delivered(fine_id, sub_id)
        repo.mark_delivered(fine_id, sub_id)

        assert repo.is_delivered(fine_id, sub_id) is True
        rows = repo._conn.execute(
            "SELECT COUNT(*) FROM client_fine_deliveries WHERE detected_fine_id = ? AND subscription_id = ?",
            (fine_id, sub_id),
        ).fetchone()
        assert rows[0] == 1
    finally:
        repo.close()


def test_mark_delivered_without_prior_attempt_does_nothing(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    fine_id = _make_detected_fine(db_path, task_id)
    sub_id = _make_subscription(db_path, task_id)

    repo = ClientFineDeliveryRepository(db_path)
    try:
        repo.mark_delivered(fine_id, sub_id)  # не должно бросать исключение

        assert repo.get(fine_id, sub_id) is None
    finally:
        repo.close()


def test_delivery_is_independent_per_subscription(tmp_path):
    """Один и тот же detected_fine_id может быть доставлен нескольким
    подписчикам одного автомобиля независимо — доставка одному не влияет
    на статус доставки другому (см. design про fan-out)."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    fine_id = _make_detected_fine(db_path, task_id)
    sub_alice = _make_subscription(db_path, task_id, telegram_user_id=1)
    sub_bob = _make_subscription(db_path, task_id, telegram_user_id=2)

    repo = ClientFineDeliveryRepository(db_path)
    try:
        repo.record_attempt(fine_id, sub_alice)
        repo.mark_delivered(fine_id, sub_alice)

        repo.record_attempt(fine_id, sub_bob)

        assert repo.is_delivered(fine_id, sub_alice) is True
        assert repo.is_delivered(fine_id, sub_bob) is False
    finally:
        repo.close()


def test_data_persists_across_repository_reopen(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    fine_id = _make_detected_fine(db_path, task_id)
    sub_id = _make_subscription(db_path, task_id)

    repo1 = ClientFineDeliveryRepository(db_path)
    repo1.record_attempt(fine_id, sub_id)
    repo1.mark_delivered(fine_id, sub_id)
    repo1.close()

    repo2 = ClientFineDeliveryRepository(db_path)
    try:
        assert repo2.is_delivered(fine_id, sub_id) is True
    finally:
        repo2.close()
