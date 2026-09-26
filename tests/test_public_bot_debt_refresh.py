"""Тесты ConversationController.handle_debt_refresh_pick/confirm/cancel/
handle_debt_list_page и "🚨 Штрафы по последней проверке" в 📊 Статистика
(@ProtocolGEbot, задача "manager Statistics / refresh для обоих ботов") —
сквозные, через РЕАЛЬНЫЙ ConversationController. Repository — настоящие
(SQLite/tmp_path), FineProvider — лёгкий фейк.
"""

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


async def _instant_sleep(_seconds: float) -> None:
    """См. tests/test_debt_refresh_service.py — та же замена реального
    asyncio.sleep для false-zero confirmation/inter-car delay (см. задачу
    "guard Georgia debt against false zero results")."""


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


def _set_amount(provider: _FakeProvider, *, car_number: str, amount: float, fingerprint: str) -> None:
    if amount <= 0:
        provider.records_by_car[car_number] = []
    else:
        provider.records_by_car[car_number] = [
            _record(car_number=car_number, fingerprint=fingerprint, amount=amount)
        ]


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
            sleep=_instant_sleep,
        )
        self.subscription_service = SubscriptionService(
            self.task_repository, self.subscription_repository,
            self.user_repository, self.check_service,
        )
        self.statistics_service = BotStatisticsService(
            self.known_users_repository, self.subscription_repository, self.detected_fine_repository,
        )
        self.debt_refresh_service = (
            DebtRefreshService(self.task_repository, self.check_service, sleep=_instant_sleep)
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
        _set_amount(self.provider, car_number=task.car_number, amount=amount, fingerprint=fingerprint)
        await self.check_service.check_task(task)

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


# ---- 📊 Статистика — compact debt block (задача п.1/п.12) ----


async def test_statistics_shows_compact_debt_block_with_disclaimer(fx):
    task = fx.make_task("AA111AA")
    await fx.seed_debt(task, amount=250)

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "🚨 Штрафы по последней проверке" in reply.text
    assert "Автомобилей: 1" in reply.text
    assert "Общая сумма: 250 ₾" in reply.text
    assert reply.debt_refresh_available is True


async def test_statistics_open_makes_zero_provider_calls(fx):
    task = fx.make_task("AA111AA")
    await fx.seed_debt(task, amount=250)
    fx.provider.requested_plates.clear()

    await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert fx.provider.requested_plates == []


async def test_statistics_never_uses_misleading_debt_or_paid_wording(fx):
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

    assert "Штрафы по последней проверке" not in reply.text
    assert reply.debt_refresh_available is False


# ---- единый формат строки CAR: OWNER: AMOUNT (задача п.1/п.13) ----


async def test_row_format_is_car_colon_owner_colon_amount(fx):
    task = fx.make_task("AA111AA")
    await fx.seed_debt(task, amount=800)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, username="ao777oa777")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "🚗 AA111AA: @ao777oa777: 800 ₾" in reply.text


async def test_row_has_no_checked_at_date_or_today_yesterday_wording(fx):
    task = fx.make_task("BB222BB")
    await fx.seed_debt(task, amount=50)

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    row_line = next(line for line in reply.text.splitlines() if line.startswith("🚗 BB222BB"))
    assert "сегодня" not in row_line
    assert "вчера" not in row_line
    assert " — " not in row_line
    assert "· " not in row_line
    assert row_line == "🚗 BB222BB: —: 50 ₾"


async def test_row_never_shows_repeated_dash_dash_dash(fx):
    task = fx.make_task("CC333CC")
    await fx.seed_debt(task, amount=50)
    # Ни add_subscription, ни record_known не вызываются вовсе.

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "🚗 CC333CC: —: 50 ₾" in reply.text
    assert "— — —" not in reply.text
    assert "None" not in reply.text


# ---- owner display (задача п.2) ----


async def test_owner_display_name_and_username(fx):
    task = fx.make_task("DD444DD")
    await fx.seed_debt(task, amount=120)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, username="DamirTat", first_name="Дамир", last_name="Татаров")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "🚗 DD444DD: Дамир Татаров (@DamirTat): 120 ₾" in reply.text


async def test_owner_display_username_only(fx):
    task = fx.make_task("EE555EE")
    await fx.seed_debt(task, amount=50)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, username="DamirTat")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "🚗 EE555EE: @DamirTat: 50 ₾" in reply.text


async def test_owner_display_name_only(fx):
    task = fx.make_task("FF666FF")
    await fx.seed_debt(task, amount=50)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, first_name="Иван", last_name="Иванов")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "🚗 FF666FF: Иван Иванов: 50 ₾" in reply.text


async def test_owner_display_dot_name_falls_back_to_username_only(fx):
    """См. задачу п.2 — пример ". (@Roma12312)" должен стать "@Roma12312",
    username при этом НЕ теряется."""
    task = fx.make_task("GG777GG")
    await fx.seed_debt(task, amount=350)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, username="Roma12312", first_name=".")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "🚗 GG777GG: @Roma12312: 350 ₾" in reply.text
    assert ". (@Roma12312)" not in reply.text


async def test_owner_display_dash_username_and_underscore_sanitized(fx):
    task = fx.make_task("HH888HH")
    await fx.seed_debt(task, amount=10)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, username="someone", first_name="-", last_name="_")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "🚗 HH888HH: @someone: 10 ₾" in reply.text


# ---- pagination продолжает работать (задача п.7/п.13) ----


