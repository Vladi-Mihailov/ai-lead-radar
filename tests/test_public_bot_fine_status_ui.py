"""Статусы оплаты в @ProtocolGEbot (reader/public_bot/conversation.py):
"🔎 Проверить сейчас" (строка статуса у штрафа, итог "к оплате", кнопка
оплаты только при неоплаченных), 📊 Статистика (подтверждённая
задолженность / оплачено / неопределённый статус, дата обновления базы,
строка вечерней перепроверки) и экраны менеджера "🧾 Статусы оплаты".
Сквозные, через настоящий ConversationController и SQLite; номера
синтетические."""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import UNLIMITED_END_DATE, ParsedFineRecord
from reader.fines.payment_status import (
    MIA_PAYMENT_FAILED_MESSAGE,
    PAYMENT_NEEDS_REVIEW,
    PAYMENT_PAID,
    PAYMENT_UNPAID,
    FineStatusRepository,
    FineStatusTracker,
    PaymentPageMarkers,
)
from reader.fines.provider import FineProvider
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.plate_display import format_plate_for_telegram
from reader.public_bot import keyboards, texts
from reader.public_bot.conversation import ConversationController
from reader.public_bot.conversation_state_repository import BotConversationStateRepository
from reader.public_bot.daily_recheck import DailyRecheckRun
from reader.public_bot.debt_refresh_service import DebtRefreshService
from reader.public_bot.known_users_repository import BotKnownUsersRepository
from reader.public_bot.statistics_service import BotStatisticsService
from reader.public_bot.subscription_repository import FineSubscriptionRepository
from reader.public_bot.subscription_service import SubscriptionService
from reader.users.repository import UserRepository

_TBILISI = ZoneInfo("Asia/Tbilisi")
_TRUSTED = 900000001
_OTHER_TRUSTED = 900000002
_OWNER = 900000101
_CHAT = 424242
_CAR = "AA111BB"


async def _instant(_seconds):
    pass


def _record(car, protocol_no, amount):
    return ParsedFineRecord(
        car_number=car, external_fine_id=protocol_no, penalty_date=date(2026, 11, 1), due_date=date(2026, 12, 31),
        delivered_status="Не вручено", fingerprint=f"fp-{car}-{protocol_no}", raw_data={"protocolNo": protocol_no},
        amount=amount,
    )


class _Provider(FineProvider):
    def __init__(self):
        self.records: dict[str, list[ParsedFineRecord]] = {}

    async def search_by_plate(self, plate):
        return list(self.records.get(plate, []))


class _FakeRecheck:
    def __init__(self, run):
        self._run = run

    def latest_run(self):
        return self._run


class _Fixture:
    def __init__(self, tmp_path, *, markers=None, recheck_run=None, with_statuses=True):
        db_path = tmp_path / "users.db"
        self.tasks = FineMonitoringTaskRepository(db_path)
        self.fines = DetectedFineRepository(db_path)
        self.subscriptions = FineSubscriptionRepository(db_path)
        self.users = UserRepository(db_path)
        self.states = BotConversationStateRepository(db_path)
        self.known = BotKnownUsersRepository(db_path)
        self.status_repository = FineStatusRepository(db_path)
        self.tracker = FineStatusTracker(self.status_repository, markers=markers)
        self.provider = _Provider()
        self.check_service = FineCheckService(
            self.provider, self.tasks, self.fines, sleep=_instant,
            status_tracker=self.tracker if with_statuses else None,
        )
        self.subscription_service = SubscriptionService(self.tasks, self.subscriptions, self.users, self.check_service)
        self.controller = ConversationController(
            self.states, self.subscription_service,
            BotStatisticsService(self.known, self.subscriptions, self.fines), self.known, tz=_TBILISI,
            trusted_operator_user_ids=frozenset({_TRUSTED, _OTHER_TRUSTED}),
            debt_refresh_service=DebtRefreshService(self.tasks, self.check_service, sleep=_instant),
            fine_status_tracker=self.tracker if with_statuses else None,
            daily_recheck_service=_FakeRecheck(recheck_run) if recheck_run is not None else None,
        )

    def car(self, car, fines, *, owner=None):
        task = self.tasks.create(
            car_number=car, label=None, start_date=date(2026, 11, 1), end_date=date(2026, 12, 31),
            telegram_chat_id=-1, created_by_user_id=1,
        )
        self.provider.records[car] = [_record(car, protocol, amount) for protocol, amount in fines]
        subscription = None
        if owner is not None:
            subscription = self.subscriptions.create(
                monitoring_task_id=task.id, car_number=car, telegram_user_id=owner, telegram_chat_id=owner,
                telegram_username=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
            )
        return task, subscription

    async def check(self, task):
        await self.check_service.check_task(self.tasks.get(task.id))

    def status(self, car, protocol):
        return self.tracker.statuses_by_protocol(car)[protocol]

    def close(self):
        for repository in (self.status_repository, self.known, self.states, self.users, self.subscriptions,
                           self.fines, self.tasks):
            repository.close()


