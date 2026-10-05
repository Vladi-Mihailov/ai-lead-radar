"""DmDraftController — "✉️ ЛС-кампании → 📨 Черновики": ручная проверка
ЛС-черновиков оператором (✅ Отправить / ✏️ Изменить / ⏭ Пропустить).

Тот же принцип, что и у DmCampaignController: без Telethon, возвращает
BotReply с inline_rows. is_trusted() проверяется заново на КАЖДОМ действии
(callback, ввод текста правки, построение карточки) — callback_data сам по
себе авторизацией не является. Переходы draft -> approved/skipped и правка
— атомарные UPDATE ... WHERE status='draft' (см. DmOutreachRepository):
второй оператор получает "этот черновик уже обработан".

✅ Отправить сейчас только переводит черновик в approved: реальной отправки
в Telegram здесь нет и быть не может (у контроллера нет ни клиента, ни
аккаунтов) — она появится вместе с выбором отправителя (Phase 3B)."""

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from reader.dm_campaigns.outreach_repository import STATUS_DRAFT, DmOutreach, DmOutreachRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.inviter_admin_bot import dm_draft_callbacks as cb
from reader.inviter_admin_bot import dm_draft_texts as texts
from reader.inviter_admin_bot.conversation import BotReply
from reader.inviter_admin_bot.conversation_state_repository import (
    AdminBotConversationStateRepository,
    AdminConversationState,
)
from reader.inviter_admin_bot.texts import ACCESS_DENIED_TEXT

logger = logging.getLogger(__name__)

STEP_AWAITING_DM_DRAFT_EDIT = "awaiting_dm_draft_edit"

_QUEUE_PAGE = 10

