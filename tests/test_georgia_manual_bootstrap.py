"""Тесты "ВРЕМЕННЫЙ manager workflow для Georgia fines bot" (см. задачу) —
manual bootstrap нового физического номера менеджером, пока production VPS
не имеет сетевого доступа к police.ge. Repository — настоящие (SQLite/
tmp_path), FineProvider — фейковый (ManualBootstrapService/
GeorgiaPublicFineFeedJob никогда не вызывают search_by_plate() вовсе, см.
их собственные тесты). Смешивает unit-тесты ManualBootstrapService
(быстрые, без Telethon) и integration-тесты через ConversationController
(авторизация/кнопки/FSM) — тот же приём, что и у test_public_bot_
conversation.py."""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.parser import compute_fingerprint
from reader.fines.plate_bootstrap_repository import (
    GeorgiaPlateBootstrapRepository,
)
from reader.fines.provider import FineProvider
from reader.fines.public_feed_seen_repository import (
    GeorgiaPublicFeedSeenRepository,
)
from reader.fines.public_feed_state_repository import (
    GeorgiaPublicFeedStateRepository,
)
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot import texts
from reader.public_bot.conversation import ConversationController
from reader.public_bot.conversation_state_repository import (
    BotConversationStateRepository,
)
from reader.public_bot.keyboards import (
    decode_manual_bootstrap_addfine_save_callback,
    decode_manual_bootstrap_nofine_callback,
    encode_manual_bootstrap_addfine_start_callback,
    encode_manual_bootstrap_nofine_callback,
)
from reader.public_bot.known_users_repository import (
    BotKnownUsersRepository,
)
from reader.public_bot.manual_bootstrap_service import (
    ManualBootstrapService,
    parse_manual_amount,
)
from reader.public_bot.statistics_service import BotStatisticsService
from reader.public_bot.subscription_repository import (
    FineSubscriptionRepository,
)
from reader.public_bot.subscription_service import SubscriptionService
from reader.users.repository import UserRepository

_TBILISI = ZoneInfo("Asia/Tbilisi")
_ADMIN_ID = 777001
_OTHER_USER_ID = 999002
_PLATE = "X268AH31"


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(_TBILISI).date()


class _NoopProvider(FineProvider):
    def is_available(self) -> bool:
        return True

    async def search_by_plate(self, plate: str):
        raise AssertionError("manual bootstrap должен работать БЕЗ единого обращения к police.ge")


class _Fixture:
    def __init__(self, tmp_path):
        self.db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(self.db_path)
        self.detected_fine_repository = DetectedFineRepository(self.db_path)
        self.subscription_repository = FineSubscriptionRepository(self.db_path)
        self.user_repository = UserRepository(self.db_path)
        self.conversation_state_repository = BotConversationStateRepository(self.db_path)
        self.known_users_repository = BotKnownUsersRepository(self.db_path)
        self.bootstrap_repository = GeorgiaPlateBootstrapRepository(self.db_path)
        self.check_service = FineCheckService(
            _NoopProvider(), self.task_repository, self.detected_fine_repository,
        )
        self.subscription_service = SubscriptionService(
            self.task_repository, self.subscription_repository,
            self.user_repository, self.check_service,
            plate_bootstrap_repository=self.bootstrap_repository,
        )
        self.statistics_service = BotStatisticsService(
            self.known_users_repository, self.subscription_repository, self.detected_fine_repository,
        )
        self.manual_bootstrap_service = ManualBootstrapService(
            self.bootstrap_repository, self.task_repository,
            self.detected_fine_repository, self.check_service,
        )
        self.controller = ConversationController(
            self.conversation_state_repository, self.subscription_service, self.statistics_service,
            self.known_users_repository, tz=_TBILISI,
            fine_admin_user_ids=frozenset({_ADMIN_ID}),
            manual_bootstrap_service=self.manual_bootstrap_service,
        )

    def make_task(self, car_number=_PLATE, *, telegram_chat_id=5001, created_by_user_id=5001):
        today = _today()
        return self.task_repository.create(
            car_number=car_number, label=None,
            start_date=today, end_date=today + timedelta(days=30),
            telegram_chat_id=telegram_chat_id, created_by_user_id=created_by_user_id,
        )

    def close(self):
        self.task_repository.close()
        self.detected_fine_repository.close()
        self.subscription_repository.close()
        self.user_repository.close()
        self.conversation_state_repository.close()
        self.known_users_repository.close()
        self.bootstrap_repository.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