@pytest.fixture()
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


# ---- 🔎 Проверить сейчас ----


async def test_check_now_shows_statuses_and_excludes_paid_from_amount_to_pay(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50), ("GE2", 100)], owner=_OWNER)
    await fx.check(task)
    fx.tracker.mark_paid(fx.status(_CAR, "GE2").id, actor_user_id=_TRUSTED, note="TBC 1")

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    assert "Статус: 🔴 Не оплачен" in reply.text
    assert "Статус: 🟢 Оплачен" in reply.text
    assert "💰 К оплате: 50 ₾" in reply.text
    assert "🟢 Оплачено, ждёт обновления базы полиции: 100 ₾" in reply.text
    assert reply.cta_buttons  # есть неоплаченный — кнопка оплаты на месте


async def test_check_now_with_only_paid_fines_offers_no_payment(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50), ("GE2", 100)], owner=_OWNER)
    await fx.check(task)
    for protocol in ("GE1", "GE2"):
        fx.tracker.mark_paid(fx.status(_CAR, protocol).id, actor_user_id=_TRUSTED, note="TBC 1")

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    assert reply.cta_buttons is None
    assert "💰 К оплате: 0 ₾" in reply.text


async def test_check_now_uncertain_payment_stays_in_amount_to_pay(fx):
    task, subscription = fx.car(_CAR, [("GE1", 50), ("GE2", 100)], owner=_OWNER)
    await fx.check(task)
    fx.tracker.mark_paid(fx.status(_CAR, "GE1").id, actor_user_id=_TRUSTED, note="TBC 1")
    fx.tracker.mark_needs_review(fx.status(_CAR, "GE2").id, actor_user_id=_TRUSTED, note="отказ")

    reply = await fx.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    # 🟡 — не оплата: долг остаётся, кнопка оплаты тоже
    assert "💰 К оплате: 100 ₾" in reply.text
    assert "🟡 Из них оплата требует проверки: 100 ₾" in reply.text
    assert reply.cta_buttons


async def test_check_now_without_statuses_keeps_old_format(tmp_path):
    fixture = _Fixture(tmp_path, with_statuses=False)
    task, subscription = fixture.car(_CAR, [("GE1", 50)], owner=_OWNER)

    reply = await fixture.controller.handle_check_now_choice(subscription.id, telegram_user_id=_OWNER)

    assert "Статус:" not in reply.text and "К оплате" not in reply.text
    assert reply.cta_buttons
    fixture.close()


# ---- 📊 Статистика ----


