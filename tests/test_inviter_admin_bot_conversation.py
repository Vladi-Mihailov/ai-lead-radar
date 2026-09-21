"""Тесты reader/inviter_admin_bot/conversation.py::AdminBotController —
диалоги/доступ. AccountAuthCoordinator подменяется fake-реализацией (та же
сигнатура, что и настоящая, см. reader/inviter_admin_bot/auth.py) — никакого
реального Telethon (auth.py уже протестирован отдельно, см.
tests/test_inviter_admin_bot_auth.py)."""

import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.inviter.models import TelegramAccount
from reader.inviter.repository import (
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)
from reader.inviter.runtime_state_repository import (
    InviterRuntimeStateRepository,
)
from reader.inviter_admin_bot import texts
from reader.inviter_admin_bot.conversation import AdminBotController
from reader.inviter_admin_bot.conversation_state_repository import (
    AdminBotConversationStateRepository,
)
from reader.inviter_admin_bot.models import AuthOutcome, AuthResult
from reader.inviter_admin_bot.service import InviterAdminService

_TRUSTED_ID = 111222333
_OTHER_ID = 999888777
_CHAT_ID = 111222333


class _FakeAuthCoordinator:
    """Та же сигнатура, что и AccountAuthCoordinator (см. auth.py) —
    заранее заданная последовательность AuthOutcome на chat_id, без
    реального Telethon."""

    def __init__(self):
        self.start_new_outcomes: dict[int, AuthOutcome] = {}
        self.start_reauth_outcomes: dict[int, AuthOutcome] = {}
        self.code_outcomes: list[AuthOutcome] = []
        self.password_outcomes: list[AuthOutcome] = []
        self.cancelled_chat_ids: list[int] = []
        self._pending: set[int] = set()
        self.start_new_calls: list[tuple[int, str]] = []

    async def start_new(self, chat_id, phone):
        self.start_new_calls.append((chat_id, phone))
        outcome = self.start_new_outcomes.get(chat_id, AuthOutcome(result=AuthResult.CODE_SENT))
        if outcome.result == AuthResult.CODE_SENT:
            self._pending.add(chat_id)
        return outcome

    async def start_reauthorize(self, chat_id, account):
        outcome = self.start_reauth_outcomes.get(chat_id, AuthOutcome(result=AuthResult.CODE_SENT))
        if outcome.result == AuthResult.CODE_SENT:
            self._pending.add(chat_id)
        return outcome

    async def submit_code(self, chat_id, code):
        outcome = self.code_outcomes.pop(0) if self.code_outcomes else AuthOutcome(result=AuthResult.FAILED)
        if outcome.result not in (AuthResult.NEEDS_PASSWORD, AuthResult.INVALID_CODE):
            self._pending.discard(chat_id)
        return outcome

    async def submit_password(self, chat_id, password):
        outcome = self.password_outcomes.pop(0) if self.password_outcomes else AuthOutcome(result=AuthResult.FAILED)
        if outcome.result != AuthResult.INVALID_PASSWORD:
            self._pending.discard(chat_id)
        return outcome

    async def cancel(self, chat_id):
        self.cancelled_chat_ids.append(chat_id)
        self._pending.discard(chat_id)

    def has_pending(self, chat_id):
        return chat_id in self._pending


class _Fixture:
    def __init__(self, tmp_path, *, trusted_admin_user_ids=frozenset({_TRUSTED_ID})):
        self.db_path = tmp_path / "inviter.db"
        self.accounts = TelegramAccountRepository(self.db_path)
        self.campaigns = InviteCampaignRepository(self.db_path)
        self.invites = UserCampaignInviteRepository(self.db_path)
        self.runtime_state = InviterRuntimeStateRepository(self.db_path)
        self.states = AdminBotConversationStateRepository(":memory:")
        self.service = InviterAdminService(
            self.accounts, self.campaigns, self.invites, self.runtime_state,
            db_path=self.db_path, trusted_admin_user_ids=trusted_admin_user_ids,
        )
        self.auth = _FakeAuthCoordinator()
        self.controller = AdminBotController(
            self.service, self.auth, self.states, sync_client_factory=lambda account: None,
        )

    def close(self):
        self.accounts.close()
        self.campaigns.close()
        self.invites.close()
        self.runtime_state.close()
        self.states.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


