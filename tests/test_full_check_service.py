"""Тесты "fine check-all" (@ProtocolGEbot, задача "add silent Georgia full
database check command") — скрытая fine-admin-only maintenance-команда.
Repository — настоящие (SQLite/tmp_path), FineProvider — scripted-фейк,
sleep — записывающий фейк (НЕ реальные секунды)."""

import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.notification_coordinator import FineNotificationCoordinator
from reader.fines.provider import FineProvider, FineProviderError
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.notifications.base import NotificationResult, NotificationService
from reader.public_bot import texts
from reader.public_bot.conversation import ConversationController
from reader.public_bot.conversation_state_repository import (
    BotConversationStateRepository,
)
from reader.public_bot.full_check_service import (
    FullCheckAlreadyInProgressError,
    FullCheckService,
)
from reader.public_bot.keyboards import main_menu_keyboard
from reader.public_bot.known_users_repository import BotKnownUsersRepository
from reader.public_bot.statistics_service import BotStatisticsService
from reader.public_bot.subscription_repository import FineSubscriptionRepository
from reader.public_bot.subscription_service import SubscriptionService
from reader.users.repository import UserRepository

_TBILISI = ZoneInfo("Asia/Tbilisi")
_FINE_ADMIN_ID = 900100200
_ORDINARY_TRUSTED_ID = 5712994689  # trusted_operator, но НЕ fine-admin
_ORDINARY_ID = 685137235
_CHAT_ID = 900100200


class _SleepLog:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _ScriptedProvider(FineProvider):
    def __init__(self, responses: dict[str, list] | None = None):
        self._responses: dict[str, list] = {k: list(v) for k, v in (responses or {}).items()}
        self.requested_plates: list[str] = []

    def script(self, plate: str, responses: list) -> None:
        self._responses[plate] = list(responses)

    async def search_by_plate(self, plate: str):
        self.requested_plates.append(plate)
        queue = self._responses.get(plate)
        if not queue:
            return []
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, Exception):
            raise response
        return response


class _FakeProgressSender:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []
        self._next_id = 1

    async def send(self, chat_id: int, text: str) -> int:
        message_id = self._next_id
        self._next_id += 1
        self.sent.append((chat_id, text))
        return message_id

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        self.edited.append((chat_id, message_id, text))


class _FakeNotificationService(NotificationService):
    def __init__(self):
        self.notify_calls: list[list] = []

    async def notify(self, events) -> NotificationResult:
        self.notify_calls.append(list(events))
        return NotificationResult(
            delivered_event_ids=[e.detected_fine_id for e in events], failed_event_ids=[],
        )


def _fine(*, car_number: str, fingerprint: str, amount: float) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number, external_fine_id=fingerprint,
        penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
        delivered_status="Не вручено", fingerprint=fingerprint,
        raw_data={"protocolNo": fingerprint}, amount=amount,
    )