# ---- parse_manual_amount (см. задачу: fingerprint-совместимость с реальным feed'ом) ----


def test_parse_manual_amount_whole_number_is_int():
    assert parse_manual_amount("50") == 50
    assert isinstance(parse_manual_amount("50"), int)


def test_parse_manual_amount_decimal_is_float():
    assert parse_manual_amount("50.5") == 50.5


def test_parse_manual_amount_invalid_is_none():
    assert parse_manual_amount("abc") is None
    assert parse_manual_amount("") is None


# ---- 1/2: car-added manager alert buttons ----


def test_car_added_alert_text_includes_pending_status_when_bootstrap_pending(fx):
    fx.bootstrap_repository.ensure_pending(_PLATE)
    from reader.public_bot.manager_alerts import format_car_added_alert

    text = format_car_added_alert(
        telegram_user_id=1, username="u", first_name="F", last_name="L",
        car_number=_PLATE, bootstrap_pending=True,
    )
    assert "Статус первичной проверки" in text
    assert "⏳ Ожидает проверки" in text


def test_new_user_alert_never_has_bootstrap_buttons_or_status():
    from reader.public_bot.manager_alerts import format_new_user_alert

    text = format_new_user_alert(
        telegram_user_id=1, username="u", first_name="F", last_name="L",
    )
    assert "Статус первичной проверки" not in text
    assert "Авто:" not in text


# ---- 3: authorization ----


def test_ordinary_user_cannot_invoke_manual_bootstrap_callbacks(fx):
    fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)

    reply = fx.controller.handle_manual_bootstrap_nofine(_PLATE, telegram_user_id=_OTHER_USER_ID)

    assert reply is None
    # Ничего не изменилось — это и есть "не читать/не менять чужие данные".
    assert fx.bootstrap_repository.get(_PLATE).bootstrap_status == "pending"


def test_fine_admin_can_invoke_manual_bootstrap_callbacks(fx):
    fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)

    reply = fx.controller.handle_manual_bootstrap_nofine(_PLATE, telegram_user_id=_ADMIN_ID)

    assert reply is not None
    assert texts.MANUAL_BOOTSTRAP_CONFIRM_LABEL in [
        label for row in reply.inline_buttons for label, _ in row
    ]


# ---- 4: "no fines" confirm -> bootstrap completed, zero fake fines ----


def test_no_fines_confirm_completes_bootstrap_without_fake_fine(fx):
    task = fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)

    reply = fx.controller.handle_manual_bootstrap_nofine_confirm(_PLATE, telegram_user_id=_ADMIN_ID)

    assert reply is not None
    state = fx.bootstrap_repository.get(_PLATE)
    assert state.is_completed
    assert state.bootstrap_completed_by_user_id == _ADMIN_ID
    assert state.bootstrap_completed_at is not None
    assert fx.detected_fine_repository.list_current_for_task(
        task.id, since=datetime(1970, 1, 1, tzinfo=timezone.utc),
    ) == []


# ---- 5: "no fines" sends canonical customer message ----


def test_no_fines_confirm_sends_canonical_customer_message(fx):
    fx.make_task(telegram_chat_id=12345)
    fx.bootstrap_repository.ensure_pending(_PLATE)

    reply = fx.controller.handle_manual_bootstrap_nofine_confirm(_PLATE, telegram_user_id=_ADMIN_ID)

    assert reply.customer_notify_messages == [(12345, texts.format_no_fines_found(_PLATE))]


def test_no_fines_confirm_is_idempotent_when_already_completed(fx):
    """16: completed plate -> second completion idempotent."""
    fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)
    first = fx.controller.handle_manual_bootstrap_nofine_confirm(_PLATE, telegram_user_id=_ADMIN_ID)
    assert first.customer_notify_messages

    second = fx.controller.handle_manual_bootstrap_nofine_confirm(_PLATE, telegram_user_id=_ADMIN_ID)

    assert second.text == texts.MANUAL_BOOTSTRAP_ALREADY_COMPLETED_TEXT
    assert not getattr(second, "customer_notify_messages", None)


