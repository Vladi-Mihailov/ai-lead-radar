"""DmDraftController — "✉️ ЛС-кампании → 📨 Черновики": ручная проверка и
отправка ЛС-черновиков оператором (✅ Отправить / ✏️ Изменить / ⏭ Пропустить).

Тот же принцип, что и у DmCampaignController: без Telethon, возвращает
BotReply с inline_rows. is_trusted() проверяется заново на КАЖДОМ действии
(callback, ввод текста правки, подтверждение отправки, построение карточки)
— callback_data сам по себе авторизацией не является.

✅ Отправить — только проверки без сети (DmSendService.preview): постоянная
причина — сразу "⚠️ Не отправлено", временная — черновик остаётся в
очереди, иначе экран подтверждения с выбранным отправителем. Реальная
отправка — ТОЛЬКО handle_send() после "✅ Да, отправить" (атомарный claim и
один send_message, см. reader/dm_campaigns/send_service.py). Ни очереди, ни
статуса approved контроллер сам не отправляет."""

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from reader.dm_campaigns import send_service as send
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
        send_service: "send.DmSendService | None" = None,
        sender_label: Callable[[int], str | None] = lambda account_id: None,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self._outreach = outreach_repository
        self._campaigns = campaign_repository
        self._states = state_repository
        self._is_trusted = is_trusted
        # Куда ведёт "⬅️ Назад" из меню черновиков (список ЛС-кампаний).
        self._back = back_callback
        # None — отправка не подключена: "✅ Отправить" ничего не отправляет.
        self._send = send_service
        self._sender_label = sender_label
        self._clock = clock

    # ---- построение экранов ----

    def _titles(self) -> dict[int, str]:
        return {c.id: c.title for c in self._campaigns.list_campaigns()}

    def _title(self, item: DmOutreach) -> str:
        return self._titles().get(item.campaign_id, f"кампания #{item.campaign_id}")

    def _menu_reply(self, *, prefix: str | None = None) -> BotReply:
        counts = {q: self._outreach.count_by_statuses(texts.QUEUE_STATUSES[q]) for q in cb.QUEUES}
        text = texts.format_menu(counts)
        rows: InlineRows = [
            [(f"{texts.QUEUE_LABELS[q]} ({counts[q]})", cb.encode(cb.ACTION_QUEUE, queue=q))] for q in cb.QUEUES
        ]
        rows.append([(texts.BACK_LABEL, self._back)])
        return BotReply(text=f"{prefix}\n\n{text}" if prefix else text, inline_rows=rows)

    def _queue_reply(self, queue: str) -> BotReply:
        statuses = texts.QUEUE_STATUSES[queue]
        items = self._outreach.list_by_statuses(statuses, limit=_QUEUE_PAGE)
        titles = self._titles()
        rows: InlineRows = [
            [(texts.queue_item_label(item, titles), cb.encode(cb.ACTION_OPEN, outreach_id=item.id))] for item in items
        ]
        rows.append([(texts.BACK_LABEL, cb.MENU)])
        return BotReply(
            text=texts.format_queue(queue, items, self._outreach.count_by_statuses(statuses)), inline_rows=rows,
        )

    def card_reply(self, item: DmOutreach, *, notice: str | None = None) -> BotReply:
        rows: InlineRows = []
        if item.status == STATUS_DRAFT:
            rows.append([
                (texts.APPROVE_LABEL, cb.encode(cb.ACTION_APPROVE, outreach_id=item.id)),
                (texts.EDIT_LABEL, cb.encode(cb.ACTION_EDIT, outreach_id=item.id)),
                (texts.SKIP_LABEL, cb.encode(cb.ACTION_SKIP, outreach_id=item.id)),
            ])
        rows.append([(texts.DRAFTS_LABEL, cb.MENU)])
        sender = self._sender_label(item.sender_account_id) if item.sender_account_id is not None else None
        return BotReply(
            text=texts.format_card(item, campaign_title=self._title(item), sender_label=sender, notice=notice),
            inline_rows=rows,
        )

    def notification_reply(self, item: DmOutreach, *, telegram_user_id: int) -> BotReply | None:
        """Карточка нового черновика для рассылки оператору; None — этому
        пользователю показывать нельзя (не trusted)."""
        if not self._is_trusted(telegram_user_id):
            return None
        return self.card_reply(item)

    # ---- callbacks (без сети) ----

    def handle_menu(self, *, telegram_user_id: int) -> BotReply:
        if not self._is_trusted(telegram_user_id):
            return BotReply(text=ACCESS_DENIED_TEXT)
        return self._menu_reply()

    def handle_callback(self, data: bytes, *, chat_id: int, telegram_user_id: int) -> BotReply | None:
        """None — не callback раздела "📨 Черновики". "✅ Да, отправить"
        (send) сюда не попадает — только handle_send()."""
        if not cb.is_draft_callback(data):
            return None
        if not self._is_trusted(telegram_user_id):
            return BotReply(text=ACCESS_DENIED_TEXT)

        parsed = cb.decode(data)
        if parsed is None or parsed.action == cb.ACTION_SEND:
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
            return self._preview(item, telegram_user_id=telegram_user_id)
        if parsed.action == cb.ACTION_SKIP:
            return self._skip(item, telegram_user_id=telegram_user_id)
        if parsed.action == cb.ACTION_EDIT:
            if item.status != STATUS_DRAFT:
                return self.card_reply(item, notice=texts.ALREADY_PROCESSED_TEXT)
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_DM_DRAFT_EDIT,
                payload={"outreach_id": item.id},
            )
            return BotReply(text=texts.format_edit_prompt(item), show_cancel_button=True)
        return self._menu_reply(prefix=texts.STALE_BUTTON_TEXT)

    def _skip(self, item: DmOutreach, *, telegram_user_id: int) -> BotReply:
        done = self._outreach.skip(item.id, operator_id=telegram_user_id, now=self._clock())
        current = self._outreach.get(item.id) or item
        if not done:
            return self.card_reply(current, notice=texts.ALREADY_PROCESSED_TEXT)
        logger.info("dm_outreach id=%s: оператор пропустил", item.id)
        return self.card_reply(current, notice=texts.SKIPPED_TEXT)

    def _preview(self, item: DmOutreach, *, telegram_user_id: int) -> BotReply:
        if item.status != STATUS_DRAFT:
            return self.card_reply(item, notice=texts.ALREADY_PROCESSED_TEXT)
        if self._send is None:
            return self.card_reply(item, notice=f"{texts.NOT_SENT_TEXT}\nПричина: отправка не подключена")
        outcome = self._send.preview(item.id, operator_id=telegram_user_id)
        current = self._outreach.get(item.id) or item
        if outcome.kind == send.KIND_READY:
            rows: InlineRows = [
                [(texts.CONFIRM_SEND_LABEL, cb.encode(cb.ACTION_SEND, outreach_id=item.id))],
                [(texts.BACK_LABEL, cb.encode(cb.ACTION_OPEN, outreach_id=item.id))],
            ]
            return BotReply(
                text=texts.format_confirm(current, campaign_title=self._title(current),
                                          sender_label=outcome.sender_label),
                inline_rows=rows,
            )
        return self._outcome_reply(current, outcome)

    def _outcome_reply(self, current: DmOutreach, outcome: "send.SendOutcome") -> BotReply:
        if outcome.kind == send.KIND_ALREADY:
            return self.card_reply(current, notice=texts.ALREADY_PROCESSED_TEXT)
        if outcome.kind in (send.KIND_SENT, send.KIND_BLOCKED, send.KIND_FAILED):
            return self.card_reply(current)  # статус и причина — в самой карточке
        # draft: не отправлено, черновик в очереди (временная причина).
        return self.card_reply(
            current, notice=f"{texts.NOT_SENT_TEXT}\nПричина: {texts.reason_text(outcome.reason)}",
        )

    # ---- "✅ Да, отправить" (единственный путь к Telegram RPC) ----

    async def handle_send(self, data: bytes, *, telegram_user_id: int) -> BotReply:
        if not self._is_trusted(telegram_user_id):
            return BotReply(text=ACCESS_DENIED_TEXT)
        parsed = cb.decode(data)
        if parsed is None or parsed.action != cb.ACTION_SEND:
            return self._menu_reply(prefix=texts.STALE_BUTTON_TEXT)
        item = self._outreach.get(parsed.outreach_id)
        if item is None:
            return self._menu_reply(prefix=texts.DRAFT_NOT_FOUND_TEXT)
        if self._send is None:
            return self.card_reply(item, notice=f"{texts.NOT_SENT_TEXT}\nПричина: отправка не подключена")
        outcome = await self._send.send(item.id, operator_id=telegram_user_id)
        return self._outcome_reply(self._outreach.get(item.id) or item, outcome)

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