class _Fixture:
    def __init__(self, tmp_path, *, with_full_check: bool = True):
        self.db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(self.db_path)
        self.detected_fine_repository = DetectedFineRepository(self.db_path)
        self.subscription_repository = FineSubscriptionRepository(self.db_path)
        self.user_repository = UserRepository(self.db_path)
        self.conversation_state_repository = BotConversationStateRepository(self.db_path)
        self.known_users_repository = BotKnownUsersRepository(self.db_path)
        self.provider = _ScriptedProvider()
        self.sleep_log = _SleepLog()
        self.check_service = FineCheckService(
            self.provider, self.task_repository, self.detected_fine_repository,
            sleep=self.sleep_log,
        )
        self.subscription_service = SubscriptionService(
            self.task_repository, self.subscription_repository, self.user_repository, self.check_service,
        )
        self.statistics_service = BotStatisticsService(
            self.known_users_repository, self.subscription_repository, self.detected_fine_repository,
        )
        self.progress_sender = _FakeProgressSender()
        self.full_check_service = (
            FullCheckService(
                self.task_repository, self.check_service, self.progress_sender,
                sleep=self.sleep_log,
            )
            if with_full_check else None
        )
        self.controller = ConversationController(
            self.conversation_state_repository, self.subscription_service, self.statistics_service,
            self.known_users_repository, tz=_TBILISI,
            trusted_operator_user_ids=frozenset({_ORDINARY_TRUSTED_ID}),
            full_check_service=self.full_check_service,
            fine_admin_user_ids=frozenset({_FINE_ADMIN_ID}),
        )

    def make_task(self, car_number: str, *, status: str = "active"):
        task = self.task_repository.create(
            car_number=car_number, label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_FINE_ADMIN_ID,
        )
        if status != "active":
            self.task_repository.set_status(task.id, status)
            task = self.task_repository.get(task.id)
        return task

    def add_subscription(self, task, *, telegram_user_id: int) -> None:
        self.subscription_repository.create(
            monitoring_task_id=task.id, car_number=task.car_number,
            telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id,
            telegram_username=None, start_date=task.start_date, end_date=task.end_date,
        )

    async def send(self, text: str, *, telegram_user_id: int = _FINE_ADMIN_ID):
        return await self.controller.handle_text(
            text, chat_id=_CHAT_ID, telegram_user_id=telegram_user_id, username=None,
        )


# ---- 1. hidden from menus/keyboards/help ----

def test_command_not_present_in_any_menu_keyboard():
    for is_trusted in (False, True):
        rows = main_menu_keyboard(is_trusted=is_trusted)
        labels = [row_button.button.text for row in rows for row_button in row]
        assert texts.FULL_CHECK_COMMAND not in labels
        assert texts.FULL_CHECK_CONFIRM_COMMAND not in labels
        assert not any("check-all" in label for label in labels)


# ---- 2. unauthorized cannot run it ----

async def test_unauthorized_sender_gets_safe_fallback_and_zero_requests(tmp_path):
    fx = _Fixture(tmp_path)
    fx.make_task("AA001AA")

    # обычный пользователь
    reply = await fx.send(texts.FULL_CHECK_COMMAND, telegram_user_id=_ORDINARY_ID)
    assert reply.text == texts.MAIN_MENU_TEXT
    assert reply.show_main_menu is True
    assert fx.provider.requested_plates == []

    # trusted, но НЕ fine-admin (два разных списка, см. задачу п.6)
    reply2 = await fx.send(texts.FULL_CHECK_COMMAND, telegram_user_id=_ORDINARY_TRUSTED_ID)
    assert reply2.text == texts.MAIN_MENU_TEXT
    assert fx.provider.requested_plates == []

    reply3 = await fx.send(texts.FULL_CHECK_CONFIRM_COMMAND, telegram_user_id=_ORDINARY_ID)
    assert reply3.text == texts.MAIN_MENU_TEXT
    assert fx.provider.requested_plates == []


# ---- 3/4. first call: zero requests, returns candidate count ----

async def test_preview_makes_zero_requests_and_shows_count(tmp_path):
    fx = _Fixture(tmp_path)
    fx.make_task("AA001AA")
    fx.make_task("BB002BB")

    reply = await fx.send(texts.FULL_CHECK_COMMAND)

    assert fx.provider.requested_plates == []
    assert "2" in reply.text
    assert texts.FULL_CHECK_CONFIRM_COMMAND in reply.text


# ---- 5. confirm starts checks ----

async def test_confirm_runs_checks_and_returns_summary(tmp_path):
    fx = _Fixture(tmp_path)
    fx.make_task("AA001AA")
    fx.provider.script("AA001AA", [[]])

    reply = await fx.send(texts.FULL_CHECK_CONFIRM_COMMAND)

    assert fx.provider.requested_plates == ["AA001AA"]
    assert "Полная проверка завершена" in reply.text
    assert "Всего: 1" in reply.text


# ---- 6/7/8/9/10: selection covers every status/state, nothing excluded ----