async def test_statistics_separates_confirmed_paid_and_uncertain(tmp_path):
    run = DailyRecheckRun(
        run_date=date(2026, 11, 18), status="completed", started_at=datetime(2026, 11, 18, 19, 0),
        heartbeat_at=datetime(2026, 11, 18, 19, 6), finished_at=datetime(2026, 11, 18, 19, 6),
        total=3, checked=3, failed=0, stop_reason=None,
    )
    fixture = _Fixture(tmp_path, recheck_run=run)
    unpaid_task, _ = fixture.car("AA111BB", [("GE1", 50), ("GE2", 100)])
    paid_task, _ = fixture.car("BB222CC", [("GE3", 30)])
    review_task, _ = fixture.car("CC333DD", [("GE4", 70)])
    for task in (unpaid_task, paid_task, review_task):
        await fixture.check(task)
    fixture.tracker.mark_paid(fixture.status("BB222CC", "GE3").id, actor_user_id=_TRUSTED, note="BOG 9")
    fixture.tracker.mark_needs_review(fixture.status("CC333DD", "GE4").id, actor_user_id=_TRUSTED, note=None)
    fixture.tracker.record_source_updated_on(date(2026, 11, 12))

    reply = fixture.controller._format_statistics_reply()

    assert "🔴 Подтверждённая задолженность: 150 ₾ (2 шт.)" in reply.text
    assert "🟡 Неопределённый статус оплаты: 70 ₾ (1 шт.)" in reply.text
    assert "🟢 Оплачено, ещё в базе полиции: 30 ₾ (1 шт.)" in reply.text
    assert "🗓 База police.ge обновлена: 12.11.2026" in reply.text
    assert "🌙 Вечерняя перепроверка 18.11: завершена — проверено 3 из 3, ошибок 0" in reply.text
    assert reply.fine_status_available
    # подтверждённый долг — первым, оплаченная машина — с нулём и строкой 🟢
    position = {car: reply.text.index(format_plate_for_telegram(car)) for car in ("AA111BB", "BB222CC", "CC333DD")}
    assert position["AA111BB"] < position["CC333DD"] < position["BB222CC"]
    paid_block = reply.text[position["BB222CC"]:]
    assert "💰 Задолженность: 0 ₾" in paid_block and "🟢 Оплачено: 30 ₾" in paid_block
    review_block = reply.text[position["CC333DD"]:position["BB222CC"]]
    assert "💰 Задолженность: 70 ₾" in review_block and "🟡 Из них требует проверки: 70 ₾" in review_block
    fixture.close()


async def test_car_with_only_gone_receipts_leaves_statistics_despite_old_snapshot(fx):
    task, _ = fx.car(_CAR, [("GE1", 70)])
    await fx.check(task)  # положительный снимок задачи остаётся прежним
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fx.tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=now - timedelta(hours=13))
    fx.tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=now + timedelta(seconds=5))

    reply = fx.controller._format_statistics_reply()

    assert "Автомобилей: 0" in reply.text
    assert "🔴 Подтверждённая задолженность: 0 ₾ (0 шт.)" in reply.text


async def test_unverified_history_is_shown_separately_not_as_debt(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    old = fx.tracker._repository.insert_listed(
        car_number=_CAR, protocol_no="OLD1", amount=30, at=datetime(2026, 10, 1, 10, 0), source="check",
    )
    fx.tracker._repository._conn.execute(
        "UPDATE georgia_fine_status SET presence = 'unverified' WHERE id = ?", (old.id,),
    )
    fx.tracker._repository._conn.commit()

    stats = fx.controller._format_statistics_reply().text
    screen = await _open_car(fx)

    assert "🔴 Подтверждённая задолженность: 50 ₾ (1 шт.)" in stats
    assert "⏳ Ждут повторной проверки police.ge: 30 ₾ (1 шт.)" in stats
    assert "⏳ Ждут повторной проверки: 30 ₾" in stats  # строка в блоке машины
    assert "№OLD1 — 30 ₾ — ⏳ Ждёт повторной проверки police.ge" in screen.text


async def test_snapshot_newer_than_statuses_is_still_shown_as_debt(fx):
    """Квитанция давно ⚪, а потом проверка БЕЗ статусов (операторский
    процесс, сбой записи статусов) нашла настоящий долг — он не прячется."""
    task, _ = fx.car(_CAR, [("GE1", 70)])
    long_ago = datetime(2026, 1, 1, 10, 0)
    fx.tracker.record_full_check(_CAR, [_record(_CAR, "GE1", 70)], complete=True, at=long_ago)
    fx.tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=long_ago + timedelta(hours=1))
    fx.tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=long_ago + timedelta(hours=14))
    assert fx.status(_CAR, "GE1").presence == "gone"
    untracked = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant)  # без статусов
    fx.provider.records[_CAR] = [_record(_CAR, "GE5", 90)]
    await untracked.check_task(fx.tasks.get(task.id))

    reply = fx.controller._format_statistics_reply()

    assert "🔴 Подтверждённая задолженность: 90 ₾" in reply.text
    assert "💰 Задолженность: 90 ₾" in reply.text