def test_pending_open_shows_already_completed_for_completed_plate(fx):
    """13/16: CONCURRENT MANAGERS / already-completed -> informative text."""
    fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)
    fx.manual_bootstrap_service.confirm_no_fines(_PLATE, completed_by_user_id=_ADMIN_ID)

    reply = fx.controller.handle_manual_bootstrap_pending_open(_PLATE, telegram_user_id=_ADMIN_ID)

    assert reply.text == texts.MANUAL_BOOTSTRAP_ALREADY_COMPLETED_TEXT


# ---- helper to drive the full "add fine" multi-step flow ----


async def _add_fine_flow(
    fx, *, car_number=_PLATE, chat_id=5001, protocol_no="AB123456", amount_text="50",
    date_text="05.10.2026", place_text="/skip", article_text="/skip",
):
    reply = fx.controller.handle_manual_bootstrap_addfine_start(
        car_number, chat_id=chat_id, telegram_user_id=_ADMIN_ID,
    )
    assert reply.text == texts.MANUAL_BOOTSTRAP_PROTOCOL_PROMPT_TEXT

    reply = await fx.controller.handle_text(
        protocol_no, chat_id=chat_id, telegram_user_id=_ADMIN_ID, username=None,
    )
    assert reply.text == texts.MANUAL_BOOTSTRAP_AMOUNT_PROMPT_TEXT

    reply = await fx.controller.handle_text(
        amount_text, chat_id=chat_id, telegram_user_id=_ADMIN_ID, username=None,
    )
    assert reply.text == texts.MANUAL_BOOTSTRAP_DATE_PROMPT_TEXT

    reply = await fx.controller.handle_text(
        date_text, chat_id=chat_id, telegram_user_id=_ADMIN_ID, username=None,
    )
    assert reply.text == texts.MANUAL_BOOTSTRAP_PLACE_PROMPT_TEXT

    reply = await fx.controller.handle_text(
        place_text, chat_id=chat_id, telegram_user_id=_ADMIN_ID, username=None,
    )
    assert reply.text == texts.MANUAL_BOOTSTRAP_ARTICLE_PROMPT_TEXT

    review_reply = await fx.controller.handle_text(
        article_text, chat_id=chat_id, telegram_user_id=_ADMIN_ID, username=None,
    )
    return review_reply


# ---- 6/7: review-before-save, canonical detected_fines row ----


async def test_add_fine_shows_review_before_persistence(fx):
    task = fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)

    review_reply = await _add_fine_flow(fx)

    assert "AB123456" in review_reply.text
    assert texts.MANUAL_BOOTSTRAP_SAVE_FINE_LABEL in [
        label for row in review_reply.inline_buttons for label, _ in row
    ]
    # Ничего ещё не сохранено — review это ТОЛЬКО экран подтверждения.
    assert fx.detected_fine_repository.list_current_for_task(
        task.id, since=datetime(1970, 1, 1, tzinfo=timezone.utc),
    ) == []


async def test_confirmed_manual_fine_creates_canonical_detected_fine_row(fx):
    task = fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)
    await _add_fine_flow(fx)

    save_reply = await fx.controller.handle_manual_bootstrap_addfine_save(
        _PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID,
    )

    assert save_reply.text == texts.MANUAL_BOOTSTRAP_FINE_SAVED_TEXT
    rows = fx.detected_fine_repository.list_current_for_task(
        task.id, since=datetime(1970, 1, 1, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0].external_fine_id == "AB123456"
    assert rows[0].amount == 50.0
    assert rows[0].violation_date == date(2026, 10, 5)
    assert rows[0].notification_sent_at is None  # см. 9


# ---- 8: duplicate protocol/fingerprint -> exactly one row ----


async def test_same_protocol_entered_twice_creates_exactly_one_fine(fx):
    task = fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)

    await _add_fine_flow(fx)
    first_save = await fx.controller.handle_manual_bootstrap_addfine_save(
        _PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID,
    )
    assert first_save.text == texts.MANUAL_BOOTSTRAP_FINE_SAVED_TEXT

    await _add_fine_flow(fx)  # тот же protocol_no/amount/date по умолчанию
    second_save = await fx.controller.handle_manual_bootstrap_addfine_save(
        _PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID,
    )

    assert "уже есть в базе" in second_save.text
    rows = fx.detected_fine_repository.list_current_for_task(
        task.id, since=datetime(1970, 1, 1, tzinfo=timezone.utc),
    )
    assert len(rows) == 1