async def test_debt_list_pagination_does_not_exceed_telegram_limit(fx):
    for i in range(35):
        task = fx.make_task(f"P{i:04d}AA")
        await fx.seed_debt(task, amount=10 + i, fingerprint=f"fp-{i}")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )
    assert len(reply.text) <= 4096
    assert reply.debt_list_total_pages == 4
    assert reply.debt_list_page == 0

    seen_cars: set[str] = set()
    for page in range(reply.debt_list_total_pages):
        page_reply = fx.controller.handle_debt_list_page(page, telegram_user_id=_TRUSTED_ID)
        assert len(page_reply.text) <= 4096
        for i in range(35):
            plate = f"P{i:04d}AA"
            if plate in page_reply.text:
                seen_cars.add(plate)
    assert len(seen_cars) == 35


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


def test_debt_refresh_button_label_reflects_new_wording():
    assert texts.DEBT_REFRESH_BUTTON_LABEL == "🔄 Проверить авто со штрафами"
    assert "задолженност" not in texts.DEBT_REFRESH_BUTTON_LABEL.lower()


# ---- handle_debt_refresh_confirm — result + stateful persistence (задача п.5/п.7/п.8) ----


async def test_ordinary_user_cannot_confirm_debt_refresh(fx):
    task = fx.make_task("DD444DD")
    await fx.seed_debt(task, amount=40)
    fx.provider.requested_plates.clear()

    reply = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_ORDINARY_ID)

    assert reply.text == texts.CALLBACK_NOT_AUTHORIZED_TEXT
    assert fx.provider.requested_plates == []


async def test_confirm_result_has_no_increased_unchanged_analytics(fx):
    """См. задачу п.7 — refresh summary больше НЕ показывает "Найдены
    новые штрафы"/"Без новых штрафов": только "Проверено"/"Не удалось
    проверить"."""
    task = fx.make_task("EE555EE")
    await fx.seed_debt(task, amount=40, fingerprint="fp-1")

    reply = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)

    assert "🔄 Проверка завершена" in reply.text
    assert "Проверено: 1" in reply.text
    assert "⚠️ Не удалось проверить: 0" in reply.text
    assert "Найдены новые штрафы" not in reply.text
    assert "Без новых штрафов" not in reply.text
    assert "Оплачено" not in reply.text


async def test_confirm_shows_updated_list_800_to_500(fx):
    task = fx.make_task("FF666FF")
    await fx.seed_debt(task, amount=800, fingerprint="fp-1")
    _set_amount(fx.provider, car_number="FF666FF", amount=500, fingerprint="fp-1")

    reply = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)

    assert "🚨 Штрафы по последней проверке" in reply.text
    assert "Автомобилей: 1" in reply.text
    assert "Общая сумма: 500 ₾" in reply.text
    assert "🚗 FF666FF: —: 500 ₾" in reply.text
    assert "800" not in reply.text


async def test_confirm_450_to_0_disappears_from_result_and_next_statistics(fx):
    task = fx.make_task("GG777GG")
    await fx.seed_debt(task, amount=450, fingerprint="fp-1")
    _set_amount(fx.provider, car_number="GG777GG", amount=0, fingerprint="fp-1")

    confirm_reply = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)
    assert "GG777GG" not in confirm_reply.text
    assert "Автомобилей: 0" in confirm_reply.text

    stats_reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )
    assert "GG777GG" not in stats_reply.text
    assert "Автомобилей: 0" in stats_reply.text


async def test_error_preserves_previous_reliable_state_in_confirm_result(fx):
    task = fx.make_task("HH888HH")
    await fx.seed_debt(task, amount=2740, fingerprint="fp-1")

    class _ErrorProvider:
        async def search_by_plate(self, plate: str):
            from reader.fines.provider import FineProviderError
            raise FineProviderError("simulated")

    fx.check_service._provider = _ErrorProvider()

    reply = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)

    assert "⚠️ Не удалось проверить: 1" in reply.text
    assert "HH888HH" in reply.text  # упомянута в failed-списке
    assert "🚗 HH888HH: —: 2740 ₾" in reply.text  # осталась в списке со СТАРОЙ суммой


async def test_confirm_with_no_known_debt_checks_nothing(fx):
    reply = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)

    assert "Проверено: 0" in reply.text
    assert fx.provider.requested_plates == []


# ---- next-refresh candidate logic на уровне conversation (задача п.5/п.9) ----


async def test_next_refresh_candidates_exclude_car_that_became_zero(fx):
    task_a = fx.make_task("A1111AA")
    task_b = fx.make_task("B2222BB")
    await fx.seed_debt(task_a, amount=800, fingerprint="fp-a")
    await fx.seed_debt(task_b, amount=450, fingerprint="fp-b")
    _set_amount(fx.provider, car_number="A1111AA", amount=500, fingerprint="fp-a")
    _set_amount(fx.provider, car_number="B2222BB", amount=0, fingerprint="fp-b")

    await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)

    fx.provider.requested_plates.clear()
    pick_reply = fx.controller.handle_debt_refresh_pick(telegram_user_id=_TRUSTED_ID)
    assert "🔄 Будет проверено автомобилей: 1" in pick_reply.text

    await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)
    assert fx.provider.requested_plates == ["A1111AA"]


# ---- handle_debt_refresh_cancel ----


async def test_cancel_returns_cancelled_text_without_checking_anything(fx):
    task = fx.make_task("II999II")
    await fx.seed_debt(task, amount=40)
    fx.provider.requested_plates.clear()

    reply = fx.controller.handle_debt_refresh_cancel(telegram_user_id=_TRUSTED_ID)

    assert reply.text == texts.DEBT_REFRESH_CANCELLED_TEXT
    assert reply.show_main_menu is True
    assert fx.provider.requested_plates == []


async def test_ordinary_user_cancel_is_still_authorized_checked(fx):
    reply = fx.controller.handle_debt_refresh_cancel(telegram_user_id=_ORDINARY_ID)
    assert reply.text == texts.CALLBACK_NOT_AUTHORIZED_TEXT