async def test_stale_statuses_with_listed_receipts_do_not_hide_a_newer_debt(fx):
    """Статусы есть (🔴 50), но более новая проверка без статусов нашла ещё
    один штраф — в статистике сумма снимка, а не устаревшие статусы."""
    task, _ = fx.car(_CAR, [("GE1", 50)])
    two_hours_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    fx.tracker.record_full_check(_CAR, [_record(_CAR, "GE1", 50)], complete=True, at=two_hours_ago)
    fx.provider.records[_CAR] = [_record(_CAR, "GE1", 50), _record(_CAR, "GE5", 30)]
    untracked = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant)  # без статусов
    await untracked.check_task(fx.tasks.get(task.id))

    stats = fx.controller._format_statistics_reply().text

    assert "💰 Задолженность: 80 ₾" in stats
    assert "🔴 Подтверждённая задолженность: 80 ₾" in stats


async def test_paid_receipt_explains_a_newer_untracked_snapshot(fx):
    """Более новый снимок, который статусы объясняют полностью (🟢 100 из
    100), отметку «оплачено» не отменяет."""
    task, _ = fx.car(_CAR, [("GE1", 100)])
    two_hours_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    fx.tracker.record_full_check(_CAR, [_record(_CAR, "GE1", 100)], complete=True, at=two_hours_ago)
    fx.tracker.mark_paid(fx.status(_CAR, "GE1").id, actor_user_id=_TRUSTED, note="TBC 1")
    untracked = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant)
    await untracked.check_task(fx.tasks.get(task.id))

    stats = fx.controller._format_statistics_reply().text

    assert "🔴 Подтверждённая задолженность: 0 ₾ (0 шт.)" in stats
    assert "🟢 Оплачено, ещё в базе полиции: 100 ₾ (1 шт.)" in stats


def _make_unverified(fx, protocol_no, amount):
    status = fx.tracker._repository.insert_listed(
        car_number=_CAR, protocol_no=protocol_no, amount=amount, at=datetime(2026, 10, 1, 10, 0), source="check",
    )
    fx.tracker._repository._conn.execute(
        "UPDATE georgia_fine_status SET presence = 'unverified' WHERE id = ?", (status.id,),
    )
    fx.tracker._repository._conn.commit()
    return status


async def test_fallback_row_does_not_count_history_twice(fx):
    """Квитанция ⏳, которую потом нашла проверка без статусов: сумма снимка —
    в долге машины, а в строке ⏳ эта квитанция второй раз не считается."""
    task, _ = fx.car(_CAR, [("OLD1", 30)])
    _make_unverified(fx, "OLD1", 30)
    untracked = FineCheckService(fx.provider, fx.tasks, fx.fines, sleep=_instant)
    await untracked.check_task(fx.tasks.get(task.id))

    stats = fx.controller._format_statistics_reply().text

    assert "🔴 Подтверждённая задолженность: 30 ₾ (1 шт.)" in stats
    assert "⏳ Ждут повторной проверки" not in stats


