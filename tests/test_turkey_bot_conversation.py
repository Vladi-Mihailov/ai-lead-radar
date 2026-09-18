"""
Тесты reader/turkey_bot/conversation.py::ConversationController — линейный
цикл "номер -> CAPTCHA -> код -> результат" (см. design report Stage 3).
Реальная сеть НЕ используется вовсе — check_factory (см.
ConversationController.__init__) подменяется фейковым GibProvider/client,
её уже проверенная транспортная механика — предмет tests/test_turkey_gib_*.
"""

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot import texts  # noqa: E402
from reader.turkey_bot.avrasya.live_session_registry import (  # noqa: E402
    LiveAvrasyaSessionRegistry,
)
from reader.turkey_bot.avrasya.models import (  # noqa: E402
    AvrasyaCaptchaChallenge,
    AvrasyaDebtItem,
    AvrasyaMessage,
    AvrasyaSubmitOutcome,
)
from reader.turkey_bot.avrasya.session import (  # noqa: E402
    AvrasyaRateLimitedError,
    AvrasyaTransportError,
)
from reader.turkey_bot.check_repository import TurkeyCheckRepository  # noqa: E402
from reader.turkey_bot.conversation import ConversationController  # noqa: E402
from reader.turkey_bot.conversation_state_repository import (  # noqa: E402
    TurkeyConversationStateRepository,
)
from reader.turkey_bot.gib.models import (  # noqa: E402
    CaptchaChallenge,
    GibFineRecord,
    GibMessage,
    GibSubmitOutcome,
)
from reader.turkey_bot.gib.session import GibTransportError  # noqa: E402
from reader.turkey_bot.gib.translation import FineTranslationError  # noqa: E402
from reader.turkey_bot.kgm.live_session_registry import (
    LiveKgmSessionRegistry,  # noqa: E402
)
from reader.turkey_bot.known_users_repository import (
    TurkeyBotKnownUsersRepository,  # noqa: E402
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


class _FakeClient:
    def __init__(self):
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeProvider:
    """Один экземпляр = одна "живая GIB-сессия" в тесте — submit_calls и
    refresh_calls позволяют проверить, что дальнейшие действия
    (refresh_captcha после rejected) идут через ТОТ ЖЕ провайдер/клиент,
    а не создают новую сессию (см. design report Stage 3: "same in-flight
    GibSession must be used for submit")."""

    def __init__(
        self,
        *,
        start_challenge: CaptchaChallenge | None = None,
        start_error: bool = False,
        submit_results: list | None = None,
        refresh_results: list | None = None,
        refresh_error: bool = False,
    ):
        self._start_challenge = start_challenge
        self._start_error = start_error
        self._submit_results = list(submit_results or [])
        self._refresh_results = list(refresh_results or [])
        self._refresh_error = refresh_error
        self.submit_calls: list[dict] = []
        self.refresh_calls = 0

    async def start(self) -> CaptchaChallenge:
        if self._start_error:
            raise GibTransportError("boom")
        return self._start_challenge

    async def refresh_captcha(self) -> CaptchaChallenge:
        self.refresh_calls += 1
        if self._refresh_error:
            raise GibTransportError("boom")
        return self._refresh_results.pop(0)

    async def submit(self, *, plate: str, image_id: str, captcha_code: str):
        self.submit_calls.append({"plate": plate, "image_id": image_id, "captcha_code": captcha_code})
        result = self._submit_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeCheckFactory:
    """Очередь провайдеров, отдаваемых по одному на каждый вызов
    check_factory() (см. ConversationController) — большинство тестов
    используют ровно один провайдер на весь сценарий, restart-recovery
    тесты — второй, отдельный (новая GIB-сессия для того же номера)."""

    def __init__(self, providers: list[_FakeProvider]):
        self._providers = list(providers)
        self.clients: list[_FakeClient] = []

    def __call__(self):
        client = _FakeClient()
        self.clients.append(client)
        provider = self._providers.pop(0)
        return client, provider


def _make_controller(
    factory: _FakeCheckFactory, *, avrasya_factory=None, translator=None,
    trusted_operator_user_ids=frozenset(),
):
    states = TurkeyConversationStateRepository(":memory:")
    checks = TurkeyCheckRepository(":memory:")
    registry = LiveGibSessionRegistry()
    garage = TurkeyUserCarsRepository(":memory:")
    known_users = TurkeyBotKnownUsersRepository(":memory:")
    statistics = TurkeyStatisticsService(known_users, checks)
    avrasya_registry = LiveAvrasyaSessionRegistry()
    toll_checks = TurkeyTollCheckRepository(":memory:")
    # KGM (см. reader/turkey_bot/kgm/*) — ни один из существующих (GIB/
    # Avrasya-focused) тестов ниже не использует его напрямую, поэтому
    # достаточно default-реестра без фейковой фабрики (тот же принцип,
    # что и у tests/test_turkey_bot_kgm_conversation.py, где заводится
    # СВОЙ, отдельный helper с фейковым KgmProvider).
    kgm_registry = LiveKgmSessionRegistry()
    kwargs = {}
    if avrasya_factory is not None:
        kwargs["avrasya_check_factory"] = avrasya_factory
    controller = ConversationController(
        states, checks, registry, garage, statistics, avrasya_registry, toll_checks,
        kgm_registry,
        check_factory=factory, translator=translator,
        trusted_operator_user_ids=frozenset(trusted_operator_user_ids),
        **kwargs,
    )
    return controller, states, checks, registry, garage, avrasya_registry, toll_checks


def _challenge(image_id: str = "cid-1", png: bytes = b"PNG-BYTES") -> CaptchaChallenge:
    return CaptchaChallenge(image_id=image_id, image_png=png)


class _FakeAvrasyaClient:
    def __init__(self):
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeAvrasyaProvider:
    """Структурная копия _FakeProvider (см. выше) для Avrasya — БЕЗ
    image_id (см. avrasya/models.py — у Avrasya его нет вовсе)."""

    def __init__(
        self,
        *,
        start_challenge: AvrasyaCaptchaChallenge | None = None,
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

    async def start(self) -> AvrasyaCaptchaChallenge:
        if self._start_error is not None:
            raise self._start_error
        return self._start_challenge

    async def refresh_captcha(self) -> AvrasyaCaptchaChallenge:
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


class _FakeAvrasyaCheckFactory:
    def __init__(self, providers: list[_FakeAvrasyaProvider]):
        self._providers = list(providers)
        self.clients: list[_FakeAvrasyaClient] = []

    def __call__(self):
        client = _FakeAvrasyaClient()
        self.clients.append(client)
        provider = self._providers.pop(0)
        return client, provider


def _avrasya_challenge(png: bytes = b"AVRASYA-JPEG") -> AvrasyaCaptchaChallenge:
    return AvrasyaCaptchaChallenge(image_png=png)


async def test_new_valid_plate_fetches_captcha_and_persists_state():
    provider = _FakeProvider(start_challenge=_challenge(image_id="cid-1", png=b"PNG1"))
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    reply = await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.photo_png == b"PNG1"
    assert reply.show_cancel_button is True

    state = states.get(_CHAT_ID)
    assert state.step == "awaiting_captcha_code"
    assert state.payload == {"plate": "34ABC123", "image_id": "cid-1", "provider": "gib"}
    assert await registry.get(_CHAT_ID) is not None


async def test_invalid_plate_does_not_start_a_check():
    factory = _FakeCheckFactory([])
    controller, states, _checks, registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    reply = await controller.handle_text("!!!", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.INVALID_PLATE_TEXT
    assert reply.photo_png is None
    assert states.get(_CHAT_ID) is None
    assert await registry.get(_CHAT_ID) is None


async def test_captcha_fetch_transport_error_shows_safe_message_and_closes_client():
    provider = _FakeProvider(start_error=True)
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    reply = await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.CAPTCHA_FETCH_FAILED_TEXT
    assert states.get(_CHAT_ID) is None
    assert await registry.get(_CHAT_ID) is None
    assert factory.clients[0].closed is True


async def test_correct_code_no_debt_reports_success_and_clears_everything():
    outcome = GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None)
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome])
    factory = _FakeCheckFactory([provider])
    controller, states, checks, registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.no_debt_text("34ABC123")
    assert states.get(_CHAT_ID) is None
    assert await registry.get(_CHAT_ID) is None
    assert factory.clients[0].closed is True
    assert checks.count_total() == 1
    assert checks.count_by_status("no_debt") == 1

    assert provider.submit_calls == [
        {"plate": "34ABC123", "image_id": "cid-1", "captcha_code": "g8fyx"}
    ]