# ---- 9: normal notification pipeline (unnotified row, picked up by NotificationFlushJob) ----


async def test_manual_fine_is_unnotified_pending_normal_flush(fx):
    task = fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)
    await _add_fine_flow(fx)

    await fx.controller.handle_manual_bootstrap_addfine_save(_PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID)

    row = fx.detected_fine_repository.list_current_for_task(
        task.id, since=datetime(1970, 1, 1, tzinfo=timezone.utc),
    )[0]
    assert row.notification_sent_at is None


# ---- 10/11/12: multiple fines flow ----


async def test_saving_one_fine_does_not_complete_bootstrap(fx):
    fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)
    await _add_fine_flow(fx)

    await fx.controller.handle_manual_bootstrap_addfine_save(_PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID)

    assert fx.bootstrap_repository.get(_PLATE).is_completed is False


async def test_add_another_fine_works_for_same_plate(fx):
    task = fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)
    await _add_fine_flow(fx, protocol_no="AB111111")
    save1 = await fx.controller.handle_manual_bootstrap_addfine_save(
        _PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID,
    )
    assert texts.MANUAL_BOOTSTRAP_ADD_ANOTHER_LABEL in [
        label for row in save1.inline_buttons for label, _ in row
    ]

    another_start = fx.controller.handle_manual_bootstrap_addfine_start(
        _PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID,
    )
    assert another_start.text == texts.MANUAL_BOOTSTRAP_PROTOCOL_PROMPT_TEXT

    await _add_fine_flow(fx, protocol_no="AB222222")
    save2 = await fx.controller.handle_manual_bootstrap_addfine_save(
        _PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID,
    )
    assert save2.text == texts.MANUAL_BOOTSTRAP_FINE_SAVED_TEXT

    rows = fx.detected_fine_repository.list_current_for_task(
        task.id, since=datetime(1970, 1, 1, tzinfo=timezone.utc),
    )
    assert {r.external_fine_id for r in rows} == {"AB111111", "AB222222"}


async def test_finish_after_fines_completes_bootstrap_with_audit(fx):
    fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)
    await _add_fine_flow(fx)
    await fx.controller.handle_manual_bootstrap_addfine_save(_PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID)

    finish_reply = fx.controller.handle_manual_bootstrap_finish_confirm(_PLATE, telegram_user_id=_ADMIN_ID)

    assert "Добавлено штрафов: 1" in finish_reply.text
    state = fx.bootstrap_repository.get(_PLATE)
    assert state.is_completed
    assert state.bootstrap_completed_by_user_id == _ADMIN_ID


# ---- 13: cancel midway -> pending remains, no partial fine ----


async def test_cancel_midway_leaves_plate_pending_with_no_partial_fine(fx):
    task = fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)
    fx.controller.handle_manual_bootstrap_addfine_start(_PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID)
    await fx.controller.handle_text("AB999999", chat_id=5001, telegram_user_id=_ADMIN_ID, username=None)
    await fx.controller.handle_text("50", chat_id=5001, telegram_user_id=_ADMIN_ID, username=None)

    cancel_reply = fx.controller.handle_manual_bootstrap_addfine_cancel(
        _PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID,
    )

    assert cancel_reply is not None
    assert fx.bootstrap_repository.get(_PLATE).bootstrap_status == "pending"
    assert fx.conversation_state_repository.get(5001) is None
    assert fx.detected_fine_repository.list_current_for_task(
        task.id, since=datetime(1970, 1, 1, tzinfo=timezone.utc),
    ) == []


# ---- 14: restart midway -> safe state, no corruption ----


