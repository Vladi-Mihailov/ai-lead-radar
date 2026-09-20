"""
Тесты reader/turkey_bot/migration_notify.py::notify_all — одноразовая
рассылка существующим пользователям после production deployment (см.
задачу "Перенос Unified Turkey функционала в production" п.8). Никаких
реальных Telegram-отправок — только фейковый MessageSender."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.turkey_bot.known_users_repository import (
    TurkeyBotKnownUsersRepository,
)
from reader.turkey_bot.migration_notification_repository import (
    TurkeyUnifiedMigrationNotificationRepository,
)
from reader.turkey_bot.migration_notify import notify_all
from reader.turkey_bot.texts import MIGRATION_NOTIFICATION_TEXT

pytestmark = pytest.mark.asyncio


class _FakeSender:
    def __init__(self, *, fail_for: set[int] | None = None):
        self.sent: list[tuple[int, str, object]] = []
        self._fail_for = fail_for or set()

    async def send_message(self, *, chat_id: int, text: str, buttons) -> None:
        if chat_id in self._fail_for:
            raise RuntimeError(f"simulated Telegram error for chat_id={chat_id}")
        self.sent.append((chat_id, text, buttons))


def _make_repos():
    known_users = TurkeyBotKnownUsersRepository(":memory:")
    notifications = TurkeyUnifiedMigrationNotificationRepository(":memory:")
    return known_users, notifications


async def test_existing_known_user_is_eligible_and_gets_notified():
    known_users, notifications = _make_repos()
    known_users.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username="alice")
    sender = _FakeSender()

    result = await notify_all(
        sender, known_users, notifications, trusted_operator_user_ids=frozenset(), dry_run=False,
    )

    assert result.eligible == 1
    assert result.sent == 1
    assert len(sender.sent) == 1


async def test_correct_chat_id_used_not_user_id_by_accident():
    """Явное требование задачи п.8/п.13 — telegram_chat_id, а не
    telegram_user_id, реально используется для отправки (тест
    намеренно использует РАЗНЫЕ значения, чтобы гарантированно поймать
    случайную путаницу)."""
    known_users, notifications = _make_repos()
    known_users.record_seen(telegram_user_id=555, telegram_chat_id=777, telegram_username=None)
    sender = _FakeSender()

    await notify_all(sender, known_users, notifications, trusted_operator_user_ids=frozenset(), dry_run=False)

    assert sender.sent[0][0] == 777


async def test_exact_notification_text_matches_spec():
    known_users, notifications = _make_repos()
    known_users.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)
    sender = _FakeSender()

    await notify_all(sender, known_users, notifications, trusted_operator_user_ids=frozenset(), dry_run=False)

    assert sender.sent[0][1] == MIGRATION_NOTIFICATION_TEXT
    assert sender.sent[0][1] == "🚗 Бот обновлён\nВаши ранее проверенные автомобили добавлены в «Мои авто»"


async def test_new_reply_keyboard_is_attached():
    from reader.turkey_bot.texts import ADD_CAR_LABEL, MY_CARS_LABEL

    known_users, notifications = _make_repos()
    known_users.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)
    sender = _FakeSender()

    await notify_all(sender, known_users, notifications, trusted_operator_user_ids=frozenset(), dry_run=False)

    buttons = sender.sent[0][2]
    first_row_labels = [btn.button.text for btn in buttons[0]]
    assert first_row_labels == [ADD_CAR_LABEL, MY_CARS_LABEL]


async def test_manager_gets_manager_keyboard_regular_user_gets_regular_keyboard():
    from reader.turkey_bot.texts import STATISTICS_LABEL

    known_users, notifications = _make_repos()
    known_users.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)  # обычный
    known_users.record_seen(telegram_user_id=2, telegram_chat_id=2, telegram_username=None)  # manager
    sender = _FakeSender()

    await notify_all(
        sender, known_users, notifications, trusted_operator_user_ids=frozenset({2}), dry_run=False,
    )

    by_chat = {chat_id: buttons for chat_id, _text, buttons in sender.sent}
    regular_labels = [label for row in by_chat[1] for label in [btn.button.text for btn in row]]
    manager_labels = [label for row in by_chat[2] for label in [btn.button.text for btn in row]]
    assert STATISTICS_LABEL not in regular_labels
    assert STATISTICS_LABEL in manager_labels


async def test_successful_send_is_recorded_as_notified():
    known_users, notifications = _make_repos()
    known_users.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)
    sender = _FakeSender()

    await notify_all(sender, known_users, notifications, trusted_operator_user_ids=frozenset(), dry_run=False)

    assert notifications.is_notified(1) is True


async def test_failed_send_is_not_recorded_as_notified():
    """Явное требование задачи п.8: "не отмечать notification как sent до
    успешного Telegram send" — сбой send_message НЕ должен приводить к
    mark_notified()."""
    known_users, notifications = _make_repos()
    known_users.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)
    sender = _FakeSender(fail_for={1})

    result = await notify_all(
        sender, known_users, notifications, trusted_operator_user_ids=frozenset(), dry_run=False,
    )

    assert result.failed == 1
    assert result.sent == 0
    assert notifications.is_notified(1) is False


async def test_one_failed_user_does_not_stop_the_rest_of_the_batch():
    known_users, notifications = _make_repos()
    known_users.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)
    known_users.record_seen(telegram_user_id=2, telegram_chat_id=2, telegram_username=None)
    known_users.record_seen(telegram_user_id=3, telegram_chat_id=3, telegram_username=None)
    sender = _FakeSender(fail_for={2})

    result = await notify_all(
        sender, known_users, notifications, trusted_operator_user_ids=frozenset(), dry_run=False,
    )

    assert result.sent == 2
    assert result.failed == 1
    assert notifications.is_notified(1) is True
    assert notifications.is_notified(2) is False
    assert notifications.is_notified(3) is True


async def test_second_run_does_not_resend_to_already_notified_users():
    known_users, notifications = _make_repos()
    known_users.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)
    sender = _FakeSender()
    await notify_all(sender, known_users, notifications, trusted_operator_user_ids=frozenset(), dry_run=False)

    second_sender = _FakeSender()
    result = await notify_all(
        second_sender, known_users, notifications, trusted_operator_user_ids=frozenset(), dry_run=False,
    )

    assert result.eligible == 0
    assert result.already_notified_skipped == 1
    assert second_sender.sent == []


async def test_retry_after_failure_succeeds_and_gets_recorded():
    """Дополнение к "failed send NOT recorded" — следующий запуск
    ДОЛЖЕН попытаться снова именно этому пользователю (не пропускать его
    навсегда из-за прошлого сбоя)."""
    known_users, notifications = _make_repos()
    known_users.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)
    failing_sender = _FakeSender(fail_for={1})
    await notify_all(failing_sender, known_users, notifications, trusted_operator_user_ids=frozenset(), dry_run=False)
    assert notifications.is_notified(1) is False

    working_sender = _FakeSender()
    result = await notify_all(
        working_sender, known_users, notifications, trusted_operator_user_ids=frozenset(), dry_run=False,
    )

    assert result.sent == 1
    assert notifications.is_notified(1) is True


async def test_dry_run_sends_nothing_and_marks_nothing():
    known_users, notifications = _make_repos()
    known_users.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)
    sender = _FakeSender()

    result = await notify_all(
        sender, known_users, notifications, trusted_operator_user_ids=frozenset(), dry_run=True,
    )

    assert result.eligible == 1
    assert result.sent == 0
    assert sender.sent == []
    assert notifications.is_notified(1) is False


async def test_dry_run_never_calls_send_message_even_via_a_sender_that_would_raise():
    """Структурная гарантия, не только по соглашению — sender, который
    ВСЕГДА бросает исключение при вызове, используется намеренно: если
    dry_run когда-нибудь начнёт вызывать send_message, тест немедленно
    упадёт."""
    class _AlwaysRaisesSender:
        async def send_message(self, *, chat_id, text, buttons):
            raise AssertionError("send_message не должен вызываться в dry-run")

    known_users, notifications = _make_repos()
    known_users.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)

    result = await notify_all(
        _AlwaysRaisesSender(), known_users, notifications,
        trusted_operator_user_ids=frozenset(), dry_run=True,
    )

    assert result.eligible == 1
    assert result.sent == 0