async def test_has_debt_shows_parsed_fine_and_never_leaks_raw_data():
    """Явное требование задачи: "do not expose raw GIB raw_data directly
    to Telegram users" - рендерится ТОЛЬКО из outcome.fines (уже
    типизированные GibFineRecord), raw_data (который в реальности несёт
    KK_HASH/KK_KIMLIK) остаётся ТОЛЬКО в TurkeyCheckRepository
    (server-side audit), никогда в тексте, который видит пользователь."""
    fine = GibFineRecord(
        protocol_no="MC00000000",
        plate="34ABC123",
        amount=Decimal("1000.00"),
        description="Example violation description",
        violation_date=date(2026, 8, 8),
        authority="EMNİYET GENEL MÜDÜRLÜĞÜ",
        late_fee=None,
        discount=None,
    )
    outcome = GibSubmitOutcome(
        kind="has_debt",
        messages=(),
        raw_data={
            "BORCLAR": [{"KK_HASH": "SECRET-HASH-VALUE", "KK_KIMLIK": "SECRET-KIMLIK-VALUE"}]
        },
        fines=(fine,),
    )
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome])
    factory = _FakeCheckFactory([provider])
    controller, _states, checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert "MC00000000" in reply.text
    assert "1000" in reply.text
    assert "08.08.2026" in reply.text
    assert "SECRET-HASH-VALUE" not in reply.text
    assert "SECRET-KIMLIK-VALUE" not in reply.text
    assert reply.extra_texts == ()

    row = checks._conn.execute(
        "SELECT raw_response, status FROM turkey_fine_checks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[1] == "has_debt"
    assert "SECRET-HASH-VALUE" in row[0]  # сырой ответ по-прежнему хранится server-side


class _FakeTranslator:
    def __init__(self, *, result=None, error=None):
        self._result = result
        self._error = error
        self.calls: list[tuple] = []

    async def translate_fines(self, fines):
        self.calls.append(fines)
        if self._error is not None:
            raise self._error
        return self._result if self._result is not None else fines


def _structured_fine(**overrides) -> GibFineRecord:
    defaults = {
        "protocol_no": "MC00000000", "plate": "34ABC123", "amount": Decimal("1000.00"),
        "description": "raw", "violation_date": date(2026, 8, 8), "authority": "ORG",
        "late_fee": None, "discount": None, "location": "Türkçe yer",
        "law_article": "51/2-B-2", "violation_description": "Türkçe ihlal",
        "location_ru": None, "violation_description_ru": None,
    }
    defaults.update(overrides)
    return GibFineRecord(**defaults)


async def test_has_debt_uses_translator_result_when_available():
    fine = _structured_fine()
    outcome = GibSubmitOutcome(kind="has_debt", messages=(), raw_data={}, fines=(fine,))
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome])
    factory = _FakeCheckFactory([provider])

    translated_fine = _structured_fine(
        location_ru="Русское место", violation_description_ru="Русское нарушение",
    )
    translator = _FakeTranslator(result=(translated_fine,))
    controller, _states, _checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory, translator=translator)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert "Русское место" in reply.text
    assert "Русское нарушение" in reply.text
    assert len(translator.calls) == 1


async def test_has_debt_falls_back_to_turkish_when_translator_fails():
    """Явное требование задачи: "Translation must NEVER make a successful
    GIB check fail" - сбой переводчика не должен ронять/портить ответ."""
    fine = _structured_fine()
    outcome = GibSubmitOutcome(kind="has_debt", messages=(), raw_data={}, fines=(fine,))
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome])
    factory = _FakeCheckFactory([provider])

    translator = _FakeTranslator(error=FineTranslationError("boom"))
    controller, _states, checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory, translator=translator)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert "Türkçe yer" in reply.text
    assert "Türkçe ihlal" in reply.text
    assert checks.count_by_status("has_debt") == 1  # результат всё равно засчитан


async def test_has_debt_without_translator_shows_turkish_text():
    fine = _structured_fine()
    outcome = GibSubmitOutcome(kind="has_debt", messages=(), raw_data={}, fines=(fine,))
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome])
    factory = _FakeCheckFactory([provider])
    controller, _states, _checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)  # translator=None по умолчанию

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert "Türkçe yer" in reply.text
    assert "Türkçe ihlal" in reply.text


async def test_rejected_code_reuses_same_provider_for_refresh_and_asks_again():
    rejected = GibSubmitOutcome(
        kind="rejected", messages=(GibMessage(type="ERROR", text="wrong code"),), raw_data=None,
    )
    provider = _FakeProvider(
        start_challenge=_challenge(image_id="cid-1", png=b"PNG1"),
        submit_results=[rejected],
        refresh_results=[_challenge(image_id="cid-2", png=b"PNG2")],
    )
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("wrongcode", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.CAPTCHA_REJECTED_RETRY_TEXT
    assert reply.photo_png == b"PNG2"
    assert reply.show_cancel_button is True

    state = states.get(_CHAT_ID)
    assert state is not None
    assert state.step == "awaiting_captcha_code"
    assert state.payload == {"plate": "34ABC123", "image_id": "cid-2", "provider": "gib"}

    # ТОТ ЖЕ провайдер/клиент - никакой новой GIB-сессии не создавалось.
    assert len(factory.clients) == 1
    assert provider.refresh_calls == 1
    assert factory.clients[0].closed is False
    assert (await registry.get(_CHAT_ID)).provider is provider


async def test_unexpected_outcome_shows_generic_error_and_records_it(caplog):
    outcome = GibSubmitOutcome(
        kind="unexpected", messages=(GibMessage(type="ERROR", text="never seen before"),), raw_data=None,
    )
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome])
    factory = _FakeCheckFactory([provider])
    controller, states, checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    with caplog.at_level("WARNING"):
        reply = await controller.handle_text("somecode", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.UNEXPECTED_ERROR_TEXT
    assert states.get(_CHAT_ID) is None
    assert checks.count_by_status("unexpected") == 1
    assert any("unexpected" in message.lower() for message in caplog.messages)
    # Логи - только сообщение GIB (не секрет), НИКОГДА не введённый код.
    assert not any("somecode" in message for message in caplog.messages)


async def test_submit_transport_error_shows_generic_error_and_records_error_status():
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[GibTransportError("boom")])
    factory = _FakeCheckFactory([provider])
    controller, states, checks, registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.TRANSPORT_ERROR_TEXT
    assert states.get(_CHAT_ID) is None
    assert await registry.get(_CHAT_ID) is None
    assert checks.count_by_status("error") == 1


