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
from typing import TYPE_CHECKING

from reader.inviter.models import TelegramAccount
from reader.inviter_admin_bot import dm_campaign_texts as dm_texts
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

# Шаги FSM раздела "✉️ ЛС-кампании" — те же строки, что и
# dm_campaign_controller.STEP_AWAITING_DM_* (продублированы здесь, т.к.
# dm_campaign_controller импортирует BotReply из этого модуля).
DM_STEPS = frozenset({
    "awaiting_dm_guideline",
    "awaiting_dm_resources",
    "awaiting_dm_follow_up_guideline",
    "awaiting_dm_account_limit",
})

if TYPE_CHECKING:
    from reader.inviter_admin_bot.dm_campaign_controller import DmCampaignController


@dataclass
class BotReply:
    text: str
    show_main_menu: bool = False
    show_cancel_button: bool = False
    accounts_page_options: list[tuple[int, str, bool, int, int]] | None = None
    account_card_id: int | None = None
    account_card_enabled: bool | None = None
    # Phase 3A: текущие права аккаунта для кнопок карточки (📩 ЛС / 👥 Инвайты).
    account_card_dm: bool = False
    account_card_invite: bool = False
    # Экран подтверждения включения инвайтов для этого аккаунта.
    invite_confirm_account_id: int | None = None
    limit_choice_account_id: int | None = None
    limits_choice_account_id: int | None = None
    # (campaign_id, label, enabled) — список "📣 Кампании".
    campaigns_page_options: list[tuple[int, str, bool]] | None = None
    # Экран одной кампании: id + enabled + есть ли у неё пул лидов
    # (только тогда показывается "🔄 Обновить лиды").
    campaign_card_id: int | None = None
    campaign_card_enabled: bool | None = None
    campaign_card_has_pool: bool = False
    # "✉️ ЛС-кампании" (см. dm_campaign_controller.py): inline-кнопки,
    # описанные данными [(подпись, callback_data)] — handlers.py строит из
    # них Button, контроллер о Telethon не знает.
    inline_rows: list[list[tuple[str, bytes]]] | None = None


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
        dm_campaigns: "DmCampaignController | None" = None,
    ):
        self._service = service
        self._auth = auth_coordinator
        self._states = state_repository
        # None — раздел "✉️ ЛС-кампании" не подключён (поведение бота
        # полностью прежнее), см. dm_campaign_controller.py.
        self._dm_campaigns = dm_campaigns
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

    def handle_dm_callback(self, data: bytes | None, *, chat_id: int, telegram_user_id: int) -> BotReply | None:
        """None — не callback раздела "✉️ ЛС-кампании" (или раздел не
        подключён); права проверяет сам DmCampaignController."""
        if self._dm_campaigns is None:
            return None
        return self._dm_campaigns.handle_callback(data, chat_id=chat_id, telegram_user_id=telegram_user_id)

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
        # Глобальные ▶️ Запустить/⏸ Приостановить заменены включением
        # каждой кампании отдельно (см. 📣 Кампании) — эти тексты больше не
        # обрабатываются как команды (см. runtime_state_repository.py про
        # перенос прежней глобальной паузы в кампании).
        if stripped == texts.CAMPAIGNS_LABEL:
            return self._format_campaigns_reply()
        if self._dm_campaigns is not None and stripped == dm_texts.DM_CAMPAIGNS_LABEL:
            self._states.clear(chat_id)
            return self._dm_campaigns.handle_menu(telegram_user_id=telegram_user_id)
        if stripped == texts.STATUS_LABEL:
            return BotReply(
                text=texts.format_account_statuses(
                    self._service.list_account_statuses(),
                    inviter_enabled=self._service.is_global_enabled(),
                    campaigns=self._service.list_campaigns(),
                ),
                show_main_menu=True,
            )
        if stripped == texts.SYNC_LABEL:
            return await self._handle_sync_all()
        if stripped == texts.HELP_LABEL:
            return BotReply(text=texts.HELP_TEXT, show_main_menu=True)

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
            if self._dm_campaigns is not None and state.step in DM_STEPS:
                return self._dm_campaigns.handle_state_input(
                    state, text, chat_id=chat_id, telegram_user_id=telegram_user_id,
                )

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

        # return_to="accounts_list" — ручной ввод лимита, открытый третьей
        # кнопкой строки "👤 Аккаунты" (см. handle_limits_manual_prompt),
        # возвращает на СПИСОК "👤 Аккаунты" с уже обновлённым значением
        # (см. design "возвращаемся к списку"), а не на карточку — ручной
        # ввод, открытый с карточки (payload без return_to,
        # handle_account_limit_manual_prompt), поведение НЕ изменилось.
        if payload.get("return_to") == "accounts_list":
            return BotReply(
                text=texts.format_limit_updated(account.name, value),
                accounts_page_options=self._accounts_page_options(),
            )
        return BotReply(
            text=texts.format_limit_updated(account.name, value),
            account_card_id=account.id, account_card_enabled=account.enabled,
            account_card_dm=account.can_send_dm, account_card_invite=account.can_invite_to_groups,
        )

    def _accounts_page_options(self) -> list[tuple[int, str, bool, int, int]]:
        return [
            (e.id, e.display_name, e.enabled, e.sent_today, e.daily_limit)
            for e in self._service.list_accounts()
        ]

    def _format_accounts_reply(self) -> BotReply:
        options = self._accounts_page_options()
        if not options:
            return BotReply(text=texts.NO_ACCOUNTS_TEXT, show_main_menu=True)
        return BotReply(text=texts.ACCOUNTS_HEADER, accounts_page_options=options)

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
            account_card_dm=card.account.can_send_dm, account_card_invite=card.account.can_invite_to_groups,
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
        # fail-closed (см. InviterAdminService.toggle_enabled) — даже
        # вручную сформированный callback на is_old-запись не включает её,
        # только показывает понятную причину отказа (см. design "Нельзя
        # включить OLD account").
        if account.is_old:
            return BotReply(text=texts.OLD_ACCOUNT_TOGGLE_BLOCKED_TEXT, show_main_menu=True)
        return self._format_accounts_reply()

    # ---- Phase 3A: права аккаунта (📩 ЛС / 👥 Инвайты) ----

    def handle_account_dm_toggle(self, account_id: int, *, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        account = self._service.toggle_can_send_dm(account_id)
        return self._card_after_capability_change(account_id, account, telegram_user_id)

    def handle_account_invite_toggle(self, account_id: int, *, telegram_user_id: int) -> BotReply:
        """Выключение инвайтов — сразу; включение — только через экран
        подтверждения (handle_account_invite_confirm)."""
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        card = self._service.account_card(account_id)
        if card is None:
            return BotReply(text=texts.ACTION_FAILED_TEXT, show_main_menu=True)
        if card.account.is_old:
            return BotReply(text=texts.OLD_ACCOUNT_CAPABILITY_BLOCKED_TEXT, show_main_menu=True)
        if not card.account.can_invite_to_groups:
            return BotReply(
                text=f"{card.display_name}\n\n{texts.INVITE_CONFIRM_TEXT}", invite_confirm_account_id=account_id,
            )
        account = self._service.set_can_invite_to_groups(account_id, False)
        return self._card_after_capability_change(account_id, account, telegram_user_id)

    def handle_account_invite_confirm(self, account_id: int, *, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        account = self._service.set_can_invite_to_groups(account_id, True)
        return self._card_after_capability_change(account_id, account, telegram_user_id)

    def _card_after_capability_change(
        self, account_id: int, account: TelegramAccount | None, telegram_user_id: int,
    ) -> BotReply:
        if account is None:
            return BotReply(text=texts.ACTION_FAILED_TEXT, show_main_menu=True)
        if account.is_old:
            return BotReply(text=texts.OLD_ACCOUNT_CAPABILITY_BLOCKED_TEXT, show_main_menu=True)
        return self.handle_account_open(account_id, telegram_user_id=telegram_user_id)

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
            account_card_dm=account.can_send_dm, account_card_invite=account.can_invite_to_groups,
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

    # ---- 👤 Аккаунты: третья кнопка "USED / LIMIT" — выбор нового значения,
    # возврат в тот же список (см. keyboards.py::limits_value_choice_keyboard
    # — бывший отдельный экран "⚙️ Лимиты" удалён, см. задачу "объедини
    # экраны") ----

    def handle_limits_open(self, account_id: int, *, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        card = self._service.account_card(account_id)
        if card is None:
            return BotReply(text=texts.ACTION_FAILED_TEXT, show_main_menu=True)
        return BotReply(
            text=texts.format_limit_prompt(card.display_name, card.usage.sent_today, card.usage.daily_limit),
            limits_choice_account_id=account_id,
        )

    def handle_limits_value(self, account_id: int, value: int, *, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        account = self._service.set_daily_limit(account_id, value)
        if account is None:
            return BotReply(text=texts.ACTION_FAILED_TEXT, show_main_menu=True)
        return BotReply(
            text=texts.format_limit_updated(account.name, value),
            accounts_page_options=self._accounts_page_options(),
        )

    def handle_limits_manual_prompt(
        self, account_id: int, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        account = self._service.get_account(account_id)
        if account is None:
            return BotReply(text=texts.ACTION_FAILED_TEXT, show_main_menu=True)
        self._states.set(
            chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_MANUAL_LIMIT,
            payload={"account_id": account_id, "return_to": "accounts_list"},
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
            return BotReply(
                text=text, account_card_id=account_id, account_card_enabled=card.account.enabled,
                account_card_dm=card.account.can_send_dm, account_card_invite=card.account.can_invite_to_groups,
            )
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
            account_card_dm=account.can_send_dm, account_card_invite=account.can_invite_to_groups,
        )

    # ---- 📣 Кампании ----

    def _format_campaigns_reply(self) -> BotReply:
        entries = self._service.list_campaigns()
        if not entries:
            return BotReply(text=texts.format_campaigns_list(entries), show_main_menu=True)
        return BotReply(
            text=texts.format_campaigns_list(entries),
            campaigns_page_options=[(e.id, e.label, e.enabled) for e in entries],
        )

    def _campaign_card_reply(self, campaign_id: int, *, prefix: str | None = None) -> BotReply:
        stats = self._service.campaign_stats(campaign_id)
        if stats is None:
            return BotReply(text=texts.CAMPAIGN_NOT_FOUND_TEXT, show_main_menu=True)
        text = texts.format_campaign_card(stats)
        if prefix:
            text = f"{prefix}\n\n{text}"
        return BotReply(
            text=text, campaign_card_id=stats.campaign_id,
            campaign_card_enabled=stats.enabled, campaign_card_has_pool=stats.has_pool,
        )

    def handle_campaigns_back(self, *, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        return self._format_campaigns_reply()

    def handle_campaign_open(self, campaign_id: int, *, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        return self._campaign_card_reply(campaign_id)

    def handle_campaign_set_enabled(
        self, campaign_id: int, enabled: bool, *, telegram_user_id: int,
    ) -> BotReply:
        """▶️/⏸ — ТОЛЬКО эта кампания; явное целевое состояние (а не
        toggle), поэтому повторное нажатие устаревшей кнопки безопасно."""
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        if self._service.set_campaign_enabled(campaign_id, enabled) is None:
            return BotReply(text=texts.CAMPAIGN_NOT_FOUND_TEXT, show_main_menu=True)
        return self._campaign_card_reply(campaign_id)

    def handle_campaign_stats(self, campaign_id: int, *, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        stats = self._service.campaign_stats(campaign_id)
        if stats is None:
            return BotReply(text=texts.CAMPAIGN_NOT_FOUND_TEXT, show_main_menu=True)
        return BotReply(
            text=texts.format_campaign_stats(stats), campaign_card_id=stats.campaign_id,
            campaign_card_enabled=stats.enabled, campaign_card_has_pool=stats.has_pool,
        )

    async def handle_campaign_refresh(self, campaign_id: int, *, telegram_user_id: int) -> BotReply:
        if not self.is_trusted(telegram_user_id):
            return self._denied()
        outcome = await self._service.refresh_campaign_leads(campaign_id)
        return self._campaign_card_reply(campaign_id, prefix=texts.format_lead_refresh(outcome))

    async def _handle_sync_all(self) -> BotReply:
        summary = await self._service.sync_all_accounts(self._sync_client_factory)
        return BotReply(text=texts.format_sync_summary(summary), show_main_menu=True)
