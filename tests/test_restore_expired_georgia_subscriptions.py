"""scripts/restore_expired_georgia_subscriptions.py: восстанавливаются только
подписки, автоматически истёкшие по старому сроку до перехода на
бессрочный мониторинг; явные отключения сохраняются, дублей задач и
подписок нет, старые квитанции не становятся "новыми штрафами", откат
возвращает только нетронутые строки.
Repository — настоящие (SQLite, tmp_path); Telegram — заглушка; номера и
идентификаторы синтетические."""

import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import UNLIMITED_END_DATE  # noqa: E402
from reader.fines.notification_control import (  # noqa: E402
    EXCLUSION_NOTIFICATION_BOUNDARY,
    EXCLUSION_SUBSCRIPTION_RESTORED,
    GeorgiaNotificationControl,
)
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository  # noqa: E402
from reader.public_bot.delivery_service import ClientDeliveryService  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from scripts import restore_expired_georgia_subscriptions as restore  # noqa: E402

_TBILISI = ZoneInfo("Asia/Tbilisi")
_EXPIRED_AT = "2026-11-10 20:00:00"  # Тбилисская полночь до перехода
_OLD_END = date(2026, 11, 10)


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(_TBILISI).date()


class _Sender:
    def __init__(self):
        self.sent: list[int] = []

    async def send_message(self, chat_id, text, *, buttons=None):
        self.sent.append(chat_id)