async def test_refresh_captcha_transport_error_after_rejected_shows_generic_error():
    rejected = GibSubmitOutcome(kind="rejected", messages=(), raw_data=None)
    provider = _FakeProvider(
        start_challenge=_challenge(), submit_results=[rejected], refresh_error=True,
    )
    factory = _FakeCheckFactory([provider])
    controller, states, checks, registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("wrongcode", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.TRANSPORT_ERROR_TEXT
    assert states.get(_CHAT_ID) is None
    assert await registry.get(_CHAT_ID) is None
    assert checks.count_by_status("error") == 1


async def test_restart_recovery_issues_fresh_captcha_for_stored_plate():
    """См. design report Stage 3: реестр пуст (как после рестарта
    процесса), но persisted-состояние ещё помнит номер - ожидаемое
    восстановление: молча выдать новую CAPTCHA для того же номера,
    ничего не отправляя в submit()."""
    states = TurkeyConversationStateRepository(":memory:")
    checks = TurkeyCheckRepository(":memory:")
    registry = LiveGibSessionRegistry()
    # Состояние "как будто" было создано ДО рестарта - но реестр (созданный
    # только что) ничего о нём не знает.
    states.set(
        _CHAT_ID, telegram_user_id=_USER_ID, step="awaiting_captcha_code",
        payload={"plate": "34ABC123", "image_id": "old-cid-before-restart"},
    )

    new_provider = _FakeProvider(start_challenge=_challenge(image_id="cid-new", png=b"FRESH"))
    factory = _FakeCheckFactory([new_provider])
    garage = TurkeyUserCarsRepository(":memory:")
    statistics = TurkeyStatisticsService(TurkeyBotKnownUsersRepository(":memory:"), checks)
    avrasya_registry = LiveAvrasyaSessionRegistry()
    toll_checks = TurkeyTollCheckRepository(":memory:")
    kgm_registry = LiveKgmSessionRegistry()
    controller = ConversationController(
        states, checks, registry, garage, statistics, avrasya_registry, toll_checks,
        kgm_registry,
        check_factory=factory,
    )

    reply = await controller.handle_text("whatever-old-code", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.SESSION_EXPIRED_RETRY_TEXT
    assert reply.photo_png == b"FRESH"
    assert reply.show_cancel_button is True

    state = states.get(_CHAT_ID)
    assert state.payload == {"plate": "34ABC123", "image_id": "cid-new", "provider": "gib"}
    assert await registry.get(_CHAT_ID) is not None
    # Устаревший код НИКОГДА не отправлялся в submit() - session-у, к
    # которой он относился, восстановить невозможно (см. design report).
    assert new_provider.submit_calls == []


async def test_restart_recovery_with_missing_plate_in_payload_shows_session_lost():
    states = TurkeyConversationStateRepository(":memory:")
    checks = TurkeyCheckRepository(":memory:")
    registry = LiveGibSessionRegistry()
    states.set(_CHAT_ID, telegram_user_id=_USER_ID, step="awaiting_captcha_code", payload={})
    factory = _FakeCheckFactory([])
    garage = TurkeyUserCarsRepository(":memory:")
    statistics = TurkeyStatisticsService(TurkeyBotKnownUsersRepository(":memory:"), checks)
    avrasya_registry = LiveAvrasyaSessionRegistry()
    toll_checks = TurkeyTollCheckRepository(":memory:")
    kgm_registry = LiveKgmSessionRegistry()
    controller = ConversationController(
        states, checks, registry, garage, statistics, avrasya_registry, toll_checks,
        kgm_registry,
        check_factory=factory,
    )

    reply = await controller.handle_text("somecode", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.SESSION_LOST_TEXT
    assert states.get(_CHAT_ID) is None


async def test_restart_recovery_transport_error_shows_captcha_fetch_failed():
    states = TurkeyConversationStateRepository(":memory:")
    checks = TurkeyCheckRepository(":memory:")
    registry = LiveGibSessionRegistry()
    states.set(
        _CHAT_ID, telegram_user_id=_USER_ID, step="awaiting_captcha_code",
        payload={"plate": "34ABC123", "image_id": "old"},
    )
    factory = _FakeCheckFactory([_FakeProvider(start_error=True)])
    garage = TurkeyUserCarsRepository(":memory:")
    statistics = TurkeyStatisticsService(TurkeyBotKnownUsersRepository(":memory:"), checks)
    avrasya_registry = LiveAvrasyaSessionRegistry()
    toll_checks = TurkeyTollCheckRepository(":memory:")
    kgm_registry = LiveKgmSessionRegistry()
    controller = ConversationController(
        states, checks, registry, garage, statistics, avrasya_registry, toll_checks,
        kgm_registry,
        check_factory=factory,
    )

    reply = await controller.handle_text("somecode", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.CAPTCHA_FETCH_FAILED_TEXT
    assert states.get(_CHAT_ID) is None


async def test_cancel_clears_active_check_and_closes_client():
    provider = _FakeProvider(start_challenge=_challenge())
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)
    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("/cancel", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.CANCEL_CONFIRM_TEXT
    assert states.get(_CHAT_ID) is None
    assert await registry.get(_CHAT_ID) is None
    assert factory.clients[0].closed is True


async def test_cancel_via_dedicated_method_matches_text_command():
    """/cancel и inline-кнопка "❌ Отмена" (см.
    reader/turkey_bot/handlers.py) ведут в один и тот же
    ConversationController.handle_cancel()."""
    provider = _FakeProvider(start_challenge=_challenge())
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)
    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_cancel(chat_id=_CHAT_ID)

    assert reply.text == texts.CANCEL_CONFIRM_TEXT
    assert states.get(_CHAT_ID) is None


async def test_cancel_with_nothing_active_says_so():
    factory = _FakeCheckFactory([])
    controller, _states, _checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    reply = await controller.handle_text("/cancel", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.NOTHING_TO_CANCEL_TEXT


async def test_start_command_resets_any_active_check():
    provider = _FakeProvider(start_challenge=_challenge())
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)
    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("/start", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.WELCOME_TEXT
    assert states.get(_CHAT_ID) is None
    assert await registry.get(_CHAT_ID) is None
    assert factory.clients[0].closed is True


async def test_captcha_attempts_counted_across_one_rejection_then_success():
    rejected = GibSubmitOutcome(kind="rejected", messages=(), raw_data=None)
    ok = GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None)
    provider = _FakeProvider(
        start_challenge=_challenge(image_id="cid-1"),
        submit_results=[rejected, ok],
        refresh_results=[_challenge(image_id="cid-2")],
    )
    factory = _FakeCheckFactory([provider])
    controller, _states, checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("wrong", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("right", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    row = checks._conn.execute(
        "SELECT captcha_attempts FROM turkey_fine_checks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == 2


async def test_different_chats_do_not_interfere_with_each_other():
    provider_a = _FakeProvider(start_challenge=_challenge(image_id="cid-a", png=b"A"))
    provider_b = _FakeProvider(start_challenge=_challenge(image_id="cid-b", png=b"B"))
    factory = _FakeCheckFactory([provider_a, provider_b])
    controller, states, _checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    reply_a = await controller.handle_text("34ABC123", chat_id=1, telegram_user_id=10)
    reply_b = await controller.handle_text("06XYZ999", chat_id=2, telegram_user_id=20)

    assert reply_a.photo_png == b"A"
    assert reply_b.photo_png == b"B"
    assert states.get(1).payload["plate"] == "34ABC123"
    assert states.get(2).payload["plate"] == "06XYZ999"


# ---- 📊 Статистика / 🚗 Мои авто (garage) ----


def test_is_trusted_true_only_for_configured_ids():
    factory = _FakeCheckFactory([])
    controller, _states, _checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(
        factory, trusted_operator_user_ids={5712994689, 410811386},
    )

    assert controller.is_trusted(5712994689) is True
    assert controller.is_trusted(410811386) is True
    assert controller.is_trusted(999) is False


async def test_trusted_user_sees_statistics_when_typing_label():
    factory = _FakeCheckFactory([])
    controller, _states, checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(
        factory, trusted_operator_user_ids={_USER_ID},
    )
    checks.record_result(
        telegram_user_id=1, telegram_chat_id=1, plate="34ABC123", captcha_attempts=1,
        status="no_debt", gib_message_text=None, raw_response=None,
    )

    reply = await controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_USER_ID,
    )

    assert "Статистика" in reply.text
    assert reply.show_main_menu is True


async def test_normal_user_typing_statistics_label_gets_invalid_plate_not_stats():
    """Явное требование задачи: "normal user cannot retrieve statistics by
    typing button text" - не trusted, поэтому текст трактуется как обычный
    (невалидный) номер, а не как запрос статистики."""
    factory = _FakeCheckFactory([])
    controller, _states, _checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(
        factory, trusted_operator_user_ids=set(),
    )

    reply = await controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_USER_ID,
    )

    assert reply.text == texts.INVALID_PLATE_TEXT
    assert "Статистика" not in reply.text
    assert reply.extra_texts == ()


async def test_empty_garage_shows_empty_message_with_main_menu():
    factory = _FakeCheckFactory([])
    controller, _states, _checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    reply = await controller.handle_text(
        texts.GARAGE_LABEL, chat_id=_CHAT_ID, telegram_user_id=_USER_ID,
    )

    assert reply.text == texts.EMPTY_GARAGE_TEXT
    assert reply.show_main_menu is True
    assert reply.garage_cars is None


async def test_garage_available_to_any_user_not_just_trusted():
    factory = _FakeCheckFactory([])
    controller, _states, _checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(
        factory, trusted_operator_user_ids=set(),
    )

    reply = await controller.handle_text(
        texts.GARAGE_LABEL, chat_id=_CHAT_ID, telegram_user_id=_USER_ID,
    )

    assert reply.text == texts.EMPTY_GARAGE_TEXT


async def test_no_debt_check_adds_car_to_garage():
    outcome = GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None)
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome])
    factory = _FakeCheckFactory([provider])
    controller, _states, _checks, _registry, garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    cars = garage.list_cars(_USER_ID)
    assert len(cars) == 1
    assert cars[0].car_number == "34ABC123"


async def test_has_debt_check_adds_car_to_garage():
    fine = GibFineRecord(
        protocol_no="MC00000000", plate="34ABC123", amount=Decimal("1000.00"),
        description="raw", violation_date=date(2026, 8, 8), authority="ORG",
        late_fee=None, discount=None,
    )
    outcome = GibSubmitOutcome(kind="has_debt", messages=(), raw_data={}, fines=(fine,))
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome])
    factory = _FakeCheckFactory([provider])
    controller, _states, _checks, _registry, garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    cars = garage.list_cars(_USER_ID)
    assert len(cars) == 1
    assert cars[0].car_number == "34ABC123"