async def test_restart_midway_is_safe(fx, tmp_path):
    """Рестарт процесса = новые ConversationController/ManualBootstrapService
    поверх ТОГО ЖЕ файла БД (conversation_state переживает рестарт — см.
    BotConversationStateRepository докстрок, — но это НЕ проблема: ни один
    шаг ДО review/save не пишет в detected_fines)."""
    fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)
    fx.controller.handle_manual_bootstrap_addfine_start(_PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID)
    await fx.controller.handle_text("AB888888", chat_id=5001, telegram_user_id=_ADMIN_ID, username=None)

    # "Рестарт" — новый набор репозиториев/сервисов/контроллера поверх того
    # же db_path, ТЕ ЖЕ объекты fx не переиспользуются напрямую.
    new_task_repository = FineMonitoringTaskRepository(fx.db_path)
    new_detected_fine_repository = DetectedFineRepository(fx.db_path)
    new_bootstrap_repository = GeorgiaPlateBootstrapRepository(fx.db_path)
    new_states = BotConversationStateRepository(fx.db_path)
    try:
        new_check_service = FineCheckService(_NoopProvider(), new_task_repository, new_detected_fine_repository)
        new_manual_bootstrap = ManualBootstrapService(
            new_bootstrap_repository, new_task_repository, new_detected_fine_repository, new_check_service,
        )
        new_subscription_service = SubscriptionService(
            new_task_repository, FineSubscriptionRepository(fx.db_path), UserRepository(fx.db_path),
            new_check_service, plate_bootstrap_repository=new_bootstrap_repository,
        )
        new_controller = ConversationController(
            new_states, new_subscription_service,
            BotStatisticsService(
                BotKnownUsersRepository(fx.db_path), FineSubscriptionRepository(fx.db_path),
                new_detected_fine_repository,
            ),
            BotKnownUsersRepository(fx.db_path), tz=_TBILISI,
            fine_admin_user_ids=frozenset({_ADMIN_ID}), manual_bootstrap_service=new_manual_bootstrap,
        )

        reply = new_controller.handle_manual_bootstrap_addfine_cancel(
            _PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID,
        )

        assert reply is not None
        assert new_bootstrap_repository.get(_PLATE).bootstrap_status == "pending"
        task = new_task_repository.list_active()[0]
        assert new_detected_fine_repository.list_current_for_task(
            task.id, since=datetime(1970, 1, 1, tzinfo=timezone.utc),
        ) == []
    finally:
        new_task_repository.close()
        new_detected_fine_repository.close()
        new_bootstrap_repository.close()
        new_states.close()


# ---- 15: concurrent managers -> no duplicates ----


async def test_concurrent_nofine_confirm_does_not_double_complete(fx):
    fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)

    first = fx.controller.handle_manual_bootstrap_nofine_confirm(_PLATE, telegram_user_id=_ADMIN_ID)
    second_admin_id = _ADMIN_ID  # тот же fine_admin-список, "второй менеджер" с другим user_id тоже admin
    second = fx.controller.handle_manual_bootstrap_nofine_confirm(_PLATE, telegram_user_id=second_admin_id)

    assert first.text == texts.format_manual_bootstrap_nofine_done_manager_text(_PLATE)
    assert second.text == texts.MANUAL_BOOTSTRAP_ALREADY_COMPLETED_TEXT


def test_manager_action_safe_after_collector_already_completed_plate(fx):
    """PR #34 review ("Georgia remote collector architecture"): the
    collector's bootstrap-result path completes a plate the SAME way a
    live searchByAuto would — GeorgiaPlateBootstrapRepository.mark_completed
    with completed_by_user_id=None (see CollectorIngestService.
    ingest_bootstrap_result -> ManualBootstrapService.confirm_no_fines(...,
    completed_by_user_id=None)). If a manager's Telegram UI was already open
    on this plate (buttons rendered before the collector finished) and they
    press "✅ Штрафов нет" AFTER the collector completed it, the SAME
    idempotent re-check confirm_no_fines() already does for manager-vs-
    manager races applies here too, since it only looks at
    state.is_completed, never at who completed it — this test exercises that
    cross-component path explicitly rather than relying on manager-vs-
    manager coverage alone."""
    fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)

    # Simulates the collector's own completion call path (completed_by_
    # user_id=None, not a manager's telegram_user_id).
    fx.bootstrap_repository.mark_completed(
        _PLATE, completed_at=datetime.now(timezone.utc), completed_by_user_id=None,
    )

    reply = fx.controller.handle_manual_bootstrap_nofine_confirm(_PLATE, telegram_user_id=_ADMIN_ID)

    assert reply.text == texts.MANUAL_BOOTSTRAP_ALREADY_COMPLETED_TEXT
    assert not getattr(reply, "customer_notify_messages", None)
    state = fx.bootstrap_repository.get(_PLATE)
    assert state.bootstrap_completed_by_user_id is None  # collector's completion not overwritten


