"""AdminBotController — пошаговые диалоги reader/inviter_admin_bot/, поверх
InviterAdminService + AccountAuthCoordinator. Намеренно ничего не знает про
Telethon Button/events (тот же принцип, что и у
reader/public_bot/conversation.py) — возвращает BotReply (что показать),
handlers.py конвертирует поля в реальные Telethon-кнопки.

ДОСТУП (см. design "ДОСТУП К ADMIN BOT"): is_trusted() проверяется ЗАНОВО
на КАЖДОМ вызове по РЕАЛЬНОМУ telegram_user_id — account_id/значения в
callback_data публичны и НЕ являются доказательством авторизации сами по
себе (тот же security-инвариант, что и во всех остальных bot-модулях
проекта)."""

from collections.abc import Callable
from dataclasses import dataclass

from reader.inviter.models import TelegramAccount
from reader.inviter_admin_bot import texts
from reader.inviter_admin_bot.auth import AccountAuthCoordinator, TelethonAuthClientLike
from reader.inviter_admin_bot.conversation_state_repository import (
    AdminBotConversationStateRepository,
)
from reader.inviter_admin_bot.models import AuthResult
from reader.inviter_admin_bot.service import InviterAdminService

STEP_AWAITING_PHONE = "awaiting_phone"
STEP_AWAITING_CODE = "awaiting_code"
STEP_AWAITING_PASSWORD = "awaiting_password"
STEP_AWAITING_MANUAL_LIMIT = "awaiting_manual_limit"


@dataclass
class BotReply:
    text: str
    show_main_menu: bool = False
    show_cancel_button: bool = False
    accounts_page_options: list[tuple[int, str, bool]] | None = None
    account_card_id: int | None = None
    account_card_enabled: bool | None = None
    limit_choice_account_id: int | None = None