async def test_rejected_captcha_does_not_add_car_to_garage():
    rejected = GibSubmitOutcome(kind="rejected", messages=(), raw_data=None)
    provider = _FakeProvider(
        start_challenge=_challenge(), submit_results=[rejected],
        refresh_results=[_challenge(image_id="cid-2")],
    )
    factory = _FakeCheckFactory([provider])
    controller, _states, _checks, _registry, garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("wrongcode", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert garage.list_cars(_USER_ID) == []


async def test_unexpected_response_does_not_add_car_to_garage():
    outcome = GibSubmitOutcome(kind="unexpected", messages=(), raw_data=None)
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome])
    factory = _FakeCheckFactory([provider])
    controller, _states, _checks, _registry, garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert garage.list_cars(_USER_ID) == []


async def test_transport_error_does_not_add_car_to_garage():
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[GibTransportError("boom")])
    factory = _FakeCheckFactory([provider])
    controller, _states, _checks, _registry, garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert garage.list_cars(_USER_ID) == []


async def test_typing_a_plate_alone_without_finishing_does_not_add_car():
    """Явное требование задачи: "Do not add a car merely because the user
    typed it" - только реально ЗАВЕРШЁННая проверка добавляет машину."""
    provider = _FakeProvider(start_challenge=_challenge())
    factory = _FakeCheckFactory([provider])
    controller, _states, _checks, _registry, garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert garage.list_cars(_USER_ID) == []


async def test_duplicate_successful_checks_do_not_duplicate_garage_entry():
    outcome1 = GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None)
    outcome2 = GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None)
    provider1 = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome1])
    provider2 = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome2])
    factory = _FakeCheckFactory([provider1, provider2])
    controller, _states, _checks, _registry, garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert len(garage.list_cars(_USER_ID)) == 1


async def test_garage_check_ownership_enforced():
    """Явное требование задачи: "A normal user must not be able to
    inspect another user's garage by forging callback data"."""
    factory = _FakeCheckFactory([])
    controller, _states, _checks, _registry, garage, _avrasya_registry, _toll_checks = _make_controller(factory)
    garage.record_successful_check(telegram_user_id=999, car_number="34ABC123")
    car_id = garage.list_cars(999)[0].id

    reply = await controller.handle_garage_check(
        car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID,
        provider="gib",
    )

    assert reply is None


async def test_garage_check_starts_captcha_flow_without_retyping_plate():
    """Явное требование задачи: "The user must not have to type the plate
    again" - клик по машине в гараже сразу запускает существующий
    CAPTCHA-flow для сохранённого номера."""
    provider = _FakeProvider(start_challenge=_challenge(image_id="cid-garage", png=b"GARAGE-PNG"))
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, _registry, garage, _avrasya_registry, _toll_checks = _make_controller(factory)
    garage.record_successful_check(telegram_user_id=_USER_ID, car_number="34ABC123")
    car_id = garage.list_cars(_USER_ID)[0].id

    reply = await controller.handle_garage_check(
        car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID,
        provider="gib",
    )

    assert reply is not None
    assert reply.photo_png == b"GARAGE-PNG"
    assert reply.show_cancel_button is True
    assert states.get(_CHAT_ID).payload["plate"] == "34ABC123"


async def test_garage_check_for_nonexistent_car_returns_none():
    factory = _FakeCheckFactory([])
    controller, _states, _checks, _registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)

    reply = await controller.handle_garage_check(
        999999, chat_id=_CHAT_ID, telegram_user_id=_USER_ID,
        provider="gib",
    )

    assert reply is None


async def test_menu_label_interrupts_an_in_flight_captcha_wait():
    """Нажатие пункта меню - явная навигация, как и /cancel - имеет
    приоритет над "любой текст = код CAPTCHA" (см. design report)."""
    provider = _FakeProvider(start_challenge=_challenge())
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, registry, _garage, _avrasya_registry, _toll_checks = _make_controller(factory)
    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert states.get(_CHAT_ID).step == "awaiting_captcha_code"

    reply = await controller.handle_text(
        texts.GARAGE_LABEL, chat_id=_CHAT_ID, telegram_user_id=_USER_ID,
    )

    assert reply.text == texts.EMPTY_GARAGE_TEXT
    assert states.get(_CHAT_ID) is None
    assert await registry.get(_CHAT_ID) is None


# ---- 🛣 Avrasya Tüneli (см. design report Stage 2B) ----


async def test_bare_plate_without_menu_button_still_defaults_to_gib():
    """Явное требование задачи: "bare plate still defaults to GİB" —
    регрессия исходного (ещё Stage 3) поведения, не новое."""
    provider = _FakeProvider(start_challenge=_challenge())
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, _registry, _garage, _avr, _toll = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert states.get(_CHAT_ID).payload["provider"] == "gib"


async def test_check_fines_button_arms_gib_and_asks_for_plate():
    factory = _FakeCheckFactory([])
    controller, states, _checks, _registry, _garage, _avr, _toll = _make_controller(factory)

    reply = await controller.handle_text(
        texts.CHECK_FINES_LABEL, chat_id=_CHAT_ID, telegram_user_id=_USER_ID,
    )

    assert reply.text == texts.ASK_PLATE_FOR_FINES_TEXT
    assert reply.show_main_menu is True
    state = states.get(_CHAT_ID)
    assert state.step == "awaiting_plate"
    assert state.payload == {"provider": "gib"}


async def test_check_tolls_button_shows_toll_provider_menu():
    """См. design report "Реализация KGM provider" п.10 — CHECK_TOLLS_LABEL
    больше НЕ armит Avrasya напрямую, а показывает выбор (Avrasya/KGM)."""
    factory = _FakeCheckFactory([])
    controller, states, _checks, _registry, _garage, _avr, _toll = _make_controller(factory)

    reply = await controller.handle_text(
        texts.CHECK_TOLLS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_USER_ID,
    )

    assert reply.text == texts.CHOOSE_TOLL_PROVIDER_TEXT
    assert reply.toll_provider_keyboard is True
    assert states.get(_CHAT_ID) is None


async def test_toll_provider_callback_avrasya_arms_avrasya_and_asks_for_plate():
    factory = _FakeCheckFactory([])
    controller, states, _checks, _registry, _garage, _avr, _toll = _make_controller(factory)

    reply = await controller.handle_toll_provider_callback(
        "avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID,
    )

    assert reply.text == texts.ASK_PLATE_FOR_TOLLS_TEXT
    assert reply.show_main_menu is True
    state = states.get(_CHAT_ID)
    assert state.step == "awaiting_plate"
    assert state.payload == {"provider": "avrasya"}


async def test_toll_provider_callback_kgm_arms_kgm_and_asks_for_plate():
    factory = _FakeCheckFactory([])
    controller, states, _checks, _registry, _garage, _avr, _toll = _make_controller(factory)

    reply = await controller.handle_toll_provider_callback(
        "kgm", chat_id=_CHAT_ID, telegram_user_id=_USER_ID,
    )

    assert reply.text == texts.ASK_PLATE_FOR_KGM_TEXT
    assert reply.show_main_menu is True
    state = states.get(_CHAT_ID)
    assert state.step == "awaiting_plate"
    assert state.payload == {"provider": "kgm"}


