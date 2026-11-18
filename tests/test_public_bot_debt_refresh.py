"""Тесты ConversationController.handle_debt_refresh_pick/confirm/cancel/
handle_debt_list_page и "🚨 Штрафы по последней проверке" в 📊 Статистика
(@ProtocolGEbot, задача "manager Statistics / refresh для обоих ботов") —
сквозные, через РЕАЛЬНЫЙ ConversationController. Repository — настоящие
(SQLite/tmp_path), FineProvider — лёгкий фейк.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from telethon.extensions import markdown
from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl.types import MessageEntityBold

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import UNLIMITED_END_DATE, ParsedFineRecord
from reader.fines.provider import FineProvider
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.plate_display import format_plate_for_telegram
from reader.public_bot import texts
from reader.public_bot.conversation import (
    _DEBT_LIST_PAGE_CHAR_BUDGET,
    _DEBT_LIST_PAGE_SIZE,
    ConversationController,
    _paginate_debt_blocks,
)
from reader.public_bot.conversation_state_repository import (
    BotConversationStateRepository,
)
from reader.public_bot.debt_refresh_service import DebtRefreshService
from reader.public_bot.handlers import _TELEGRAM_SAFE_TEXT_LIMIT, _split_telegram_text
from reader.public_bot.known_users_repository import (
    BotKnownUsersRepository,
)
from reader.public_bot.statistics_service import BotStatisticsService
from reader.public_bot.subscription_repository import (
    FineSubscriptionRepository,
)
from reader.public_bot.subscription_service import SubscriptionService
from reader.users.models import TelegramUserInfo
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
        """Действующая подписка — бессрочная, как у всех живых подписок
        после отмены выбора срока мониторинга."""
        self.subscription_repository.create(
            monitoring_task_id=task.id, car_number=task.car_number,
            telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id,
            telegram_username=None, start_date=task.start_date, end_date=UNLIMITED_END_DATE,
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


def _debt_block(car_number: str, amount: str, *owner_lines: str) -> str:
    """Ожидаемый блок машины debt-списка (см. texts.format_debt_row)."""
    return "\n".join([
        f"🚗 Госномер: {format_plate_for_telegram(car_number)}",
        f"💰 Задолженность: {amount}",
        *owner_lines,
    ])


def _block_of(text: str, car_number: str) -> str:
    """Блок одной машины из ответа — от "🚗 Госномер: <номер>" до пустой
    строки (блоки разделены пустой строкой, см. format_debt_list_section)."""
    start = text.index(f"🚗 Госномер: {format_plate_for_telegram(car_number)}")
    end = text.find("\n\n", start)
    return text[start:] if end == -1 else text[start:end]


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


# ---- denylisted car_number (см. reader/fines/suppression.py, задача про
# приватность конкретного автомобиля) — 📊 Статистика не должна показать
# задолженность по этому номеру, даже если она была записана ДО появления
# denylist'а ----


async def test_statistics_does_not_show_debt_row_for_suppressed_plate(fx):
    task = fx.make_task("O687KE761")
    # Симулирует задолженность, персистентную ДО появления denylist'а —
    # прямой repository-вызов, не check_task() (который сейчас уже
    # отказался бы её туда записать).
    fx.task_repository.record_successful_check(task.id, total_amount=98765)

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "O687KE761" not in reply.text
    assert "98765" not in reply.text
    # Агрегат честно показывает "0 машин/0 ₾" — denylisted задолженность не
    # просто скрыта из построчного списка, а полностью исключена из
    # list_debt_car_groups(), из которого строится и сам агрегат.
    assert "Автомобилей: 0" in reply.text
    assert "Общая сумма: 0 ₾" in reply.text


async def test_statistics_still_shows_unrelated_plate_debt_alongside_suppressed_one(fx):
    suppressed_task = fx.make_task("O687KE761")
    fx.task_repository.record_successful_check(suppressed_task.id, total_amount=98765)
    unrelated_task = fx.make_task("P004XC163")
    await fx.seed_debt(unrelated_task, amount=40)

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "O687KE761" not in reply.text
    assert _debt_block("P004XC163", "40 ₾", "👤 Пользователь не привязан") in reply.text


# ---- формат блока машины: госномер / задолженность / пользователь / Telegram
# (задача "дополнить статистику штрафов информацией о пользователях") ----


async def test_row_format_is_plate_debt_user_telegram_block(fx):
    task = fx.make_task("AA111AA")
    await fx.seed_debt(task, amount=800)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, username="ao777oa777", first_name="Иван", last_name="Петров")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert _block_of(reply.text, "AA111AA") == (
        f"🚗 Госномер: {format_plate_for_telegram('AA111AA')}\n"
        "💰 Задолженность: 800 ₾\n"
        "👤 Пользователь: Иван Петров\n"
        "🔗 Telegram: @ao777oa777"
    )


async def test_row_has_no_checked_at_date_or_today_yesterday_wording(fx):
    task = fx.make_task("BB222BB")
    await fx.seed_debt(task, amount=50)

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    block = _block_of(reply.text, "BB222BB")
    assert "сегодня" not in block
    assert "вчера" not in block
    assert " — " not in block
    assert "· " not in block
    assert block == _debt_block("BB222BB", "50 ₾", "👤 Пользователь не привязан")


async def test_car_without_linked_user_shows_not_linked(fx):
    task = fx.make_task("CC333CC")
    await fx.seed_debt(task, amount=50)
    # Ни add_subscription, ни record_known не вызываются вовсе.

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert _block_of(reply.text, "CC333CC") == _debt_block("CC333CC", "50 ₾", "👤 Пользователь не привязан")
    assert "🔗 Telegram" not in reply.text
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

    assert _block_of(reply.text, "DD444DD") == _debt_block(
        "DD444DD", "120 ₾", "👤 Пользователь: Дамир Татаров", "🔗 Telegram: @DamirTat",
    )


async def test_owner_display_username_only(fx):
    task = fx.make_task("EE555EE")
    await fx.seed_debt(task, amount=50)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, username="DamirTat")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert _block_of(reply.text, "EE555EE") == _debt_block(
        "EE555EE", "50 ₾", "👤 Пользователь: Имя не указано", "🔗 Telegram: @DamirTat",
    )


async def test_owner_display_name_only(fx):
    task = fx.make_task("FF666FF")
    await fx.seed_debt(task, amount=50)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, first_name="Иван", last_name="Иванов")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert _block_of(reply.text, "FF666FF") == _debt_block(
        "FF666FF", "50 ₾", "👤 Пользователь: Иван Иванов", "🔗 Telegram: Логин не указан",
    )


async def test_owner_display_dot_name_falls_back_to_username_only(fx):
    """См. задачу п.2 — placeholder-имя "." не показывается как имя,
    username при этом НЕ теряется."""
    task = fx.make_task("GG777GG")
    await fx.seed_debt(task, amount=350)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, username="Roma12312", first_name=".")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert _block_of(reply.text, "GG777GG") == _debt_block(
        "GG777GG", "350 ₾", "👤 Пользователь: Имя не указано", "🔗 Telegram: @Roma12312",
    )
    assert "👤 Пользователь: ." not in reply.text


async def test_owner_display_dash_username_and_underscore_sanitized(fx):
    task = fx.make_task("HH888HH")
    await fx.seed_debt(task, amount=10)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, username="someone", first_name="-", last_name="_")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert _block_of(reply.text, "HH888HH") == _debt_block(
        "HH888HH", "10 ₾", "👤 Пользователь: Имя не указано", "🔗 Telegram: @someone",
    )


async def test_linked_user_who_never_wrote_to_bot_shows_both_placeholders(fx):
    """Подписка есть (пользователь привязан), но профиля нет ни в
    bot_known_users, ни в users — это НЕ "не привязан"."""
    task = fx.make_task("JJ999JJ")
    await fx.seed_debt(task, amount=70)
    fx.add_subscription(task, telegram_user_id=42)

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert _block_of(reply.text, "JJ999JJ") == _debt_block(
        "JJ999JJ", "70 ₾", "👤 Пользователь: Имя не указано", "🔗 Telegram: Логин не указан",
    )


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
            if format_plate_for_telegram(plate) in page_reply.text:
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
    assert _debt_block("FF666FF", "500 ₾", "👤 Пользователь не привязан") in reply.text
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
    assert format_plate_for_telegram("HH888HH") in reply.text  # упомянута в failed-списке
    # осталась в списке со СТАРОЙ суммой
    assert _debt_block("HH888HH", "2740 ₾", "👤 Пользователь не привязан") in reply.text


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


# ---- Georgia debt deduplication by physical car_number (см. задачу) ----


async def test_one_plate_one_task_shows_one_row(fx):
    task = fx.make_task("TT001TT")
    await fx.seed_debt(task, amount=200)
    fx.add_subscription(task, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, username="alice")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "Автомобилей: 1" in reply.text
    assert _block_of(reply.text, "TT001TT") == _debt_block(
        "TT001TT", "200 ₾", "👤 Пользователь: Имя не указано", "🔗 Telegram: @alice",
    )


async def test_one_plate_two_tasks_same_owner_shows_one_row_owner_once(fx):
    """См. production-находку P004XC163 — тот же telegram-владелец
    добавил один и тот же номер дважды (две задачи) — владелец должен
    показаться ОДИН раз, не дважды."""
    task_a = fx.make_task("TT002TT")
    task_b = fx.make_task("TT002TT")
    await fx.seed_debt(task_a, amount=200, fingerprint="fp-a")
    await fx.seed_debt(task_b, amount=200, fingerprint="fp-a")
    fx.add_subscription(task_a, telegram_user_id=1)
    fx.add_subscription(task_b, telegram_user_id=1)  # ТОТ ЖЕ owner
    fx.record_known(telegram_user_id=1, username="bob")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "Автомобилей: 1" in reply.text
    block = _block_of(reply.text, "TT002TT")
    assert block == _debt_block("TT002TT", "200 ₾", "👤 Пользователь: Имя не указано", "🔗 Telegram: @bob")
    assert block.count("👤 Пользователь") == 1  # НЕ дважды "@bob"


async def test_one_plate_multiple_owners_each_listed_amount_once(fx):
    task_a = fx.make_task("TT003TT")
    task_b = fx.make_task("TT003TT")
    await fx.seed_debt(task_a, amount=300, fingerprint="fp-a")
    await fx.seed_debt(task_b, amount=300, fingerprint="fp-a")
    fx.add_subscription(task_a, telegram_user_id=1)
    fx.add_subscription(task_b, telegram_user_id=2)
    fx.record_known(telegram_user_id=1, username="alice")
    fx.record_known(telegram_user_id=2, username="carol")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    block = _block_of(reply.text, "TT003TT")
    assert block == _debt_block(
        "TT003TT", "300 ₾",
        "👤 Пользователь: Имя не указано", "🔗 Telegram: @carol",
        "👤 Пользователь: Имя не указано", "🔗 Telegram: @alice",
    )
    assert block.count("300 ₾") == 1
    assert "Общая сумма: 300 ₾" in reply.text


async def test_two_different_plates_sum_normally(fx):
    task_a = fx.make_task("TT004TT")
    task_b = fx.make_task("TT005TT")
    await fx.seed_debt(task_a, amount=100)
    await fx.seed_debt(task_b, amount=250)

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "Автомобилей: 2" in reply.text
    assert "Общая сумма: 350 ₾" in reply.text


async def test_manager_summary_duplicate_task_rows_do_not_inflate_car_count_or_total(fx):
    """См. production снимок: P004XC163 (2 tasks, 200 ₾ каждая) не должен
    выглядеть как "2 автомобиля / 400 ₾"."""
    task_a = fx.make_task("TT006TT")
    task_b = fx.make_task("TT006TT")
    await fx.seed_debt(task_a, amount=200, fingerprint="fp-a")
    await fx.seed_debt(task_b, amount=200, fingerprint="fp-a")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "Автомобилей: 1" in reply.text
    assert "Общая сумма: 200 ₾" in reply.text  # НЕ 400 ₾


async def test_refresh_preview_counts_unique_plates_for_duplicate_tasks(fx):
    """raw debt rows=2 для одного и того же car_number -> preview должен
    показать 1 автомобиль, не 2 (см. задачу Part 3)."""
    task_a = fx.make_task("TT007TT")
    task_b = fx.make_task("TT007TT")
    await fx.seed_debt(task_a, amount=50, fingerprint="fp-a")
    await fx.seed_debt(task_b, amount=50, fingerprint="fp-a")
    fx.provider.requested_plates.clear()

    reply = fx.controller.handle_debt_refresh_pick(telegram_user_id=_TRUSTED_ID)

    assert reply.text == "🔄 Будет проверено автомобилей: 1"
    assert fx.provider.requested_plates == []


async def test_refresh_notification_eligibility_not_duplicated_by_fan_out_persistence(fx):
    """См. задачу Part 6/Part 9 п.13 — refresh() для 2 задач одного
    car_number с ОДНИМ новым штрафом должен создать РОВНО 2
    detected_fines-строки (по одной на КАЖДУЮ задачу — та же независимая
    per-task notification-семантика, что и при вызове check_task()
    отдельно для каждой), НЕ 4 (не задваивается общим fetch) и НЕ 1 (не
    теряется для второй задачи)."""
    task_a = fx.make_task("TT008TT")
    task_b = fx.make_task("TT008TT")
    # Обе задачи уже видели fp-old (100 ₾) — остаются debt-кандидатами,
    # чтобы refresh() их вообще выбрал.
    await fx.seed_debt(task_a, amount=100, fingerprint="fp-old")
    await fx.seed_debt(task_b, amount=100, fingerprint="fp-old")
    # Новая проверка находит ДОПОЛНИТЕЛЬНЫЙ, ранее невиданный штраф fp-new —
    # ГЕНУИННО новый для ОБЕИХ задач (ни одна ещё не видела fp-new).
    fx.provider.records_by_car["TT008TT"] = [
        _record(car_number="TT008TT", fingerprint="fp-old", amount=100),
        _record(car_number="TT008TT", fingerprint="fp-new", amount=50),
    ]

    await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)

    rows = fx.detected_fine_repository._conn.execute(
        "SELECT monitoring_task_id, notification_sent_at FROM detected_fines WHERE fingerprint = 'fp-new'",
    ).fetchall()
    assert len(rows) == 2  # ровно по одной строке на КАЖДУЮ задачу, не 4 и не 1
    task_ids_with_new_fine = {row[0] for row in rows}
    assert task_ids_with_new_fine == {task_a.id, task_b.id}
    for _task_id, notification_sent_at in rows:
        assert notification_sent_at is None  # обычная semantics "ждёт уведомления" — НЕ silent


async def test_active_and_completed_duplicate_plate_shows_as_one_car(fx):
    """См. задачу Part 7 — НЕ меняем status-фильтрацию, но группировка
    должна дать ОДНУ карточку для active+completed дубля одного номера
    (тот же сценарий, что и production P004XC163)."""
    task_active = fx.make_task("TT009TT")
    task_completed = fx.make_task("TT009TT")
    await fx.seed_debt(task_active, amount=200, fingerprint="fp-a")
    await fx.seed_debt(task_completed, amount=200, fingerprint="fp-a")
    fx.task_repository.set_status(task_completed.id, "completed")

    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert "Автомобилей: 1" in reply.text


# ---- пользователи в debt-списке (задача "дополнить статистику штрафов
# информацией о пользователях") ----


async def _open_statistics(fx, telegram_user_id: int = _TRUSTED_ID):
    return await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=telegram_user_id, telegram_user_id=telegram_user_id, username=None,
    )


def _create_subscription(fx, task, *, telegram_user_id: int, end_date: date = UNLIMITED_END_DATE, **kwargs):
    return fx.subscription_repository.create(
        monitoring_task_id=task.id, car_number=task.car_number,
        telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id,
        telegram_username=None, start_date=task.start_date, end_date=end_date, **kwargs,
    )


async def test_all_subscribers_of_one_task_are_listed_and_debt_shown_once(fx):
    """Раньше на задачу показывался один представитель — теперь ВСЕ
    пользователи, отслеживающие машину; сумма и итоги не дублируются."""
    task = fx.make_task("KK100KK")
    await fx.seed_debt(task, amount=250)
    fx.add_subscription(task, telegram_user_id=1)
    fx.add_subscription(task, telegram_user_id=2)
    fx.record_known(telegram_user_id=1, username="ivan_petrov", first_name="Иван", last_name="Петров")
    fx.record_known(telegram_user_id=2, first_name="Анна")

    reply = await _open_statistics(fx)

    # Порядок — как у Search: новые подписки первыми.
    assert _block_of(reply.text, "KK100KK") == _debt_block(
        "KK100KK", "250 ₾",
        "👤 Пользователь: Анна", "🔗 Telegram: Логин не указан",
        "👤 Пользователь: Иван Петров", "🔗 Telegram: @ivan_petrov",
    )
    assert "Автомобилей: 1" in reply.text
    assert "Общая сумма: 250 ₾" in reply.text
    assert reply.text.count("250 ₾") == 2  # итог + одна строка задолженности, без дублей на владельца


async def test_sums_order_and_totals_unchanged_with_users_on_several_cars(fx):
    task_a = fx.make_task("KK110KK")
    task_b = fx.make_task("KK120KK")
    await fx.seed_debt(task_a, amount=100, fingerprint="fp-a")
    await fx.seed_debt(task_b, amount=40.5, fingerprint="fp-b")
    fx.add_subscription(task_a, telegram_user_id=1)
    fx.add_subscription(task_a, telegram_user_id=2)
    fx.add_subscription(task_b, telegram_user_id=1)
    fx.record_known(telegram_user_id=1, username="one")
    fx.record_known(telegram_user_id=2, username="two")

    reply = await _open_statistics(fx)

    assert "Автомобилей: 2" in reply.text
    assert "Общая сумма: 140.5 ₾" in reply.text
    assert "💰 Задолженность: 100 ₾" in _block_of(reply.text, "KK110KK")
    assert "💰 Задолженность: 40.5 ₾" in _block_of(reply.text, "KK120KK")
    # Сортировка прежняя — по сумме по убыванию.
    plate_a = format_plate_for_telegram("KK110KK")
    plate_b = format_plate_for_telegram("KK120KK")
    assert reply.text.index(plate_a) < reply.text.index(plate_b)


async def test_subscriber_of_other_task_of_same_plate_is_listed(fx):
    """Привязка — по физическому номеру: долг известен по одной задаче, а
    клиент подписан на другую задачу того же номера (ещё без успешной
    проверки, поэтому её нет в debt-группе)."""
    debt_task = fx.make_task("KK200KK")
    await fx.seed_debt(debt_task, amount=90)
    client_task = fx.make_task("KK200KK")
    fx.add_subscription(client_task, telegram_user_id=3)
    fx.record_known(telegram_user_id=3, username="client3", first_name="Нино")

    reply = await _open_statistics(fx)

    assert _block_of(reply.text, "KK200KK") == _debt_block(
        "KK200KK", "90 ₾", "👤 Пользователь: Нино", "🔗 Telegram: @client3",
    )


async def test_pending_claim_and_archived_subscription_are_not_linked_users(fx):
    task = fx.make_task("KK300KK")
    await fx.seed_debt(task, amount=60)
    fx.subscription_repository.create_pending_claim(
        monitoring_task_id=task.id, car_number=task.car_number, owner_username_hint="invited_client",
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_ID,
        start_date=task.start_date, end_date=task.end_date,
        claim_token="token-kk300", claim_token_expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    archived = _create_subscription(fx, task, telegram_user_id=4)
    fx.subscription_repository.archive(archived.id)
    fx.record_known(telegram_user_id=4, username="deleted_owner", first_name="Гога")

    reply = await _open_statistics(fx)

    assert _block_of(reply.text, "KK300KK") == _debt_block("KK300KK", "60 ₾", "👤 Пользователь не привязан")
    assert "invited_client" not in reply.text
    assert "deleted_owner" not in reply.text


async def test_manager_who_added_car_without_client_is_never_shown_as_its_user(fx):
    task = fx.task_repository.create(
        car_number="KK400KK", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        telegram_chat_id=_TRUSTED_ID, created_by_user_id=_TRUSTED_ID,
    )
    await fx.seed_debt(task, amount=30)
    fx.record_known(telegram_user_id=_TRUSTED_ID, username="manager_login", first_name="Менеджер")
    fx.user_repository.upsert(TelegramUserInfo(
        user_id=_TRUSTED_ID, username="manager_login", first_name="Менеджер", last_name=None,
    ))

    reply = await _open_statistics(fx)

    assert _block_of(reply.text, "KK400KK") == _debt_block("KK400KK", "30 ₾", "👤 Пользователь не привязан")
    assert "manager_login" not in reply.text
    assert "Менеджер" not in reply.text


async def test_delegated_car_shows_client_not_manager(fx):
    task = fx.make_task("KK500KK")
    await fx.seed_debt(task, amount=45)
    _create_subscription(
        fx, task, telegram_user_id=5,
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_ID,
    )
    fx.record_known(telegram_user_id=5, username="client5", first_name="Клиент")
    fx.record_known(telegram_user_id=_TRUSTED_ID, username="manager_login", first_name="Менеджер")

    reply = await _open_statistics(fx)

    assert _block_of(reply.text, "KK500KK") == _debt_block(
        "KK500KK", "45 ₾", "👤 Пользователь: Клиент", "🔗 Telegram: @client5",
    )
    assert "manager_login" not in reply.text


async def test_profile_falls_back_to_users_table_for_client_who_never_wrote_to_bot(fx):
    """Клиенту машину добавил менеджер, сам клиент боту не писал (нет в
    bot_known_users) — имя/логин из users, сохранённые при добавлении."""
    task = fx.make_task("KK600KK")
    await fx.seed_debt(task, amount=80)
    fx.add_subscription(task, telegram_user_id=6)
    fx.user_repository.upsert(TelegramUserInfo(user_id=6, username="nino_b", first_name="Нино", last_name="Беридзе"))

    reply = await _open_statistics(fx)

    assert _block_of(reply.text, "KK600KK") == _debt_block(
        "KK600KK", "80 ₾", "👤 Пользователь: Нино Беридзе", "🔗 Telegram: @nino_b",
    )


async def test_bot_known_profile_takes_priority_and_missing_parts_come_from_users(fx):
    task_fresh = fx.make_task("KK700KK")
    task_partial = fx.make_task("KK710KK")
    await fx.seed_debt(task_fresh, amount=80, fingerprint="fp-a")
    await fx.seed_debt(task_partial, amount=70, fingerprint="fp-b")
    fx.add_subscription(task_fresh, telegram_user_id=7)
    fx.add_subscription(task_partial, telegram_user_id=8)
    fx.record_known(telegram_user_id=7, username="fresh_login", first_name="Свежее", last_name="Имя")
    fx.user_repository.upsert(TelegramUserInfo(user_id=7, username="old_login", first_name="Старое", last_name="Имя"))
    fx.record_known(telegram_user_id=8, username="only_login")
    fx.user_repository.upsert(TelegramUserInfo(user_id=8, username="other_login", first_name="Леван", last_name=None))

    reply = await _open_statistics(fx)

    assert _block_of(reply.text, "KK700KK") == _debt_block(
        "KK700KK", "80 ₾", "👤 Пользователь: Свежее Имя", "🔗 Telegram: @fresh_login",
    )
    assert _block_of(reply.text, "KK710KK") == _debt_block(
        "KK710KK", "70 ₾", "👤 Пользователь: Леван", "🔗 Telegram: @only_login",
    )
    assert "old_login" not in reply.text
    assert "other_login" not in reply.text


async def test_owner_name_and_username_cannot_inject_telegram_markdown(fx):
    task = fx.make_task("KK800KK")
    await fx.seed_debt(task, amount=15)
    fx.add_subscription(task, telegram_user_id=9)
    fx.record_known(
        telegram_user_id=9, username="evil__user", first_name="**Иван** `x`", last_name="[Нажми](http://evil.example)",
    )

    reply = await _open_statistics(fx)

    plain_text, entities = markdown.parse(reply.text)
    surrogated = add_surrogate(plain_text)
    bold_texts = {del_surrogate(surrogated[e.offset:e.offset + e.length]) for e in entities}
    assert all(isinstance(entity, MessageEntityBold) for entity in entities)
    assert bold_texts == {"KK"}  # только буквы госномера, имя ничего не форматирует
    visible = plain_text.replace("​", "")
    assert "👤 Пользователь: **Иван** ˋxˋ [Нажми](http://evil.example)" in visible
    # "__" в настоящем Telegram username невозможен — это не логин, и он не
    # показывается (а не вставляется с невидимым символом, который увёл бы
    # упоминание на другой аккаунт).
    assert "🔗 Telegram: Логин не указан" in visible
    assert "evil__user" not in visible


async def test_debt_list_sql_query_count_does_not_grow_with_cars_and_owners(fx):
    """Без N+1: подписки, bot_known_users и users читаются фиксированным
    числом запросов, сколько бы машин и владельцев ни было на странице."""
    statements: list[str] = []
    for repository in (fx.subscription_repository, fx.known_users_repository, fx.user_repository):
        repository._conn.set_trace_callback(statements.append)

    async def count_statistics_queries() -> int:
        statements.clear()
        await _open_statistics(fx)
        return len(statements)

    first = fx.make_task("NA000AA")
    await fx.seed_debt(first, amount=10, fingerprint="fp-0")
    fx.add_subscription(first, telegram_user_id=1000)
    fx.record_known(telegram_user_id=1000, username="u1000")
    queries_for_one_car = await count_statistics_queries()

    for i in range(1, 10):
        task = fx.make_task(f"NA{i:03d}AA")
        await fx.seed_debt(task, amount=10 + i, fingerprint=f"fp-{i}")
        for j in range(3):
            user_id = 1000 + i * 10 + j
            fx.add_subscription(task, telegram_user_id=user_id)
            fx.record_known(telegram_user_id=user_id, username=f"u{user_id}")
    reply = await _open_statistics(fx)
    queries_for_ten_cars = await count_statistics_queries()

    assert reply.text.count("👤 Пользователь:") == 1 + 9 * 3
    assert queries_for_ten_cars == queries_for_one_car


async def test_client_screens_never_show_other_users_of_the_same_car(fx):
    """Имя/логин пользователей машины видит только менеджер в 📊
    Статистика — не обычный клиент: ни в "Мои авто", ни в карточке своей
    машины, ни при попытке открыть статистику."""
    task = fx.make_task("KK900KK")
    await fx.seed_debt(task, amount=500)
    own = _create_subscription(fx, task, telegram_user_id=_ORDINARY_ID)
    fx.add_subscription(task, telegram_user_id=2)
    fx.record_known(telegram_user_id=_ORDINARY_ID, username="me_client", first_name="Я")
    fx.record_known(telegram_user_id=2, username="other_owner", first_name="Чужой", last_name="Владелец")

    replies = [
        await fx.controller.handle_text(
            texts.MY_CARS_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID, username=None,
        ),
        fx.controller.handle_my_car_open(own.id, 0, telegram_user_id=_ORDINARY_ID),
        await _open_statistics(fx, telegram_user_id=_ORDINARY_ID),
    ]

    for reply in replies:
        assert reply is not None
        everything_shown = repr(reply)  # текст И все кнопки/опции ответа
        assert "other_owner" not in everything_shown
        assert "Чужой" not in everything_shown
        assert "👤 Пользователь" not in everything_shown
        assert "🔗 Telegram" not in everything_shown


async def test_manager_own_subscription_is_never_shown_as_car_user(fx):
    """Собственная (legacy/тестовая) подписка trusted-оператора на машину
    клиента — не "пользователь машины": показывается только клиент."""
    task = fx.make_task("KK410KK")
    await fx.seed_debt(task, amount=35)
    fx.add_subscription(task, telegram_user_id=_TRUSTED_ID)
    fx.add_subscription(task, telegram_user_id=11)
    fx.record_known(telegram_user_id=_TRUSTED_ID, username="manager_login", first_name="Менеджер")
    fx.record_known(telegram_user_id=11, username="client11", first_name="Клиент")

    reply = await _open_statistics(fx)

    assert _block_of(reply.text, "KK410KK") == _debt_block(
        "KK410KK", "35 ₾", "👤 Пользователь: Клиент", "🔗 Telegram: @client11",
    )
    assert "manager_login" not in reply.text


async def test_car_linked_only_to_manager_shows_not_linked(fx):
    task = fx.make_task("KK420KK")
    await fx.seed_debt(task, amount=35)
    fx.add_subscription(task, telegram_user_id=_TRUSTED_ID)
    fx.record_known(telegram_user_id=_TRUSTED_ID, username="manager_login", first_name="Менеджер")

    reply = await _open_statistics(fx)

    assert _block_of(reply.text, "KK420KK") == _debt_block("KK420KK", "35 ₾", "👤 Пользователь не привязан")
    assert "manager_login" not in reply.text


async def test_resold_car_shows_current_subscriber_not_former_one(fx):
    task = fx.make_task("KK430KK")
    await fx.seed_debt(task, amount=75)
    former = _create_subscription(fx, task, telegram_user_id=21)
    assert fx.subscription_repository.stop_by_owner_or_creator(former.id, telegram_user_id=21)
    _create_subscription(fx, task, telegram_user_id=22)
    fx.record_known(telegram_user_id=21, username="former_owner", first_name="Бывший")
    fx.record_known(telegram_user_id=22, username="new_owner", first_name="Новый")

    reply = await _open_statistics(fx)

    assert _block_of(reply.text, "KK430KK") == _debt_block(
        "KK430KK", "75 ₾", "👤 Пользователь: Новый", "🔗 Telegram: @new_owner",
    )
    assert "former_owner" not in reply.text


async def test_user_with_notifications_off_is_shown_when_nobody_is_active(fx):
    """Уведомления выключены, но машина по-прежнему в "Мои авто"
    пользователя — он привязан, это не "не привязан"."""
    task = fx.make_task("KK440KK")
    await fx.seed_debt(task, amount=25)
    stopped = _create_subscription(fx, task, telegram_user_id=23)
    assert fx.subscription_repository.stop_by_owner_or_creator(stopped.id, telegram_user_id=23)
    fx.record_known(telegram_user_id=23, username="paused_owner", first_name="Пауза")

    reply = await _open_statistics(fx)

    assert _block_of(reply.text, "KK440KK") == _debt_block(
        "KK440KK", "25 ₾", "👤 Пользователь: Пауза", "🔗 Telegram: @paused_owner",
    )


async def test_expired_subscriber_is_shown_only_when_nobody_is_active(fx):
    task_with_active = fx.make_task("KK450KK")
    task_only_expired = fx.make_task("KK460KK")
    await fx.seed_debt(task_with_active, amount=65, fingerprint="fp-a")
    await fx.seed_debt(task_only_expired, amount=55, fingerprint="fp-b")
    _create_subscription(fx, task_with_active, telegram_user_id=24, end_date=date(2026, 9, 1))
    _create_subscription(fx, task_with_active, telegram_user_id=25)
    _create_subscription(fx, task_only_expired, telegram_user_id=24, end_date=date(2026, 9, 1))
    assert fx.subscription_repository.expire_elapsed(today=date(2026, 10, 1)) == 2
    fx.record_known(telegram_user_id=24, username="expired_owner")
    fx.record_known(telegram_user_id=25, username="active_owner")

    reply = await _open_statistics(fx)

    assert _block_of(reply.text, "KK450KK") == _debt_block(
        "KK450KK", "65 ₾", "👤 Пользователь: Имя не указано", "🔗 Telegram: @active_owner",
    )
    assert _block_of(reply.text, "KK460KK") == _debt_block(
        "KK460KK", "55 ₾", "👤 Пользователь: Имя не указано", "🔗 Telegram: @expired_owner",
    )


async def test_debt_pages_with_many_long_named_users_stay_single_messages(fx):
    """Длинные блоки (3 пользователя с максимальными Telegram-именами) —
    на странице меньше 10 машин, но каждая страница остаётся ОДНИМ
    сообщением (handlers не режет её, пагинация редактирует целую
    страницу), каждая машина — ровно на одной странице, а границы страниц
    у 📊 Статистика и у итога проверки одинаковые."""
    plates = [f"LP{i:03d}AA" for i in range(12)]
    for i, plate in enumerate(plates):
        task = fx.make_task(plate)
        await fx.seed_debt(task, amount=100 + i, fingerprint=f"fp-{i}")
        for j in range(3):
            user_id = 5000 + i * 10 + j
            fx.add_subscription(task, telegram_user_id=user_id)
            fx.record_known(
                telegram_user_id=user_id, username=f"user_{user_id}", first_name="Я" * 64, last_name="Ж" * 64,
            )

    first_page = await _open_statistics(fx)
    total_pages = first_page.debt_list_total_pages
    assert total_pages > 2  # не 2 страницы по 10 машин

    seen: list[str] = []
    for page in range(total_pages):
        reply = fx.controller.handle_debt_list_page(page, telegram_user_id=_TRUSTED_ID)
        assert len(reply.text) <= _TELEGRAM_SAFE_TEXT_LIMIT
        assert _split_telegram_text(reply.text) == [reply.text]
        seen.extend(plate for plate in plates if format_plate_for_telegram(plate) in reply.text)
    assert sorted(seen) == sorted(plates)

    refresh_reply = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)
    assert refresh_reply.debt_list_total_pages == total_pages
    assert _split_telegram_text(refresh_reply.text) == [refresh_reply.text]


async def test_mass_failed_refresh_with_long_blocks_stays_single_message(fx):
    """police.ge/REDNET недоступен — не проверилась ни одна машина: список
    непроверенных номеров ограничен ("и ещё N"), поэтому итог вместе с
    многострочной страницей остаётся одним сообщением; счётчик точный."""
    plates = [f"MF{i:03d}AA" for i in range(45)]
    for i, plate in enumerate(plates):
        task = fx.make_task(plate)
        await fx.seed_debt(task, amount=100 + i, fingerprint=f"fp-{i}")
        for j in range(3):
            user_id = 7000 + i * 10 + j
            fx.add_subscription(task, telegram_user_id=user_id)
            fx.record_known(
                telegram_user_id=user_id, username=f"user_{user_id}", first_name="Я" * 64, last_name="Ж" * 64,
            )

    class _ErrorProvider:
        async def search_by_plate(self, plate: str):
            from reader.fines.provider import FineProviderError
            raise FineProviderError("simulated")

    fx.check_service._provider = _ErrorProvider()

    reply = await fx.controller.handle_debt_refresh_confirm(telegram_user_id=_TRUSTED_ID)

    assert "⚠️ Не удалось проверить: 45" in reply.text
    failed_line = next(line for line in reply.text.splitlines() if line.startswith("Не удалось проверить: "))
    assert failed_line.endswith(" и ещё 25")
    assert failed_line.count(", ") == 19  # ровно 20 номеров
    assert _split_telegram_text(reply.text) == [reply.text]


async def test_active_status_with_past_end_date_counts_as_inactive(fx):
    """status='active', но срок уже прошёл, а expire_elapsed() ещё не
    прошёлся по строке — подписка не действующая (is_effectively_active),
    поэтому при действующем пользователе она не показывается, а без него —
    показывается как остальные неактивные."""
    task_with_active = fx.make_task("KK470KK")
    task_only_lapsed = fx.make_task("KK480KK")
    await fx.seed_debt(task_with_active, amount=45, fingerprint="fp-a")
    await fx.seed_debt(task_only_lapsed, amount=40, fingerprint="fp-b")
    _create_subscription(fx, task_with_active, telegram_user_id=26, end_date=date(2026, 9, 1))
    _create_subscription(fx, task_with_active, telegram_user_id=27)
    _create_subscription(fx, task_only_lapsed, telegram_user_id=26, end_date=date(2026, 9, 1))
    fx.record_known(telegram_user_id=26, username="lapsed_owner")
    fx.record_known(telegram_user_id=27, username="current_owner")

    reply = await _open_statistics(fx)

    assert _block_of(reply.text, "KK470KK") == _debt_block(
        "KK470KK", "45 ₾", "👤 Пользователь: Имя не указано", "🔗 Telegram: @current_owner",
    )
    assert _block_of(reply.text, "KK480KK") == _debt_block(
        "KK480KK", "40 ₾", "👤 Пользователь: Имя не указано", "🔗 Telegram: @lapsed_owner",
    )


async def test_out_of_range_debt_page_is_clamped_with_length_based_pages(fx):
    for i in range(12):
        task = fx.make_task(f"OR{i:03d}AA")
        await fx.seed_debt(task, amount=10 + i, fingerprint=f"fp-{i}")

    first = await _open_statistics(fx)
    last_page = first.debt_list_total_pages - 1

    assert fx.controller.handle_debt_list_page(99, telegram_user_id=_TRUSTED_ID).debt_list_page == last_page
    assert fx.controller.handle_debt_list_page(-1, telegram_user_id=_TRUSTED_ID).debt_list_page == 0


def test_paginate_debt_blocks_by_car_count_and_length():
    assert _paginate_debt_blocks([]) == []

    tiny = [f"b{i}" for i in range(_DEBT_LIST_PAGE_SIZE + 1)]
    assert _paginate_debt_blocks(tiny) == [tiny[:_DEBT_LIST_PAGE_SIZE], tiny[_DEBT_LIST_PAGE_SIZE:]]

    half = (_DEBT_LIST_PAGE_CHAR_BUDGET - 2) // 2  # два блока + "\n\n" ровно в бюджет
    exact = ["a" * half, "b" * (_DEBT_LIST_PAGE_CHAR_BUDGET - 2 - half)]
    assert _paginate_debt_blocks(exact) == [exact]
    over = [exact[0], exact[1] + "b"]
    assert _paginate_debt_blocks(over) == [[over[0]], [over[1]]]

    huge = "h" * (_DEBT_LIST_PAGE_CHAR_BUDGET + 100)
    assert _paginate_debt_blocks(["x", huge, "y"]) == [["x"], [huge], ["y"]]
