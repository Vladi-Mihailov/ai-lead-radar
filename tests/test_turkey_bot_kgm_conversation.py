"""
Тесты reader/turkey_bot/conversation.py — ветка KGM (см. design report
"Реализация KGM provider") — структурная копия соответствующих Avrasya
тестов в tests/test_turkey_bot_conversation.py, но с ОТДЕЛЬНЫМ, локальным
helper'ом (_make_kgm_controller), чтобы не трогать существующий
_make_controller/70+ его вызовов. Реальная сеть/CAPTCHA здесь не
используются — KgmProvider подменяется лёгким фейком.
"""

import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot import texts  # noqa: E402
from reader.turkey_bot.avrasya.live_session_registry import (  # noqa: E402
    LiveAvrasyaSessionRegistry,
)
from reader.turkey_bot.check_repository import TurkeyCheckRepository  # noqa: E402
from reader.turkey_bot.conversation import ConversationController  # noqa: E402
from reader.turkey_bot.conversation_state_repository import (  # noqa: E402
    TurkeyConversationStateRepository,
)
from reader.turkey_bot.kgm.live_session_registry import (
    LiveKgmSessionRegistry,  # noqa: E402
)
from reader.turkey_bot.kgm.models import (  # noqa: E402
    KgmCaptchaChallenge,
    KgmDebtItem,
    KgmOperatorResult,
    KgmSubmitOutcome,
)
from reader.turkey_bot.kgm.session import KgmTransportError  # noqa: E402
from reader.turkey_bot.known_users_repository import (  # noqa: E402
    TurkeyBotKnownUsersRepository,
)
from reader.turkey_bot.live_session_registry import LiveGibSessionRegistry  # noqa: E402
from reader.turkey_bot.statistics_service import TurkeyStatisticsService  # noqa: E402
from reader.turkey_bot.toll_check_repository import (
    TurkeyTollCheckRepository,  # noqa: E402
)
from reader.turkey_bot.user_cars_repository import (
    TurkeyUserCarsRepository,  # noqa: E402
)

_CHAT_ID = 111
_USER_ID = 222


class _FakeCheckFactory:
    """Пустая GIB-фабрика — ни один тест этого файла не начинает GIB-
    проверку, но конструктор ConversationController требует check_factory."""

    def __call__(self):
        raise AssertionError("GIB check factory should not be used in KGM tests")


class _FakeKgmClient:
    def __init__(self):
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeKgmProvider:
    def __init__(
        self,
        *,
        start_challenge: KgmCaptchaChallenge | None = None,
        start_error: Exception | None = None,
        submit_results: list | None = None,
        refresh_results: list | None = None,
        refresh_error: Exception | None = None,
    ):
        self._start_challenge = start_challenge
        self._start_error = start_error
        self._submit_results = list(submit_results or [])
        self._refresh_results = list(refresh_results or [])
        self._refresh_error = refresh_error
        self.submit_calls: list[dict] = []
        self.refresh_calls = 0

    async def start(self) -> KgmCaptchaChallenge:
        if self._start_error is not None:
            raise self._start_error
        return self._start_challenge

    async def refresh_captcha(self) -> KgmCaptchaChallenge:
        self.refresh_calls += 1
        if self._refresh_error is not None:
            raise self._refresh_error
        return self._refresh_results.pop(0)

    async def submit(self, *, plate: str, captcha_code: str):
        self.submit_calls.append({"plate": plate, "captcha_code": captcha_code})
        result = self._submit_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeKgmCheckFactory:
    def __init__(self, providers: list[_FakeKgmProvider]):
        self._providers = list(providers)
        self.clients: list[_FakeKgmClient] = []

    def __call__(self):
        client = _FakeKgmClient()
        self.clients.append(client)
        provider = self._providers.pop(0)
        return client, provider


def _kgm_challenge(png: bytes = b"KGM-JPEG") -> KgmCaptchaChallenge:
    return KgmCaptchaChallenge(image_png=png)


