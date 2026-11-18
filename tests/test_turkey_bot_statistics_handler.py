"""Тесты reader/turkey_bot/conversation.py::ConversationController.handle_statistics
(см. задачу "доработать 📊 Статистика Turkey bot") — сквозные, через
РЕАЛЬНЫЙ ConversationController (не изолированные texts.py/
statistics_service.py unit-тесты, см. tests/test_turkey_bot_texts_statistics.py
/tests/test_turkey_statistics_service.py) — покрывает orchestration
(get_debt_car_groups + get_known_profiles + форматирование), authorization и,
КРИТИЧЕСКИ ВАЖНО, отсутствие любых provider/network вызовов."""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.plate_display import format_plate_for_telegram
from reader.turkey_bot import texts
from reader.turkey_bot.conversation import ConversationController
from reader.turkey_bot.conversation_state_repository import (
    TurkeyConversationStateRepository,
)
from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.statistics_service import TurkeyStatisticsService
from reader.turkey_bot.unified.models import (
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    total_amount_for,
)
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository

pytestmark = pytest.mark.asyncio

_TRUSTED_ID = 5712994689
_ORDINARY_ID = 685137235
_TZ = ZoneInfo("UTC")


class _Fixture:
    def __init__(self, *, trusted_operator_user_ids=frozenset({_TRUSTED_ID})):
        self.states = TurkeyConversationStateRepository(":memory:")
        self.garage = TurkeyUserCarsRepository(":memory:")
        self.runs = TurkeyCheckRunRepository(":memory:")
        self.subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
        self.known_users = TurkeyBotKnownUsersRepository(":memory:")
        self.statistics = TurkeyStatisticsService(self.known_users, self.runs, self.subscriptions)
        # check_service=None — намеренно: handle_statistics НЕ должен
        # обращаться к нему вообще (см. задачу "КРИТИЧЕСКИ ВАЖНО: НИКАКИХ
        # LIVE CHECK") — если бы он это сделал, тест упал бы с
        # AttributeError('NoneType' object has no attribute ...), а не
        # тихо прошёл.
        self.controller = ConversationController(
            self.states, self.garage, self.runs, self.subscriptions, self.statistics, check_service=None,
            trusted_operator_user_ids=frozenset(trusted_operator_user_ids), tz=_TZ,
        )

    def add_car(self, *, telegram_user_id: int, car_number: str) -> None:
        self.garage.add_car(telegram_user_id=telegram_user_id, car_number=car_number)

    def record_known(
        self, *, telegram_user_id: int, username: str | None = None,
        first_name: str | None = None, last_name: str | None = None,
    ) -> None:
        self.known_users.record_seen(
            telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, telegram_username=username,
            first_name=first_name, last_name=last_name,
        )

    def save_run(
        self, *, telegram_user_id: int, plate: str, providers: tuple[ProviderCheckResult, ...],
        days_ago: int = 0,
    ) -> None:
        now = datetime.now(timezone.utc)
        result = UnifiedCheckResult(
            plate=plate, started_at=now, finished_at=now,
            overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers),
            providers=providers,
        )
        run_id = self.runs.save(
            result, telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, initiator="manual",
        )
        finished_at = (now - timedelta(days=days_ago)).isoformat()
        self.runs._conn.execute("UPDATE turkey_check_runs SET finished_at = ? WHERE id = ?", (finished_at, run_id))
        self.runs._conn.commit()


def _provider(status: ProviderStatus, amount: Decimal, *, provider: str = "gib") -> ProviderCheckResult:
    return ProviderCheckResult(
        provider=provider, status=status, debt_count=1 if status == ProviderStatus.HAS_DEBT else 0,
        principal_amount=amount if status == ProviderStatus.HAS_DEBT else Decimal(0),
        penalty_amount=Decimal(0), total_amount=amount if status == ProviderStatus.HAS_DEBT else Decimal(0),
        items=(), error_type="transport_error" if status == ProviderStatus.ERROR else None,
        checked_at=datetime.now(timezone.utc),
    )


def _full_check(gib_amount: Decimal) -> tuple[ProviderCheckResult, ...]:
    """Полная проверка: GİB с долгом + KGM без долга. Run только с GİB
    означал бы "по KGM нет достоверных данных", и строка списка получила
    бы пометку ⚠️ и пояснения по провайдерам (см. задачу "Turkey debt list:
    последний успешный результат отдельно по каждому провайдеру и
    госномеру") — эти тесты проверяют только отображение владельца."""
    return (_provider(ProviderStatus.HAS_DEBT, gib_amount), _provider(ProviderStatus.NO_DEBT, Decimal(0), provider="kgm"))


