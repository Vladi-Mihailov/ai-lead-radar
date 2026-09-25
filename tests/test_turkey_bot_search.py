"""Тесты reader/turkey_bot/conversation.py — manager/trusted Search (см.
задачу "manager/trusted Search") — ТОЛЬКО manager/trusted, self-service не
затронут.

В отличие от Georgian bot — Search здесь читает ПОСЛЕДНИЙ СОХРАНЁННЫЙ
unified-результат (TurkeyCheckRunRepository.get_latest_for_owner), никакой
live-проверки не запускает (см. задачу п.9: "переиспользовать
существующую production unified-total business logic", подтверждено
пользователем для Georgia specifically — для Turkey уже есть persisted
authoritative total_amount, использовать live check незачем)."""

import datetime as dt
from decimal import Decimal

import pytest

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
    DebtItem,
    OverallStatus,
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


def _provider(
    name: str, *, status: ProviderStatus, amount: Decimal = Decimal(0), items: tuple[DebtItem, ...] = (),
) -> ProviderCheckResult:
    now = dt.datetime.now(dt.timezone.utc)
    return ProviderCheckResult(
        provider=name, status=status,
        debt_count=len(items), principal_amount=amount if status == ProviderStatus.HAS_DEBT else Decimal(0),
        penalty_amount=Decimal(0), total_amount=amount,
        items=items, error_type="timeout" if status == ProviderStatus.ERROR else None,
        checked_at=now,
    )


def _make_unified_result(plate: str, providers: tuple[ProviderCheckResult, ...]) -> UnifiedCheckResult:
    """Итог/overall_status считаются ЧЕРЕЗ authoritative total_amount_for()/
    derive_overall_status() (см. задачу п.9/п.18: "переиспользовать
    существующую production unified-total business logic"), не вручную —
    та же функция, что использует реальный check flow при сохранении run."""
    now = dt.datetime.now(dt.timezone.utc)
    return UnifiedCheckResult(
        plate=plate, started_at=now, finished_at=now,
        overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers),
        providers=providers,
    )