def _make_kgm_controller(kgm_factory, *, trusted_operator_user_ids=frozenset()):
    states = TurkeyConversationStateRepository(":memory:")
    checks = TurkeyCheckRepository(":memory:")
    registry = LiveGibSessionRegistry()
    garage = TurkeyUserCarsRepository(":memory:")
    known_users = TurkeyBotKnownUsersRepository(":memory:")
    statistics = TurkeyStatisticsService(known_users, checks)
    avrasya_registry = LiveAvrasyaSessionRegistry()
    toll_checks = TurkeyTollCheckRepository(":memory:")
    kgm_registry = LiveKgmSessionRegistry()
    controller = ConversationController(
        states, checks, registry, garage, statistics, avrasya_registry, toll_checks,
        kgm_registry,
        check_factory=_FakeCheckFactory(), kgm_check_factory=kgm_factory,
        trusted_operator_user_ids=frozenset(trusted_operator_user_ids),
    )
    return controller, states, garage, toll_checks, kgm_registry


def _kgm_item(
    *, operator="kgm", entry="HENDEK", exit_="TOPAĞAÇ SGS", vehicle_class="1",
    base_toll=Decimal("40.00"), payable=Decimal("40.00"),
) -> KgmDebtItem:
    return KgmDebtItem(
        operator=operator, date_time=datetime(2026, 9, 15, 9, 39, 48),  # noqa: DTZ001
        entry_station=entry, exit_station=exit_, vehicle_class=vehicle_class,
        base_toll=base_toll, payable_amount=payable,
        penalty_free_deadline=date(2026, 9, 30),
    )


def _kgm_has_debt_outcome() -> KgmSubmitOutcome:
    kgm_op = KgmOperatorResult(
        operator_key="kgm", operator_name="KGM", items=(_kgm_item(),), subtotal=Decimal("40.00"),
    )
    return KgmSubmitOutcome(
        kind="has_debt", operators=(kgm_op,),
        kgm_total=Decimal("40.00"), yid_total=Decimal(0), grand_total=Decimal("40.00"),
    )