@pytest.fixture
def fx():
    return _Fixture()


# ---- authorization ----


async def test_ordinary_user_cannot_open_statistics(fx):
    """См. handle_text: STATISTICS_LABEL требует self._is_trusted(...) —
    для обычного пользователя условие ложно, текст трактуется как
    попытка ввести номер (тот же безопасный fallback, что и у
    STOP_MONITORING_LABEL/SEARCH_LABEL, см. задачу п.12: "не менять
    существующие trusted checks") — никакой статистики/задолженности не
    раскрывается."""
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, username="alenaogir")
    fx.save_run(telegram_user_id=777, plate="M295YB196", providers=(_provider(ProviderStatus.HAS_DEBT, Decimal(500)),))

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID)

    assert "Пользователи" not in reply.text
    assert "Задолженность" not in reply.text
    assert "@alenaogir" not in reply.text
    assert "M295YB196" not in reply.text
    assert not reply.extra_texts


async def test_trusted_user_opens_statistics(fx):
    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    assert texts.STATISTICS_LABEL in reply.text
    assert "👥 Пользователи" in reply.text
    assert "🚨 Задолженность по последней проверке" in reply.text


# ---- no network/provider calls (see class docstring re: check_service=None) ----


async def test_statistics_does_not_call_check_service(fx):
    """См. фикстуру _Fixture — check_service=None. Если бы handle_statistics
    когда-либо обращался к нему (GİB/Avrasya/KGM/unified check), этот вызов
    упал бы с AttributeError, а не молча прошёл."""
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.save_run(telegram_user_id=777, plate="M295YB196", providers=(_provider(ProviderStatus.HAS_DEBT, Decimal(500)),))

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    assert format_plate_for_telegram("M295YB196") in "".join((reply.text, *reply.extra_texts))


# ---- username list removed ----


async def test_statistics_no_longer_lists_individual_usernames(fx):
    fx.record_known(telegram_user_id=111, username="alenaogir")
    fx.record_known(telegram_user_id=222, username="Mihailov_vm")

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    full_text = "\n".join((reply.text, *reply.extra_texts))
    assert "@alenaogir" not in full_text
    assert "@Mihailov_vm" not in full_text


# ---- owner display variants (см. задачу п.7) ----


async def test_debt_owner_shown_with_name_and_username(fx):
    fx.add_car(telegram_user_id=1, car_number="M295YB196")
    fx.record_known(telegram_user_id=1, username="ivanov_i", first_name="Иван", last_name="Иванов")
    fx.save_run(telegram_user_id=1, plate="M295YB196", providers=(_provider(ProviderStatus.HAS_DEBT, Decimal(2740)),))

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    full_text = "\n".join((reply.text, *reply.extra_texts))
    assert "👤 Пользователь: Иван Иванов\n🔗 Telegram: @ivanov_i" in full_text


def _debt_block(car_number: str, amount: str, *owner_lines: str) -> str:
    """Ожидаемый блок машины debt-списка (см. texts.format_debt_row) — для
    полной проверки (GİB + KGM, без строк по провайдерам)."""
    return "\n".join([
        f"🚗 Госномер: {format_plate_for_telegram(car_number)}",
        f"💰 Задолженность: {amount}",
        *owner_lines,
    ])


async def _statistics_text(fx, telegram_user_id: int = _TRUSTED_ID) -> str:
    reply = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=telegram_user_id, telegram_user_id=telegram_user_id,
    )
    return "\n".join((reply.text, *reply.extra_texts))


async def test_debt_owner_shown_with_name_only(fx):
    fx.add_car(telegram_user_id=1, car_number="M295YB196")
    fx.record_known(telegram_user_id=1, first_name="Иван", last_name="Иванов")
    fx.save_run(telegram_user_id=1, plate="M295YB196", providers=_full_check(Decimal(750)))

    full_text = await _statistics_text(fx)

    assert _debt_block(
        "M295YB196", "750 ₺", "👤 Пользователь: Иван Иванов", "🔗 Telegram: Логин не указан",
    ) in full_text


async def test_debt_owner_shown_with_username_only(fx):
    fx.add_car(telegram_user_id=1, car_number="M295YB196")
    fx.record_known(telegram_user_id=1, username="anna_k")
    fx.save_run(telegram_user_id=1, plate="M295YB196", providers=_full_check(Decimal(1250)))

    full_text = await _statistics_text(fx)

    assert _debt_block(
        "M295YB196", "1 250 ₺", "👤 Пользователь: Имя не указано", "🔗 Telegram: @anna_k",
    ) in full_text


