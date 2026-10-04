"""DmCampaignController — раздел "✉️ ЛС-кампании" inviter_admin_bot
(Phase 1: только настройка кампаний, ничего не отправляет).

Тот же принцип, что и у AdminBotController (conversation.py): ничего не
знает про Telethon — возвращает BotReply, а inline-кнопки описывает
данными (inline_rows: [(подпись, callback_data)]), handlers.py превращает
их в Button. is_trusted() проверяется заново на КАЖДОМ входе (меню,
callback, продолжение FSM); идентификаторы из callback_data и payload FSM
всегда перечитываются из БД.

Источник аккаунтов — ТОЛЬКО существующий TelegramAccountRepository
(telegram_accounts): ни копии данных аккаунта, ни сессий здесь нет.
Источник групп — config/groups.yaml через groups_provider (тот же
Group.identifier, что использует Reader)."""

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from reader.dm_campaigns.models import DmCampaign, normalize_list_input
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.groups import Group
from reader.inviter.models import TelegramAccount
from reader.inviter.repository import TelegramAccountRepository
from reader.inviter_admin_bot import dm_campaign_callbacks as cb
from reader.inviter_admin_bot import dm_campaign_texts as texts
from reader.inviter_admin_bot.conversation import BotReply
from reader.inviter_admin_bot.conversation_state_repository import (
    AdminBotConversationStateRepository,
    AdminConversationState,
)
from reader.inviter_admin_bot.texts import ACCESS_DENIED_TEXT
from reader.time_display import format_tbilisi

logger = logging.getLogger(__name__)

STEP_AWAITING_DM_GUIDELINE = "awaiting_dm_guideline"
STEP_AWAITING_DM_RESOURCES = "awaiting_dm_resources"
STEP_AWAITING_DM_FOLLOW_UP = "awaiting_dm_follow_up_guideline"
STEP_AWAITING_DM_ACCOUNT_LIMIT = "awaiting_dm_account_limit"

# conversation.DM_STEPS — то, по чему AdminBotController передаёт ввод
# сюда; обязан совпадать с шагами выше (проверяется тестом).

InlineRows = list[list[tuple[str, bytes]]]


def _account_label(account: TelegramAccount) -> str:
    """Та же логика, что и InviterAdminService._display_name."""
    if account.name.startswith("@"):
        return account.name
    if account.telegram_user_id is not None:
        return f"Telegram ID {account.telegram_user_id}"
    return account.name


def _account_status(account: TelegramAccount, now: datetime) -> str | None:
    if account.is_old:
        return "устаревшая запись"
    if not account.enabled:
        return "отключён"
    if account.blocked_until is not None and account.blocked_until > now:
        return f"заблокирован до {format_tbilisi(account.blocked_until)}"
    return None


def _group_label(group: Group) -> str:
    return group.title or (f"@{group.username}" if group.username else str(group.id))


def _same_source(a: str, b: str) -> bool:
    return a.strip().lstrip("@").lower() == b.strip().lstrip("@").lower()


