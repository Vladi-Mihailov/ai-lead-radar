"""▶️ менеджера возвращает клиентские подписки, отключённые его же ⛔
(reader/public_bot/subscription_service.py::resume_task_with_client_subscriptions,
reader/public_bot/subscription_repository.py::restore_after_manager_resume).

Только подписки, отключённые ИМЕННО предыдущим ⛔ менеджера и с тех пор не
изменённые; решения клиентов, удалённые машины, истёкшие приглашения и
отключения без записи о причине не трогаются; повторное ▶️ ничего не
дублирует; известные штрафы восстановленному клиенту новыми не приходят.
SQLite настоящий (tmp_path), Telegram — заглушка; данные синтетические."""

import asyncio
import functools
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import UNLIMITED_END_DATE  # noqa: E402
from reader.fines.notification_control import EXCLUSION_SUBSCRIPTION_RESTORED  # noqa: E402
from reader.fines.provider import FineProvider  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.conversation import ConversationController  # noqa: E402
from reader.public_bot.conversation_state_repository import BotConversationStateRepository  # noqa: E402
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository  # noqa: E402
from reader.public_bot.delivery_service import ClientDeliveryService, exclude_known_fines_for_subscription  # noqa: E402
from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402
from reader.public_bot.statistics_service import BotStatisticsService  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import SubscriptionService  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_TBILISI = ZoneInfo("Asia/Tbilisi")
_MANAGER = 900000001
_CLIENT_A = 700001
_CLIENT_B = 700002
_CAR = "AA111BB"
_TODAY = date(2026, 11, 21)


class _NoProvider(FineProvider):
    async def search_by_plate(self, plate):
        return []


class _Sender:
    def __init__(self):
        self.sent: list[int] = []

    async def send_message(self, chat_id, text, *, buttons=None):
        self.sent.append(chat_id)


class _Fixture:
    def __init__(self, tmp_path, *, with_excluder=True):
        self.db_path = tmp_path / "users.db"
        self.tasks = FineMonitoringTaskRepository(self.db_path)
        self.fines = DetectedFineRepository(self.db_path)
        self.subscriptions = FineSubscriptionRepository(self.db_path)
        self.users = UserRepository(self.db_path)
        self.deliveries = ClientFineDeliveryRepository(self.db_path)
        self.states = BotConversationStateRepository(self.db_path)
        self.known = BotKnownUsersRepository(self.db_path)
        excluder = functools.partial(
            exclude_known_fines_for_subscription, detected_fine_repository=self.fines,
            delivery_repository=self.deliveries, reason=EXCLUSION_SUBSCRIPTION_RESTORED,
        ) if with_excluder else None
        self.service = SubscriptionService(
            self.tasks, self.subscriptions, self.users,
            FineCheckService(_NoProvider(), self.tasks, self.fines),
            restored_history_excluder=excluder,
        )
        self.controller = ConversationController(
            self.states, self.service, BotStatisticsService(self.known, self.subscriptions, self.fines), self.known,
            tz=_TBILISI, trusted_operator_user_ids=frozenset({_MANAGER}),
        )
        self.sender = _Sender()
        self.delivery = ClientDeliveryService(self.fines, self.subscriptions, self.deliveries, self.sender, tz=_TBILISI)
        self.task = self.tasks.create(
            car_number=_CAR, label=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
            telegram_chat_id=_MANAGER, created_by_user_id=_MANAGER, monitoring_scope="client_bot",
        )

    def client(self, user_id, *, task=None, created_by=None):
        task = task or self.task
        return self.subscriptions.create(
            monitoring_task_id=task.id, car_number=task.car_number, telegram_user_id=user_id,
            telegram_chat_id=user_id, telegram_username=None, start_date=date(2026, 11, 1),
            end_date=UNLIMITED_END_DATE, created_by_telegram_user_id=created_by, created_by_telegram_chat_id=created_by,
        )

    def invitation(self, *, expires_at):
        return self.subscriptions.create_pending_claim(
            monitoring_task_id=self.task.id, car_number=_CAR, owner_username_hint="future_client",
            created_by_telegram_user_id=_MANAGER, created_by_telegram_chat_id=_MANAGER,
            start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE, claim_token=f"tok-{expires_at:%H%M%S}",
            claim_token_expires_at=expires_at,
        )

    def fine(self, protocol_no):
        return self.fines.create(
            monitoring_task_id=self.task.id, car_number=_CAR, external_fine_id=protocol_no,
            fingerprint=f"fp-{protocol_no}", penalty_date=None, due_date=None, delivered_status=None,
            raw_data="{}", amount=50,
        )

    def stop(self):
        reply = self.controller.handle_trusted_stop_confirm(self.task.id, telegram_user_id=_MANAGER)
        assert self.tasks.get(self.task.id).status == "stopped", reply.text
        return reply

    def resume(self):
        return self.controller.handle_trusted_task_continue(self.task.id, 0, telegram_user_id=_MANAGER)

    def status(self, subscription):
        return self.subscriptions.get(subscription.id).status

    def count(self, table):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def close(self):
        for repository in (self.known, self.states, self.deliveries, self.users, self.subscriptions, self.fines,
                           self.tasks):
            repository.close()