def _looks_like_phone(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("+"):
        return False
    digits = stripped[1:]
    return digits.isdigit() and 7 <= len(digits) <= 15


class AdminBotController:
    def __init__(
        self,
        service: InviterAdminService,
        auth_coordinator: AccountAuthCoordinator,
        state_repository: AdminBotConversationStateRepository,
        *,
        sync_client_factory: Callable[[TelegramAccount], TelethonAuthClientLike],
    ):
        self._service = service
        self._auth = auth_coordinator
        self._states = state_repository
        # sync_one_account()/sync_all_accounts() (InviterAdminService) —
        # тот же client_factory(account) приём инъекции, что и у
        # InviterService/reader/inviter/manage.py::sync_accounts (см.
        # design: переиспользовать существующую identity-логику, а не
        # писать вторую). Один и тот же factory для "🔄 Синхронизировать"
        # (все аккаунты) и карточки ("🔄 Проверить/синхронизировать" —
        # один аккаунт).
        self._sync_client_factory = sync_client_factory

    def is_trusted(self, telegram_user_id: int) -> bool:
        return self._service.is_trusted(telegram_user_id)

    def _denied(self) -> BotReply:
        return BotReply(text=texts.ACCESS_DENIED_TEXT)

    # ---- текстовые сообщения (меню + шаги диалога) ----

    async def handle_text(self, text: str, *, chat_id: int, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()

        stripped = text.strip()

        if stripped in ("/start", texts.CANCEL_BUTTON_LABEL):
            await self._auth.cancel(chat_id)
            self._states.clear(chat_id)
            if stripped == texts.CANCEL_BUTTON_LABEL:
                return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)

        if stripped == texts.ACCOUNTS_LABEL:
            return self._format_accounts_reply()
        if stripped == texts.ADD_ACCOUNT_LABEL:
            self._states.set(chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_PHONE)
            return BotReply(text=texts.PHONE_PROMPT, show_cancel_button=True)
        if stripped == texts.START_LABEL:
            self._service.set_global_enabled(True)
            return BotReply(text=texts.GLOBAL_ENABLED_TEXT, show_main_menu=True)
        if stripped == texts.PAUSE_LABEL:
            self._service.set_global_enabled(False)
            return BotReply(text=texts.GLOBAL_PAUSED_TEXT, show_main_menu=True)
        if stripped == texts.STATUS_LABEL:
            return BotReply(text=texts.format_status(self._service.status_snapshot()), show_main_menu=True)
        if stripped == texts.LIMITS_LABEL:
            return self._format_accounts_reply()
        if stripped == texts.SYNC_LABEL:
            return await self._handle_sync_all()

        state = self._states.get(chat_id)
        if state is not None and state.telegram_user_id == telegram_user_id:
            if state.step == STEP_AWAITING_PHONE:
                return await self._handle_phone_input(stripped, chat_id=chat_id, telegram_user_id=telegram_user_id)
            if state.step == STEP_AWAITING_CODE:
                return await self._handle_code_input(stripped, chat_id=chat_id, telegram_user_id=telegram_user_id, payload=state.payload)
            if state.step == STEP_AWAITING_PASSWORD:
                return await self._handle_password_input(stripped, chat_id=chat_id, telegram_user_id=telegram_user_id, payload=state.payload)
            if state.step == STEP_AWAITING_MANUAL_LIMIT:
                return self._handle_manual_limit_input(stripped, state.payload or {})

        return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)

    async def _handle_phone_input(self, phone: str, *, chat_id: int, telegram_user_id: int) -> BotReply:
        if not _looks_like_phone(phone):
            return BotReply(text=texts.INVALID_PHONE_TEXT, show_cancel_button=True)

        outcome = await self._auth.start_new(chat_id, phone)
        if outcome.result == AuthResult.CODE_SENT:
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_CODE,
                payload={"phone": phone},
            )
            return BotReply(text=texts.CODE_SENT_TEXT, show_cancel_button=True)

        self._states.clear(chat_id)
        return BotReply(text=texts.format_auth_failed(outcome.error_summary or "Не удалось начать авторизацию."), show_main_menu=True)

    async def _handle_code_input(
        self, code: str, *, chat_id: int, telegram_user_id: int, payload: dict | None,
    ) -> BotReply:
        outcome = await self._auth.submit_code(chat_id, code)
        return await self._handle_auth_step_outcome(
            outcome, chat_id=chat_id, telegram_user_id=telegram_user_id, payload=payload,
        )

    async def _handle_password_input(
        self, password: str, *, chat_id: int, telegram_user_id: int, payload: dict | None,
    ) -> BotReply:
        outcome = await self._auth.submit_password(chat_id, password)
        return await self._handle_auth_step_outcome(
            outcome, chat_id=chat_id, telegram_user_id=telegram_user_id, payload=payload,
        )

    async def _handle_auth_step_outcome(
        self, outcome, *, chat_id: int, telegram_user_id: int, payload: dict | None,
    ) -> BotReply:
        if outcome.result == AuthResult.NEEDS_PASSWORD:
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_PASSWORD, payload=payload,
            )
            return BotReply(text=texts.PASSWORD_PROMPT_TEXT, show_cancel_button=True)
        if outcome.result == AuthResult.INVALID_CODE:
            return BotReply(text=texts.INVALID_CODE_RETRY_TEXT, show_cancel_button=True)
        if outcome.result == AuthResult.INVALID_PASSWORD:
            return BotReply(text=texts.INVALID_PASSWORD_RETRY_TEXT, show_cancel_button=True)

        self._states.clear(chat_id)
        if outcome.result == AuthResult.AUTHORIZED and outcome.account is not None:
            display_name = outcome.account.name
            return BotReply(text=texts.format_auth_success(display_name), show_main_menu=True)

        return BotReply(
            text=texts.format_auth_failed(outcome.error_summary or "Не удалось авторизовать аккаунт."),
            show_main_menu=True,
        )

    def _handle_manual_limit_input(self, raw: str, payload: dict) -> BotReply:
        account_id = payload.get("account_id")
        try:
            value = int(raw.strip())
            if value < 1:
                raise ValueError
        except ValueError:
            return BotReply(text=texts.format_invalid_manual_limit_after_prompt(), show_cancel_button=True)

        if account_id is None:
            return BotReply(text=texts.ACTION_FAILED_TEXT, show_main_menu=True)

        account = self._service.set_daily_limit(account_id, value)
        if account is None:
            return BotReply(text=texts.ACTION_FAILED_TEXT, show_main_menu=True)
        return BotReply(
            text=texts.format_limit_updated(account.name, value),
            account_card_id=account.id, account_card_enabled=account.enabled,
        )

    def _format_accounts_reply(self) -> BotReply:
        entries = self._service.list_accounts()
        if not entries:
            return BotReply(text=texts.NO_ACCOUNTS_TEXT, show_main_menu=True)
        return BotReply(
            text=texts.ACCOUNTS_HEADER,
            accounts_page_options=[(e.id, e.display_name, e.enabled) for e in entries],
        )

    # ---- 👤 Аккаунты: открыть карточку / toggle / лимит / sync / reauth ----

    def handle_account_open(self, account_id: int, *, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        card = self._service.account_card(account_id)
        if card is None:
            return BotReply(text=texts.ACTION_FAILED_TEXT, show_main_menu=True)
        return BotReply(
            text=texts.format_account_card(card),
            account_card_id=account_id, account_card_enabled=card.account.enabled,
        )

    def handle_accounts_back(self, *, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        return self._format_accounts_reply()

    def handle_account_toggle(self, account_id: int, *, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        account = self._service.toggle_enabled(account_id)
        if account is None:
            return BotReply(text=texts.ACTION_FAILED_TEXT, show_main_menu=True)
        return self._format_accounts_reply()

    def handle_account_limit_open(self, account_id: int, *, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        card = self._service.account_card(account_id)
        if card is None:
            return BotReply(text=texts.ACTION_FAILED_TEXT, show_main_menu=True)
        return BotReply(
            text=texts.format_limit_prompt(card.display_name, card.usage.sent_today, card.usage.daily_limit),
            limit_choice_account_id=account_id,
        )

    def handle_account_limit_value(self, account_id: int, value: int, *, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        account = self._service.set_daily_limit(account_id, value)
        if account is None:
            return BotReply(text=texts.ACTION_FAILED_TEXT, show_main_menu=True)
        return BotReply(
            text=texts.format_limit_updated(account.name, value),
            account_card_id=account.id, account_card_enabled=account.enabled,
        )

    def handle_account_limit_manual_prompt(
        self, account_id: int, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        account = self._service.get_account(account_id)
        if account is None:
            return BotReply(text=texts.ACTION_FAILED_TEXT, show_main_menu=True)
        self._states.set(
            chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_MANUAL_LIMIT,
            payload={"account_id": account_id},
        )
        return BotReply(text=texts.LIMIT_MANUAL_PROMPT_TEXT, show_cancel_button=True)

    async def handle_account_sync(self, account_id: int, *, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        outcome = await self._service.sync_one_account(account_id, self._sync_client_factory)
        if outcome is None:
            return BotReply(text=texts.ACTION_FAILED_TEXT, show_main_menu=True)
        card = self._service.account_card(account_id)
        text = texts.format_sync_one_result(outcome)
        if card is not None:
            return BotReply(text=text, account_card_id=account_id, account_card_enabled=card.account.enabled)
        return BotReply(text=text, show_main_menu=True)

    async def handle_account_reauthorize(
        self, account_id: int, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        account = self._service.get_account(account_id)
        if account is None:
            return BotReply(text=texts.ACTION_FAILED_TEXT, show_main_menu=True)

        outcome = await self._auth.start_reauthorize(chat_id, account)
        if outcome.result == AuthResult.CODE_SENT:
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_CODE,
                payload={"phone": account.phone, "account_id": account_id},
            )
            return BotReply(text=texts.CODE_SENT_TEXT, show_cancel_button=True)

        return BotReply(
            text=texts.format_auth_failed(outcome.error_summary or "Не удалось начать переавторизацию."),
            account_card_id=account_id, account_card_enabled=account.enabled,
        )

    async def _handle_sync_all(self) -> BotReply:
        summary = await self._service.sync_all_accounts(self._sync_client_factory)
        return BotReply(text=texts.format_sync_summary(summary), show_main_menu=True)
