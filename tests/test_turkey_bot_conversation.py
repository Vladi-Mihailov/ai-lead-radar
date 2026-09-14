"""
Тесты reader/turkey_bot/conversation.py::ConversationController — линейный
цикл "номер -> CAPTCHA -> код -> результат" (см. design report Stage 3).
Реальная сеть НЕ используется вовсе — check_factory (см.
ConversationController.__init__) подменяется фейковым GibProvider/client,
её уже проверенная транспортная механика — предмет tests/test_turkey_gib_*.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot import texts  # noqa: E402
from reader.turkey_bot.check_repository import TurkeyCheckRepository  # noqa: E402
from reader.turkey_bot.conversation import ConversationController  # noqa: E402
from reader.turkey_bot.conversation_state_repository import (  # noqa: E402
    TurkeyConversationStateRepository,
)
from reader.turkey_bot.gib.models import (  # noqa: E402
    CaptchaChallenge,
    GibMessage,
    GibSubmitOutcome,
)
from reader.turkey_bot.gib.session import GibTransportError  # noqa: E402
from reader.turkey_bot.live_session_registry import LiveGibSessionRegistry  # noqa: E402

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


def _make_controller(factory: _FakeCheckFactory):
    states = TurkeyConversationStateRepository(":memory:")
    checks = TurkeyCheckRepository(":memory:")
    registry = LiveGibSessionRegistry()
    controller = ConversationController(states, checks, registry, check_factory=factory)
    return controller, states, checks, registry


def _challenge(image_id: str = "cid-1", png: bytes = b"PNG-BYTES") -> CaptchaChallenge:
    return CaptchaChallenge(image_id=image_id, image_png=png)


async def test_new_valid_plate_fetches_captcha_and_persists_state():
    provider = _FakeProvider(start_challenge=_challenge(image_id="cid-1", png=b"PNG1"))
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, registry = _make_controller(factory)

    reply = await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.photo_png == b"PNG1"
    assert reply.show_cancel_button is True

    state = states.get(_CHAT_ID)
    assert state.step == "awaiting_captcha_code"
    assert state.payload == {"plate": "34ABC123", "image_id": "cid-1"}
    assert await registry.get(_CHAT_ID) is not None


async def test_invalid_plate_does_not_start_a_check():
    factory = _FakeCheckFactory([])
    controller, states, _checks, registry = _make_controller(factory)

    reply = await controller.handle_text("!!!", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.INVALID_PLATE_TEXT
    assert reply.photo_png is None
    assert states.get(_CHAT_ID) is None
    assert await registry.get(_CHAT_ID) is None


async def test_captcha_fetch_transport_error_shows_safe_message_and_closes_client():
    provider = _FakeProvider(start_error=True)
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, registry = _make_controller(factory)

    reply = await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.CAPTCHA_FETCH_FAILED_TEXT
    assert states.get(_CHAT_ID) is None
    assert await registry.get(_CHAT_ID) is None
    assert factory.clients[0].closed is True


async def test_correct_code_no_debt_reports_success_and_clears_everything():
    outcome = GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None)
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome])
    factory = _FakeCheckFactory([provider])
    controller, states, checks, registry = _make_controller(factory)

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


async def test_has_debt_never_leaks_raw_data_to_user_text():
    """Явное требование задачи: "do not expose raw GIB raw_data directly
    to Telegram users" - только безопасная заглушка в reply.text, полный
    raw_data - только в TurkeyCheckRepository (server-side)."""
    outcome = GibSubmitOutcome(
        kind="has_debt", messages=(), raw_data=[{"secretAmount": 12345, "secretField": "x"}],
    )
    provider = _FakeProvider(start_challenge=_challenge(), submit_results=[outcome])
    factory = _FakeCheckFactory([provider])
    controller, _states, checks, _registry = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.has_debt_text("34ABC123")
    assert "secretAmount" not in reply.text
    assert "secretField" not in reply.text
    assert "12345" not in reply.text

    row = checks._conn.execute(
        "SELECT raw_response, status FROM turkey_fine_checks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[1] == "has_debt"
    assert "secretAmount" in row[0]  # сырой ответ по-прежнему хранится server-side


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
    controller, states, _checks, registry = _make_controller(factory)

    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("wrongcode", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.CAPTCHA_REJECTED_RETRY_TEXT
    assert reply.photo_png == b"PNG2"
    assert reply.show_cancel_button is True

    state = states.get(_CHAT_ID)
    assert state is not None
    assert state.step == "awaiting_captcha_code"
    assert state.payload == {"plate": "34ABC123", "image_id": "cid-2"}

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
    controller, states, checks, _registry = _make_controller(factory)

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
    controller, states, checks, registry = _make_controller(factory)

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
    controller, states, checks, registry = _make_controller(factory)

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
    controller = ConversationController(states, checks, registry, check_factory=factory)

    reply = await controller.handle_text("whatever-old-code", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.SESSION_EXPIRED_RETRY_TEXT
    assert reply.photo_png == b"FRESH"
    assert reply.show_cancel_button is True

    state = states.get(_CHAT_ID)
    assert state.payload == {"plate": "34ABC123", "image_id": "cid-new"}
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
    controller = ConversationController(states, checks, registry, check_factory=factory)

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
    controller = ConversationController(states, checks, registry, check_factory=factory)

    reply = await controller.handle_text("somecode", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.CAPTCHA_FETCH_FAILED_TEXT
    assert states.get(_CHAT_ID) is None


async def test_cancel_clears_active_check_and_closes_client():
    provider = _FakeProvider(start_challenge=_challenge())
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, registry = _make_controller(factory)
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
    controller, states, _checks, _registry = _make_controller(factory)
    await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    reply = await controller.handle_cancel(chat_id=_CHAT_ID)

    assert reply.text == texts.CANCEL_CONFIRM_TEXT
    assert states.get(_CHAT_ID) is None


async def test_cancel_with_nothing_active_says_so():
    factory = _FakeCheckFactory([])
    controller, _states, _checks, _registry = _make_controller(factory)

    reply = await controller.handle_text("/cancel", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)

    assert reply.text == texts.NOTHING_TO_CANCEL_TEXT


async def test_start_command_resets_any_active_check():
    provider = _FakeProvider(start_challenge=_challenge())
    factory = _FakeCheckFactory([provider])
    controller, states, _checks, registry = _make_controller(factory)
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
    controller, _states, checks, _registry = _make_controller(factory)

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
    controller, states, _checks, _registry = _make_controller(factory)

    reply_a = await controller.handle_text("34ABC123", chat_id=1, telegram_user_id=10)
    reply_b = await controller.handle_text("06XYZ999", chat_id=2, telegram_user_id=20)

    assert reply_a.photo_png == b"A"
    assert reply_b.photo_png == b"B"
    assert states.get(1).payload["plate"] == "34ABC123"
    assert states.get(2).payload["plate"] == "06XYZ999"