InlineRows = list[list[tuple[str, bytes]]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DmDraftController:
    def __init__(
        self,
        outreach_repository: DmOutreachRepository,
        campaign_repository: DmCampaignRepository,
        state_repository: AdminBotConversationStateRepository,
        *,
        is_trusted: Callable[[int], bool],
        back_callback: bytes,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self._outreach = outreach_repository
        self._campaigns = campaign_repository
        self._states = state_repository
        self._is_trusted = is_trusted
        # Куда ведёт "⬅️ Назад" из меню черновиков (список ЛС-кампаний).
        self._back = back_callback
        self._clock = clock

    # ---- построение экранов ----

    def _titles(self) -> dict[int, str]:
        return {c.id: c.title for c in self._campaigns.list_campaigns()}

    def _menu_reply(self, *, prefix: str | None = None) -> BotReply:
        counts = {q: self._outreach.count_by_status(texts.QUEUE_STATUS[q]) for q in cb.QUEUES}
        text = texts.format_menu(counts)
        rows: InlineRows = [
            [(f"{texts.QUEUE_LABELS[q]} ({counts[q]})", cb.encode(cb.ACTION_QUEUE, queue=q))] for q in cb.QUEUES
        ]
        rows.append([(texts.BACK_LABEL, self._back)])
        return BotReply(text=f"{prefix}\n\n{text}" if prefix else text, inline_rows=rows)

    def _queue_reply(self, queue: str) -> BotReply:
        status = texts.QUEUE_STATUS[queue]
        items = self._outreach.list_by_status(status, limit=_QUEUE_PAGE)
        titles = self._titles()
        rows: InlineRows = [
            [(texts.queue_item_label(item, titles), cb.encode(cb.ACTION_OPEN, outreach_id=item.id))] for item in items
        ]
        rows.append([(texts.BACK_LABEL, cb.MENU)])
        return BotReply(
            text=texts.format_queue(queue, items, self._outreach.count_by_status(status)), inline_rows=rows,
        )

    def card_reply(self, item: DmOutreach, *, notice: str | None = None) -> BotReply:
        title = self._titles().get(item.campaign_id, f"кампания #{item.campaign_id}")
        rows: InlineRows = []
        if item.status == STATUS_DRAFT:
            rows.append([
                (texts.APPROVE_LABEL, cb.encode(cb.ACTION_APPROVE, outreach_id=item.id)),
                (texts.EDIT_LABEL, cb.encode(cb.ACTION_EDIT, outreach_id=item.id)),
                (texts.SKIP_LABEL, cb.encode(cb.ACTION_SKIP, outreach_id=item.id)),
            ])
        rows.append([(texts.DRAFTS_LABEL, cb.MENU)])
        return BotReply(text=texts.format_card(item, campaign_title=title, notice=notice), inline_rows=rows)

    def notification_reply(self, item: DmOutreach, *, telegram_user_id: int) -> BotReply | None:
        """Карточка нового черновика для рассылки оператору; None — этому
        пользователю показывать нельзя (не trusted)."""
        if not self._is_trusted(telegram_user_id):
            return None
        return self.card_reply(item)

    # ---- callbacks ----

    def handle_menu(self, *, telegram_user_id: int) -> BotReply:
        if not self._is_trusted(telegram_user_id):
            return BotReply(text=ACCESS_DENIED_TEXT)
        return self._menu_reply()

    def handle_callback(self, data: bytes, *, chat_id: int, telegram_user_id: int) -> BotReply | None:
        """None — не callback раздела "📨 Черновики"."""
        if not cb.is_draft_callback(data):
            return None
        if not self._is_trusted(telegram_user_id):
            return BotReply(text=ACCESS_DENIED_TEXT)

        parsed = cb.decode(data)
        if parsed is None:
            return self._menu_reply(prefix=texts.STALE_BUTTON_TEXT)
        if parsed.action == "menu":
            return self._menu_reply()
        if parsed.action == cb.ACTION_QUEUE:
            return self._queue_reply(parsed.queue)

        item = self._outreach.get(parsed.outreach_id)
        if item is None:
            return self._menu_reply(prefix=texts.DRAFT_NOT_FOUND_TEXT)
        if parsed.action == cb.ACTION_OPEN:
            return self.card_reply(item)
        if parsed.action == cb.ACTION_APPROVE:
            return self._decide(item, approve=True, telegram_user_id=telegram_user_id)
        if parsed.action == cb.ACTION_SKIP:
            return self._decide(item, approve=False, telegram_user_id=telegram_user_id)
        if parsed.action == cb.ACTION_EDIT:
            if item.status != STATUS_DRAFT:
                return self.card_reply(item, notice=texts.ALREADY_PROCESSED_TEXT)
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_DM_DRAFT_EDIT,
                payload={"outreach_id": item.id},
            )
            return BotReply(text=texts.format_edit_prompt(item), show_cancel_button=True)
        return self._menu_reply(prefix=texts.STALE_BUTTON_TEXT)

    def _decide(self, item: DmOutreach, *, approve: bool, telegram_user_id: int) -> BotReply:
        now = self._clock()
        if approve:
            done = self._outreach.approve(item.id, operator_id=telegram_user_id, now=now)
        else:
            done = self._outreach.skip(item.id, operator_id=telegram_user_id, now=now)
        current = self._outreach.get(item.id) or item
        if not done:
            return self.card_reply(current, notice=texts.ALREADY_PROCESSED_TEXT)
        logger.info(
            "dm_outreach id=%s: оператор %s", item.id, "одобрил (без отправки)" if approve else "пропустил",
        )
        return self.card_reply(current, notice=texts.APPROVED_TEXT if approve else texts.SKIPPED_TEXT)

    # ---- FSM: новый текст черновика ----

    def handle_state_input(
        self, state: AdminConversationState, text: str, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        if not self._is_trusted(telegram_user_id):
            return BotReply(text=ACCESS_DENIED_TEXT)
        outreach_id = (state.payload or {}).get("outreach_id")
        item = self._outreach.get(outreach_id) if outreach_id is not None else None
        if item is None:
            self._states.clear(chat_id)
            return self._menu_reply(prefix=texts.DRAFT_NOT_FOUND_TEXT)
        try:
            done = self._outreach.edit_primary_text(item.id, text, operator_id=telegram_user_id, now=self._clock())
        except ValueError as exc:
            return BotReply(text=f"❌ {exc}\n\n{texts.format_edit_prompt(item)}", show_cancel_button=True)
        self._states.clear(chat_id)
        current = self._outreach.get(item.id) or item
        if not done:
            return self.card_reply(current, notice=texts.ALREADY_PROCESSED_TEXT)
        return self.card_reply(current, notice=texts.EDITED_TEXT)