async def test_debt_owner_without_profile_shows_both_placeholders(fx):
    fx.add_car(telegram_user_id=1, car_number="M295YB196")
    # record_known НЕ вызывается вовсе.
    fx.save_run(telegram_user_id=1, plate="M295YB196", providers=_full_check(Decimal(100)))

    full_text = await _statistics_text(fx)

    assert _debt_block(
        "M295YB196", "100 ₺", "👤 Пользователь: Имя не указано", "🔗 Telegram: Логин не указан",
    ) in full_text
    assert "None" not in full_text


# ---- пользователи в debt-списке (задача "дополнить статистику штрафов
# информацией о пользователях") ----


async def test_several_owners_of_one_plate_each_listed_and_debt_shown_once(fx):
    for user_id in (1, 2):
        fx.add_car(telegram_user_id=user_id, car_number="34ABC123")
    fx.record_known(telegram_user_id=1, username="mehmet_k", first_name="Мехмет", last_name="Кая")
    fx.record_known(telegram_user_id=2, first_name="Айше")
    fx.save_run(telegram_user_id=1, plate="34ABC123", providers=_full_check(Decimal(2500)))

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)
    full_text = "\n".join((reply.text, *reply.extra_texts))

    # Порядок — как у manager car list/Search: новые записи гаража первыми.
    assert _debt_block(
        "34ABC123", "2 500 ₺",
        "👤 Пользователь: Айше", "🔗 Telegram: Логин не указан",
        "👤 Пользователь: Мехмет Кая", "🔗 Telegram: @mehmet_k",
    ) in full_text
    assert "Автомобилей: 1" in reply.text
    assert "Общая сумма: 2 500 ₺" in reply.text
    assert full_text.count("2 500 ₺") == 2  # итог + одна строка задолженности, без дублей на владельца


async def test_sums_order_and_totals_unchanged_with_owner_lines(fx):
    fx.add_car(telegram_user_id=1, car_number="34AAA111")
    fx.add_car(telegram_user_id=2, car_number="34BBB222")
    fx.add_car(telegram_user_id=3, car_number="34BBB222")
    fx.save_run(telegram_user_id=1, plate="34AAA111", providers=_full_check(Decimal(300)))
    fx.save_run(telegram_user_id=2, plate="34BBB222", providers=_full_check(Decimal("1200.50")))

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)
    debt_list = "\n".join(reply.extra_texts)

    assert "Автомобилей: 2" in reply.text
    assert "Общая сумма: 1 500,50 ₺" in reply.text
    # Сортировка прежняя — по сумме по убыванию.
    assert debt_list.index(format_plate_for_telegram("34BBB222")) < debt_list.index(format_plate_for_telegram("34AAA111"))
    assert debt_list.count("💰 Задолженность:") == 2


async def test_owner_text_from_profile_cannot_inject_telegram_markdown(fx):
    from telethon.extensions import markdown
    from telethon.tl.types import MessageEntityBold

    fx.add_car(telegram_user_id=1, car_number="34ABC123")
    fx.record_known(telegram_user_id=1, username="evil__user", first_name="**Мехмет**", last_name="[Нажми](http://evil.example)")
    fx.save_run(telegram_user_id=1, plate="34ABC123", providers=_full_check(Decimal(500)))

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    for message in reply.extra_texts:
        plain_text, entities = markdown.parse(message)
        assert all(isinstance(entity, MessageEntityBold) for entity in entities)
    plain_text, entities = markdown.parse("\n".join(reply.extra_texts))
    assert len(entities) == 1  # только буквы госномера ABC
    visible = plain_text.replace("​", "")
    assert "👤 Пользователь: **Мехмет** [Нажми](http://evil.example)" in visible
    assert "🔗 Telegram: Логин не указан" in visible  # "__" — не настоящий Telegram username