async def test_unverified_receipt_marked_failed_payment_stays_in_debt(fx):
    """Квитанция из истории (⏳), по которой сайт МВД отказал в оплате, —
    🟡 и в долге, как любая 🟡; машина видна в списке даже без снимка долга."""
    fx.car(_CAR, [])  # ни одной успешной проверки — снимка долга нет
    status = _make_unverified(fx, "OLD1", 40)
    fx.tracker.mark_needs_review(status.id, actor_user_id=_TRUSTED, note="ჯარიმის გადახდა ვერ მოხერხდა!")

    stats = fx.controller._format_statistics_reply().text

    assert "🟡 Неопределённый статус оплаты: 40 ₾ (1 шт.)" in stats
    assert "💰 Задолженность: 40 ₾" in stats and "🟡 Из них требует проверки: 40 ₾" in stats
    assert "⏳ Ждут повторной проверки" not in stats


async def test_statistics_without_statuses_is_unchanged(tmp_path):
    fixture = _Fixture(tmp_path, with_statuses=False)
    task, _ = fixture.car(_CAR, [("GE1", 50)])
    await fixture.check(task)

    reply = fixture.controller._format_statistics_reply()

    assert "Общая сумма: 50 ₾" in reply.text
    assert "Подтверждённая задолженность" not in reply.text
    assert not reply.fine_status_available
    fixture.close()


# ---- 🧾 Статусы оплаты ----


def _press_confirm(fx, reply, *, user=_TRUSTED):
    status_id, token = keyboards.decode_fine_status_confirm_callback(reply.inline_buttons[0][0][1])
    return fx.controller.handle_fine_status_confirm(status_id, token, chat_id=_CHAT, telegram_user_id=user)


async def _open_car(fx, car=_CAR, user=_TRUSTED):
    assert fx.controller.handle_fine_status_prompt(chat_id=_CHAT, telegram_user_id=user) is not None
    return await fx.controller.handle_text(car, chat_id=_CHAT, telegram_user_id=user, username=None)