@pytest.fixture()
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


def test_manager_resume_restores_the_subscriptions_his_stop_turned_off(fx):
    subscription = fx.client(_CLIENT_A)
    fx.stop()
    assert fx.status(subscription) == "stopped"

    reply = fx.resume()

    assert fx.tasks.get(fx.task.id).status == "active"
    assert fx.status(subscription) == "active"
    assert fx.subscriptions.get(subscription.id).end_date == UNLIMITED_END_DATE
    assert "🔔 Уведомления клиентов включены снова: 1" in reply.text
    assert [s.id for s in fx.subscriptions.list_all_deliverable(today=_TODAY)] == [subscription.id]


def test_toggle_off_button_is_also_recorded_and_undone(fx):
    subscription = fx.client(_CLIENT_A)
    fx.controller.handle_trusted_task_toggle(fx.task.id, 0, telegram_user_id=_MANAGER)
    assert fx.status(subscription) == "stopped"
    fx.resume()
    assert fx.status(subscription) == "active"


def test_client_who_turned_notifications_off_before_the_manager_stays_off(fx):
    own_off = fx.client(_CLIENT_A)
    fx.service.turn_off_car(own_off.id, telegram_user_id=_CLIENT_A)
    other = fx.client(_CLIENT_B)
    fx.stop()

    reply = fx.resume()

    assert fx.status(own_off) == "stopped"
    assert fx.status(other) == "active"
    assert "включены снова: 1" in reply.text


def test_client_who_turned_on_and_off_again_after_the_manager_stays_off(fx):
    subscription = fx.client(_CLIENT_A)
    fx.stop()
    assert fx.service.turn_on_car(subscription.id, telegram_user_id=_CLIENT_A, today=_TODAY) is not None
    assert fx.service.turn_off_car(subscription.id, telegram_user_id=_CLIENT_A) is not None
    fx.tasks.set_status(fx.task.id, "stopped")  # клиент включал — задача тоже поднималась

    reply = fx.resume()

    assert fx.status(subscription) == "stopped"
    assert "включены снова" not in reply.text


def test_client_who_turned_back_on_himself_is_not_duplicated(fx):
    subscription = fx.client(_CLIENT_A)
    fx.stop()
    fx.service.turn_on_car(subscription.id, telegram_user_id=_CLIENT_A, today=_TODAY)
    before = fx.count("fine_monitoring_subscriptions")

    fx.resume()

    assert fx.status(subscription) == "active"
    assert fx.count("fine_monitoring_subscriptions") == before


def test_several_clients_on_one_car_are_handled_one_by_one(fx):
    first = fx.client(_CLIENT_A)
    second = fx.client(_CLIENT_B)
    fx.stop()
    fx.service.delete_car(second.id, telegram_user_id=_CLIENT_B)

    reply = fx.resume()

    assert fx.status(first) == "active"
    assert fx.status(second) == "archived"  # удалённый автомобиль не возвращается
    assert "включены снова: 1" in reply.text


def test_repeated_resume_changes_nothing_and_creates_no_duplicates(fx):
    subscription = fx.client(_CLIENT_A)
    fx.stop()
    fx.resume()
    counts = (fx.count("fine_monitoring_subscriptions"), fx.count("fine_monitoring_tasks"))
    fx.service.turn_off_car(subscription.id, telegram_user_id=_CLIENT_A)

    reply = fx.resume()

    assert fx.status(subscription) == "stopped"  # решение клиента после первого ▶️ — его
    assert (fx.count("fine_monitoring_subscriptions"), fx.count("fine_monitoring_tasks")) == counts
    assert "включены снова" not in reply.text