async def test_toll_provider_callback_kgm_then_plate_starts_kgm_captcha_flow():
    provider = _FakeKgmProvider(start_challenge=_kgm_challenge(png=b"KGM-PNG"))
    factory = _FakeKgmCheckFactory([provider])
    controller, states, _garage, _toll, _kgm_registry = _make_kgm_controller(factory)

    await controller.handle_toll_provider_callback("kgm", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.photo_png == b"KGM-PNG"
    assert reply.show_cancel_button is True
    state = states.get(_CHAT_ID)
    assert state.step == "awaiting_captcha_code"
    assert state.payload == {"plate": "34ABC123", "provider": "kgm"}


async def test_kgm_has_debt_for_normal_non_manager_user_includes_cta_buttons():
    """См. design report: CTA-кнопки для ВСЕХ пользователей, включая
    обычных non-manager, тем же способом, что и у GIB/Avrasya."""
    outcome = _kgm_has_debt_outcome()
    provider = _FakeKgmProvider(start_challenge=_kgm_challenge(), submit_results=[outcome])
    factory = _FakeKgmCheckFactory([provider])
    controller, _states, _garage, _toll, _kgm_registry = _make_kgm_controller(
        factory, trusted_operator_user_ids=frozenset(),
    )
    assert controller.is_trusted(_USER_ID) is False

    await controller.handle_toll_provider_callback("kgm", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("M295YB196", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("15 53893", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.cta_buttons is not None
    labels = [label for label, _url in reply.cta_buttons]
    assert "💳 Оплатить в рублях" in labels
    assert "🚗 ОСАГО Турции" in labels


async def test_kgm_has_debt_message_contains_operator_and_totals():
    outcome = _kgm_has_debt_outcome()
    provider = _FakeKgmProvider(start_challenge=_kgm_challenge(), submit_results=[outcome])
    factory = _FakeKgmCheckFactory([provider])
    controller, _states, _garage, _toll, _kgm_registry = _make_kgm_controller(factory)

    await controller.handle_toll_provider_callback("kgm", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("M295YB196", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("15 53893", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert "HENDEK" in reply.text
    assert "TOPAĞAÇ SGS" in reply.text
    assert "40 ₺" in reply.text
    assert reply.show_main_menu is True


async def test_kgm_has_debt_records_toll_check_with_provider_kgm():
    outcome = _kgm_has_debt_outcome()
    provider = _FakeKgmProvider(start_challenge=_kgm_challenge(), submit_results=[outcome])
    factory = _FakeKgmCheckFactory([provider])
    controller, _states, _garage, toll_checks, _kgm_registry = _make_kgm_controller(factory)

    await controller.handle_toll_provider_callback("kgm", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("M295YB196", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("15 53893", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert toll_checks.count_total() == 1
    assert toll_checks.count_by_status("has_debt") == 1


async def test_kgm_has_debt_adds_car_to_shared_garage():
    outcome = _kgm_has_debt_outcome()
    provider = _FakeKgmProvider(start_challenge=_kgm_challenge(), submit_results=[outcome])
    factory = _FakeKgmCheckFactory([provider])
    controller, _states, garage, _toll, _kgm_registry = _make_kgm_controller(factory)

    await controller.handle_toll_provider_callback("kgm", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("M295YB196", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("15 53893", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    cars = garage.list_cars(_USER_ID)
    assert [car.car_number for car in cars] == ["M295YB196"]


async def test_kgm_no_debt_shows_success_text_and_adds_car():
    outcome = KgmSubmitOutcome(
        kind="no_debt", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0),
    )
    provider = _FakeKgmProvider(start_challenge=_kgm_challenge(), submit_results=[outcome])
    factory = _FakeKgmCheckFactory([provider])
    controller, _states, garage, toll_checks, _kgm_registry = _make_kgm_controller(factory)

    await controller.handle_toll_provider_callback("kgm", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("codehere", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.kgm_no_debt_text("34ABC123")
    assert reply.cta_buttons is None
    assert toll_checks.count_by_status("no_debt") == 1
    assert [car.car_number for car in garage.list_cars(_USER_ID)] == ["34ABC123"]


async def test_kgm_rejected_captcha_shows_new_captcha_and_keeps_state():
    provider = _FakeKgmProvider(
        start_challenge=_kgm_challenge(png=b"FIRST"),
        submit_results=[KgmSubmitOutcome(kind="rejected", message="metin hatası")],
        refresh_results=[_kgm_challenge(png=b"SECOND")],
    )
    factory = _FakeKgmCheckFactory([provider])
    controller, states, _garage, toll_checks, _kgm_registry = _make_kgm_controller(factory)

    await controller.handle_toll_provider_callback("kgm", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("wrongcode", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.CAPTCHA_REJECTED_RETRY_TEXT
    assert reply.photo_png == b"SECOND"
    assert provider.refresh_calls == 1
    state = states.get(_CHAT_ID)
    assert state.step == "awaiting_captcha_code"
    # rejected НЕ пишется как завершённая проверка и НЕ трогает гараж (см.
    # design report: тот же принцип, что и у GIB/Avrasya rejected).
    assert toll_checks.count_total() == 0


async def test_kgm_unexpected_does_not_add_car_and_records_status():
    outcome = KgmSubmitOutcome(kind="unexpected", message="checksum mismatch")
    provider = _FakeKgmProvider(start_challenge=_kgm_challenge(), submit_results=[outcome])
    factory = _FakeKgmCheckFactory([provider])
    controller, _states, garage, toll_checks, _kgm_registry = _make_kgm_controller(factory)

    await controller.handle_toll_provider_callback("kgm", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("codehere", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.KGM_UNEXPECTED_TEXT
    assert toll_checks.count_by_status("unexpected") == 1
    assert garage.list_cars(_USER_ID) == []


async def test_kgm_transport_error_on_submit_shows_transport_error_text():
    provider = _FakeKgmProvider(
        start_challenge=_kgm_challenge(), submit_results=[KgmTransportError("boom")],
    )
    factory = _FakeKgmCheckFactory([provider])
    controller, _states, _garage, toll_checks, _kgm_registry = _make_kgm_controller(factory)

    await controller.handle_toll_provider_callback("kgm", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("codehere", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.KGM_TRANSPORT_ERROR_TEXT
    assert toll_checks.count_by_status("error") == 1


async def test_kgm_transport_error_on_start_shows_captcha_fetch_failed():
    provider = _FakeKgmProvider(start_error=KgmTransportError("boom"))
    factory = _FakeKgmCheckFactory([provider])
    controller, states, _garage, _toll, _kgm_registry = _make_kgm_controller(factory)

    await controller.handle_toll_provider_callback("kgm", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.KGM_CAPTCHA_FETCH_FAILED_TEXT
    assert states.get(_CHAT_ID) is None


async def test_kgm_restart_recovery_issues_fresh_captcha_for_stored_plate():
    """Аналог test_avrasya_restart_recovery_issues_fresh_captcha_for_stored_plate
    для KGM — живая сессия отсутствует (рестарт/idle-TTL), но persisted
    plate есть."""
    states = TurkeyConversationStateRepository(":memory:")
    states.set(
        _CHAT_ID, telegram_user_id=_USER_ID, step="awaiting_captcha_code",
        payload={"plate": "34ABC123", "provider": "kgm"},
    )
    new_provider = _FakeKgmProvider(start_challenge=_kgm_challenge(png=b"FRESH"))
    factory = _FakeKgmCheckFactory([new_provider])
    checks = TurkeyCheckRepository(":memory:")
    registry = LiveGibSessionRegistry()
    garage = TurkeyUserCarsRepository(":memory:")
    statistics = TurkeyStatisticsService(TurkeyBotKnownUsersRepository(":memory:"), checks)
    avrasya_registry = LiveAvrasyaSessionRegistry()
    toll_checks = TurkeyTollCheckRepository(":memory:")
    kgm_registry = LiveKgmSessionRegistry()
    controller = ConversationController(
        states, checks, registry, garage, statistics, avrasya_registry, toll_checks,
        kgm_registry,
        check_factory=_FakeCheckFactory(), kgm_check_factory=factory,
    )

    reply = await controller.handle_text("whatever-old-code", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.SESSION_EXPIRED_RETRY_TEXT
    assert reply.photo_png == b"FRESH"
    assert new_provider.submit_calls == []


async def test_cancel_clears_active_kgm_check_and_closes_client():
    provider = _FakeKgmProvider(start_challenge=_kgm_challenge())
    factory = _FakeKgmCheckFactory([provider])
    controller, states, _garage, _toll, kgm_registry = _make_kgm_controller(factory)

    await controller.handle_toll_provider_callback("kgm", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert await kgm_registry.get(_CHAT_ID) is not None

    reply = await controller.handle_cancel(chat_id=_CHAT_ID)

    assert reply.text == texts.CANCEL_CONFIRM_TEXT
    assert await kgm_registry.get(_CHAT_ID) is None
    assert states.get(_CHAT_ID) is None
    assert factory.clients[0].closed is True


async def test_garage_kgm_button_uses_stored_plate_without_retyping():
    provider = _FakeKgmProvider(start_challenge=_kgm_challenge(png=b"GARAGE-KGM"))
    factory = _FakeKgmCheckFactory([provider])
    controller, _states, garage, _toll, _kgm_registry = _make_kgm_controller(factory)
    garage.record_successful_check(telegram_user_id=_USER_ID, car_number="34ABC123")
    car = garage.list_cars(_USER_ID)[0]

    reply = await controller.handle_garage_check(
        car.id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID, provider="kgm",
    )

    assert reply is not None
    assert reply.photo_png == b"GARAGE-KGM"
    assert provider.submit_calls == []