class _Fixture:
    def __init__(self, tmp_path):
        self.db_path = tmp_path / "users.db"
        self.tasks = FineMonitoringTaskRepository(self.db_path)
        self.fines = DetectedFineRepository(self.db_path)
        self.subscriptions = FineSubscriptionRepository(self.db_path)
        self.deliveries = ClientFineDeliveryRepository(self.db_path)
        self.control = GeorgiaNotificationControl(self.db_path)

    def sql(self, statement, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.execute(statement, params)
        conn.commit()
        conn.close()

    def task(self, car, *, status="completed", end=_OLD_END):
        task = self.tasks.create(
            car_number=car, label=None, start_date=date(2026, 10, 10), end_date=end,
            telegram_chat_id=-1, created_by_user_id=1, monitoring_scope="client_bot",
        )
        if status != "active":
            self.tasks.set_status(task.id, status)
        return self.tasks.get(task.id)

    def subscription(self, task, user, *, status="expired", end=_OLD_END, updated_at=_EXPIRED_AT, delegated=False):
        subscription = self.subscriptions.create(
            monitoring_task_id=task.id, car_number=task.car_number, telegram_user_id=user,
            telegram_chat_id=user, telegram_username=None, start_date=date(2026, 10, 10), end_date=end,
            created_by_telegram_user_id=9000 if delegated else None,
            created_by_telegram_chat_id=9000 if delegated else None,
        )
        self.sql("UPDATE fine_monitoring_subscriptions SET status = ?, updated_at = ? WHERE id = ?",
                 (status, updated_at, subscription.id))
        return self.subscriptions.get(subscription.id)

    def fine(self, task, protocol_no):
        fine = self.fines.create(
            monitoring_task_id=task.id, car_number=task.car_number, external_fine_id=protocol_no,
            fingerprint=f"fp-{task.id}-{protocol_no}", penalty_date=None, due_date=None,
            delivered_status=None, raw_data="{}", amount=50,
        )
        self.sql("UPDATE detected_fines SET first_detected_at = '2026-10-20 10:00:00' WHERE id = ?", (fine.id,))
        return fine

    def plan(self):
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            return restore.build_restore_plan(conn, today=_today())
        finally:
            conn.close()

    def apply(self, plan=None):
        return restore.apply_restore(self.db_path, plan or self.plan(), today=_today())

    def close(self):
        for repository in (self.control, self.deliveries, self.subscriptions, self.fines, self.tasks):
            repository.close()


@pytest.fixture()
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


def _reasons(plan):
    return {reason: count for (_, reason), count in plan.reasons.items()}


def test_plan_restores_only_automatic_expiries_before_the_migration(fx):
    plain = fx.subscription(fx.task("AA001AA"), 1)
    # дубль: две истёкшие строки одного клиента на одну машину — только последняя
    dup_task = fx.task("AA002AA")
    fx.subscription(dup_task, 2, end=date(2026, 10, 1))
    newest = fx.subscription(dup_task, 2)
    # уже есть действующая подписка на эту машину
    live_task = fx.task("AA003AA", status="active", end=UNLIMITED_END_DATE)
    fx.subscription(live_task, 3)
    fx.subscription(live_task, 3, status="active", end=UNLIMITED_END_DATE)
    # позже клиент сам выключил эту машину
    off_task = fx.task("AA004AA")
    fx.subscription(off_task, 4)
    fx.subscription(off_task, 4, status="stopped", updated_at="2026-11-12 09:00:00")
    # обслуживание остановлено явно
    fx.subscription(fx.task("AA005AA", status="stopped"), 5)
    # истекла уже после перехода
    fx.subscription(fx.task("AA006AA"), 6, updated_at="2026-11-18 20:00:00")
    # непринятое приглашение и выключенная клиентом машина не трогаются
    fx.subscription(fx.task("AA007AA"), 7, status="pending_claim")
    fx.subscription(fx.task("AA008AA"), 8, status="stopped")

    plan = fx.plan()

    assert sorted(c.id for c in plan.candidates) == sorted([plain.id, newest.id])
    assert plan.expired_total == 7
    assert _reasons(plan) == {
        "более старый дубль истёкшей подписки той же машины": 1,
        "у клиента уже есть действующая подписка на эту машину": 1,
        "позже клиент сам выключил или удалил эту машину": 1,
        "обслуживание остановлено явно (задача остановлена)": 1,
        "истекла не до перехода на бессрочный мониторинг": 1,
    }
    assert ("сомнительно", "истекла не до перехода на бессрочный мониторинг") in plan.reasons
    assert (plan.new_plates, plan.new_plates_pending_bootstrap) == (2, 0)


def test_restore_uses_the_existing_unlimited_mechanism_without_duplicates(fx):
    completed = fx.task("AA001AA")
    on_completed = fx.subscription(completed, 1)
    shared = fx.task("AA002AA", status="active", end=date(2026, 12, 1))
    on_active = fx.subscription(shared, 2)
    old_task = fx.task("AA003AA")
    current_task = fx.task("AA003AA", status="active", end=UNLIMITED_END_DATE)
    moved = fx.subscription(old_task, 3)

    result = fx.apply()

    assert result.restored == 3
    for subscription_id in (on_completed.id, on_active.id, moved.id):
        restored = fx.subscriptions.get(subscription_id)
        assert (restored.status, restored.end_date) == ("active", UNLIMITED_END_DATE)
    assert fx.tasks.get(completed.id).status == "active"
    assert fx.tasks.get(shared.id).end_date == UNLIMITED_END_DATE
    assert fx.subscriptions.get(moved.id).monitoring_task_id == current_task.id
    assert fx.tasks.get(old_task.id).status == "completed"  # вторая активная задача номера не появилась
    for car in ("AA001AA", "AA002AA", "AA003AA"):
        assert len(fx.tasks.get_active_by_car_number(car)) == 1
    assert len(fx.subscriptions.list_all_deliverable(today=_today())) == 3


def test_stored_fines_are_excluded_and_nothing_is_sent_after_resume(fx):
    task = fx.task("AA001AA")
    subscription = fx.subscription(task, 1, delegated=True)
    old = fx.fine(task, "OLD1")
    fx.control.pause()

    result = fx.apply()

    assert result.excluded_deliveries == 2  # владелец и менеджер-создатель
    for role in ("owner", "trusted_operator"):
        delivery = fx.deliveries.get(old.id, subscription.id, role)
        assert (delivery.delivered_at, delivery.exclusion_reason) == (None, EXCLUSION_SUBSCRIPTION_RESTORED)

    sender = _Sender()
    service = ClientDeliveryService(
        fx.fines, fx.subscriptions, fx.deliveries, sender, tz=_TBILISI, notification_control=fx.control,
    )
    excluded = service.exclude_pending(reason=EXCLUSION_NOTIFICATION_BOUNDARY)
    fx.control.resume(
        client_excluded=excluded, operator_excluded=0,
        at=datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) - timedelta(hours=1),
    )

    import asyncio
    asyncio.run(service.run_once())
    assert sender.sent == []

    fx.fines.create(  # новый штраф после включения доставки — уходит
        monitoring_task_id=task.id, car_number=task.car_number, external_fine_id="NEW1", fingerprint="fp-new",
        penalty_date=None, due_date=None, delivered_status=None, raw_data="{}", amount=50,
    )
    asyncio.run(service.run_once())
    assert sorted(sender.sent) == [1, 9000]


def test_execute_requires_the_notification_pause(fx, capsys):
    fx.subscription(fx.task("AA001AA"), 1)

    assert restore.main(["--execute", "--db-path", str(fx.db_path)]) == 1
    assert fx.subscriptions.get(1).status == "expired"

    fx.control.pause()
    assert restore.main(["--execute", "--db-path", str(fx.db_path)]) == 0
    assert fx.subscriptions.get(1).status == "active"
    assert "Восстановлено подписок: 1" in capsys.readouterr().out


def test_restore_is_idempotent(fx):
    fx.subscription(fx.task("AA001AA"), 1)
    assert fx.apply().restored == 1

    plan = fx.plan()
    assert (plan.candidates, fx.apply(plan).restored) == ([], 0)


def test_rows_changed_after_the_preview_are_skipped(fx):
    deleted = fx.subscription(fx.task("AA001AA"), 1)
    turned_on = fx.subscription(fx.task("AA002AA"), 2)
    plan = fx.plan()
    fx.subscriptions.archive(deleted.id)  # клиент удалил машину после просмотра
    fx.sql("UPDATE fine_monitoring_subscriptions SET status = 'active', end_date = ? WHERE id = ?",
           (UNLIMITED_END_DATE.isoformat(), turned_on.id))  # и сам включил другую

    result = fx.apply(plan)

    assert (result.restored, sum(result.skipped.values())) == (0, 2)
    assert fx.subscriptions.get(deleted.id).status == "archived"