def test_valid_invitation_is_restored_and_expired_one_is_not(fx):
    now = datetime.now(timezone.utc)
    valid = fx.invitation(expires_at=now + timedelta(days=3))
    fx.stop()
    fx.resume()
    assert fx.status(valid) == "pending_claim"

    fx.stop()
    # истекло час назад (в тот же день по UTC) — в формате, который пишет production
    conn = sqlite3.connect(fx.db_path)
    conn.execute("UPDATE fine_monitoring_subscriptions SET claim_token_expires_at = ? WHERE id = ?",
                 ((now - timedelta(minutes=1)).isoformat(), valid.id))
    conn.commit()
    conn.close()
    fx.resume()
    assert fx.status(valid) == "stopped"


def test_stop_without_a_record_of_its_cause_is_not_restored(fx):
    """Отключение до появления журнала (или иным путём) — причину надёжно
    установить нельзя: не восстанавливается."""
    subscription = fx.client(_CLIENT_A)
    fx.tasks.set_status(fx.task.id, "stopped")
    fx.subscriptions.stop_all_for_task(fx.task.id)

    reply = fx.resume()

    assert fx.tasks.get(fx.task.id).status == "active"
    assert fx.status(subscription) == "stopped"
    assert "включены снова" not in reply.text


def test_client_with_another_live_subscription_for_the_car_is_not_restored_twice(fx):
    subscription = fx.client(_CLIENT_A)
    fx.stop()
    other_task = fx.tasks.create(
        car_number=_CAR, label=None, start_date=date(2026, 11, 20), end_date=UNLIMITED_END_DATE,
        telegram_chat_id=_MANAGER, created_by_user_id=_MANAGER, monitoring_scope="client_bot",
    )
    fx.client(_CLIENT_A, task=other_task)  # клиент добавил машину заново

    fx.resume()

    assert fx.status(subscription) == "stopped"


def test_only_the_latest_manager_stop_is_undone(fx):
    first = fx.client(_CLIENT_A)
    fx.stop()
    fx.resume()
    fx.service.turn_off_car(first.id, telegram_user_id=_CLIENT_A)  # клиент выключил сам
    second = fx.client(_CLIENT_B)
    fx.stop()

    fx.resume()

    assert fx.status(first) == "stopped"
    assert fx.status(second) == "active"


def test_no_historic_notifications_after_restore_only_new_fines(fx):
    subscription = fx.client(_CLIENT_A)
    fx.stop()
    old = fx.fine("OLD1")  # известен до включения (например, найден, пока мониторинг был выключен)

    fx.resume()
    asyncio.run(fx.delivery.run_once())

    assert fx.sender.sent == []
    delivery = fx.deliveries.get(old.id, subscription.id, "owner")
    assert (delivery.delivered_at, delivery.exclusion_reason) == (None, EXCLUSION_SUBSCRIPTION_RESTORED)

    fx.fine("NEW1")  # новый штраф после включения — приходит, один раз
    asyncio.run(fx.delivery.run_once())
    asyncio.run(fx.delivery.run_once())
    assert fx.sender.sent == [_CLIENT_A]


def test_delegated_subscription_restores_for_client_and_manager_without_history(fx):
    subscription = fx.client(_CLIENT_A, created_by=_MANAGER)
    old = fx.fine("OLD1")
    fx.stop()
    fx.resume()
    asyncio.run(fx.delivery.run_once())
    assert fx.status(subscription) == "active"
    assert fx.sender.sent == []
    for role in ("owner", "trusted_operator"):
        assert fx.deliveries.get(old.id, subscription.id, role).exclusion_reason == EXCLUSION_SUBSCRIPTION_RESTORED


def test_without_history_exclusion_nothing_is_restored(tmp_path):
    fixture = _Fixture(tmp_path, with_excluder=False)
    subscription = fixture.client(_CLIENT_A)
    fixture.stop()
    fixture.resume()
    assert fixture.tasks.get(fixture.task.id).status == "active"
    assert fixture.status(subscription) == "stopped"
    fixture.close()


def test_stop_records_exactly_the_subscriptions_it_turned_off(fx):
    active = fx.client(_CLIENT_A)
    own_off = fx.client(_CLIENT_B)
    fx.service.turn_off_car(own_off.id, telegram_user_id=_CLIENT_B)
    fx.stop()
    conn = sqlite3.connect(fx.db_path)
    try:
        rows = conn.execute(
            "SELECT subscription_id, previous_status FROM fine_task_manager_stop_subscriptions"
        ).fetchall()
        stops = conn.execute("SELECT monitoring_task_id, stopped_by, resumed_at FROM fine_task_manager_stops").fetchall()
    finally:
        conn.close()
    assert rows == [(active.id, "active")]
    assert stops == [(fx.task.id, _MANAGER, None)]


