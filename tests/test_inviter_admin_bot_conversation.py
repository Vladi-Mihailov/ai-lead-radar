"""Тесты reader/inviter_admin_bot/conversation.py::AdminBotController —
диалоги/доступ. AccountAuthCoordinator подменяется fake-реализацией (та же
сигнатура, что и настоящая, см. reader/inviter_admin_bot/auth.py) — никакого
реального Telethon (auth.py уже протестирован отдельно, см.
tests/test_inviter_admin_bot_auth.py)."""

import sys
from datetime import datetime, timedelta, timezone
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


def _make_account(fx, *, name="@vvz982", telegram_user_id=100, daily_limit=15, enabled=True, is_old=False):
    return fx.accounts.create(
        name=name, phone="+995500000001", session_name=name.lstrip("@"),
        session_path=str(fx.db_path.parent / "sessions" / name.lstrip("@")),
        daily_limit=daily_limit, enabled=enabled, telegram_user_id=telegram_user_id, is_old=is_old,
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


# ---- ⚙️ Лимиты: список показывает USED / LIMIT, не enabled ----


async def test_limits_list_shows_used_over_limit_not_enabled(fx):
    _make_account(fx, name="@vladimihailov", telegram_user_id=1, daily_limit=15, enabled=True)
    _make_account(fx, name="@ao777oa777", telegram_user_id=2, daily_limit=15, enabled=False)

    reply = await fx.controller.handle_text(texts.LIMITS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert reply.text == texts.LIMITS_HEADER
    by_name = {name: (used, limit) for _id, name, used, limit in reply.limits_page_options}
    assert by_name == {"@vladimihailov": (0, 15), "@ao777oa777": (0, 15)}
    # enabled нигде не участвует в этих данных — только id/имя/used/daily_limit.
    assert all(len(entry) == 4 for entry in reply.limits_page_options)


async def test_limits_list_used_reuses_inviter_joined_plus_pending_formula(fx):
    """USED — ТА ЖЕ формула joined_today + pending_today, что использует
    сам inviter для расходования daily budget (см. design "не придумывать
    новый счётчик"), а не что-то новое."""
    account = _make_account(fx, name="@vvz982", telegram_user_id=1, daily_limit=15)
    campaign = fx.campaigns.create(name="Campaign", keyword="осаго", target_chat="@t")
    now = datetime.now(timezone.utc)
    fx.invites.create(user_id=1, campaign_id=campaign.id, account_id=account.id, status="joined", invited_at=now, verified_at=now)
    fx.invites.create(user_id=2, campaign_id=campaign.id, account_id=account.id, status="pending", invited_at=now)
    fx.invites.create(user_id=3, campaign_id=campaign.id, account_id=account.id, status="failed", invited_at=now)

    reply = await fx.controller.handle_text(texts.LIMITS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    used_by_id = {aid: used for aid, _name, used, _limit in reply.limits_page_options}
    # joined + pending = 2, failed НЕ считается — та же формула, что и card.usage.
    assert used_by_id[account.id] == 2


async def test_limits_list_empty_shows_helpful_message(fx):
    reply = await fx.controller.handle_text(texts.LIMITS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    assert reply.text == texts.NO_ACCOUNTS_TEXT


def test_limits_open_shows_value_chooser(fx):
    account = _make_account(fx, daily_limit=15)

    reply = fx.controller.handle_limits_open(account.id, telegram_user_id=_TRUSTED_ID)

    assert "Сегодня: 0 / 15" in reply.text
    assert reply.limits_choice_account_id == account.id


def test_limits_value_selection_updates_and_returns_to_limits_list(fx):
    account = _make_account(fx, daily_limit=15)

    reply = fx.controller.handle_limits_value(account.id, 20, telegram_user_id=_TRUSTED_ID)

    assert fx.accounts.get(account.id).daily_limit == 20
    limits_by_id = {aid: limit for aid, _name, _used, limit in reply.limits_page_options}
    assert limits_by_id[account.id] == 20
    # После изменения возвращаемся именно к списку лимитов, не к карточке.
    assert reply.account_card_id is None


async def test_limits_manual_flow_returns_to_limits_list(fx):
    account = _make_account(fx, daily_limit=15)

    prompt_reply = fx.controller.handle_limits_manual_prompt(
        account.id, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID,
    )
    assert prompt_reply.text == texts.LIMIT_MANUAL_PROMPT_TEXT

    final_reply = await fx.controller.handle_text("42", chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert fx.accounts.get(account.id).daily_limit == 42
    limits_by_id = {aid: limit for aid, _name, _used, limit in final_reply.limits_page_options}
    assert limits_by_id[account.id] == 42
    assert final_reply.account_card_id is None


def test_limits_back_returns_to_limits_list(fx):
    _make_account(fx, daily_limit=15)
    reply = fx.controller.handle_limits_back(telegram_user_id=_TRUSTED_ID)
    assert reply.text == texts.LIMITS_HEADER


def test_account_card_limit_flow_still_returns_to_card_unchanged(fx):
    """Регрессия: изменение лимита с карточки аккаунта ("⚙️ Изменить
    лимит") по-прежнему возвращает на карточку, а не на список лимитов —
    новый флоу через "⚙️ Лимиты" НЕ подменяет старый."""
    account = _make_account(fx, daily_limit=15)

    reply = fx.controller.handle_account_limit_value(account.id, 20, telegram_user_id=_TRUSTED_ID)

    assert fx.accounts.get(account.id).daily_limit == 20
    assert reply.account_card_id == account.id
    assert reply.limits_page_options is None


def test_unauthorized_user_cannot_open_or_change_limits(fx):
    account = _make_account(fx, daily_limit=15)

    open_reply = fx.controller.handle_limits_open(account.id, telegram_user_id=_OTHER_ID)
    value_reply = fx.controller.handle_limits_value(account.id, 20, telegram_user_id=_OTHER_ID)

    assert open_reply.text == texts.ACCESS_DENIED_TEXT
    assert value_reply.text == texts.ACCESS_DENIED_TEXT
    assert fx.accounts.get(account.id).daily_limit == 15  # не изменилось


# ---- 📊 Статус: per-account enabled/blocked, раздельно ----


async def test_status_screen_enabled_and_not_blocked(fx):
    _make_account(fx, name="@vvz982", telegram_user_id=1, enabled=True)

    reply = await fx.controller.handle_text(texts.STATUS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert "🟢 @vvz982" in reply.text
    assert "Аккаунт: включён" in reply.text
    assert "Блокировка: нет" in reply.text


async def test_status_screen_disabled_and_not_blocked(fx):
    _make_account(fx, name="@Mihailov_vm", telegram_user_id=1, enabled=False)

    reply = await fx.controller.handle_text(texts.STATUS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert "⚪ @Mihailov_vm" in reply.text
    assert "Аккаунт: выключен" in reply.text
    assert "Блокировка: нет" in reply.text


async def test_status_screen_blocked_until_future_shows_blocked_with_reason(fx):
    account = _make_account(fx, name="@wwww86w", telegram_user_id=1, enabled=True)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    fx.accounts.update(account.id, blocked_until=future, blocked_reason="peer_flood")

    reply = await fx.controller.handle_text(texts.STATUS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert "🔴 @wwww86w" in reply.text
    assert "Аккаунт: включён" in reply.text
    assert "Блокировка: до" in reply.text
    assert "Причина: peer_flood" in reply.text
    assert "Блокировка: нет" not in reply.text  # единственный аккаунт в тесте — заблокирован
    assert "Последняя причина" not in reply.text  # активная блокировка — не историческая


async def test_status_screen_blocked_until_past_shown_as_not_blocked_with_last_reason(fx):
    """blocked_until уже прошёл -> НЕ 🔴, "Блокировка: нет", но
    исторический blocked_reason остаётся видимым отдельной строкой
    "Последняя причина" (см. design "не менять blocked state ради UI")."""
    account = _make_account(fx, name="@vvz982", telegram_user_id=1, enabled=True)
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    fx.accounts.update(account.id, blocked_until=past, blocked_reason="peer_flood")

    reply = await fx.controller.handle_text(texts.STATUS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert "🟢 @vvz982" in reply.text
    assert "🔴" not in reply.text
    assert "Блокировка: нет" in reply.text
    assert "Последняя причина: peer_flood" in reply.text
    assert "Блокировка: до" not in reply.text


async def test_status_screen_no_blocked_reason_hides_last_reason_line(fx):
    """Никогда не блокировался — blocked_reason=None — строки "Последняя
    причина" быть не должно вообще."""
    _make_account(fx, name="@vvz982", telegram_user_id=1, enabled=True)

    reply = await fx.controller.handle_text(texts.STATUS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert "Последняя причина" not in reply.text


async def test_status_screen_shows_used_over_limit_per_account(fx):
    account = _make_account(fx, name="@vvz982", telegram_user_id=1, daily_limit=15)
    campaign = fx.campaigns.create(name="Campaign", keyword="осаго", target_chat="@t")
    now = datetime.now(timezone.utc)
    fx.invites.create(user_id=1, campaign_id=campaign.id, account_id=account.id, status="joined", invited_at=now, verified_at=now)
    fx.invites.create(user_id=2, campaign_id=campaign.id, account_id=account.id, status="pending", invited_at=now)

    reply = await fx.controller.handle_text(texts.STATUS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert "Сегодня: 2 / 15" in reply.text


async def test_status_screen_missing_username_falls_back_to_telegram_id(fx):
    fx.accounts.create(
        name="tg_995500000009", phone="+995500000009", session_name="tg_995500000009",
        session_path=str(fx.db_path.parent / "sessions" / "tg_995500000009"),
        daily_limit=15, enabled=True, telegram_user_id=777777,
    )

    reply = await fx.controller.handle_text(texts.STATUS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert "Telegram ID 777777" in reply.text


async def test_status_screen_enabled_state_not_mixed_with_block_state(fx):
    """enabled/disabled и blocked/unblocked — два разных, независимых
    состояния (см. design): включённый заблокированный аккаунт должен
    показывать ОБЕ строки, а не одну вместо другой."""
    account = _make_account(fx, name="@wwww86w", telegram_user_id=1, enabled=True)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    fx.accounts.update(account.id, blocked_until=future, blocked_reason="peer_flood")

    reply = await fx.controller.handle_text(texts.STATUS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    account_block = next(b for b in reply.text.split("\n\n") if "@wwww86w" in b)
    assert "Аккаунт: включён" in account_block
    assert "Блокировка: до" in account_block


# ---- 👤 Аккаунты: enabled НЕ зависит от глобального inviter_enabled ----


async def test_account_enabled_icon_independent_of_global_pause(fx):
    """▶️ Запустить / ⏸ Приостановить управляют ГЛОБАЛЬНЫМ inviter_enabled;
    🟢/⚪ у конкретного аккаунта — ТОЛЬКО account.enabled (см. design "Не
    смешивать эти состояния") — приостановка глобально не должна погасить
    🟢 у включённого аккаунта."""
    _make_account(fx, name="@vvz982", telegram_user_id=1, enabled=True)

    await fx.controller.handle_text(texts.PAUSE_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    accounts_reply = await fx.controller.handle_text(texts.ACCOUNTS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    enabled_by_name = {name: enabled for _id, name, enabled in accounts_reply.accounts_page_options}
    assert enabled_by_name["@vvz982"] is True  # глобальная пауза не трогает account.enabled

    status_reply = await fx.controller.handle_text(texts.STATUS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    assert "🟢 @vvz982" in status_reply.text  # тоже не 🔴/⚪ из-за глобальной паузы
    assert "Автоприглашение: ⏸ приостановлено" in status_reply.text


async def test_global_pause_does_not_change_account_enabled_flag(fx):
    account = _make_account(fx, enabled=True)

    await fx.controller.handle_text(texts.PAUSE_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert fx.accounts.get(account.id).enabled is True


# ---- ℹ️ Справка ----


async def test_help_screen_accessible_to_trusted_admin(fx):
    reply = await fx.controller.handle_text(texts.HELP_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert reply.text == texts.HELP_TEXT
    assert reply.show_main_menu is True


async def test_help_screen_explains_global_vs_account_state(fx):
    reply = await fx.controller.handle_text(texts.HELP_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert "глобально" in reply.text
    assert "🟢" in reply.text and "⚪" in reply.text and "🔴" in reply.text
    assert "3 / 15" in reply.text


async def test_help_screen_denied_for_unauthorized_user(fx):
    reply = await fx.controller.handle_text(texts.HELP_LABEL, chat_id=_CHAT_ID, telegram_user_id=_OTHER_ID)

    assert reply.text == texts.ACCESS_DENIED_TEXT


# ---- OLD accounts: скрыты из operational-списков, toggle fail-closed ----


async def test_old_account_hidden_from_accounts_limits_and_status_screens(fx):
    current = _make_account(fx, name="@Iv_vla_sov", telegram_user_id=6, is_old=False)
    _make_account(fx, name="@Iv_vla_sov", telegram_user_id=6, is_old=True, enabled=False)

    accounts_reply = await fx.controller.handle_text(texts.ACCOUNTS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    limits_reply = await fx.controller.handle_text(texts.LIMITS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)
    status_reply = await fx.controller.handle_text(texts.STATUS_LABEL, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID)

    assert [a[0] for a in accounts_reply.accounts_page_options] == [current.id]
    assert [o[0] for o in limits_reply.limits_page_options] == [current.id]
    assert status_reply.text.count("@Iv_vla_sov") == 1  # только canonical, не оба


def test_toggle_on_old_account_via_callback_is_blocked_even_if_manually_crafted(fx):
    """F. Даже если callback на is_old account_id сформирован вручную
    (напрямую, минуя список, где его и так не видно после E) —
    переключение отклоняется с понятным текстом, enabled не меняется."""
    old = _make_account(fx, name="@bdlapq", telegram_user_id=9, is_old=True, enabled=True)

    reply = fx.controller.handle_account_toggle(old.id, telegram_user_id=_TRUSTED_ID)

    assert reply.text == texts.OLD_ACCOUNT_TOGGLE_BLOCKED_TEXT
    assert fx.accounts.get(old.id).enabled is True


def test_toggle_on_current_account_still_works_as_before(fx):
    """G. CURRENT-аккаунт по-прежнему переключается как раньше — фикс не
    задевает обычный флоу."""
    current = _make_account(fx, is_old=False, enabled=True)

    reply = fx.controller.handle_account_toggle(current.id, telegram_user_id=_TRUSTED_ID)

    assert fx.accounts.get(current.id).enabled is False
    options_by_id = {aid: enabled for aid, _name, enabled in reply.accounts_page_options}
    assert options_by_id[current.id] is False