def test_rollback_returns_untouched_rows_and_their_tasks(fx):
    task = fx.task("AA001AA")
    untouched = fx.subscription(task, 1)
    other_task = fx.task("AA002AA")
    changed = fx.subscription(other_task, 2)
    fx.apply()
    fx.subscriptions.stop_by_owner_or_creator(changed.id, telegram_user_id=2)  # клиент выключил после

    conn = sqlite3.connect(fx.db_path, isolation_level=None)
    try:
        preview = restore.rollback_restore(conn, today=_today(), execute=False)
        assert fx.subscriptions.get(untouched.id).status == "active"  # dry-run ничего не пишет
        done = restore.rollback_restore(conn, today=_today(), execute=True)
    finally:
        conn.close()

    assert preview == done
    back = fx.subscriptions.get(untouched.id)
    assert (back.status, back.end_date, back.monitoring_task_id) == ("expired", _OLD_END, task.id)
    assert (fx.tasks.get(task.id).status, fx.tasks.get(task.id).end_date) == ("completed", _OLD_END)
    assert fx.subscriptions.get(changed.id).status == "stopped"


def test_rollback_keeps_a_task_other_subscribers_still_use(fx):
    task = fx.task("AA001AA")
    restored = fx.subscription(task, 1)
    fx.apply()
    fx.subscription(task, 2, status="active", end=UNLIMITED_END_DATE)  # новый клиент той же машины

    conn = sqlite3.connect(fx.db_path, isolation_level=None)
    try:
        restore.rollback_restore(conn, today=_today(), execute=True)
    finally:
        conn.close()

    assert fx.subscriptions.get(restored.id).status == "expired"
    assert fx.tasks.get(task.id).status == "active"


def test_later_delete_and_unclaimed_invitations_are_respected(fx):
    deleted_task = fx.task("AA001AA")
    fx.subscription(deleted_task, 1)
    fx.subscription(deleted_task, 1, status="archived", updated_at="2026-11-12 09:00:00")
    invited = fx.task("AA002AA")
    restored = fx.subscription(invited, 2)
    fx.subscriptions.create_pending_claim(  # чужое непринятое приглашение на ту же машину
        monitoring_task_id=invited.id, car_number="AA002AA", owner_username_hint="someone",
        created_by_telegram_user_id=9000, created_by_telegram_chat_id=9000,
        start_date=date(2026, 10, 10), end_date=_OLD_END, claim_token="t", claim_token_expires_at=datetime(2026, 10, 20),
    )

    plan = fx.plan()

    assert [c.id for c in plan.candidates] == [restored.id]
    assert _reasons(plan) == {"позже клиент сам выключил или удалил эту машину": 1}
    fx.apply(plan)
    conn = sqlite3.connect(fx.db_path)
    others = conn.execute("SELECT status FROM fine_monitoring_subscriptions WHERE monitoring_task_id = ? AND id != ?",
                          (invited.id, restored.id)).fetchall()
    conn.close()
    assert others == [("pending_claim",)]


def test_an_active_sibling_with_an_elapsed_date_is_doubtful(fx):
    task = fx.task("AA001AA")
    fx.subscription(task, 1)
    fx.subscription(task, 1, status="active", end=date(2026, 11, 15))

    plan = fx.plan()

    assert plan.candidates == []
    assert ("сомнительно", "у клиента есть другая активная строка этой машины с прошедшим сроком") in plan.reasons


def test_rollback_restores_updated_at_so_the_row_can_be_restored_again(fx):
    subscription = fx.subscription(fx.task("AA001AA"), 1)
    fx.apply()
    conn = sqlite3.connect(fx.db_path, isolation_level=None)
    try:
        restore.rollback_restore(conn, today=_today(), execute=True)
    finally:
        conn.close()

    assert fx.subscriptions.get(subscription.id).status == "expired"
    assert [c.id for c in fx.plan().candidates] == [subscription.id]
    assert fx.apply().restored == 1


def test_rollback_of_a_shared_task_follows_restore_order_not_ids(fx):
    task = fx.task("AA001AA")
    first = fx.subscription(task, 1)
    second = fx.subscription(task, 2)
    plan = fx.plan()
    plan.candidates.sort(key=lambda c: c.id, reverse=True)  # восстановление не по порядку id
    fx.apply(plan)

    conn = sqlite3.connect(fx.db_path, isolation_level=None)
    try:
        restore.rollback_restore(conn, today=_today(), execute=True)
    finally:
        conn.close()

    assert {fx.subscriptions.get(first.id).status, fx.subscriptions.get(second.id).status} == {"expired"}
    assert (fx.tasks.get(task.id).status, fx.tasks.get(task.id).end_date) == ("completed", _OLD_END)