def _make_account(fx, *, name="@vvz982", telegram_user_id=100, daily_limit=15, enabled=True):
    return fx.accounts.create(
        name=name, phone="+995500000001", session_name=name.lstrip("@"),
        session_path=str(fx.db_path.parent / "sessions" / name.lstrip("@")),
        daily_limit=daily_limit, enabled=enabled, telegram_user_id=telegram_user_id,
    )


# ---- 1. unauthorized user denied ----


async def test_unauthorized_user_sees_access_denied_and_no_data(fx):
    _make_account(fx)

    reply = await fx.controller.handle_text(texts.ACCOUNTS_LABEL, chat_id=_OTHER_ID, telegram_user_id=_OTHER_ID)

    assert reply.text == texts.ACCESS_DENIED_TEXT
    assert reply.accounts_page_options is None
    assert reply.show_main_menu is False


def test_unauthorized_user_cannot_open_account_card(fx):
    account = _make_account(fx)

    reply = fx.controller.handle_account_open(account.id, telegram_user_id=_OTHER_ID)

    assert reply.text == texts.ACCESS_DENIED_TEXT
    assert reply.account_card_id is None


def test_unauthorized_user_cannot_toggle_account(fx):
    account = _make_account(fx, enabled=True)

    reply = fx.controller.handle_account_toggle(account.id, telegram_user_id=_OTHER_ID)

    assert reply.text == texts.ACCESS_DENIED_TEXT
    assert fx.accounts.get(account.id).enabled is True  # ничего не изменилось


async def test_unauthorized_user_start_and_pause_are_denied(fx):
    start_reply = await fx.controller.handle_text(texts.START_LABEL, chat_id=_OTHER_ID, telegram_user_id=_OTHER_ID)
    pause_reply = await fx.controller.handle_text(texts.PAUSE_LABEL, chat_id=_OTHER_ID, telegram_user_id=_OTHER_ID)

    assert start_reply.text == texts.ACCESS_DENIED_TEXT
    assert pause_reply.text == texts.ACCESS_DENIED_TEXT
    assert fx.service.is_global_enabled() is True  # default, никем не тронуто


# ---- 2. accounts list ----