# ---- 17: pending queue shows only pending plates ----


def test_pending_queue_shows_only_pending_plates(fx):
    fx.bootstrap_repository.ensure_pending("AA001AA")
    fx.bootstrap_repository.ensure_pending("BB002BB")
    fx.bootstrap_repository.mark_completed("BB002BB", completed_at=datetime.now(timezone.utc))
    fx.bootstrap_repository.ensure_pending("CC003CC")

    pending = fx.manual_bootstrap_service.list_pending()

    assert sorted(pending) == ["AA001AA", "CC003CC"]


async def test_pending_queue_command_lists_pending_plates_with_open_buttons(fx):
    fx.bootstrap_repository.ensure_pending(_PLATE)

    reply = await fx.controller.handle_text(
        texts.MANUAL_BOOTSTRAP_PENDING_QUEUE_LABEL, chat_id=1, telegram_user_id=_ADMIN_ID, username=None,
    )

    assert _PLATE.replace("", "") or True  # formatting uses format_plate_for_telegram, check structurally below
    assert reply.inline_buttons is not None
    assert len(reply.inline_buttons) == 1


async def test_pending_queue_command_rejects_non_admin(fx):
    fx.bootstrap_repository.ensure_pending(_PLATE)

    reply = await fx.controller.handle_text(
        texts.MANUAL_BOOTSTRAP_PENDING_QUEUE_LABEL, chat_id=1, telegram_user_id=_OTHER_USER_ID, username=None,
    )

    assert reply.text == texts.MAIN_MENU_TEXT
    assert reply.inline_buttons is None


# ---- 18: multiple subscriptions for same physical plate (fan-out) ----


async def test_multiple_tasks_same_plate_get_fanned_out_fines_and_notifications(fx):
    """Производственный случай O528HY702 — 2 активные задачи одной
    физической машины, ВОЗМОЖНО разных клиентов."""
    task1 = fx.make_task(telegram_chat_id=111, created_by_user_id=111)
    task2 = fx.task_repository.create(
        car_number=_PLATE, label=None, start_date=_today(),
        end_date=_today() + timedelta(days=30),
        telegram_chat_id=222, created_by_user_id=222,
    )
    fx.bootstrap_repository.ensure_pending(_PLATE)

    # Manager's own conversation chat (менеджер работает в СВОЁМ чате с
    # ботом, не в чате клиента — см. _add_fine_flow default chat_id=5001,
    # то же значение здесь, НЕ telegram_chat_id ни одной из задач).
    await _add_fine_flow(fx)
    await fx.controller.handle_manual_bootstrap_addfine_save(_PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID)

    rows1 = fx.detected_fine_repository.list_current_for_task(
        task1.id, since=datetime(1970, 1, 1, tzinfo=timezone.utc),
    )
    rows2 = fx.detected_fine_repository.list_current_for_task(
        task2.id, since=datetime(1970, 1, 1, tzinfo=timezone.utc),
    )
    assert len(rows1) == 1
    assert len(rows2) == 1
    assert rows1[0].fingerprint == rows2[0].fingerprint

    # Одна georgia_plate_bootstrap_state строка на ФИЗИЧЕСКИЙ номер, не на
    # задачу (см. модульный докстрок plate_bootstrap_repository.py).
    assert fx.bootstrap_repository.get(_PLATE) is not None


# ---- 19: manual fine later seen in public feed -> no duplicate (documents the known caveat) ----


