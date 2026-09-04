"""
Тесты FineSubscriptionRepository — trusted-operator delegated flow
(pending_claim / claim / list_managed_by_creator / stop_by_owner_or_creator
для создателя-не-владельца), см. design report. Отдельно от
test_fine_subscription_repository.py (Stage 1, self-service), чтобы не
раздувать один файл.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.subscription_repository import (  # noqa: E402
    DuplicatePendingClaimError,
    FineSubscriptionRepository,
)

_CHAT_ID = -100999
_USER_ID = 111
_TRUSTED_ID = 5712994689
_TRUSTED_CHAT_ID = 5712994689


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


def _make_repo(tmp_path) -> FineSubscriptionRepository:
    return FineSubscriptionRepository(tmp_path / "users.db")


def _future_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=7)


def _past_expiry() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=1)


# ---- create() с created_by_*/owner_username_hint (delegated, резолвлено сразу) ----


def test_create_stores_delegation_attribution(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        sub = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=42, telegram_chat_id=42, telegram_username="owner_real",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            owner_username_hint="owner_real",
            created_by_telegram_user_id=_TRUSTED_ID,
            created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        )

        assert sub.is_delegated() is True
        assert sub.created_by_telegram_user_id == _TRUSTED_ID
        assert sub.created_by_telegram_chat_id == _TRUSTED_CHAT_ID
        assert sub.owner_username_hint == "owner_real"
    finally:
        repo.close()


def test_self_service_create_leaves_delegation_fields_none(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        sub = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=42, telegram_chat_id=42, telegram_username="client",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        assert sub.is_delegated() is False
        assert sub.created_by_telegram_user_id is None
    finally:
        repo.close()


# ---- count_active_or_pending_for_task / stop_all_for_task (trusted
# task-level admin ⛔, см. design report: пересмотр архитектуры) ----


def test_count_active_or_pending_for_task_counts_active_and_pending(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="one",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )
        repo.create_pending_claim(
            monitoring_task_id=task_id, car_number="B957MA09",
            owner_username_hint="two",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=_future_expiry(),
        )
        stopped_sub = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=3, telegram_chat_id=3, telegram_username="three",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        repo.stop_by_owner_or_creator(stopped_sub.id, telegram_user_id=3)

        count = repo.count_active_or_pending_for_task(task_id)

        assert count == 2
    finally:
        repo.close()


def test_count_active_or_pending_for_task_is_zero_when_none(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        assert repo.count_active_or_pending_for_task(task_id) == 0
    finally:
        repo.close()


def test_stop_all_for_task_stops_active_and_pending_ignores_other_tasks(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)
    other_task_id = _make_task(db_path, car_number="OTHER01")

    repo = FineSubscriptionRepository(db_path)
    try:
        active_sub = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="one",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )
        pending_sub = repo.create_pending_claim(
            monitoring_task_id=task_id, car_number="B957MA09",
            owner_username_hint="two",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=_future_expiry(),
        )
        other_task_sub = repo.create(
            monitoring_task_id=other_task_id, car_number="OTHER01",
            telegram_user_id=4, telegram_chat_id=4, telegram_username="four",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        affected = repo.stop_all_for_task(task_id)

        assert affected == 2
        assert repo.get(active_sub.id).status == "stopped"
        assert repo.get(active_sub.id).stopped_at is not None
        assert repo.get(pending_sub.id).status == "stopped"
        # Другая задача не затронута.
        assert repo.get(other_task_sub.id).status == "active"
    finally:
        repo.close()


def test_stop_all_for_task_is_noop_when_no_subscriptions(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        affected = repo.stop_all_for_task(task_id)

        assert affected == 0
    finally:
        repo.close()


def test_stop_all_for_task_does_not_touch_already_stopped_or_expired(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        stopped_sub = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="one",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        repo.stop_by_owner_or_creator(stopped_sub.id, telegram_user_id=1)
        before_stopped_at = repo.get(stopped_sub.id).stopped_at

        affected = repo.stop_all_for_task(task_id)

        assert affected == 0
        assert repo.get(stopped_sub.id).stopped_at == before_stopped_at
    finally:
        repo.close()


# ---- pending_claim ----


def test_create_pending_claim_leaves_owner_fields_null(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        sub = repo.create_pending_claim(
            monitoring_task_id=task_id, car_number="B957MA09",
            owner_username_hint="unknown_person",
            created_by_telegram_user_id=_TRUSTED_ID,
            created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=_future_expiry(),
        )

        assert sub.status == "pending_claim"
        assert sub.telegram_user_id is None
        assert sub.telegram_chat_id is None
        assert sub.owner_username_hint == "unknown_person"
        assert sub.claim_token == "tok-1"
        assert sub.is_delegated() is True
    finally:
        repo.close()


def test_duplicate_pending_claim_for_same_task_and_hint_is_rejected(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        repo.create_pending_claim(
            monitoring_task_id=task_id, car_number="B957MA09",
            owner_username_hint="unknown_person",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=_future_expiry(),
        )

        with pytest.raises(DuplicatePendingClaimError):
            repo.create_pending_claim(
                monitoring_task_id=task_id, car_number="B957MA09",
                owner_username_hint="unknown_person",
                created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
                start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
                claim_token="tok-2", claim_token_expires_at=_future_expiry(),
            )
    finally:
        repo.close()


def test_duplicate_pending_claim_hint_is_case_insensitive(tmp_path):
    """Telegram username регистронезависим — "@Ivan" и "@ivan" должны
    считаться одним и тем же незавершённым приглашением (COLLATE NOCASE)."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        repo.create_pending_claim(
            monitoring_task_id=task_id, car_number="B957MA09",
            owner_username_hint="Ivan",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=_future_expiry(),
        )

        with pytest.raises(DuplicatePendingClaimError):
            repo.create_pending_claim(
                monitoring_task_id=task_id, car_number="B957MA09",
                owner_username_hint="ivan",
                created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
                start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
                claim_token="tok-2", claim_token_expires_at=_future_expiry(),
            )
    finally:
        repo.close()