class DmCampaignController:
    def __init__(
        self,
        repository: DmCampaignRepository,
        account_repository: TelegramAccountRepository,
        state_repository: AdminBotConversationStateRepository,
        *,
        is_trusted: Callable[[int], bool],
        groups_provider: Callable[[], list[Group]],
    ):
        self._repository = repository
        self._accounts = account_repository
        self._states = state_repository
        self._is_trusted = is_trusted
        self._groups_provider = groups_provider

    # ---- общие ----

    @staticmethod
    def _denied() -> BotReply:
        return BotReply(text=ACCESS_DENIED_TEXT)

    def _groups(self) -> list[Group]:
        try:
            return list(self._groups_provider())
        except Exception:
            logger.warning("ЛС-кампании: не удалось прочитать список групп", exc_info=True)
            return []

    def _list_reply(self, *, prefix: str | None = None) -> BotReply:
        campaigns = self._repository.list_campaigns()
        text = texts.format_list_screen(campaigns)
        if prefix:
            text = f"{prefix}\n\n{text}"
        rows: InlineRows = [
            [(f"{texts.state_icon(c.enabled)} {c.title}", cb.encode(cb.ACTION_OPEN, c.id))]
            for c in campaigns
        ]
        return BotReply(text=text, inline_rows=rows)

    def _not_found(self) -> BotReply:
        return self._list_reply(prefix=texts.CAMPAIGN_NOT_FOUND_TEXT)

    def _source_labels(self, campaign: DmCampaign) -> list[str]:
        groups = self._groups()
        labels = []
        for source in campaign.source_chats:
            group = next((g for g in groups if _same_source(str(g.identifier), source)), None)
            labels.append(_group_label(group) if group else f"{source} (нет в groups.yaml)")
        return labels

    def _card_reply(self, campaign: DmCampaign, *, prefix: str | None = None) -> BotReply:
        text = texts.format_campaign_card(
            campaign,
            source_labels=self._source_labels(campaign),
            accounts_selected=len(self._repository.list_campaign_accounts(campaign.id)),
        )
        if prefix:
            text = f"{prefix}\n\n{text}"
        cid = campaign.id
        rows: InlineRows = [
            [(texts.DISABLE_LABEL, cb.encode(cb.ACTION_DISABLE, cid)) if campaign.enabled
             else (texts.ENABLE_LABEL, cb.encode(cb.ACTION_ENABLE, cid))],
            [(texts.GUIDELINE_LABEL, cb.encode(cb.ACTION_GUIDELINE, cid))],
            [(texts.RESOURCES_LABEL, cb.encode(cb.ACTION_RESOURCES, cid))],
            [(texts.SOURCES_LABEL, cb.encode(cb.ACTION_SOURCES, cid))],
            [(texts.ACCOUNTS_LABEL, cb.encode(cb.ACTION_ACCOUNTS, cid))],
            [(texts.FOLLOW_UP_DISABLE_LABEL, cb.encode(cb.ACTION_FOLLOW_UP_DISABLE, cid)) if campaign.follow_up_enabled
             else (texts.FOLLOW_UP_ENABLE_LABEL, cb.encode(cb.ACTION_FOLLOW_UP_ENABLE, cid))],
            [(texts.FOLLOW_UP_TEXT_LABEL, cb.encode(cb.ACTION_FOLLOW_UP_TEXT, cid))],
            [(texts.BACK_LABEL, cb.LIST)],
        ]
        return BotReply(text=text, inline_rows=rows)

    def _sources_reply(self, campaign: DmCampaign, *, notice: str | None = None) -> BotReply:
        selected = list(campaign.source_chats)
        rows: InlineRows = []
        shown: list[str] = []
        for group in self._groups():
            ident = str(group.identifier)
            is_selected = any(_same_source(ident, s) for s in selected)
            try:
                data = cb.encode(cb.ACTION_SOURCE_TOGGLE, campaign.id, source=ident)
            except ValueError:
                logger.warning("ЛС-кампании: идентификатор группы %r не помещается в callback", ident)
                continue
            rows.append([(f"{'☑' if is_selected else '☐'} {_group_label(group)}", data)])
            shown.append(ident)
        # Выбранные ранее, но уже отсутствующие в groups.yaml — показываем,
        # чтобы их можно было снять, а не прятать молча.
        for source in selected:
            if any(_same_source(source, s) for s in shown):
                continue
            try:
                data = cb.encode(cb.ACTION_SOURCE_TOGGLE, campaign.id, source=source)
            except ValueError:
                continue
            rows.append([(f"☑ {source} — нет в groups.yaml", data)])
        rows.append([(texts.ALL_GROUPS_LABEL, cb.encode(cb.ACTION_SOURCES_ALL, campaign.id))])
        rows.append([(texts.DONE_LABEL, cb.encode(cb.ACTION_OPEN, campaign.id))])
        return BotReply(
            text=texts.format_sources_screen(campaign, selected_count=len(selected), notice=notice),
            inline_rows=rows,
        )

    def _accounts_reply(self, campaign: DmCampaign, *, notice: str | None = None) -> BotReply:
        now = datetime.now(timezone.utc)
        allowed = {entry.account_id: entry for entry in self._repository.list_campaign_accounts(campaign.id)}
        accounts = {a.id: a for a in self._accounts.list()}
        lines: list[str] = []
        rows: InlineRows = []
        for account in accounts.values():
            # is_old (история дублей) — так же, как и на остальных
            # операционных экранах, не предлагается, но если такая запись
            # уже разрешена — показывается, чтобы её можно было снять.
            if account.is_old and account.id not in allowed:
                continue
            entry = allowed.get(account.id)
            label = _account_label(account)
            status = _account_status(account, now)
            mark = "☑" if entry else "☐"
            details = [status] if status else []
            if entry:
                details.append(f"лимит ЛС: {texts.format_limit_value(entry.daily_limit)}")
            lines.append(f"{mark} {label}" + (f" — {', '.join(details)}" if details else ""))
            row = [(f"{mark} {label}", cb.encode(cb.ACTION_ACCOUNT_TOGGLE, campaign.id, account_id=account.id))]
            if entry:
                row.append((
                    f"🔢 {texts.format_limit_value(entry.daily_limit)}",
                    cb.encode(cb.ACTION_ACCOUNT_LIMIT, campaign.id, account_id=account.id),
                ))
            rows.append(row)
        for account_id in allowed:
            if account_id not in accounts:
                lines.append(f"☑ аккаунт #{account_id} — не найден")
                rows.append([(
                    f"☑ аккаунт #{account_id}",
                    cb.encode(cb.ACTION_ACCOUNT_TOGGLE, campaign.id, account_id=account_id),
                )])
        rows.append([(texts.DONE_LABEL, cb.encode(cb.ACTION_OPEN, campaign.id))])
        return BotReply(text=texts.format_accounts_screen(campaign, lines, notice=notice), inline_rows=rows)

    # ---- вход из главного меню ----

    def handle_menu(self, *, telegram_user_id: int) -> BotReply:
        if not self._is_trusted(telegram_user_id):
            return self._denied()
        return self._list_reply()

    # ---- callbacks ----

    def handle_callback(self, data: bytes, *, chat_id: int, telegram_user_id: int) -> BotReply | None:
        """None — это не callback раздела ЛС-кампаний (handlers.py
        продолжит обычную цепочку). Повреждённый/неизвестный dmc_-callback —
        "кнопка устарела" и список кампаний."""
        if not cb.is_dm_callback(data):
            return None
        if not self._is_trusted(telegram_user_id):
            return self._denied()

        parsed = cb.decode(data)
        if parsed is None:
            return self._list_reply(prefix=texts.STALE_BUTTON_TEXT)
        if parsed.action == "list":
            return self._list_reply()

        campaign = self._repository.get_campaign(parsed.campaign_id)
        if campaign is None:
            return self._not_found()

        action = parsed.action
        if action == cb.ACTION_OPEN:
            return self._card_reply(campaign)
        if action in (cb.ACTION_ENABLE, cb.ACTION_DISABLE):
            updated = self._repository.set_enabled(campaign.id, action == cb.ACTION_ENABLE)
            return self._card_reply(updated) if updated else self._not_found()
        if action in (cb.ACTION_FOLLOW_UP_ENABLE, cb.ACTION_FOLLOW_UP_DISABLE):
            updated = self._repository.set_follow_up_enabled(campaign.id, action == cb.ACTION_FOLLOW_UP_ENABLE)
            return self._card_reply(updated) if updated else self._not_found()
        if action == cb.ACTION_GUIDELINE:
            return self._start_text_step(chat_id, telegram_user_id, STEP_AWAITING_DM_GUIDELINE, campaign)
        if action == cb.ACTION_FOLLOW_UP_TEXT:
            return self._start_text_step(chat_id, telegram_user_id, STEP_AWAITING_DM_FOLLOW_UP, campaign)
        if action == cb.ACTION_RESOURCES:
            return self._start_text_step(chat_id, telegram_user_id, STEP_AWAITING_DM_RESOURCES, campaign)
        if action == cb.ACTION_SOURCES:
            return self._sources_reply(campaign)
        if action == cb.ACTION_SOURCES_ALL:
            updated = self._repository.update_source_chats(campaign.id, ())
            return self._sources_reply(updated) if updated else self._not_found()
        if action == cb.ACTION_SOURCE_TOGGLE:
            return self._toggle_source(campaign, parsed.source)
        if action == cb.ACTION_ACCOUNTS:
            return self._accounts_reply(campaign)
        if action == cb.ACTION_ACCOUNT_TOGGLE:
            return self._toggle_account(campaign, parsed.account_id)
        if action == cb.ACTION_ACCOUNT_LIMIT:
            return self._start_limit_step(chat_id, telegram_user_id, campaign, parsed.account_id)
        return self._list_reply(prefix=texts.STALE_BUTTON_TEXT)

    def _toggle_source(self, campaign: DmCampaign, source: str) -> BotReply:
        selected = list(campaign.source_chats)
        existing = next((s for s in selected if _same_source(s, source)), None)
        if existing is not None:
            selected.remove(existing)
        else:
            # Добавить можно только группу, которая реально есть в
            # groups.yaml — callback_data не доверяем.
            group = next((g for g in self._groups() if _same_source(str(g.identifier), source)), None)
            if group is None:
                return self._sources_reply(campaign, notice=texts.STALE_BUTTON_TEXT)
            selected.append(str(group.identifier))
        updated = self._repository.update_source_chats(campaign.id, selected)
        return self._sources_reply(updated) if updated else self._not_found()

    def _toggle_account(self, campaign: DmCampaign, account_id: int) -> BotReply:
        allowed = self._repository.get_campaign_account(campaign.id, account_id) is not None
        if allowed:
            self._repository.set_campaign_account_enabled(campaign.id, account_id, False)
            return self._accounts_reply(campaign)
        account = self._accounts.get(account_id)
        if account is None:
            return self._accounts_reply(campaign, notice="⚠️ Аккаунт не найден.")
        if account.is_old:
            return self._accounts_reply(
                campaign, notice="⚠️ Устаревшую запись аккаунта (is_old) разрешить нельзя.",
            )
        if not self._repository.set_campaign_account_enabled(campaign.id, account_id, True):
            return self._accounts_reply(campaign, notice="⚠️ Не удалось сохранить.")
        return self._accounts_reply(campaign)

    # ---- FSM ----

    def _start_text_step(
        self, chat_id: int, telegram_user_id: int, step: str, campaign: DmCampaign,
    ) -> BotReply:
        self._states.set(chat_id, telegram_user_id=telegram_user_id, step=step, payload={"campaign_id": campaign.id})
        return BotReply(text=self._prompt_for(step, campaign), show_cancel_button=True)

    def _start_limit_step(
        self, chat_id: int, telegram_user_id: int, campaign: DmCampaign, account_id: int,
    ) -> BotReply:
        entry = self._repository.get_campaign_account(campaign.id, account_id)
        account = self._accounts.get(account_id)
        if entry is None or account is None:
            return self._accounts_reply(campaign, notice="⚠️ Аккаунт не разрешён этой кампании.")
        self._states.set(
            chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_DM_ACCOUNT_LIMIT,
            payload={"campaign_id": campaign.id, "account_id": account_id},
        )
        return BotReply(
            text=texts.format_limit_prompt(campaign, _account_label(account), entry.daily_limit),
            show_cancel_button=True,
        )

    @staticmethod
    def _prompt_for(step: str, campaign: DmCampaign) -> str:
        if step == STEP_AWAITING_DM_GUIDELINE:
            return texts.format_guideline_prompt(campaign)
        if step == STEP_AWAITING_DM_FOLLOW_UP:
            return texts.format_follow_up_prompt(campaign)
        return texts.format_resources_prompt(campaign)

    def handle_state_input(
        self, state: AdminConversationState, text: str, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        if not self._is_trusted(telegram_user_id):
            return self._denied()
        payload = state.payload or {}
        campaign = self._repository.get_campaign(payload.get("campaign_id")) if payload.get("campaign_id") else None
        if campaign is None:
            self._states.clear(chat_id)
            return self._not_found()

        raw = text.strip()
        cleared = raw == texts.CLEAR_MARKER

        if state.step == STEP_AWAITING_DM_ACCOUNT_LIMIT:
            return self._handle_limit_input(state, campaign, raw, cleared, chat_id=chat_id)

        try:
            if state.step == STEP_AWAITING_DM_GUIDELINE:
                updated = self._repository.update_guideline(campaign.id, None if cleared else raw)
            elif state.step == STEP_AWAITING_DM_FOLLOW_UP:
                updated = self._repository.update_follow_up_guideline(campaign.id, None if cleared else raw)
            elif state.step == STEP_AWAITING_DM_RESOURCES:
                updated = self._repository.update_resources(campaign.id, () if cleared else normalize_list_input(raw))
            else:
                self._states.clear(chat_id)
                return self._list_reply(prefix=texts.STALE_BUTTON_TEXT)
        except ValueError as exc:
            return BotReply(
                text=texts.format_invalid_input(str(exc), self._prompt_for(state.step, campaign)),
                show_cancel_button=True,
            )

        self._states.clear(chat_id)
        if updated is None:
            return self._not_found()
        return self._card_reply(updated, prefix=texts.SAVED_TEXT)

    def _handle_limit_input(
        self, state: AdminConversationState, campaign: DmCampaign, raw: str, cleared: bool, *, chat_id: int,
    ) -> BotReply:
        account_id = (state.payload or {}).get("account_id")
        account = self._accounts.get(account_id) if account_id is not None else None
        entry = self._repository.get_campaign_account(campaign.id, account_id) if account_id is not None else None
        if account is None or entry is None:
            self._states.clear(chat_id)
            return self._accounts_reply(campaign, notice="⚠️ Аккаунт больше не разрешён этой кампании.")

        prompt = texts.format_limit_prompt(campaign, _account_label(account), entry.daily_limit)
        if cleared:
            value = None
        else:
            try:
                value = int(raw)
            except ValueError:
                return BotReply(text=texts.format_invalid_input("Нужно целое число.", prompt), show_cancel_button=True)
        try:
            updated = self._repository.set_campaign_account_daily_limit(campaign.id, account_id, value)
        except ValueError as exc:
            return BotReply(text=texts.format_invalid_input(str(exc), prompt), show_cancel_button=True)

        self._states.clear(chat_id)
        if updated is None:
            return self._accounts_reply(campaign, notice="⚠️ Аккаунт больше не разрешён этой кампании.")
        return self._accounts_reply(campaign, notice=texts.SAVED_TEXT)