async def test_plate_after_tolls_button_starts_avrasya_captcha_flow():
    avrasya_provider = _FakeAvrasyaProvider(start_challenge=_avrasya_challenge(png=b"AVRASYA-PNG"))
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, states, _checks, _registry, _garage, _avr, _toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.photo_png == b"AVRASYA-PNG"
    assert reply.show_cancel_button is True
    state = states.get(_CHAT_ID)
    assert state.step == "awaiting_captcha_code"
    assert state.payload == {"plate": "A123AA123", "provider": "avrasya"}
    assert await _avr.get(_CHAT_ID) is not None


async def test_invalid_plate_after_tolls_button_shows_invalid_plate_text():
    """Инвалидный номер НЕ разоружает provider — пользователь остаётся
    "armed" для Avrasya и может просто отправить номер ещё раз, не нажимая
    CHECK_TOLLS_LABEL заново."""
    controller, states, _checks, _registry, _garage, _avr, _toll = _make_controller(_FakeCheckFactory([]))
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("!!!", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.INVALID_PLATE_TEXT
    state = states.get(_CHAT_ID)
    assert state is not None
    assert state.step == "awaiting_plate"
    assert state.payload == {"provider": "avrasya"}


async def test_avrasya_captcha_fetch_transport_error_shows_safe_message():
    avrasya_factory = _FakeAvrasyaCheckFactory([_FakeAvrasyaProvider(start_error=AvrasyaTransportError("boom"))])
    controller, states, _checks, _registry, _garage, _avr, _toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.AVRASYA_CAPTCHA_FETCH_FAILED_TEXT
    assert states.get(_CHAT_ID) is None
    assert avrasya_factory.clients[0].closed is True


async def test_avrasya_captcha_fetch_rate_limited_shows_rate_limit_message():
    avrasya_factory = _FakeAvrasyaCheckFactory(
        [_FakeAvrasyaProvider(start_error=AvrasyaRateLimitedError("slow down"))]
    )
    controller, states, _checks, _registry, _garage, _avr, _toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.AVRASYA_RATE_LIMITED_TEXT
    assert states.get(_CHAT_ID) is None


async def test_avrasya_no_debt_reports_success_and_clears_everything():
    outcome = AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data="")
    avrasya_provider = _FakeAvrasyaProvider(start_challenge=_avrasya_challenge(), submit_results=[outcome])
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, states, _checks, _registry, _garage, avr, toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("123456", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.avrasya_no_debt_text("A123AA123")
    assert reply.show_main_menu is True
    assert states.get(_CHAT_ID) is None
    assert await avr.get(_CHAT_ID) is None
    assert avrasya_factory.clients[0].closed is True
    assert toll.count_total() == 1
    assert toll.count_by_status("no_debt") == 1
    assert avrasya_provider.submit_calls == [{"plate": "A123AA123", "captcha_code": "123456"}]


async def test_avrasya_rejected_captcha_refreshes_and_stays_in_avrasya_flow():
    rejected = AvrasyaSubmitOutcome(
        kind="rejected", status_code=400,
        messages=(AvrasyaMessage(property_name="Captcha", error_message="wrong"),), raw_data={},
    )
    avrasya_provider = _FakeAvrasyaProvider(
        start_challenge=_avrasya_challenge(png=b"P1"), submit_results=[rejected],
        refresh_results=[_avrasya_challenge(png=b"P2")],
    )
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, states, _checks, _registry, _garage, avr, _toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("000000", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.CAPTCHA_REJECTED_RETRY_TEXT
    assert reply.photo_png == b"P2"
    assert reply.show_cancel_button is True
    state = states.get(_CHAT_ID)
    assert state.step == "awaiting_captcha_code"
    assert state.payload == {"plate": "A123AA123", "provider": "avrasya"}
    # ТОТ ЖЕ провайдер/клиент - никакой новой Avrasya-сессии не создавалось.
    assert len(avrasya_factory.clients) == 1
    assert avrasya_provider.refresh_calls == 1
    assert avrasya_factory.clients[0].closed is False
    assert (await avr.get(_CHAT_ID)).provider is avrasya_provider


async def test_avrasya_submit_rate_limited_shows_rate_limit_and_records_error():
    avrasya_provider = _FakeAvrasyaProvider(
        start_challenge=_avrasya_challenge(), submit_results=[AvrasyaRateLimitedError("slow down")],
    )
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, states, _checks, _registry, _garage, avr, toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("000000", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.AVRASYA_RATE_LIMITED_TEXT
    assert states.get(_CHAT_ID) is None
    assert await avr.get(_CHAT_ID) is None
    assert toll.count_by_status("error") == 1


async def test_avrasya_submit_transport_error_shows_generic_error_and_records_error():
    avrasya_provider = _FakeAvrasyaProvider(
        start_challenge=_avrasya_challenge(), submit_results=[AvrasyaTransportError("boom")],
    )
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, states, _checks, _registry, _garage, avr, toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("000000", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.AVRASYA_TRANSPORT_ERROR_TEXT
    assert states.get(_CHAT_ID) is None
    assert await avr.get(_CHAT_ID) is None
    assert toll.count_by_status("error") == 1


async def test_avrasya_unexpected_outcome_shows_safe_message_never_raw_json(caplog):
    """Явное требование задачи: "do not guess its schema or expose raw
    JSON to the user" — пользователь видит только AVRASYA_UNEXPECTED_TEXT,
    сырой JSON остаётся ТОЛЬКО в TurkeyTollCheckRepository."""
    outcome = AvrasyaSubmitOutcome(
        kind="unexpected", status_code=200, messages=(), raw_data={"Subcriptions": ["SECRET-LOOKING-DATA"]},
    )
    avrasya_provider = _FakeAvrasyaProvider(start_challenge=_avrasya_challenge(), submit_results=[outcome])
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, states, _checks, _registry, _garage, _avr, toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    with caplog.at_level("WARNING"):
        reply = await controller.handle_text("123456", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.AVRASYA_UNEXPECTED_TEXT
    assert "SECRET-LOOKING-DATA" not in reply.text
    assert states.get(_CHAT_ID) is None
    assert toll.count_by_status("unexpected") == 1

    row = toll._conn.execute(
        "SELECT raw_response FROM turkey_toll_checks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "SECRET-LOOKING-DATA" in row[0]  # сырой ответ по-прежнему хранится server-side


def _real_debt_raw_data() -> dict:
    """Точная production-форма (Stage 2C, turkey_toll_checks.id=3,
    2026-09-16, plate M295YB196) — используется как AvrasyaSubmitOutcome.
    raw_data в тестах ниже (сырое хранение), НЕ как источник рендера (см.
    conversation.py: рендер строится ТОЛЬКО из outcome.debt_items)."""
    return {
        "Response": "OK", "DcsResponse": "SUCCESSFUL",
        "Subcriptions": [
            {
                "ClientSubscriptionCustomerReferenceValue": "M295YB196",
                "DebtItems": [{
                    "PrincipalTaxIncludedBalanceAmount": 330.0,
                    "TotalTaxIncludedBalanceAmount": 330.0,
                    "ExitDate": "2*************6", "ExitStation": "A*****A",
                    "IsAuthenticate": False,
                }],
                "DebtorContactFullName": "*******", "ServiceFileTypeName": "EARLY_COLLECTION_FILE",
            },
            {
                "DebtItems": [{
                    "PrincipalTaxIncludedBalanceAmount": 225.0,
                    "TotalFixedIncomeTaxIncludedBalanceAmount": 900.0,
                    "TotalTaxIncludedBalanceAmount": 1125.0, "IsAuthenticate": False,
                }],
                "ServiceFileTypeName": "COLLECTION_FILE",
            },
            {
                "DebtItems": [{
                    "PrincipalTaxIncludedBalanceAmount": 225.0,
                    "TotalFixedIncomeTaxIncludedBalanceAmount": 900.0,
                    "TotalTaxIncludedBalanceAmount": 1125.0, "IsAuthenticate": False,
                }],
                "ServiceFileTypeName": "COLLECTION_FILE",
            },
        ],
        "IsShowButton": False,
    }


def _real_debt_items() -> tuple[AvrasyaDebtItem, ...]:
    return (
        AvrasyaDebtItem(
            principal_amount=Decimal("330.0"), total_amount=Decimal("330.0"),
            service_file_type="EARLY_COLLECTION_FILE",
        ),
        AvrasyaDebtItem(
            principal_amount=Decimal("225.0"), total_amount=Decimal("1125.0"),
            service_file_type="COLLECTION_FILE", penalty_amount=Decimal("900.0"),
        ),
        AvrasyaDebtItem(
            principal_amount=Decimal("225.0"), total_amount=Decimal("1125.0"),
            service_file_type="COLLECTION_FILE", penalty_amount=Decimal("900.0"),
        ),
    )


async def test_avrasya_has_debt_shows_real_summary_never_raw_json_and_records_has_debt():
    """См. design report Stage 2C — точный production-пример (M295YB196,
    turkey_toll_checks.id=3, 2026-09-16): confirmed has_debt теперь
    рендерит реальную сумму, а не общий AVRASYA_UNEXPECTED_TEXT."""
    outcome = AvrasyaSubmitOutcome(
        kind="has_debt", status_code=200, messages=(), raw_data=_real_debt_raw_data(),
        debt_items=_real_debt_items(),
    )
    avrasya_provider = _FakeAvrasyaProvider(start_challenge=_avrasya_challenge(), submit_results=[outcome])
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, _states, _checks, _registry, garage, _avr, toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("M295YB196", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("123456", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.format_avrasya_has_debt_message("M295YB196", _real_debt_items())
    assert "Итого к оплате: 2 580" in reply.text
    assert reply.show_main_menu is True

    # Явное требование задачи: те же CTA-кнопки/URL, что и в Георгии-боте
    # (см. tests/test_public_bot_conversation.py про идентичный ассерт).
    assert reply.cta_buttons is not None
    assert len(reply.cta_buttons) == 2
    labels = [label for label, _url in reply.cta_buttons]
    assert labels == ["💳 Оплатить в рублях", "🚗 ОСАГО Турции"]
    urls = {url for _label, url in reply.cta_buttons}
    assert urls == {"https://t.me/tplgee"}

    # Замаскированные/внутренние поля НИКОГДА не должны попасть в текст
    # пользователю (см. задачу).
    for forbidden in ("ExitDate", "ExitStation", "DebtorContactFullName", "IsAuthenticate", "Subcriptions"):
        assert forbidden not in reply.text

    assert toll.count_by_status("has_debt") == 1
    row = toll._conn.execute(
        "SELECT raw_response FROM turkey_toll_checks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "ExitStation" in row[0]  # сырой ответ по-прежнему хранится server-side (audit)

    # has_debt - тоже "успешная" завершённая проверка (см. design report:
    # "successful Avrasya check adding/reusing the same saved vehicle").
    cars = garage.list_cars(_USER_ID)
    assert len(cars) == 1
    assert cars[0].car_number == "M295YB196"


# ---- Avrasya has_debt CTA-кнопки (см. design report: "append these CTAs
# only after a confirmed has_debt result" — те же кнопки/URL, что и у
# Георгии-бота) ----


async def test_avrasya_no_debt_does_not_include_cta_buttons():
    outcome = AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data="")
    avrasya_provider = _FakeAvrasyaProvider(start_challenge=_avrasya_challenge(), submit_results=[outcome])
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, _states, _checks, _registry, _garage, _avr, _toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("123456", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.avrasya_no_debt_text("A123AA123")
    assert reply.cta_buttons is None


async def test_avrasya_unexpected_does_not_include_cta_buttons():
    outcome = AvrasyaSubmitOutcome(kind="unexpected", status_code=200, messages=(), raw_data={"Foo": "bar"})
    avrasya_provider = _FakeAvrasyaProvider(start_challenge=_avrasya_challenge(), submit_results=[outcome])
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, _states, _checks, _registry, _garage, _avr, _toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("123456", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.AVRASYA_UNEXPECTED_TEXT
    assert reply.cta_buttons is None


async def test_avrasya_rejected_captcha_does_not_include_cta_buttons():
    rejected = AvrasyaSubmitOutcome(
        kind="rejected", status_code=400,
        messages=(AvrasyaMessage(property_name="Captcha", error_message="wrong"),), raw_data={},
    )
    avrasya_provider = _FakeAvrasyaProvider(
        start_challenge=_avrasya_challenge(), submit_results=[rejected],
        refresh_results=[_avrasya_challenge()],
    )
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, _states, _checks, _registry, _garage, _avr, _toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("000000", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.CAPTCHA_REJECTED_RETRY_TEXT
    assert reply.cta_buttons is None


async def test_avrasya_transport_error_does_not_include_cta_buttons():
    avrasya_provider = _FakeAvrasyaProvider(
        start_challenge=_avrasya_challenge(), submit_results=[AvrasyaTransportError("boom")],
    )
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, _states, _checks, _registry, _garage, _avr, _toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("000000", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.AVRASYA_TRANSPORT_ERROR_TEXT
    assert reply.cta_buttons is None


def _assert_has_the_commercial_cta_buttons(cta_buttons, *, expected_username="tplgee"):
    assert cta_buttons is not None
    assert len(cta_buttons) == 2
    labels = [label for label, _url in cta_buttons]
    assert labels == ["💳 Оплатить в рублях", "🚗 ОСАГО Турции"]
    urls = {url for _label, url in cta_buttons}
    assert urls == {f"https://t.me/{expected_username}"}


async def test_gib_has_debt_includes_commercial_cta_buttons_for_normal_user():
    """Регрессия для реального production-бага (M295YB196, 2026-09-17,
    2 штрафа по 40 TRY): GIB has_debt для ОБЫЧНОГО (не-trusted)
    пользователя должен показывать те же коммерческие CTA-кнопки, что и
    Avrasya has_debt — до фикса cta_buttons вообще не выставлялся в этой
    ветке (см. design report "fix: show Turkey fine CTAs for all users")."""
    fine = GibFineRecord(
        protocol_no="MC00000000", plate="34ABC123", amount=Decimal("1000.00"),
        description="raw", violation_date=date(2026, 8, 8), authority="ORG",
        late_fee=None, discount=None,
    )
    outcome = GibSubmitOutcome(kind="has_debt", messages=(), raw_data={}, fines=(fine,))
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome])
    factory = _FakeCheckFactory([provider])
    controller, _states, _checks, _registry, _garage, _avr, _toll = _make_controller(
        factory, trusted_operator_user_ids=set(),
    )

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert controller.is_trusted(_USER_ID) is False
    _assert_has_the_commercial_cta_buttons(reply.cta_buttons)


async def test_gib_has_debt_includes_commercial_cta_buttons_for_trusted_user():
    """Тот же результат для trusted/manager-пользователя — CTA-кнопки НЕ
    зависят от is_trusted (см. design report: "Trusted/manager status
    must not change whether these two CTA buttons exist")."""
    fine = GibFineRecord(
        protocol_no="MC00000000", plate="34ABC123", amount=Decimal("1000.00"),
        description="raw", violation_date=date(2026, 8, 8), authority="ORG",
        late_fee=None, discount=None,
    )
    outcome = GibSubmitOutcome(kind="has_debt", messages=(), raw_data={}, fines=(fine,))
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome])
    factory = _FakeCheckFactory([provider])
    controller, _states, _checks, _registry, _garage, _avr, _toll = _make_controller(
        factory, trusted_operator_user_ids={_USER_ID},
    )

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert controller.is_trusted(_USER_ID) is True
    _assert_has_the_commercial_cta_buttons(reply.cta_buttons)


async def test_gib_has_debt_cta_urls_use_the_configured_contact_username_not_hardcoded():
    """Явное требование задачи: "URLs resolve through the configured
    operator/contact mechanism... not hardcoded" — меняем конфиг и
    проверяем, что URL меняется вместе с ним (не зафиксирован на
    "tplgee" в коде)."""
    fine = GibFineRecord(
        protocol_no="MC00000000", plate="34ABC123", amount=Decimal("1000.00"),
        description="raw", violation_date=date(2026, 8, 8), authority="ORG",
        late_fee=None, discount=None,
    )
    outcome = GibSubmitOutcome(kind="has_debt", messages=(), raw_data={}, fines=(fine,))
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome])
    factory = _FakeCheckFactory([provider])
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
        check_factory=factory, payment_help_contact_username="some_other_contact",
    )

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    _assert_has_the_commercial_cta_buttons(reply.cta_buttons, expected_username="some_other_contact")


async def test_gib_and_avrasya_has_debt_use_the_same_cta_buttons():
    """Требование задачи: GIB и Avrasya используют ОДИН и тот же CTA
    helper/architecture — не два независимых источника, которые могли бы
    разойтись (см. design report про переименование
    _avrasya_debt_cta_buttons -> _debt_cta_buttons)."""
    gib_fine = GibFineRecord(
        protocol_no="MC00000000", plate="34ABC123", amount=Decimal("1000.00"),
        description="raw", violation_date=date(2026, 8, 8), authority="ORG",
        late_fee=None, discount=None,
    )
    gib_outcome = GibSubmitOutcome(kind="has_debt", messages=(), raw_data={}, fines=(gib_fine,))
    gib_provider = _FakeProvider(start_challenge=_challenge(), submit_results=[gib_outcome])
    factory = _FakeCheckFactory([gib_provider])

    avrasya_outcome = AvrasyaSubmitOutcome(
        kind="has_debt", status_code=200, messages=(), raw_data=_real_debt_raw_data(),
        debt_items=_real_debt_items(),
    )
    avrasya_provider = _FakeAvrasyaProvider(start_challenge=_avrasya_challenge(), submit_results=[avrasya_outcome])
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])

    controller, _states, _checks, _registry, _garage, _avr, _toll = _make_controller(
        factory, avrasya_factory=avrasya_factory,
    )

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    gib_reply = await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("M295YB196", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    avrasya_reply = await controller.handle_text("123456", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert gib_reply.cta_buttons == avrasya_reply.cta_buttons


async def test_avrasya_restart_recovery_issues_fresh_captcha_for_stored_plate():
    """Аналог test_restart_recovery_issues_fresh_captcha_for_stored_plate
    (GIB) для Avrasya (см. design report Stage 2B: "Missing/expired live
    Avrasya session → create a fresh session/CAPTCHA for the persisted
    plate")."""
    states = TurkeyConversationStateRepository(":memory:")
    checks = TurkeyCheckRepository(":memory:")
    registry = LiveGibSessionRegistry()
    garage = TurkeyUserCarsRepository(":memory:")
    statistics = TurkeyStatisticsService(TurkeyBotKnownUsersRepository(":memory:"), checks)
    avrasya_registry = LiveAvrasyaSessionRegistry()
    toll_checks = TurkeyTollCheckRepository(":memory:")
    states.set(
        _CHAT_ID, telegram_user_id=_USER_ID, step="awaiting_captcha_code",
        payload={"plate": "A123AA123", "provider": "avrasya"},
    )
    new_provider = _FakeAvrasyaProvider(start_challenge=_avrasya_challenge(png=b"FRESH"))
    avrasya_factory = _FakeAvrasyaCheckFactory([new_provider])
    kgm_registry = LiveKgmSessionRegistry()
    controller = ConversationController(
        states, checks, registry, garage, statistics, avrasya_registry, toll_checks,
        kgm_registry,
        check_factory=_FakeCheckFactory([]), avrasya_check_factory=avrasya_factory,
    )

    reply = await controller.handle_text("whatever-old-code", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.SESSION_EXPIRED_RETRY_TEXT
    assert reply.photo_png == b"FRESH"
    assert reply.show_cancel_button is True
    state = states.get(_CHAT_ID)
    assert state.payload == {"plate": "A123AA123", "provider": "avrasya"}
    assert await avrasya_registry.get(_CHAT_ID) is not None
    assert new_provider.submit_calls == []


async def test_avrasya_restart_recovery_with_missing_plate_shows_session_lost():
    states = TurkeyConversationStateRepository(":memory:")
    checks = TurkeyCheckRepository(":memory:")
    registry = LiveGibSessionRegistry()
    garage = TurkeyUserCarsRepository(":memory:")
    statistics = TurkeyStatisticsService(TurkeyBotKnownUsersRepository(":memory:"), checks)
    avrasya_registry = LiveAvrasyaSessionRegistry()
    toll_checks = TurkeyTollCheckRepository(":memory:")
    states.set(
        _CHAT_ID, telegram_user_id=_USER_ID, step="awaiting_captcha_code",
        payload={"provider": "avrasya"},
    )
    kgm_registry = LiveKgmSessionRegistry()
    controller = ConversationController(
        states, checks, registry, garage, statistics, avrasya_registry, toll_checks,
        kgm_registry,
        check_factory=_FakeCheckFactory([]), avrasya_check_factory=_FakeAvrasyaCheckFactory([]),
    )

    reply = await controller.handle_text("somecode", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.SESSION_LOST_TEXT
    assert states.get(_CHAT_ID) is None


async def test_cancel_clears_active_avrasya_check_and_closes_client():
    avrasya_provider = _FakeAvrasyaProvider(start_challenge=_avrasya_challenge())
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, states, _checks, _registry, _garage, avr, _toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_text("/cancel", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.CANCEL_CONFIRM_TEXT
    assert states.get(_CHAT_ID) is None
    assert await avr.get(_CHAT_ID) is None
    assert avrasya_factory.clients[0].closed is True


async def test_captcha_attempts_counted_across_one_avrasya_rejection_then_success():
    rejected = AvrasyaSubmitOutcome(kind="rejected", status_code=400, messages=(), raw_data={})
    ok = AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data="")
    avrasya_provider = _FakeAvrasyaProvider(
        start_challenge=_avrasya_challenge(), submit_results=[rejected, ok],
        refresh_results=[_avrasya_challenge()],
    )
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, _states, _checks, _registry, _garage, _avr, toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("wrong", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("right", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    row = toll._conn.execute(
        "SELECT captcha_attempts FROM turkey_toll_checks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == 2


async def test_avrasya_no_debt_adds_car_to_garage_shared_with_gib():
    """Явное требование задачи: "Reuse the existing turkey_bot_user_cars;
    no separate Avrasya garage"."""
    outcome = AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data="")
    avrasya_provider = _FakeAvrasyaProvider(start_challenge=_avrasya_challenge(), submit_results=[outcome])
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, _states, _checks, _registry, garage, _avr, _toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("123456", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    cars = garage.list_cars(_USER_ID)
    assert len(cars) == 1
    assert cars[0].car_number == "A123AA123"


async def test_avrasya_rejected_does_not_add_car_to_garage():
    rejected = AvrasyaSubmitOutcome(kind="rejected", status_code=400, messages=(), raw_data={})
    avrasya_provider = _FakeAvrasyaProvider(
        start_challenge=_avrasya_challenge(), submit_results=[rejected],
        refresh_results=[_avrasya_challenge()],
    )
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, _states, _checks, _registry, garage, _avr, _toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("wrong", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert garage.list_cars(_USER_ID) == []


async def test_garage_gib_button_uses_stored_plate_without_retyping():
    """Явное требование задачи: гараж-кнопка "🚔 Проверить штрафы" —
    существующая GIB CAPTCHA-flow для сохранённого номера."""
    provider = _FakeProvider(start_challenge=_challenge(image_id="cid-garage", png=b"GIB-GARAGE"))
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, _registry, garage, _avr, _toll = _make_controller(factory)
    garage.record_successful_check(telegram_user_id=_USER_ID, car_number="34ABC123")
    car_id = garage.list_cars(_USER_ID)[0].id

    reply = await controller.handle_garage_check(
        car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID, provider="gib",
    )

    assert reply is not None
    assert reply.photo_png == b"GIB-GARAGE"
    assert states.get(_CHAT_ID).payload == {
        "plate": "34ABC123", "image_id": "cid-garage", "provider": "gib",
    }


async def test_garage_avrasya_button_uses_stored_plate_without_retyping():
    """Явное требование задачи: гараж-кнопка "🛣 Проверить платные дороги" —
    Avrasya CAPTCHA-flow для СОХРАНЁННОГО номера, без повторного ввода."""
    avrasya_provider = _FakeAvrasyaProvider(start_challenge=_avrasya_challenge(png=b"AVRASYA-GARAGE"))
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, states, _checks, _registry, garage, avr, _toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    garage.record_successful_check(telegram_user_id=_USER_ID, car_number="A123AA123")
    car_id = garage.list_cars(_USER_ID)[0].id

    reply = await controller.handle_garage_check(
        car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID, provider="avrasya",
    )

    assert reply is not None
    assert reply.photo_png == b"AVRASYA-GARAGE"
    assert reply.show_cancel_button is True
    assert states.get(_CHAT_ID).payload == {"plate": "A123AA123", "provider": "avrasya"}
    assert await avr.get(_CHAT_ID) is not None


async def test_garage_check_ownership_enforced_for_avrasya_provider_too():
    """Явное требование задачи: владение проверяется одинаково для ОБОИХ
    провайдеров (см. design report: "trusted status... does not grant
    managers access to another user's Turkey garage or checks")."""
    controller, _states, _checks, _registry, garage, _avr, _toll = _make_controller(_FakeCheckFactory([]))
    garage.record_successful_check(telegram_user_id=999, car_number="A123AA123")
    car_id = garage.list_cars(999)[0].id

    reply = await controller.handle_garage_check(
        car_id, chat_id=_CHAT_ID, telegram_user_id=_USER_ID, provider="avrasya",
    )

    assert reply is None


async def test_switching_menu_button_mid_avrasya_wait_closes_avrasya_session():
    """Единый lock/единая обработка (см. design report Stage 2B: "Use one
    common per-chat lock across GİB and Avrasya so the two flows cannot
    race") — навигация по меню (CHECK_FINES_LABEL) во время ЖИВОЙ
    Avrasya-CAPTCHA-сессии (тот же принцип, что и у test_menu_label_
    interrupts_an_in_flight_captcha_wait для GIB выше) корректно закрывает
    прошлую сессию, а не оставляет её висеть."""
    avrasya_provider = _FakeAvrasyaProvider(start_challenge=_avrasya_challenge())
    avrasya_factory = _FakeAvrasyaCheckFactory([avrasya_provider])
    controller, states, _checks, _registry, _garage, avr, _toll = _make_controller(
        _FakeCheckFactory([]), avrasya_factory=avrasya_factory,
    )
    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert states.get(_CHAT_ID).step == "awaiting_captcha_code"
    assert await avr.get(_CHAT_ID) is not None

    reply = await controller.handle_text(
        texts.CHECK_FINES_LABEL, chat_id=_CHAT_ID, telegram_user_id=_USER_ID,
    )

    assert reply.text == texts.ASK_PLATE_FOR_FINES_TEXT
    assert await avr.get(_CHAT_ID) is None
    assert avrasya_factory.clients[0].closed is True
    state = states.get(_CHAT_ID)
    assert state.step == "awaiting_plate"
    assert state.payload == {"provider": "gib"}


# ---- ℹ️ Справка (см. design report) ----


async def test_help_label_opens_help_menu():
    controller, _states, _checks, _registry, _garage, _avr, _toll = _make_controller(_FakeCheckFactory([]))

    reply = await controller.handle_text(texts.HELP_LABEL, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.HELP_MENU_TEXT
    assert reply.help_keyboard == "menu"


async def test_help_label_interrupts_an_in_flight_captcha_wait():
    """Тот же принцип, что и у test_menu_label_interrupts_an_in_flight_
    captcha_wait выше — ℹ️ Справка тоже явная навигация."""
    provider = _FakeProvider(start_challenge=_challenge())
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, registry, _garage, _avr, _toll = _make_controller(factory)
    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert states.get(_CHAT_ID).step == "awaiting_captcha_code"

    reply = await controller.handle_text(texts.HELP_LABEL, chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.HELP_MENU_TEXT
    assert states.get(_CHAT_ID) is None
    assert await registry.get(_CHAT_ID) is None


async def test_help_callback_terms_section():
    controller, _states, _checks, _registry, _garage, _avr, _toll = _make_controller(_FakeCheckFactory([]))

    reply = await controller.handle_help_callback("terms")

    assert reply.text == texts.HELP_TERMS_TEXT
    assert reply.help_keyboard == "section"
    assert "🚔 Штрафы GİB" in reply.text
    assert "🛣 Avrasya Tüneli" in reply.text
    assert "Стоимость проездов" in reply.text
    assert "Начисленные штрафы" in reply.text
    assert "Итого к оплате" in reply.text


async def test_help_callback_gib_section():
    controller, _states, _checks, _registry, _garage, _avr, _toll = _make_controller(_FakeCheckFactory([]))

    reply = await controller.handle_help_callback("gib")

    assert reply.text == texts.HELP_GIB_TEXT
    assert reply.help_keyboard == "section"
    assert texts.CHECK_FINES_LABEL in reply.text
    # Исправление формулировки (см. задачу): шаг 2 — ввод номера, ссылка
    # на "🚗 Мои авто" для сохранённого авто вынесена ОТДЕЛЬНОЙ строкой,
    # а не объединена с шагом 2.
    assert texts.GARAGE_LABEL in reply.text
    assert "Выберите сохранённый автомобиль" not in reply.text


async def test_help_callback_avrasya_section():
    controller, _states, _checks, _registry, _garage, _avr, _toll = _make_controller(_FakeCheckFactory([]))

    reply = await controller.handle_help_callback("avrasya")

    assert reply.text == texts.HELP_AVRASYA_TEXT
    assert reply.help_keyboard == "section"
    assert texts.CHECK_TOLLS_LABEL in reply.text
    assert "Avrasya Tüneli" in reply.text
    # Исправление формулировки (см. задачу): та же поправка, что и для
    # HELP_GIB_TEXT выше.
    assert texts.GARAGE_LABEL in reply.text
    assert "Выберите сохранённый автомобиль" not in reply.text


async def test_help_callback_payment_section():
    controller, _states, _checks, _registry, _garage, _avr, _toll = _make_controller(_FakeCheckFactory([]))

    reply = await controller.handle_help_callback("payment")

    assert reply.text == texts.HELP_PAYMENT_TEXT
    assert reply.help_keyboard == "section"
    assert "💳 Оплатить в рублях" in reply.text
    # Явное требование задачи: не утверждать, что бот сам принимает оплату.
    assert "сам не принимает оплату" in reply.text
    # URL не должен повторяться текстом (см. задачу: "do not hardcode or
    # invent another Telegram URL" — единственный источник — сама кнопка).
    assert "t.me" not in reply.text
    assert "tplgee" not in reply.text


async def test_help_callback_menu_returns_to_help_menu():
    """"⬅️ Назад" ИЗ раздела — в Help-меню (не в главное меню)."""
    controller, _states, _checks, _registry, _garage, _avr, _toll = _make_controller(_FakeCheckFactory([]))

    reply = await controller.handle_help_callback("menu")

    assert reply.text == texts.HELP_MENU_TEXT
    assert reply.help_keyboard == "menu"


async def test_help_back_to_main_returns_normal_main_menu():
    controller, _states, _checks, _registry, _garage, _avr, _toll = _make_controller(_FakeCheckFactory([]))

    reply = await controller.handle_help_back_to_main()

    assert reply.text == texts.WELCOME_TEXT
    assert reply.show_main_menu is True
    assert reply.help_keyboard is None


async def test_avrasya_has_debt_message_uses_updated_terminology():
    """Явное требование задачи: "Стоимость проездов"/"Начисленные штрафы"
    вместо старых "Сумма проездов"/"Штрафы" — расчёты не изменились (см.
    test_avrasya_has_debt_shows_real_summary_never_raw_json_and_records_
    has_debt выше — "Итого к оплате: 2 580" там же)."""
    message = texts.format_avrasya_has_debt_message("M295YB196", _real_debt_items())

    assert message == (
        "⚠️ M295YB196: найдены неоплаченные проезды по Avrasya Tüneli.\n"
        "\n"
        "Проездов: 3\n"
        "Стоимость проездов: 780 ₺\n"
        "Начисленные штрафы: 1 800 ₺\n"
        "Итого к оплате: 2 580 ₺"
    )
    assert "Сумма проездов" not in message
    assert "\nШтрафы:" not in message