def test_get_pending_claim_for_task_and_hint_is_case_insensitive(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        created = repo.create_pending_claim(
            monitoring_task_id=task_id, car_number="B957MA09",
            owner_username_hint="Ivan",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=_future_expiry(),
        )

        found = repo.get_pending_claim_for_task_and_hint(task_id, "ivan")
        assert found is not None
        assert found.id == created.id
    finally:
        repo.close()


def test_refresh_pending_claim_extends_period_and_replaces_token(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        created = repo.create_pending_claim(
            monitoring_task_id=task_id, car_number="B957MA09",
            owner_username_hint="unknown_person",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            start_date=date(2026, 9, 1), end_date=date(2026, 10, 1),
            claim_token="tok-old", claim_token_expires_at=_future_expiry(),
        )

        refreshed = repo.refresh_pending_claim(
            created.id, start_date=date(2026, 9, 10), end_date=date(2027, 9, 10),
            claim_token="tok-new", claim_token_expires_at=_future_expiry(),
        )

        assert refreshed.id == created.id  # не дубль
        assert refreshed.end_date == date(2027, 9, 10)
        assert refreshed.claim_token == "tok-new"
        # Старый токен погашен — по нему больше claim() не сработает.
        assert repo.claim(
            "tok-old", telegram_user_id=1, telegram_chat_id=1, telegram_username="x",
            now=datetime.now(timezone.utc),
        ) is None
    finally:
        repo.close()


# ---- claim() ----


def test_claim_binds_real_sender_identity(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        pending = repo.create_pending_claim(
            monitoring_task_id=task_id, car_number="B957MA09",
            owner_username_hint="real_owner",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=_future_expiry(),
        )

        claimed = repo.claim(
            "tok-1", telegram_user_id=777, telegram_chat_id=777, telegram_username="real_owner_actual",
            now=datetime.now(timezone.utc),
        )

        assert claimed is not None
        assert claimed.id == pending.id
        assert claimed.status == "active"
        assert claimed.telegram_user_id == 777
        assert claimed.telegram_chat_id == 777
        assert claimed.telegram_username == "real_owner_actual"
        assert claimed.claim_token is None  # погашен
        # Атрибуция того, кто это заказал, сохраняется даже после claim.
        assert claimed.created_by_telegram_user_id == _TRUSTED_ID
    finally:
        repo.close()


def test_claim_rejects_unknown_token(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        result = repo.claim(
            "does-not-exist", telegram_user_id=1, telegram_chat_id=1, telegram_username="x",
            now=datetime.now(timezone.utc),
        )
        assert result is None
    finally:
        repo.close()


def test_claim_rejects_already_claimed_token(tmp_path):
    """Повторный переход по той же ссылке ПОСЛЕ успешного claim не должен
    ничего менять (например, случайно перепривязать подписку к другому
    отправителю)."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        repo.create_pending_claim(
            monitoring_task_id=task_id, car_number="B957MA09",
            owner_username_hint="real_owner",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=_future_expiry(),
        )
        first = repo.claim(
            "tok-1", telegram_user_id=777, telegram_chat_id=777, telegram_username="real_owner",
            now=datetime.now(timezone.utc),
        )
        assert first is not None

        second = repo.claim(
            "tok-1", telegram_user_id=999, telegram_chat_id=999, telegram_username="attacker",
            now=datetime.now(timezone.utc),
        )

        assert second is None
        # Владелец остался тем, кто claimed первым.
        assert repo.get(first.id).telegram_user_id == 777
    finally:
        repo.close()


def test_claim_rejects_expired_token(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        pending = repo.create_pending_claim(
            monitoring_task_id=task_id, car_number="B957MA09",
            owner_username_hint="real_owner",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=_past_expiry(),
        )

        result = repo.claim(
            "tok-1", telegram_user_id=777, telegram_chat_id=777, telegram_username="real_owner",
            now=datetime.now(timezone.utc),
        )

        assert result is None
        assert repo.get(pending.id).status == "pending_claim"  # не изменилась
    finally:
        repo.close()


# ---- list_managed_by_creator ----


def test_list_managed_by_creator_returns_only_this_creators_delegations(tmp_path):
    db_path = tmp_path / "users.db"
    task_a = _make_task(db_path, car_number="AA001AA")
    task_b = _make_task(db_path, car_number="BB002BB")
    other_trusted_id = 410811386

    repo = FineSubscriptionRepository(db_path)
    try:
        managed_by_first = repo.create(
            monitoring_task_id=task_a, car_number="AA001AA",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="owner1",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            owner_username_hint="owner1",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        )
        repo.create(
            monitoring_task_id=task_b, car_number="BB002BB",
            telegram_user_id=2, telegram_chat_id=2, telegram_username="owner2",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            owner_username_hint="owner2",
            created_by_telegram_user_id=other_trusted_id, created_by_telegram_chat_id=other_trusted_id,
        )
        # Собственная (не delegated) подписка того же trusted-оператора не
        # должна попасть в его "managed" список.
        repo.create(
            monitoring_task_id=task_a, car_number="AA001AA",
            telegram_user_id=_TRUSTED_ID, telegram_chat_id=_TRUSTED_CHAT_ID, telegram_username="trusted_self",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        managed = repo.list_managed_by_creator(_TRUSTED_ID)

        assert [s.id for s in managed] == [managed_by_first.id]
    finally:
        repo.close()


# ---- stop_by_owner_or_creator: создатель delegated-подписки ----


def test_creator_can_stop_delegated_subscription_they_created(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        sub = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=42, telegram_chat_id=42, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            owner_username_hint="owner",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        )

        stopped = repo.stop_by_owner_or_creator(sub.id, telegram_user_id=_TRUSTED_ID)

        assert stopped is True
        assert repo.get(sub.id).status == "stopped"
    finally:
        repo.close()


def test_claimed_owner_can_also_stop_delegated_subscription(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        sub = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=42, telegram_chat_id=42, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            owner_username_hint="owner",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        )

        stopped = repo.stop_by_owner_or_creator(sub.id, telegram_user_id=42)

        assert stopped is True
    finally:
        repo.close()


def test_unrelated_third_party_cannot_stop_delegated_subscription(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        sub = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=42, telegram_chat_id=42, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            owner_username_hint="owner",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        )

        stopped = repo.stop_by_owner_or_creator(sub.id, telegram_user_id=999999)

        assert stopped is False
        assert repo.get(sub.id).status == "active"
    finally:
        repo.close()


def test_self_service_subscription_still_only_stoppable_by_its_owner(tmp_path):
    """Регресс: обычная (не-delegated) подписка — created_by_telegram_user_id
    IS NULL — "OR created_by_telegram_user_id = :id" не должно НИКОГДА
    случайно совпасть (SQL: NULL = что угодно никогда не TRUE)."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        sub = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=42, telegram_chat_id=42, telegram_username="alice",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        stopped_by_stranger = repo.stop_by_owner_or_creator(sub.id, telegram_user_id=999999)
        assert stopped_by_stranger is False

        stopped_by_owner = repo.stop_by_owner_or_creator(sub.id, telegram_user_id=42)
        assert stopped_by_owner is True
    finally:
        repo.close()


def test_creator_can_stop_own_pending_claim_invitation(tmp_path):
    """См. design report Stage 4 — trusted-оператор должен уметь отменить
    ещё НЕ claimed приглашение через ⛔ Остановить мониторинг."""
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        pending = repo.create_pending_claim(
            monitoring_task_id=task_id, car_number="B957MA09",
            owner_username_hint="unknown_person",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=_future_expiry(),
        )

        stopped = repo.stop_by_owner_or_creator(pending.id, telegram_user_id=_TRUSTED_ID)

        assert stopped is True
        assert repo.get(pending.id).status == "stopped"
    finally:
        repo.close()


def test_stranger_cannot_stop_pending_claim_invitation(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path)

    repo = FineSubscriptionRepository(db_path)
    try:
        pending = repo.create_pending_claim(
            monitoring_task_id=task_id, car_number="B957MA09",
            owner_username_hint="unknown_person",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=_future_expiry(),
        )

        stopped = repo.stop_by_owner_or_creator(pending.id, telegram_user_id=999999)

        assert stopped is False
        assert repo.get(pending.id).status == "pending_claim"
    finally:
        repo.close()


# ---- list_all_deliverable / max_relevant_end_date_for_car (client delivery
# poller + task lifecycle, см. design report Stage 4) ----


def test_list_all_deliverable_includes_active_and_pending_claim(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path, car_number="B957MA09")

    repo = FineSubscriptionRepository(db_path)
    try:
        active = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="alice",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )
        pending = repo.create_pending_claim(
            monitoring_task_id=task_id, car_number="B957MA09",
            owner_username_hint="unknown_person",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=_future_expiry(),
        )

        deliverable = repo.list_all_deliverable(today=date(2026, 9, 15))

        assert {s.id for s in deliverable} == {active.id, pending.id}
    finally:
        repo.close()


def test_list_all_deliverable_excludes_expired_and_stopped(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path, car_number="B957MA09")

    repo = FineSubscriptionRepository(db_path)
    try:
        expired = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="alice",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        stopped = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=2, telegram_chat_id=2, telegram_username="bob",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )
        repo.stop_by_owner_or_creator(stopped.id, telegram_user_id=2)

        deliverable = repo.list_all_deliverable(today=date(2026, 9, 15))

        assert expired.id not in {s.id for s in deliverable}
        assert stopped.id not in {s.id for s in deliverable}
    finally:
        repo.close()


def test_max_relevant_end_date_for_car_returns_max_across_active_and_pending(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path, car_number="B957MA09")

    repo = FineSubscriptionRepository(db_path)
    try:
        repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="alice",
            start_date=date(2026, 9, 1), end_date=date(2026, 10, 1),
        )
        repo.create_pending_claim(
            monitoring_task_id=task_id, car_number="B957MA09",
            owner_username_hint="unknown_person",
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            start_date=date(2026, 9, 1), end_date=date(2027, 3, 1),  # самый долгий
            claim_token="tok-1", claim_token_expires_at=_future_expiry(),
        )

        result = repo.max_relevant_end_date_for_car("B957MA09", today=date(2026, 9, 15))

        assert result == date(2027, 3, 1)
    finally:
        repo.close()


def test_max_relevant_end_date_for_car_ignores_stopped_and_expired(tmp_path):
    db_path = tmp_path / "users.db"
    task_id = _make_task(db_path, car_number="B957MA09")

    repo = FineSubscriptionRepository(db_path)
    try:
        old = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="alice",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        stopped = repo.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=2, telegram_chat_id=2, telegram_username="bob",
            start_date=date(2026, 9, 1), end_date=date(2027, 1, 1),
        )
        repo.stop_by_owner_or_creator(stopped.id, telegram_user_id=2)

        result = repo.max_relevant_end_date_for_car("B957MA09", today=date(2026, 9, 15))

        assert result is None
        assert old.end_date == date(2026, 1, 31)  # sanity: подписка реально просрочена
    finally:
        repo.close()


def test_max_relevant_end_date_for_car_returns_none_when_no_subscriptions(tmp_path):
    repo = _make_repo(tmp_path)
    try:
        assert repo.max_relevant_end_date_for_car("ZZ999ZZ", today=date(2026, 9, 15)) is None
    finally:
        repo.close()