class _Fixture:
    def __init__(self, *, trusted_operator_user_ids=frozenset({_TRUSTED_ID})):
        self.states = TurkeyConversationStateRepository(":memory:")
        self.garage = TurkeyUserCarsRepository(":memory:")
        self.runs = TurkeyCheckRunRepository(":memory:")
        self.subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
        self.known_users = TurkeyBotKnownUsersRepository(":memory:")
        self.statistics = TurkeyStatisticsService(self.known_users, self.runs, self.subscriptions)
        self.controller = ConversationController(
            self.states, self.garage, self.runs, self.subscriptions, self.statistics, check_service=None,
            trusted_operator_user_ids=frozenset(trusted_operator_user_ids),
        )

    def add_car(self, *, telegram_user_id: int, car_number: str) -> int:
        return self.garage.add_car(telegram_user_id=telegram_user_id, car_number=car_number).id

    def record_known(
        self,
        *,
        telegram_user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> None:
        self.known_users.record_seen(
            telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, telegram_username=username,
            first_name=first_name, last_name=last_name,
        )

    def save_run(self, *, telegram_user_id: int, result: UnifiedCheckResult) -> None:
        self.runs.save(
            result, telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, initiator="manual",
        )


@pytest.fixture
def fx():
    return _Fixture()


async def _open_search(fx) -> None:
    reply = await fx.controller.handle_text(
        texts.SEARCH_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID,
    )
    assert reply.search_prompt is True


async def _search(fx, query: str, *, telegram_user_id: int = _TRUSTED_ID):
    await _open_search(fx)
    return await fx.controller.handle_text(query, chat_id=telegram_user_id, telegram_user_id=telegram_user_id)


# ---- 1/2/3. menu layout (см. tests/test_turkey_bot_keyboards.py::
# test_manager_main_menu_layout / test_regular_user_main_menu_layout —
# keyboard-level, не дублируем здесь) ----


# ---- 4. trusted opens Search ----


async def test_trusted_opens_search(fx):
    reply = await fx.controller.handle_text(texts.SEARCH_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    assert reply.search_prompt is True
    assert reply.text == texts.SEARCH_ENTRY_TEXT
    assert fx.states.get(_TRUSTED_ID) is not None


# ---- 5. ordinary cannot open Search ----


async def test_ordinary_cannot_open_search(fx):
    reply = await fx.controller.handle_text(texts.SEARCH_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID)

    assert reply.search_prompt is False
    # SEARCH_LABEL текст сам по себе не проходит normalize_plate -> трактуется
    # как невалидный номер (тот же безопасный fallback, что и у STATISTICS_LABEL/
    # STOP_MONITORING_LABEL для не-trusted, см. handle_text).
    assert reply.search_result_shown is False


# ---- 6/7/8. @username / username / case-insensitive ----


async def test_search_by_username_with_at(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, username="Mihailov_vm")

    reply = await _search(fx, "@Mihailov_vm")

    assert reply.search_result_shown is True
    assert "M295YB196" in reply.text
    assert "@Mihailov_vm" in reply.text


async def test_search_by_username_without_at(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, username="Mihailov_vm")

    reply = await _search(fx, "Mihailov_vm")

    assert reply.search_result_shown is True
    assert "M295YB196" in reply.text


async def test_search_by_username_is_case_insensitive(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, username="Mihailov_vm")

    reply = await _search(fx, "@MIHAILOV_VM")

    assert reply.search_result_shown is True
    # Актуальный known username (корректный регистр), не введённый запрос as-is.
    assert "@Mihailov_vm" in reply.text


# ---- name search (см. задачу "add name search to trusted bot search") ----


async def test_search_by_first_name(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, first_name="Иван")

    reply = await _search(fx, "Иван")

    assert reply.search_result_shown is True
    assert "M295YB196" in reply.text
    assert "👤 Иван" in reply.text


async def test_search_by_first_name_is_case_insensitive(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, first_name="Иван")

    reply = await _search(fx, "иВАН")

    assert "M295YB196" in reply.text


async def test_search_by_last_name(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, first_name="Иван", last_name="Иванов")

    reply = await _search(fx, "Иванов")

    assert "M295YB196" in reply.text
    assert "👤 Иван Иванов" in reply.text


async def test_search_by_full_name(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, first_name="Иван", last_name="Иванов")

    reply = await _search(fx, "Иван Иванов")

    assert "M295YB196" in reply.text


async def test_search_by_full_name_is_case_insensitive(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, first_name="Иван", last_name="Иванов")

    reply = await _search(fx, "иван иванов")

    assert "M295YB196" in reply.text


async def test_search_by_name_finds_two_different_users_with_same_name(fx):
    """Имена НЕ уникальны (см. задачу п.6) — оба telegram_user_id
    показываются, не только первый попавшийся."""
    fx.add_car(telegram_user_id=111, car_number="AA001AA")
    fx.record_known(telegram_user_id=111, first_name="Иван", last_name="Иванов")
    fx.add_car(telegram_user_id=222, car_number="BB002BB")
    fx.record_known(telegram_user_id=222, username="ivan2", first_name="Иван")

    reply = await _search(fx, "Иван")

    assert "AA001AA" in reply.text
    assert "BB002BB" in reply.text
    assert "🔎 Результаты поиска" in reply.text


async def test_search_result_shows_name_and_username_together(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, username="ivanov_i", first_name="Иван", last_name="Иванов")

    reply = await _search(fx, "Иван Иванов")

    assert "👤 Иван Иванов (@ivanov_i)" in reply.text


async def test_search_result_shows_name_without_username(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, first_name="Иван", last_name="Иванов")

    reply = await _search(fx, "Иван Иванов")

    assert "👤 Иван Иванов" in reply.text
    assert "@" not in reply.text.split("👤 Иван Иванов")[1].split("\n")[0]


async def test_search_result_shows_username_without_name(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, username="mihailov_vm")

    reply = await _search(fx, "@mihailov_vm")

    assert "👤 @mihailov_vm" in reply.text


async def test_search_result_shows_dash_when_no_name_and_no_username(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    # record_known НЕ вызывается вовсе.

    reply = await _search(fx, "M295YB196")

    assert "👤 —" in reply.text
    assert "None" not in reply.text


async def test_search_by_bare_username_without_at_still_works(fx):
    """Регресс: bare username (без "@") по-прежнему находится, теперь
    через объединённый username+name lookup (см. задачу п.5/п.8: "не
    сломать существующий поиск")."""
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, username="mihailov_vm")

    reply = await _search(fx, "mihailov_vm")

    assert "M295YB196" in reply.text
    assert "@mihailov_vm" in reply.text


async def test_name_search_pagination_does_not_lose_matched_users(fx):
    """Пагинация не должна "терять" ни одного из НЕСКОЛЬКИХ разных
    telegram_user_id, найденных по имени (см. задачу п.10/п.6)."""
    for i in range(8):
        fx.add_car(telegram_user_id=1000 + i, car_number=f"I{i:03d}AA01")
        fx.record_known(telegram_user_id=1000 + i, first_name="Иван")

    await _open_search(fx)
    page0 = await fx.controller.handle_text("Иван", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)
    assert page0.search_query_type == "person"
    assert page0.search_total_pages == 1  # 8 машин, PAGE_SIZE=10 -> одна страница
    for i in range(8):
        assert f"I{i:03d}AA01" in page0.text


async def test_ordinary_user_cannot_use_name_search(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, first_name="Иван")

    menu_reply = await fx.controller.handle_text(texts.SEARCH_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID)
    assert menu_reply.search_prompt is False

    forged_page = fx.controller.handle_search_page("person", "Иван", 0, telegram_user_id=_ORDINARY_ID)
    assert forged_page is None


# ---- 9. Turkey plate normalization ----


async def test_search_by_plate_normalizes_input(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")

    reply = await _search(fx, "m295 yb196")

    assert reply.search_result_shown is True
    assert "M295YB196" in reply.text
    assert reply.text != texts.format_search_not_found("M295YB196")


# ---- 10. username -> multiple cars ----


async def test_search_by_username_shows_all_cars(fx):
    fx.add_car(telegram_user_id=777, car_number="AA001AA")
    fx.add_car(telegram_user_id=777, car_number="BB002BB")
    fx.record_known(telegram_user_id=777, username="alenaogir")

    reply = await _search(fx, "@alenaogir")

    assert "AA001AA" in reply.text
    assert "BB002BB" in reply.text
    assert "🔎 Результаты поиска" in reply.text


async def test_search_by_username_does_not_duplicate_plate_line(fx):
    """format_unified_check_result() уже открывается строкой "🚗 {plate}"
    — username-mode блок не должен добавлять её ещё раз поверх (см.
    texts.format_search_block докстрок про "🚗 X" два раза подряд)."""
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, username="alenaogir")
    result = _make_unified_result(
        "M295YB196", (
            _provider("gib", status=ProviderStatus.NO_DEBT),
            _provider("avrasya", status=ProviderStatus.NO_DEBT),
            _provider("kgm", status=ProviderStatus.NO_DEBT),
        ),
    )
    fx.save_run(telegram_user_id=777, result=result)

    reply = await _search(fx, "@alenaogir")

    assert reply.text.count("M295YB196") == 1


# ---- 11. plate -> correct owner ----


async def test_search_by_plate_shows_correct_owner(fx):
    fx.add_car(telegram_user_id=777, car_number="AA001AA")
    fx.add_car(telegram_user_id=888, car_number="BB002BB")
    fx.record_known(telegram_user_id=777, username="owner_one")
    fx.record_known(telegram_user_id=888, username="owner_two")

    reply = await _search(fx, "AA001AA")

    assert "@owner_one" in reply.text
    assert "@owner_two" not in reply.text


# ---- 12. duplicate plate -> multiple owners ----


async def test_search_by_duplicate_plate_shows_every_owner_separately(fx):
    fx.add_car(telegram_user_id=111, car_number="A123AA123")
    fx.add_car(telegram_user_id=222, car_number="A123AA123")
    fx.record_known(telegram_user_id=111, username="owner_one")
    fx.record_known(telegram_user_id=222, username="owner_two")

    reply = await _search(fx, "A123AA123")

    assert "@owner_one" in reply.text
    assert "@owner_two" in reply.text
    assert "🔎 Результаты поиска" in reply.text


# ---- 13. missing username -> — ----


async def test_search_missing_username_shows_dash(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    # record_known НЕ вызывается.

    reply = await _search(fx, "M295YB196")

    assert "👤 —" in reply.text
    assert "@None" not in reply.text
    assert "None" not in reply.text


# ---- 14. latest unified result selected correctly ----


async def test_search_shows_latest_run_not_an_older_one(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    old_result = _make_unified_result(
        "M295YB196", (
            _provider("gib", status=ProviderStatus.HAS_DEBT, amount=Decimal(80)),
            _provider("avrasya", status=ProviderStatus.NO_DEBT),
            _provider("kgm", status=ProviderStatus.NO_DEBT),
        ),
    )
    fx.save_run(telegram_user_id=777, result=old_result)
    new_result = _make_unified_result(
        "M295YB196", (
            _provider("gib", status=ProviderStatus.NO_DEBT),
            _provider("avrasya", status=ProviderStatus.NO_DEBT),
            _provider("kgm", status=ProviderStatus.NO_DEBT),
        ),
    )
    fx.save_run(telegram_user_id=777, result=new_result)

    reply = await _search(fx, "M295YB196")

    assert "Итого: 0" in reply.text or "0 ₺" in reply.text
    assert "80" not in reply.text


# ---- 15/16/17/18. GİB/Avrasya/KGM displayed correctly + authoritative total ----


async def test_search_shows_gib_avrasya_kgm_and_authoritative_total(fx):
    """Пример из задачи п.9: GİB=80, Avrasya=2580, KGM=2660 -> Итого=2740
    (Avrasya НЕ задваивается — total_amount_for() исключает её, её долг
    уже учтён внутри KGM)."""
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, username="Mihailov_vm")
    result = _make_unified_result(
        "M295YB196", (
            _provider("gib", status=ProviderStatus.HAS_DEBT, amount=Decimal(80)),
            _provider("avrasya", status=ProviderStatus.HAS_DEBT, amount=Decimal(2580)),
            _provider("kgm", status=ProviderStatus.HAS_DEBT, amount=Decimal(2660)),
        ),
    )
    assert result.total_amount == Decimal(2740)  # sanity: та же формула, что в проде
    fx.save_run(telegram_user_id=777, result=result)

    reply = await _search(fx, "M295YB196")

    assert "GİB" in reply.text
    assert "Avrasya" in reply.text or "Тунели" in reply.text
    assert "KGM" in reply.text or "Дороги" in reply.text
    assert "2 740" in reply.text or "2740" in reply.text
    assert "5 320" not in reply.text and "5320" not in reply.text  # НЕ задвоено


# ---- 19. PARTIAL remains PARTIAL semantics ----


async def test_search_partial_shows_partial_not_definitive_total(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    result = _make_unified_result(
        "M295YB196", (
            _provider("gib", status=ProviderStatus.HAS_DEBT, amount=Decimal(80)),
            _provider("avrasya", status=ProviderStatus.NO_DEBT),
            _provider("kgm", status=ProviderStatus.ERROR),
        ),
    )
    assert result.overall_status == OverallStatus.PARTIAL
    fx.save_run(telegram_user_id=777, result=result)

    reply = await _search(fx, "M295YB196")

    assert "может быть неполной" in reply.text


# ---- 20. ERROR does not become 0 ----


async def test_search_error_does_not_become_zero(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    result = _make_unified_result(
        "M295YB196", (
            _provider("gib", status=ProviderStatus.ERROR),
            _provider("avrasya", status=ProviderStatus.ERROR),
            _provider("kgm", status=ProviderStatus.ERROR),
        ),
    )
    assert result.overall_status == OverallStatus.ERROR
    fx.save_run(telegram_user_id=777, result=result)

    reply = await _search(fx, "M295YB196")

    assert "Итоговая сумма не определена" in reply.text
    assert "Итого: 0" not in reply.text


async def test_search_never_checked_shows_honest_placeholder_not_zero(fx):
    """Машина добавлена, но ни одной проверки ещё не было (см.
    get_latest_for_owner -> None) — честно "Ещё не проверялось", а НЕ
    подделанный 0 ₺/OK-статус."""
    fx.add_car(telegram_user_id=777, car_number="M295YB196")

    reply = await _search(fx, "M295YB196")

    assert "Ещё не проверялось" in reply.text


# ---- 21. not found ----


async def test_search_not_found(fx):
    reply = await _search(fx, "ZZZ999ZZ")

    assert reply.text == texts.format_search_not_found("ZZZ999ZZ")
    assert reply.search_result_shown is True


async def test_search_username_not_found(fx):
    reply = await _search(fx, "@nobody_at_all")

    assert reply.text == texts.format_search_not_found("@nobody_at_all")


# ---- 22. pagination ----


async def test_search_pagination_across_many_cars(fx):
    for i in range(15):
        fx.add_car(telegram_user_id=777, car_number=f"P{i:03d}AA01")
    fx.record_known(telegram_user_id=777, username="alenaogir")

    await _open_search(fx)
    page0 = await fx.controller.handle_text("@alenaogir", chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)
    assert page0.search_total_pages == 2
    assert page0.search_page == 0
    assert "P014AA01" in page0.text  # ORDER BY id DESC -> новые первыми
    assert "P004AA01" not in page0.text

    page1 = fx.controller.handle_search_page("username", "alenaogir", 1, telegram_user_id=_TRUSTED_ID)
    assert page1.search_page == 1
    assert "P000AA01" in page1.text
    assert "P014AA01" not in page1.text

    clamped_high = fx.controller.handle_search_page("username", "alenaogir", 999, telegram_user_id=_TRUSTED_ID)
    assert clamped_high.search_page == 1


# ---- 23. forged callbacks denied ----


async def test_search_callbacks_reject_ordinary_user(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")

    assert fx.controller.handle_search_page("car", "M295YB196", 0, telegram_user_id=_ORDINARY_ID) is None
    assert fx.controller.handle_search_new(chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID) is None
    assert fx.controller.handle_search_back(chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID) is None
    assert fx.controller.handle_search_page("person", "Иван", 0, telegram_user_id=_ORDINARY_ID) is None


async def test_search_state_input_rejects_ordinary_user_with_hijacked_state(fx):
    """Forged/устаревшее conversation_state для НЕ-trusted telegram_user_id
    (см. задачу п.14: "проверка не только при входе через меню")."""
    from reader.turkey_bot.conversation import _STEP_AWAITING_SEARCH_QUERY

    fx.states.set(chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID, step=_STEP_AWAITING_SEARCH_QUERY)

    reply = await fx.controller.handle_text("@anyone", chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID)

    assert reply.search_result_shown is False
    assert reply.show_main_menu is True


# ---- 24. Back/New Search ----


async def test_search_back_returns_to_main_menu(fx):
    await _open_search(fx)

    reply = fx.controller.handle_search_back(chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    assert reply.show_main_menu is True
    assert fx.states.get(_TRUSTED_ID) is None


async def test_search_new_returns_to_entry_prompt(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    await _search(fx, "M295YB196")

    reply = fx.controller.handle_search_new(chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    assert reply.search_prompt is True
    assert reply.text == texts.SEARCH_ENTRY_TEXT


# ---- underlying ⛔ Остановить мониторинг functionality NOT removed ----


async def test_stop_monitoring_underlying_flow_still_works_for_trusted(fx):
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.subscriptions.enable(
        telegram_user_id=777, telegram_chat_id=777, plate="M295YB196",
        next_check_at=dt.datetime.now(dt.timezone.utc),
    )

    reply = await fx.controller.handle_text(
        texts.STOP_MONITORING_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID,
    )

    assert "остановлен" in reply.text.lower()
    assert fx.subscriptions.get(telegram_user_id=777, plate="M295YB196").active is False