async def test_all_statuses_and_states_are_selected_and_checked(tmp_path):
    fx = _Fixture(tmp_path)
    active_task = fx.make_task("AA001AA", status="active")
    stopped_task = fx.make_task("BB002BB", status="stopped")
    completed_task = fx.make_task("CC003CC", status="completed")

    # previous debt > 0
    fx.provider.script("AA001AA", [[_fine(car_number="AA001AA", fingerprint="fp-a", amount=100)]])
    # previous = 0 (genuinely no fines)
    fx.provider.script("BB002BB", [[]])
    # previous state NULL/unknown is the default for a freshly created task —
    # completed_task above has never been checked, last_successful_total_amount is None.
    assert completed_task.last_successful_total_amount is None
    fx.provider.script("CC003CC", [[]])

    reply = await fx.send(texts.FULL_CHECK_CONFIRM_COMMAND)

    assert set(fx.provider.requested_plates) == {"AA001AA", "BB002BB", "CC003CC"}
    assert "Всего: 3" in reply.text
    assert "Не удалось проверить: 0" in reply.text

    updated_active = fx.task_repository.get(active_task.id)
    updated_stopped = fx.task_repository.get(stopped_task.id)
    updated_completed = fx.task_repository.get(completed_task.id)
    assert updated_active.last_successful_total_amount == 100
    assert updated_stopped.last_successful_total_amount == 0
    assert updated_completed.last_successful_total_amount == 0
    assert updated_active.last_check_status == "ok"
    assert updated_stopped.last_check_status == "ok"
    assert updated_completed.last_check_status == "ok"


# ---- 11. duplicate subscriptions sharing task -> checked once ----