async def test_only_trusted_managers_can_use_payment_statuses(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    status_id = fx.status(_CAR, "GE1").id

    assert fx.controller.handle_fine_status_prompt(chat_id=_CHAT, telegram_user_id=_OWNER) is None
    assert fx.controller.handle_fine_status_open(status_id, chat_id=_CHAT, telegram_user_id=_OWNER) is None
    assert fx.controller.handle_fine_status_action(status_id, "paid", chat_id=_CHAT, telegram_user_id=_OWNER) is None
    assert fx.controller.handle_fine_status_confirm(status_id, "abcd1234", chat_id=_CHAT, telegram_user_id=_OWNER) is None
    assert fx.status(_CAR, "GE1").payment_status == PAYMENT_UNPAID


async def test_car_screen_lists_receipts_with_buttons(fx):
    task, _ = fx.car(_CAR, [("GE1", 50), ("GE2", 100)])
    await fx.check(task)

    reply = await _open_car(fx, car="aa 111-bb")

    assert "№GE1 — 50 ₾ — 🔴 Не оплачен" in reply.text
    assert len(reply.inline_buttons) == 2
    assert reply.inline_buttons[0][0][1] == keyboards.encode_fine_status_open_callback(fx.status(_CAR, "GE1").id)


async def test_mark_paid_requires_reason_and_confirmation(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    status_id = fx.status(_CAR, "GE1").id

    prompt = fx.controller.handle_fine_status_action(status_id, "paid", chat_id=_CHAT, telegram_user_id=_TRUSTED)
    assert prompt.text == texts.FINE_STATUS_PAYMENT_NOTE_PROMPT
    empty = fx.controller._handle_fine_payment_note_input(
        " \t ", chat_id=_CHAT, telegram_user_id=_TRUSTED, payload={"status_id": status_id},
    )
    assert empty.text == texts.FINE_STATUS_PAYMENT_NOTE_EMPTY_TEXT
    confirm = await fx.controller.handle_text(
        "TBC Pay 123456, 18.11.2026", chat_id=_CHAT, telegram_user_id=_TRUSTED, username=None,
    )
    assert "Станет: 🟢 Оплачен" in confirm.text and "TBC Pay 123456" in confirm.text
    assert fx.status(_CAR, "GE1").payment_status == PAYMENT_UNPAID  # до подтверждения — ничего

    done = _press_confirm(fx, confirm)

    status = fx.status(_CAR, "GE1")
    assert status.payment_status == PAYMENT_PAID and status.payment_updated_by == _TRUSTED
    assert texts.FINE_STATUS_SAVED_TEXT in done.text and "Основание (квитанция)" in done.text


async def test_mia_failure_button_is_needs_review_whatever_the_markers(tmp_path):
    fixture = _Fixture(tmp_path, markers=PaymentPageMarkers(already_paid=("ქვითარი უკვე გადახდილია",)))
    task, _ = fixture.car(_CAR, [("GE1", 50)])
    await fixture.check(task)
    status_id = fixture.status(_CAR, "GE1").id

    confirm = fixture.controller.handle_fine_status_action(
        status_id, "failed", chat_id=_CHAT, telegram_user_id=_TRUSTED,
    )
    assert "Станет: 🟡 Оплата требует проверки" in confirm.text
    _press_confirm(fixture, confirm)

    assert fixture.status(_CAR, "GE1").payment_status == PAYMENT_NEEDS_REVIEW
    fixture.close()


def test_generic_failure_can_never_be_configured_as_paid(tmp_path):
    with pytest.raises(ValueError):
        _Fixture(tmp_path, markers=PaymentPageMarkers(already_paid=(MIA_PAYMENT_FAILED_MESSAGE,)))


@pytest.mark.parametrize("page_text, expected_status, expected_label", [
    ("ჯარიმის გადახდა ვერ მოხერხდა!", PAYMENT_NEEDS_REVIEW, "Станет: 🟡 Оплата требует проверки"),
    ("Unknown error 0x1", PAYMENT_NEEDS_REVIEW, "Станет: 🟡 Оплата требует проверки"),
    ("ქვითარი უკვე გადახდილია", PAYMENT_PAID, "Станет: 🟢 Оплачен"),
    ("Technical error, try later", PAYMENT_UNPAID, "Станет: без изменений"),
])
async def test_payment_page_text_classification(tmp_path, page_text, expected_status, expected_label):
    fixture = _Fixture(tmp_path, markers=PaymentPageMarkers(
        already_paid=("ქვითარი უკვე გადახდილია",), technical_error=("technical error",),
    ))
    task, _ = fixture.car(_CAR, [("GE1", 50)])
    await fixture.check(task)
    status_id = fixture.status(_CAR, "GE1").id

    fixture.controller.handle_fine_status_action(status_id, "text", chat_id=_CHAT, telegram_user_id=_TRUSTED)
    confirm = await fixture.controller.handle_text(page_text, chat_id=_CHAT, telegram_user_id=_TRUSTED, username=None)
    assert expected_label in confirm.text
    _press_confirm(fixture, confirm)

    assert fixture.status(_CAR, "GE1").payment_status == expected_status
    assert fixture.tracker.events(status_id)[-1]["event"] in (
        "payment_paid", "payment_needs_review", "payment_page_technical_error",
    )
    fixture.close()


async def test_stale_or_foreign_confirmation_changes_nothing(fx):
    task, _ = fx.car(_CAR, [("GE1", 50), ("GE2", 60)])
    await fx.check(task)
    first, second = fx.status(_CAR, "GE1").id, fx.status(_CAR, "GE2").id
    confirm = fx.controller.handle_fine_status_action(first, "failed", chat_id=_CHAT, telegram_user_id=_TRUSTED)
    _, token = keyboards.decode_fine_status_confirm_callback(confirm.inline_buttons[0][0][1])

    other_manager = _press_confirm(fx, confirm, user=_OTHER_TRUSTED)
    wrong_receipt = fx.controller.handle_fine_status_confirm(second, token, chat_id=_CHAT, telegram_user_id=_TRUSTED)

    assert other_manager.text == texts.STALE_DIALOG_TEXT and wrong_receipt.text == texts.STALE_DIALOG_TEXT
    assert fx.status(_CAR, "GE1").payment_status == PAYMENT_UNPAID
    assert fx.status(_CAR, "GE2").payment_status == PAYMENT_UNPAID
    # нажатия чужой/неверной кнопки не сбросили настоящий диалог
    _press_confirm(fx, confirm)
    assert fx.status(_CAR, "GE1").payment_status == PAYMENT_NEEDS_REVIEW


async def test_button_of_an_older_screen_does_not_apply_a_newer_action(tmp_path):
    fixture = _Fixture(tmp_path, markers=PaymentPageMarkers(already_paid=("ქვითარი უკვე გადახდილია",)))
    task, _ = fixture.car(_CAR, [("GE1", 50)])
    await fixture.check(task)
    status_id = fixture.status(_CAR, "GE1").id
    old_screen = fixture.controller.handle_fine_status_action(
        status_id, "failed", chat_id=_CHAT, telegram_user_id=_TRUSTED,
    )  # «Станет: 🟡»
    fixture.controller.handle_fine_status_action(status_id, "text", chat_id=_CHAT, telegram_user_id=_TRUSTED)
    new_screen = await fixture.controller.handle_text(
        "ქვითარი უკვე გადახდილია", chat_id=_CHAT, telegram_user_id=_TRUSTED, username=None,
    )  # «Станет: 🟢»
    assert "Станет: 🟢 Оплачен" in new_screen.text

    stale = _press_confirm(fixture, old_screen)

    assert stale.text == texts.STALE_DIALOG_TEXT
    assert fixture.status(_CAR, "GE1").payment_status == PAYMENT_UNPAID
    fixture.close()


async def test_confirmation_rejected_when_status_changed_meanwhile(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    status_id = fx.status(_CAR, "GE1").id
    confirm = fx.controller.handle_fine_status_action(status_id, "failed", chat_id=_CHAT, telegram_user_id=_TRUSTED)
    # тем временем другой менеджер подтвердил оплату по квитанции
    fx.tracker.mark_paid(status_id, actor_user_id=_OTHER_TRUSTED, note="BOG 77")

    reply = _press_confirm(fx, confirm)

    assert texts.FINE_STATUS_CHANGED_TEXT in reply.text
    assert fx.status(_CAR, "GE1").payment_status == PAYMENT_PAID


async def test_manager_note_is_escaped_on_confirmation_screen(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    status_id = fx.status(_CAR, "GE1").id
    fx.controller.handle_fine_status_action(status_id, "paid", chat_id=_CHAT, telegram_user_id=_TRUSTED)

    confirm = await fx.controller.handle_text(
        "**TBC** [x](http://evil) 123", chat_id=_CHAT, telegram_user_id=_TRUSTED, username=None,
    )

    assert "**TBC**" not in confirm.text and "](" not in confirm.text


async def test_cancel_and_clear(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    status_id = fx.status(_CAR, "GE1").id
    fx.tracker.mark_needs_review(status_id, actor_user_id=_TRUSTED, note=None)

    detail = fx.controller.handle_fine_status_open(status_id, chat_id=_CHAT, telegram_user_id=_TRUSTED)
    actions = [data for row in detail.inline_buttons for _, data in row]
    assert keyboards.encode_fine_status_action_callback(status_id, "unpaid") in actions

    fx.controller.handle_fine_status_action(status_id, "unpaid", chat_id=_CHAT, telegram_user_id=_TRUSTED)
    cancelled = fx.controller.handle_fine_status_cancel(status_id, chat_id=_CHAT, telegram_user_id=_TRUSTED)
    assert texts.FINE_STATUS_CANCELLED_TEXT in cancelled.text
    assert fx.status(_CAR, "GE1").payment_status == PAYMENT_NEEDS_REVIEW

    confirm = fx.controller.handle_fine_status_action(status_id, "unpaid", chat_id=_CHAT, telegram_user_id=_TRUSTED)
    _press_confirm(fx, confirm)
    assert fx.status(_CAR, "GE1").payment_status == PAYMENT_UNPAID


async def test_gone_receipt_is_shown_but_not_actionable(fx):
    task, _ = fx.car(_CAR, [("GE1", 50)])
    await fx.check(task)
    status_id = fx.status(_CAR, "GE1").id
    t0 = datetime(2026, 11, 18, 19, 0)
    fx.tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=t0)
    fx.tracker.record_full_check(_CAR, [], complete=True, empty_confirmed=True, at=t0 + timedelta(hours=13))

    screen = await _open_car(fx)
    action = fx.controller.handle_fine_status_action(status_id, "paid", chat_id=_CHAT, telegram_user_id=_TRUSTED)

    assert "⚪ Больше не отображается в базе полиции" in screen.text
    assert screen.inline_buttons is None
    assert action.text == texts.FINE_STATUS_NOT_FOUND_TEXT


def test_callback_round_trips_and_rejects_garbage():
    assert keyboards.decode_fine_status_open_callback(keyboards.encode_fine_status_open_callback(42)) == 42
    assert keyboards.decode_fine_status_action_callback(
        keyboards.encode_fine_status_action_callback(42, "failed"),
    ) == (42, "failed")
    assert keyboards.decode_fine_status_action_callback(b"fsta:42:delete") is None
    assert keyboards.decode_fine_status_action_callback(b"fsta:x:paid") is None
    assert keyboards.decode_fine_status_confirm_callback(
        keyboards.encode_fine_status_confirm_callback(7, "a1b2c3d4"),
    ) == (7, "a1b2c3d4")
    assert keyboards.decode_fine_status_confirm_callback(b"fsty:7") is None
    assert keyboards.decode_fine_status_confirm_callback(b"fsty:7:..") is None
    assert keyboards.decode_fine_status_cancel_callback(keyboards.encode_fine_status_cancel_callback(7)) == 7
    assert keyboards.decode_fine_status_back_callback(keyboards.encode_fine_status_back_callback(7)) == 7
    assert keyboards.is_fine_status_ask_callback(b"fstask")
    assert all(len(keyboards.encode_fine_status_action_callback(10**9, a)) <= 64 for a in keyboards.FINE_STATUS_ACTIONS)


def test_daily_recheck_line_labels_every_stop_reason():
    for reason in ("blocked", "unavailable", "errors", "cutoff", "busy", "interrupted"):
        run = DailyRecheckRun(
            run_date=date(2026, 11, 18), status="postponed", started_at=datetime(2026, 11, 18, 19, 0),
            heartbeat_at=datetime(2026, 11, 18, 19, 1), finished_at=datetime(2026, 11, 18, 19, 1),
            total=5, checked=2, failed=0, stop_reason=reason,
        )
        line = texts.format_daily_recheck_line(run)
        assert reason not in line, line  # у каждой причины — русская подпись


def test_statistics_keyboard_has_payment_status_button_only_when_enabled():
    with_button = keyboards.debt_statistics_keyboard(page=None, total_pages=None, fine_status_button=True)
    without = keyboards.debt_statistics_keyboard(page=None, total_pages=None)
    assert any(button.text == texts.FINE_STATUS_BUTTON_LABEL for row in with_button for button in row)
    assert not any(button.text == texts.FINE_STATUS_BUTTON_LABEL for row in without for button in row)