async def test_manager_garage_row_is_never_shown_as_car_user(fx):
    """Менеджер добавляет машину только в СВОЙ гараж — как пользователь
    машины он не показывается (только клиент; без клиента — "не
    привязан")."""
    fx.add_car(telegram_user_id=_TRUSTED_ID, car_number="34ABC123")
    fx.add_car(telegram_user_id=7, car_number="34ABC123")
    fx.add_car(telegram_user_id=_TRUSTED_ID, car_number="34XYZ789")
    fx.record_known(telegram_user_id=_TRUSTED_ID, username="manager_login", first_name="Менеджер")
    fx.record_known(telegram_user_id=7, username="client7", first_name="Клиент")
    fx.save_run(telegram_user_id=7, plate="34ABC123", providers=_full_check(Decimal(800)))
    fx.save_run(telegram_user_id=_TRUSTED_ID, plate="34XYZ789", providers=_full_check(Decimal(600)))

    full_text = await _statistics_text(fx)

    assert _debt_block("34ABC123", "800 ₺", "👤 Пользователь: Клиент", "🔗 Telegram: @client7") in full_text
    assert _debt_block("34XYZ789", "600 ₺", "👤 Пользователь не привязан") in full_text
    assert "manager_login" not in full_text
    assert "Менеджер" not in full_text


async def test_debt_list_profile_queries_do_not_grow_with_cars_and_owners(fx):
    """Без N+1: профили владельцев читаются фиксированным числом запросов,
    сколько бы машин и владельцев ни было в списке."""
    statements: list[str] = []
    fx.known_users._conn.set_trace_callback(statements.append)

    async def count_profile_queries() -> int:
        statements.clear()
        await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)
        return len(statements)

    fx.add_car(telegram_user_id=1000, car_number="34NA000")
    fx.record_known(telegram_user_id=1000, username="u1000")
    fx.save_run(telegram_user_id=1000, plate="34NA000", providers=_full_check(Decimal(100)))
    queries_for_one_car = await count_profile_queries()

    for i in range(1, 10):
        plate = f"34NA{i:03d}"
        for j in range(3):
            user_id = 1000 + i * 10 + j
            fx.add_car(telegram_user_id=user_id, car_number=plate)
            fx.record_known(telegram_user_id=user_id, username=f"u{user_id}")
        fx.save_run(telegram_user_id=1000 + i * 10, plate=plate, providers=_full_check(Decimal(100 + i)))
    full_text = await _statistics_text(fx)
    queries_for_ten_cars = await count_profile_queries()

    assert full_text.count("👤 Пользователь:") == 1 + 9 * 3
    assert queries_for_ten_cars == queries_for_one_car


async def test_client_screens_never_show_other_owners_of_the_same_car(fx):
    """Имя/логин владельцев машины видит только менеджер в 📊 Статистика —
    не обычный клиент: ни в "Мои авто", ни в карточке своей машины, ни при
    попытке открыть статистику."""
    fx.add_car(telegram_user_id=_ORDINARY_ID, car_number="34ABC123")
    fx.add_car(telegram_user_id=2, car_number="34ABC123")
    fx.record_known(telegram_user_id=_ORDINARY_ID, username="me_client", first_name="Я")
    fx.record_known(telegram_user_id=2, username="other_owner", first_name="Чужой", last_name="Владелец")
    fx.save_run(telegram_user_id=2, plate="34ABC123", providers=_full_check(Decimal(900)))
    own_car = next(car for car in fx.garage.list_cars(_ORDINARY_ID) if car.car_number == "34ABC123")

    replies = [
        fx.controller.handle_my_cars(chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID),
        fx.controller.handle_car_open(own_car.id, telegram_user_id=_ORDINARY_ID),
        await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID),
    ]

    for reply in replies:
        assert reply is not None
        everything_shown = repr(reply)  # текст, extra_texts И все кнопки/опции ответа
        assert "other_owner" not in everything_shown
        assert "Чужой" not in everything_shown
        assert "👤 Пользователь" not in everything_shown
        assert "🔗 Telegram" not in everything_shown


# ---- Telegram message limit (см. задачу п.11) ----


async def test_large_debt_list_splits_into_multiple_messages_without_truncation():
    fx = _Fixture()
    for i in range(500):
        fx.add_car(telegram_user_id=1000 + i, car_number=f"P{i:04d}AA01")
        fx.record_known(telegram_user_id=1000 + i, username=f"user{i}")
        fx.save_run(
            telegram_user_id=1000 + i, plate=f"P{i:04d}AA01",
            providers=(_provider(ProviderStatus.HAS_DEBT, Decimal(100 + i)),),
        )

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    assert len(reply.extra_texts) > 1
    for message in (reply.text, *reply.extra_texts):
        assert len(message) <= 4096

    full_text = "\n".join((reply.text, *reply.extra_texts))
    for i in range(500):
        assert format_plate_for_telegram(f"P{i:04d}AA01") in full_text  # никто не потерян/не обрезан
    assert "Автомобилей: 500" in reply.text