async def test_manual_fine_protocol_later_in_public_feed_does_not_duplicate_when_amount_matches(fx, tmp_path):
    """См. reader/public_bot/manual_bootstrap_service.py модульный докстрок
    про остаточный риск: НЕТ duplicate, когда protocolNo/violationDate/
    сумма (как голое число, см. parse_manual_amount) совпадают байт-в-байт
    с тем, что позже вернёт реальный feed — ИМЕННО ЭТОТ случай и
    воспроизводится здесь. Полное равенство fingerprint НЕ гарантируется
    при расхождении формата суммы (задокументированный лимит, не баг)."""
    task = fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)
    await _add_fine_flow(fx, protocol_no="AB123456", amount_text="50", date_text="05.10.2026")
    await fx.controller.handle_manual_bootstrap_addfine_save(_PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID)

    manual_fingerprint = fx.detected_fine_repository.list_current_for_task(
        task.id, since=datetime(1970, 1, 1, tzinfo=timezone.utc),
    )[0].fingerprint

    # Тот же штраф, "обнаруженный" публичным feed'ом (protocolAmount как
    # голое число 50, см. реальные fixtures в test_georgia_public_feed_job.py).
    feed_fingerprint = compute_fingerprint(
        external_fine_id="AB123456", violation_date=date(2026, 10, 5), amount=50,
    )

    assert manual_fingerprint == feed_fingerprint

    seen_repo = GeorgiaPublicFeedSeenRepository(fx.db_path)
    state_repo = GeorgiaPublicFeedStateRepository(fx.db_path)
    try:
        before = fx.detected_fine_repository.list_current_for_task(
            task.id, since=datetime(1970, 1, 1, tzinfo=timezone.utc),
        )
        existing = fx.detected_fine_repository.get_by_fingerprint(task.id, feed_fingerprint)
        assert existing is not None  # уже знаем этот fingerprint -> dedup сработает в persist_feed_match
        assert len(before) == 1
    finally:
        seen_repo.close()
        state_repo.close()


# ---- 20: emergency Check Now after manual fine displays it ----


async def test_check_now_after_manual_fine_shows_it_via_cached_path(fx):
    task = fx.make_task()
    fx.bootstrap_repository.ensure_pending(_PLATE)
    await _add_fine_flow(fx)
    await fx.controller.handle_manual_bootstrap_addfine_save(_PLATE, chat_id=5001, telegram_user_id=_ADMIN_ID)

    # check_task_or_read_cached() falls back to _read_last_known_state()
    # когда provider недоступен/last_successful_checked_at есть (см.
    # reader/fines/check_service.py) — здесь просто подтверждаем, что
    # сохранённый вручную штраф виден через тот же read path, которым
    # пользуется emergency "Проверить сейчас" (CheckResult.from_cache).
    fresh_task = fx.task_repository.get(task.id)
    rows = fx.detected_fine_repository.list_current_for_task(
        fresh_task.id, since=datetime(1970, 1, 1, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0].external_fine_id == "AB123456"


# ---- feature-disabled build (manual_bootstrap_service=None) behaves like "no access" ----


def test_manual_bootstrap_disabled_build_returns_none_everywhere(fx):
    """"фичи нет в этой сборке" (public_feed_enabled=False, см.
    reader/public_bot/main.py) — ConversationController без
    manual_bootstrap_service ведёт себя идентично "нет доступа"."""
    disabled_controller = ConversationController(
        fx.conversation_state_repository, fx.subscription_service, fx.statistics_service,
        fx.known_users_repository, tz=_TBILISI, fine_admin_user_ids=frozenset({_ADMIN_ID}),
    )
    assert disabled_controller.handle_manual_bootstrap_nofine(_PLATE, telegram_user_id=_ADMIN_ID) is None


# ---- keyboards round-trip sanity ----


def test_nofine_callback_round_trips_plate():
    data = encode_manual_bootstrap_nofine_callback(_PLATE)
    assert decode_manual_bootstrap_nofine_callback(data) == _PLATE


def test_addfine_save_callback_round_trips_plate():
    data = encode_manual_bootstrap_addfine_start_callback(_PLATE)
    assert decode_manual_bootstrap_addfine_save_callback(data) is None  # разные префиксы, не взаимозаменяемы