def test_resume_closes_the_journal_entry_with_each_outcome(fx):
    restored = fx.client(_CLIENT_A)
    deleted = fx.client(_CLIENT_B)
    fx.stop()
    fx.service.delete_car(deleted.id, telegram_user_id=_CLIENT_B)
    fx.resume()
    conn = sqlite3.connect(fx.db_path)
    try:
        stop = conn.execute("SELECT resumed_at, resume_outcome FROM fine_task_manager_stops").fetchone()
        outcomes = dict(conn.execute(
            "SELECT subscription_id, outcome FROM fine_task_manager_stop_subscriptions"
        ).fetchall())
    finally:
        conn.close()
    assert stop[0] is not None and stop[1] == "resumed"
    assert outcomes == {restored.id: "restored", deleted.id: "car_deleted"}


# ---- После независимого review ----


def test_later_stop_keeps_earlier_untouched_subscriptions_restorable(fx):
    """A и B; ⛔; A включил сам (задача поднялась); ⛔ снова; ▶️ — возвращаются
    оба: B никто не трогал с первого ⛔."""
    first = fx.client(_CLIENT_A)
    second = fx.client(_CLIENT_B)
    fx.stop()
    fx.service.turn_on_car(first.id, telegram_user_id=_CLIENT_A, today=_TODAY)
    assert fx.tasks.get(fx.task.id).status == "active"
    fx.stop()

    reply = fx.resume()

    assert fx.status(first) == "active" and fx.status(second) == "active"
    assert "включены снова: 2" in reply.text


def test_failed_history_exclusion_restores_nothing_and_a_retry_completes_it(tmp_path):
    fixture = _Fixture(tmp_path)
    subscription = fixture.client(_CLIENT_A)
    old = fixture.fine("OLD1")
    fixture.fine("OLD2")
    fixture.stop()
    real_excluder = fixture.service._restored_history_excluder
    calls = []

    def failing_excluder(sub):
        calls.append(sub.id)
        fixture.deliveries.exclude(old.id, sub.id, "owner", reason="partial")
        raise sqlite3.OperationalError("database is locked")

    fixture.service._restored_history_excluder = failing_excluder
    with pytest.raises(sqlite3.OperationalError):
        fixture.resume()
    assert fixture.status(subscription) == "stopped"  # ничего не включено — старое не уйдёт
    asyncio.run(fixture.delivery.run_once())
    assert fixture.sender.sent == []

    fixture.service._restored_history_excluder = real_excluder
    fixture.resume()  # повтор: запись журнала осталась открытой
    asyncio.run(fixture.delivery.run_once())
    assert fixture.status(subscription) == "active"
    assert fixture.sender.sent == []
    assert calls == [subscription.id]
    fixture.close()


def test_invitation_is_not_restored_when_the_person_was_invited_again(fx):
    now = datetime.now(timezone.utc)
    invitation = fx.invitation(expires_at=now + timedelta(days=3))
    fx.stop()
    new_task = fx.tasks.create(
        car_number=_CAR, label=None, start_date=date(2026, 11, 20), end_date=UNLIMITED_END_DATE,
        telegram_chat_id=_MANAGER, created_by_user_id=_MANAGER, monitoring_scope="client_bot",
    )
    fx.subscriptions.create_pending_claim(
        monitoring_task_id=new_task.id, car_number=_CAR, owner_username_hint="FUTURE_client",
        created_by_telegram_user_id=_MANAGER, created_by_telegram_chat_id=_MANAGER,
        start_date=date(2026, 11, 20), end_date=UNLIMITED_END_DATE, claim_token="tok-new",
        claim_token_expires_at=now + timedelta(days=7),
    )

    fx.resume()

    assert fx.status(invitation) == "stopped"


def test_restored_invitation_excludes_history_for_the_future_owner_too(fx):
    invitation = fx.invitation(expires_at=datetime.now(timezone.utc) + timedelta(days=3))
    old = fx.fine("OLD1")
    fx.stop()
    fx.resume()
    assert fx.status(invitation) == "pending_claim"
    for role in ("trusted_operator", "owner"):
        assert fx.deliveries.get(old.id, invitation.id, role).exclusion_reason == EXCLUSION_SUBSCRIPTION_RESTORED