async def test_trusted_user_sees_accounts_list(fx):
    _make_account(fx, name="@vvz982", telegram_user_id=1)
    _make_account(fx, name="@ib85gnat", telegram_user_id=2, enabled=False)

    reply = await fx.controller.handle_text(texts.ACCOUNTS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert reply.text == texts.ACCOUNTS_HEADER
    names = {name for _id, name, _enabled in reply.accounts_page_options}
    assert names == {"@vvz982", "@ib85gnat"}


async def test_accounts_list_empty_shows_helpful_message(fx):
    reply = await fx.controller.handle_text(texts.ACCOUNTS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    assert reply.text == texts.NO_ACCOUNTS_TEXT


# ---- 3. account card ----


def test_open_account_shows_card(fx):
    account = _make_account(fx, name="@vvz982", daily_limit=15)

    reply = fx.controller.handle_account_open(account.id, telegram_user_id=_TRUSTED_ID)

    assert "@vvz982" in reply.text
    assert "Telegram ID: 100" in reply.text
    assert reply.account_card_id == account.id
    assert reply.account_card_enabled is True


def test_open_missing_account_fails_safely(fx):
    reply = fx.controller.handle_account_open(999999, telegram_user_id=_TRUSTED_ID)
    assert reply.text == texts.ACTION_FAILED_TEXT


# ---- 4. enable/disable account ----


def test_toggle_account_from_card_flow(fx):
    account = _make_account(fx, enabled=True)

    reply = fx.controller.handle_account_toggle(account.id, telegram_user_id=_TRUSTED_ID)

    assert fx.accounts.get(account.id).enabled is False
    options_by_id = {aid: enabled for aid, _name, enabled in reply.accounts_page_options}
    assert options_by_id[account.id] is False


def test_accounts_back_returns_to_list(fx):
    _make_account(fx)
    reply = fx.controller.handle_accounts_back(telegram_user_id=_TRUSTED_ID)
    assert reply.text == texts.ACCOUNTS_HEADER


# ---- 5. daily limit update ----


def test_limit_choice_screen_shows_current_usage(fx):
    account = _make_account(fx, daily_limit=15)

    reply = fx.controller.handle_account_limit_open(account.id, telegram_user_id=_TRUSTED_ID)

    assert "Сегодня: 0 / 15" in reply.text
    assert reply.limit_choice_account_id == account.id


def test_limit_value_selection_updates_and_shows_card(fx):
    account = _make_account(fx, daily_limit=15)

    reply = fx.controller.handle_account_limit_value(account.id, 25, telegram_user_id=_TRUSTED_ID)

    assert fx.accounts.get(account.id).daily_limit == 25
    assert reply.account_card_id == account.id


async def test_manual_limit_flow_end_to_end(fx):
    account = _make_account(fx, daily_limit=15)

    prompt_reply = fx.controller.handle_account_limit_manual_prompt(
        account.id, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID,
    )
    assert prompt_reply.text == texts.LIMIT_MANUAL_PROMPT_TEXT

    final_reply = await fx.controller.handle_text("42", chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert fx.accounts.get(account.id).daily_limit == 42
    assert final_reply.account_card_id == account.id


async def test_manual_limit_rejects_non_numeric_input(fx):
    account = _make_account(fx, daily_limit=15)
    fx.controller.handle_account_limit_manual_prompt(account.id, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    reply = await fx.controller.handle_text("не число", chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert "❌" in reply.text
    assert fx.accounts.get(account.id).daily_limit == 15  # не изменилось


# ---- 8/9/10. Успешная авторизация: телефон -> код -> (2FA) -> готово ----


async def test_add_account_phone_step_prompts_for_code(fx):
    phone_reply = await fx.controller.handle_text(texts.ADD_ACCOUNT_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    assert phone_reply.text == texts.PHONE_PROMPT

    code_reply = await fx.controller.handle_text("+995571024864", chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert code_reply.text == texts.CODE_SENT_TEXT
    assert fx.auth.start_new_calls == [(_CHAT_ID, "+995571024864")]


async def test_add_account_rejects_invalid_phone_format(fx):
    await fx.controller.handle_text(texts.ADD_ACCOUNT_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    reply = await fx.controller.handle_text("not a phone", chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert reply.text == texts.INVALID_PHONE_TEXT
    assert fx.auth.start_new_calls == []  # никогда не дошло до Telethon


async def test_add_account_code_step_authorizes_without_2fa(fx):
    account = TelegramAccount(
        id=1, name="@newacc", phone="995500000001", session_name="newacc", session_path="x",
        daily_limit=30, enabled=True, created_at=datetime(2026, 1, 1), last_used_at=None,  # noqa: DTZ001
        telegram_user_id=42,
    )
    fx.auth.code_outcomes = [AuthOutcome(result=AuthResult.AUTHORIZED, account=account)]

    await fx.controller.handle_text(texts.ADD_ACCOUNT_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    await fx.controller.handle_text("+995571024864", chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    final_reply = await fx.controller.handle_text("13579", chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert "@newacc" in final_reply.text
    assert final_reply.show_main_menu is True
    assert fx.states.get(_CHAT_ID) is None  # состояние диалога сброшено


async def test_add_account_code_step_needs_password_then_authorizes(fx):
    fx.auth.code_outcomes = [AuthOutcome(result=AuthResult.NEEDS_PASSWORD)]
    account = TelegramAccount(
        id=2, name="@withpw", phone="995500000002", session_name="withpw", session_path="x",
        daily_limit=30, enabled=True, created_at=datetime(2026, 1, 1), last_used_at=None,  # noqa: DTZ001
        telegram_user_id=43,
    )
    fx.auth.password_outcomes = [AuthOutcome(result=AuthResult.AUTHORIZED, account=account)]

    await fx.controller.handle_text(texts.ADD_ACCOUNT_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    await fx.controller.handle_text("+995571024864", chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    password_prompt = await fx.controller.handle_text("13579", chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert password_prompt.text == texts.PASSWORD_PROMPT_TEXT

    final_reply = await fx.controller.handle_text("my-2fa-password", chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert "@withpw" in final_reply.text
    assert fx.states.get(_CHAT_ID) is None


async def test_add_account_invalid_code_lets_admin_retry_same_step(fx):
    fx.auth.code_outcomes = [
        AuthOutcome(result=AuthResult.INVALID_CODE, error_summary="Код неверный или устарел. Введите код ещё раз."),
    ]

    await fx.controller.handle_text(texts.ADD_ACCOUNT_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    await fx.controller.handle_text("+995571024864", chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    reply = await fx.controller.handle_text("00000", chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert reply.text == texts.INVALID_CODE_RETRY_TEXT
    state = fx.states.get(_CHAT_ID)
    assert state is not None and state.step == "awaiting_code"  # остаёмся на этом же шаге


async def test_cancel_during_auth_flow_stops_conversation_and_cancels_coordinator(fx):
    await fx.controller.handle_text(texts.ADD_ACCOUNT_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    await fx.controller.handle_text("+995571024864", chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    reply = await fx.controller.handle_text(texts.CANCEL_BUTTON_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert reply.show_main_menu is True
    assert fx.states.get(_CHAT_ID) is None
    assert _CHAT_ID in fx.auth.cancelled_chat_ids


# ---- 11. secrets never persisted in conversation state ----


async def test_phone_code_and_password_never_land_in_conversation_state_payload(fx):
    """phone — не секрет, допустим в payload (нужен для повторного
    start_new при ретрае и т.п.); код/пароль НИКОГДА не должны попасть в
    payload — они существуют только как параметры вызова auth-координатора
    (см. reader/inviter_admin_bot/auth.py про их обработку)."""
    fx.auth.code_outcomes = [AuthOutcome(result=AuthResult.NEEDS_PASSWORD)]

    await fx.controller.handle_text(texts.ADD_ACCOUNT_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    await fx.controller.handle_text("+995571024864", chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    state_after_phone = fx.states.get(_CHAT_ID)
    assert state_after_phone.payload == {"phone": "+995571024864"}

    secret_code = "13579"
    await fx.controller.handle_text(secret_code, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    state_after_code = fx.states.get(_CHAT_ID)
    assert state_after_code.step == "awaiting_password"
    payload_str = str(state_after_code.payload)
    assert secret_code not in payload_str

    # Сырая БД-строка тоже не должна содержать код — не только Python-объект.
    raw_row = fx.states._conn.execute(
        "SELECT payload FROM inviter_admin_bot_conversation_state WHERE chat_id = ?", (_CHAT_ID,),
    ).fetchone()
    assert secret_code not in (raw_row[0] or "")


# ---- 13. global inviter pause/resume ----


async def test_pause_and_start_toggle_global_state(fx):
    pause_reply = await fx.controller.handle_text(texts.PAUSE_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    assert pause_reply.text == texts.GLOBAL_PAUSED_TEXT
    assert fx.service.is_global_enabled() is False

    start_reply = await fx.controller.handle_text(texts.START_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    assert start_reply.text == texts.GLOBAL_ENABLED_TEXT
    assert fx.service.is_global_enabled() is True


async def test_status_screen_shows_pause_state(fx):
    await fx.controller.handle_text(texts.PAUSE_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    reply = await fx.controller.handle_text(texts.STATUS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert "приостановлено" in reply.text