async def test_task_with_multiple_subscriptions_checked_exactly_once(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task("AA001AA")
    fx.add_subscription(task, telegram_user_id=111)
    fx.add_subscription(task, telegram_user_id=222)
    fx.provider.script("AA001AA", [[]])

    reply = await fx.send(texts.FULL_CHECK_CONFIRM_COMMAND)

    assert fx.provider.requested_plates == ["AA001AA"]
    assert "Всего: 1" in reply.text


# ---- 12/13/14. sequential, 2s delay between tasks, no trailing delay ----

async def test_checks_sequentially_with_2_second_delay_no_trailing(tmp_path):
    fx = _Fixture(tmp_path)
    fx.make_task("AA001AA")
    fx.make_task("BB002BB")
    fx.make_task("CC003CC")
    for car in ("AA001AA", "BB002BB", "CC003CC"):
        fx.provider.script(car, [[]])

    await fx.send(texts.FULL_CHECK_CONFIRM_COMMAND)

    assert fx.provider.requested_plates == ["AA001AA", "BB002BB", "CC003CC"]
    # Ровно 2 паузы по 2.0 сек (между A-B и B-C), НЕ после C.
    assert fx.sleep_log.calls == [2.0, 2.0]


# ---- 15. existing 5-second false-zero confirmation preserved ----

async def test_false_zero_confirmation_still_uses_5_seconds_during_full_check(tmp_path):
    fx = _Fixture(tmp_path)
    task_a = fx.make_task("AA001AA")
    task_b = fx.make_task("BB002BB")

    # Seed AA001AA with a real previous positive amount (genuine prior check).
    fx.provider.script("AA001AA", [[_fine(car_number="AA001AA", fingerprint="fp-seed", amount=200)]])
    seeded = await fx.check_service.check_task(task_a)
    assert seeded.status == "ok"
    fx.provider.requested_plates.clear()
    fx.sleep_log.calls.clear()

    # Full-check batch: AA001AA gets a false empty, confirms back to 200;
    # BB002BB is a plain new zero (no previous positive amount, no confirmation).
    fx.provider.script(
        "AA001AA",
        [[], [_fine(car_number="AA001AA", fingerprint="fp-seed", amount=200)]],
    )
    fx.provider.script("BB002BB", [[]])

    reply = await fx.send(texts.FULL_CHECK_CONFIRM_COMMAND)

    assert "Всего: 2" in reply.text
    # 5.0 (false-zero confirmation for AA001AA) + 2.0 (inter-car delay A->B).
    assert fx.sleep_log.calls == [5.0, 2.0]
    updated_a = fx.task_repository.get(task_a.id)
    assert updated_a.last_successful_total_amount == 200
    assert task_b is not None  # sanity: both tasks existed


# ---- 16/17. one task ERROR does not stop scan, preserves previous state ----

async def test_one_task_error_does_not_stop_scan_and_preserves_previous_amount(tmp_path):
    fx = _Fixture(tmp_path)
    task_a = fx.make_task("AA001AA")
    fx.make_task("BB002BB")
    fx.make_task("CC003CC")

    # Seed a real previous amount for AA001AA.
    fx.provider.script("AA001AA", [[_fine(car_number="AA001AA", fingerprint="fp-a", amount=50)]])
    await fx.check_service.check_task(task_a)
    fx.provider.requested_plates.clear()
    fx.sleep_log.calls.clear()

    fx.provider.script("AA001AA", [FineProviderError("network down")])
    fx.provider.script("BB002BB", [[]])
    fx.provider.script("CC003CC", [[]])

    reply = await fx.send(texts.FULL_CHECK_CONFIRM_COMMAND)

    assert set(fx.provider.requested_plates) == {"AA001AA", "BB002BB", "CC003CC"}
    assert "Не удалось проверить: 1" in reply.text
    assert "Всего: 3" in reply.text
    updated_a = fx.task_repository.get(task_a.id)
    assert updated_a.last_successful_total_amount == 50  # preserved, not 0/None
    assert updated_a.last_check_status == "error"


# ---- 18. concurrency guard ----

async def test_second_confirm_while_running_is_rejected(tmp_path):
    fx = _Fixture(tmp_path)
    fx.make_task("AA001AA")
    fx.provider.script("AA001AA", [[]])
    fx.full_check_service._in_progress = True  # эмулируем "уже выполняется"

    reply = await fx.send(texts.FULL_CHECK_CONFIRM_COMMAND)

    assert reply.text == texts.FULL_CHECK_ALREADY_IN_PROGRESS_TEXT
    assert fx.provider.requested_plates == []


async def test_full_check_service_raises_when_already_in_progress_directly(tmp_path):
    """Модульный тест самого сервиса (без ConversationController) — тот же
    приём, что и RefreshAlreadyInProgressError у DebtRefreshService."""
    fx = _Fixture(tmp_path)
    fx.make_task("AA001AA")
    fx.provider.script("AA001AA", [[]])
    fx.full_check_service._in_progress = True

    with pytest.raises(FullCheckAlreadyInProgressError):
        await fx.full_check_service.run(chat_id=_CHAT_ID)


# ---- 19/20. technical scan sends ZERO client notifications, not pending ----

async def test_scan_discovered_fine_is_not_pending_for_later_flush(tmp_path):
    fx = _Fixture(tmp_path)
    fx.make_task("AA001AA")
    fx.provider.script("AA001AA", [[_fine(car_number="AA001AA", fingerprint="fp-new", amount=75)]])

    await fx.send(texts.FULL_CHECK_CONFIRM_COMMAND)

    pending = fx.detected_fine_repository.list_pending_notifications()
    assert pending == []
    fine = fx.detected_fine_repository.get_by_fingerprint(
        fx.task_repository.list_all_task_ids()[0], "fp-new",
    )
    assert fine is not None
    assert fine.notification_sent_at is not None


# ---- 21. running NotificationFlushJob-equivalent after scan sends nothing ----

async def test_flush_pending_after_scan_sends_nothing_for_scan_created_fine(tmp_path):
    fx = _Fixture(tmp_path)
    fx.make_task("AA001AA")
    fx.provider.script("AA001AA", [[_fine(car_number="AA001AA", fingerprint="fp-new", amount=75)]])

    await fx.send(texts.FULL_CHECK_CONFIRM_COMMAND)

    notification_service = _FakeNotificationService()
    coordinator = FineNotificationCoordinator(
        fx.detected_fine_repository, fx.task_repository, notification_service,
    )
    result = await coordinator.flush_pending()

    assert notification_service.notify_calls == []
    assert result.delivered_event_ids == []


# ---- 22. later genuinely new fine found by ordinary monitoring DOES notify ----

async def test_later_genuine_fine_from_ordinary_monitoring_still_notifies(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task("AA001AA")
    fx.provider.script("AA001AA", [[_fine(car_number="AA001AA", fingerprint="fp-scan", amount=75)]])
    await fx.send(texts.FULL_CHECK_CONFIRM_COMMAND)

    # Regular monitoring (default notification_policy="normal") later finds
    # a genuinely NEW fine (different fingerprint) for the same task.
    fx.provider.script("AA001AA", [[
        _fine(car_number="AA001AA", fingerprint="fp-scan", amount=75),
        _fine(car_number="AA001AA", fingerprint="fp-genuine-new", amount=30),
    ]])
    result = await fx.check_service.check_task(task)
    assert result.status == "ok"

    pending = fx.detected_fine_repository.list_pending_notifications()
    assert len(pending) == 1
    assert pending[0].fingerprint == "fp-genuine-new"

    notification_service = _FakeNotificationService()
    coordinator = FineNotificationCoordinator(
        fx.detected_fine_repository, fx.task_repository, notification_service,
    )
    flush_result = await coordinator.flush_pending()
    assert len(notification_service.notify_calls) == 1
    assert len(notification_service.notify_calls[0]) == 1
    assert notification_service.notify_calls[0][0].external_fine_id == "fp-genuine-new"
    assert flush_result.delivered_event_ids


# ---- 23. latest-state correctly updates from scan ----

async def test_latest_state_updates_from_scan(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task("AA001AA")
    fx.provider.script("AA001AA", [[_fine(car_number="AA001AA", fingerprint="fp-a", amount=120)]])

    await fx.send(texts.FULL_CHECK_CONFIRM_COMMAND)

    updated = fx.task_repository.get(task.id)
    assert updated.last_successful_total_amount == 120
    assert updated.last_successful_checked_at is not None


# ---- 24. detected_fines dedup remains correct ----

async def test_detected_fines_dedup_unaffected_by_silent_scan(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task("AA001AA")
    fx.provider.script("AA001AA", [[_fine(car_number="AA001AA", fingerprint="fp-a", amount=50)]])
    await fx.check_service.check_task(task)  # normal (pre-existing) detection

    fx.provider.script("AA001AA", [[_fine(car_number="AA001AA", fingerprint="fp-a", amount=50)]])
    await fx.send(texts.FULL_CHECK_CONFIRM_COMMAND)

    all_rows = fx.detected_fine_repository.list_by_car_number("AA001AA")
    assert len(all_rows) == 1  # никакого дубликата — тот же (task_id, fingerprint)
    assert all_rows[0].notification_sent_at is None  # уже известный, mark_seen не трогает поле


# ---- progress reporting sanity ----

async def test_progress_messages_are_edited_not_spammed(tmp_path):
    fx = _Fixture(tmp_path)
    for i in range(12):
        fx.make_task(f"P{i:03d}AA")
        fx.provider.script(f"P{i:03d}AA", [[]])

    await fx.send(texts.FULL_CHECK_CONFIRM_COMMAND)

    assert len(fx.progress_sender.sent) == 1  # одно исходное сообщение
    # Редактируется на 10-й и на последней (12-й) — не на каждой машине.
    assert len(fx.progress_sender.edited) == 2


# ---- provider not configured (full_check_service=None) ----

async def test_missing_service_falls_back_gracefully(tmp_path):
    fx = _Fixture(tmp_path, with_full_check=False)
    reply = await fx.send(texts.FULL_CHECK_COMMAND)
    assert reply.text == texts.MAIN_MENU_TEXT
