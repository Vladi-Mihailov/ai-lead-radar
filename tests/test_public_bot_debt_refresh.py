"""Тесты ConversationController.handle_debt_refresh_pick/confirm/cancel/
handle_debt_list_page и "🚨 Известные штрафы" в 📊 Статистика
(@ProtocolGEbot, задача "OPTIONAL DEBT REFRESH") — сквозные, через
РЕАЛЬНЫЙ ConversationController. Repository — настоящие (SQLite/tmp_path),
FineProvider — лёгкий фейк.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.provider import FineProvider
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot import texts
from reader.public_bot.conversation import ConversationController
from reader.public_bot.conversation_state_repository import (
    BotConversationStateRepository,
)
from reader.public_bot.debt_refresh_service import DebtRefreshService
from reader.public_bot.known_users_repository import (
    BotKnownUsersRepository,
)
from reader.public_bot.statistics_service import BotStatisticsService
from reader.public_bot.subscription_repository import (
    FineSubscriptionRepository,
)
from reader.public_bot.subscription_service import SubscriptionService
from reader.users.repository import UserRepository

_TBILISI = ZoneInfo("Asia/Tbilisi")
_TRUSTED_ID = 5712994689
_ORDINARY_ID = 685137235
_CHAT_ID = -100999
_USER_ID = 111


class _FakeProvider(FineProvider):
    def __init__(self):
        self.records_by_car: dict[str, list[ParsedFineRecord]] = {}
        self.requested_plates: list[str] = []

    async def search_by_plate(self, plate: str):
        self.requested_plates.append(plate)
        return self.records_by_car.get(plate, [])


def _record(*, car_number: str, fingerprint: str, amount: float) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number, external_fine_id=fingerprint,
        penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
        delivered_status="Не вручено", fingerprint=fingerprint,
        raw_data={"protocolNo": fingerprint}, amount=amount,
    )


class _Fixture:
    def __init__(self, tmp_path, *, with_debt_refresh: bool = True):
        self.db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(self.db_path)
        self.detected_fine_repository = DetectedFineRepository(self.db_path)
        self.subscription_repository = FineSubscriptionRepository(self.db_path)
        self.user_repository = UserRepository(self.db_path)
        self.conversation_state_repository = BotConversationStateRepository(self.db_path)
        self.known_users_repository = BotKnownUsersRepository(self.db_path)
        self.provider = _FakeProvider()
        self.check_service = FineCheckService(
            self.provider, self.task_repository, self.detected_fine_repository,
        )
        self.subscription_service = SubscriptionService(
            self.task_repository, self.subscription_repository,
            self.user_repository, self.check_service,
        )
        self.statistics_service = BotStatisticsService(
            self.known_users_repository, self.subscription_repository, self.detected_fine_repository,
        )
        self.debt_refresh_service = (
            DebtRefreshService(self.task_repository, self.detected_fine_repository, self.check_service)
            if with_debt_refresh else None
        )
        self.controller = ConversationController(
            self.conversation_state_repository, self.subscription_service, self.statistics_service,
            self.known_users_repository, tz=_TBILISI,
            trusted_operator_user_ids=frozenset({_TRUSTED_ID}),
            debt_refresh_service=self.debt_refresh_service,
        )

    def make_task(self, car_number: str):
        return self.task_repository.create(
            car_number=car_number, label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

    def add_subscription(self, task, *, telegram_user_id: int) -> None:
        self.subscription_repository.create(
            monitoring_task_id=task.id, car_number=task.car_number,
            telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id,
            telegram_username=None, start_date=task.start_date, end_date=task.end_date,
        )

    def record_known(
        self, *, telegram_user_id: int, username: str | None = None,
        first_name: str | None = None, last_name: str | None = None,
    ) -> None:
        self.known_users_repository.record_seen(
            telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id,
            telegram_username=username, first_name=first_name, last_name=last_name,
        )

    async def seed_debt(self, task, *, amount: float, fingerprint: str = "fp-seed") -> None:
        self.provider.records_by_car[task.car_number] = [
            _record(car_number=task.car_number, fingerprint=fingerprint, amount=amount)
        ]
        await self.check_service.check_task(task)

    def backdate_last_successful_check(self, task_id: int, *, days_ago: int) -> None:
        """Симулирует "проверено N дней назад" — record_successful_check()
        сам всегда пишет CURRENT_TIMESTAMP, поэтому для теста recency
        сдвигаем время назад напрямую в БД (та же техника, что и в
        tests/test_turkey_bot_statistics_handler.py для finished_at)."""
        when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        self.task_repository._conn.execute(
            "UPDATE fine_monitoring_tasks SET last_successful_checked_at = ? WHERE id = ?",
            (when, task_id),
        )
        self.task_repository._conn.commit()

    def close(self):
        self.task_repository.close()
        self.detected_fine_repository.close()
        self.subscription_repository.close()
        self.user_repository.close()
        self.conversation_state_repository.close()
        self.known_users_repository.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


# ---- 📊 Статистика — компактный debt block (задача п.1) ----


async def test_statistics_shows_compact_debt_block_with_disclaimer(fx):
    task = fx.make_task("AA111AA")
    await fx.seed_debt(task, amount=250)

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "🚨 Известные штрафы" in reply.text
    assert "Автомобилей со штрафами: 1" in reply.text
    assert "Общая сумма найденных штрафов: 250 ₾" in reply.text
    assert "ℹ️ Оплаченные штрафы могут учитываться в сумме." in reply.text
    assert reply.debt_refresh_available is True


async def test_statistics_never_uses_misleading_debt_or_paid_wording(fx):
    """См. задачу п.1 — police.ge не подтверждает оплату, поэтому этих
    формулировок не должно быть нигде в тексте 📊 Статистика."""
    task = fx.make_task("AA111AA")
    await fx.seed_debt(task, amount=250)

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "Текущая задолженность" not in reply.text
    assert "Общая задолженность" not in reply.text
    assert "Оплачено" not in reply.text


async def test_statistics_without_debt_refresh_service_falls_back_to_main_menu(tmp_path):
    fx = _Fixture(tmp_path, with_debt_refresh=False)
    try:
        reply = await fx.controller.handle_text(
            texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
        )
        assert reply.show_main_menu is True
        assert reply.debt_refresh_available is False
    finally:
        fx.close()


async def test_ordinary_user_does_not_see_debt_block(fx):
    task = fx.make_task("AA111AA")
    await fx.seed_debt(task, amount=250)

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID, username=None,
    )

    assert "Известные штрафы" not in reply.text
    assert reply.debt_refresh_available is False


# ---- itemized-список (задача п.2) ----


async def test_itemized_list_shows_all_known_debt_cars(fx):
    task_a = fx.make_task("AA111AA")
    task_b = fx.make_task("BB222BB")
    await fx.seed_debt(task_a, amount=120)
    await fx.seed_debt(task_b, amount=80)

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "Автомобили со штрафами:" in reply.text
    assert "AA111AA" in reply.text
    assert "BB222BB" in reply.text


async def test_itemized_list_owner_display_name_and_username(fx):
    task = fx.make_task("CC333CC")
    await fx.seed_debt(task, amount=120)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, username="DamirTat", first_name="Дамир", last_name="Татаров")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "🚗 CC333CC — Дамир Татаров (@DamirTat) — 120 ₾ · сегодня" in reply.text


async def test_itemized_list_owner_display_username_only(fx):
    task = fx.make_task("DD444DD")
    await fx.seed_debt(task, amount=50)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, username="DamirTat")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "🚗 DD444DD — @DamirTat — 50 ₾ · сегодня" in reply.text


async def test_itemized_list_owner_display_name_only(fx):
    task = fx.make_task("EE555EE")
    await fx.seed_debt(task, amount=50)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, first_name="Иван", last_name="Иванов")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "🚗 EE555EE — Иван Иванов — 50 ₾ · сегодня" in reply.text


async def test_itemized_list_owner_display_dash_when_neither(fx):
    task = fx.make_task("FF666FF")
    await fx.seed_debt(task, amount=50)
    # Ни add_subscription, ни record_known не вызываются вовсе.

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "🚗 FF666FF — — — 50 ₾ · сегодня" in reply.text
    assert "None" not in reply.text


async def test_itemized_list_recency_absolute_date_when_not_today(fx):
    task = fx.make_task("GG777GG")
    await fx.seed_debt(task, amount=50)
    fx.backdate_last_successful_check(task.id, days_ago=3)

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    expected_date = (datetime.now(timezone.utc) - timedelta(days=3)).astimezone(_TBILISI).strftime("%d.%m")
    assert f"🚗 GG777GG — — — 50 ₾ · {expected_date}" in reply.text


async def test_itemized_list_sorted_by_amount_desc(fx):
    small = fx.make_task("HH888HH")
    big = fx.make_task("II999II")
    await fx.seed_debt(small, amount=10)
    await fx.seed_debt(big, amount=500)

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert reply.text.index("II999II") < reply.text.index("HH888HH")


async def test_itemized_list_tie_break_by_more_recent_check_first(fx):
    older = fx.make_task("JJ000JJ")
    newer = fx.make_task("KK111KK")
    await fx.seed_debt(older, amount=100)
    await fx.seed_debt(newer, amount=100)
    fx.backdate_last_successful_check(older.id, days_ago=5)
    # newer оставлен "сегодня" (только что записан seed_debt через check_task,
    # но у него нет last_successful_checked_at, т.к. seed_debt не вызывает
    # DebtRefreshService — используется fallback last_seen_at, который тоже
    # "сегодня").

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert reply.text.index("KK111KK") < reply.text.index("JJ000JJ")


async def test_debt_list_pagination_does_not_exceed_telegram_limit(fx):
    """35 машин -> 4 страницы по 10 (см. _DEBT_LIST_PAGE_SIZE) — ни одна
    машина не теряется молча, каждое сообщение помещается в лимит."""
    for i in range(35):
        task = fx.make_task(f"P{i:04d}AA")
        await fx.seed_debt(task, amount=10 + i, fingerprint=f"fp-{i}")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )
    assert len(reply.text) <= 4096
    assert reply.debt_list_total_pages == 4
    assert reply.debt_list_page == 0
    first_page_cars = sum(1 for i in range(35) if f"P{i:04d}AA" in reply.text)
    assert first_page_cars == 10

    seen_cars: set[str] = set()
    for page in range(reply.debt_list_total_pages):
        page_reply = fx.controller.handle_debt_list_page(page, telegram_user_id=_TRUSTED_ID)
        assert len(page_reply.text) <= 4096
        for i in range(35):
            plate = f"P{i:04d}AA"
            if plate in page_reply.text:
                seen_cars.add(plate)
    assert len(seen_cars) == 35  # ни одна машина не потеряна/не обрезана


async def test_debt_list_page_ordinary_user_forbidden(fx):
    reply = fx.controller.handle_debt_list_page(0, telegram_user_id=_ORDINARY_ID)
    assert reply is None


# ---- handle_debt_refresh_pick ----


async def test_ordinary_user_cannot_pick_debt_refresh(fx):
    reply = fx.controller.handle_debt_refresh_pick(telegram_user_id=_ORDINARY_ID)
    assert reply.text == texts.CALLBACK_NOT_AUTHORIZED_TEXT
    assert fx.provider.requested_plates == []


async def test_pick_with_zero_debt_cars_makes_no_network_calls(fx):
    reply = fx.controller.handle_debt_refresh_pick(telegram_user_id=_TRUSTED_ID)

    assert reply.text == texts.DEBT_REFRESH_NONE_TEXT
    assert "нет" in reply.text
    assert reply.debt_refresh_confirm is False
    assert fx.provider.requested_plates == []


async def test_pick_with_debt_cars_shows_count_prompt_without_checking(fx):
    task_a = fx.make_task("BB222BB")
    task_b = fx.make_task("CC333CC")
    await fx.seed_debt(task_a, amount=40)
    await fx.seed_debt(task_b, amount=60)
    fx.provider.requested_plates.clear()

    reply = fx.controller.handle_debt_refresh_pick(telegram_user_id=_TRUSTED_ID)

    assert reply.text == "🔄 Будет проверено автомобилей: 2"
    assert reply.debt_refresh_confirm is True
    assert fx.provider.requested_plates == []


# ---- button label (задача п.3) ----


def test_debt_refresh_button_label_reflects_new_wording():
    assert texts.DEBT_REFRESH_BUTTON_LABEL == "🔄 Проверить авто со штрафами"
    assert "задолженност" not in texts.DEBT_REFRESH_BUTTON_LABEL.lower()


# ---- handle_debt_refresh_confirm / summary (задача п.4) ----


async def test_ordinary_user_cannot_confirm_debt_refresh(fx):
    task = fx.make_task("DD444DD")
    await fx.seed_debt(task, amount=40)
    fx.provider.requested_plates.clear()

    reply = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_ORDINARY_ID)

    assert reply.text == texts.CALLBACK_NOT_AUTHORIZED_TEXT
    assert fx.provider.requested_plates == []


async def test_confirm_runs_refresh_and_returns_manager_summary(fx):
    increased_task = fx.make_task("EE555EE")
    unchanged_task = fx.make_task("FF666FF")
    await fx.seed_debt(increased_task, amount=40, fingerprint="fp-1")
    await fx.seed_debt(unchanged_task, amount=70, fingerprint="fp-2")
    fx.provider.requested_plates.clear()  # seed_debt() сам дёрнул provider — не в счёт
    # increased_task получит ещё один штраф на этой проверке.
    fx.provider.records_by_car[increased_task.car_number] = [
        _record(car_number=increased_task.car_number, fingerprint="fp-1", amount=40),
        _record(car_number=increased_task.car_number, fingerprint="fp-1b", amount=15),
    ]

    reply = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)

    assert "🔄 Проверка завершена" in reply.text
    assert "Проверено: 2" in reply.text
    assert "🚨 Найдены новые штрафы: 1" in reply.text
    assert "✅ Без новых штрафов: 1" in reply.text
    assert "⚠️ Не удалось проверить: 0" in reply.text
    assert "Оплачено" not in reply.text
    assert "Остался долг" not in reply.text
    assert reply.show_main_menu is True
    assert sorted(fx.provider.requested_plates) == ["EE555EE", "FF666FF"]


async def test_confirm_lists_failed_car_numbers(fx):
    task = fx.make_task("GG777GG")
    await fx.seed_debt(task, amount=40)
    fx.provider.records_by_car.pop(task.car_number, None)

    class _ErrorProvider:
        async def search_by_plate(self, plate: str):
            from reader.fines.provider import FineProviderError
            raise FineProviderError("simulated")

    fx.check_service._provider = _ErrorProvider()

    reply = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)

    assert "⚠️ Не удалось проверить: 1" in reply.text
    assert "GG777GG" in reply.text


async def test_confirm_with_no_known_debt_checks_nothing(fx):
    reply = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)

    assert "Проверено: 0" in reply.text
    assert fx.provider.requested_plates == []


# ---- handle_debt_refresh_cancel ----


async def test_cancel_returns_cancelled_text_without_checking_anything(fx):
    task = fx.make_task("HH888HH")
    await fx.seed_debt(task, amount=40)
    fx.provider.requested_plates.clear()

    reply = fx.controller.handle_debt_refresh_cancel(telegram_user_id=_TRUSTED_ID)

    assert reply.text == texts.DEBT_REFRESH_CANCELLED_TEXT
    assert reply.show_main_menu is True
    assert fx.provider.requested_plates == []


async def test_ordinary_user_cancel_is_still_authorized_checked(fx):
    reply = fx.controller.handle_debt_refresh_cancel(telegram_user_id=_ORDINARY_ID)
    assert reply.text == texts.CALLBACK_NOT_AUTHORIZED_TEXT
