"""Ручной режим ЛС-кампаний: черновик -> карточка trusted-оператору в
inviter_admin_bot ("✉️ ЛС-кампании → 📨 Черновики") -> ✅ approved /
✏️ правка / ⏭ skipped. Атомарные переходы, повторная проверка прав на
каждом действии, режим кампании по умолчанию manual, никакой отправки ЛС.
Плюс: @tplgee вне insurance — только при страховом вопросе.
Без Telegram и OpenAI."""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.dm_campaigns.models import MODE_AUTO, MODE_MANUAL
from reader.dm_campaigns.outreach_repository import (
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_PENDING_CONTEXT,
    STATUS_SKIPPED,
    DmOutreachRepository,
)
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.resources import TPLGEE, allowed_resources
from reader.dm_campaigns.sendability import SENDABILITY_PREMIUM_REQUIRED, SENDABILITY_USERNAME
from reader.inviter_admin_bot import dm_campaign_callbacks as dmc
from reader.inviter_admin_bot import dm_draft_callbacks as cb
from reader.inviter_admin_bot import dm_draft_texts as texts
from reader.inviter_admin_bot.conversation_state_repository import AdminBotConversationStateRepository
from reader.inviter_admin_bot.dm_campaign_controller import DmCampaignController
from reader.inviter_admin_bot.dm_draft_controller import STEP_AWAITING_DM_DRAFT_EDIT, DmDraftController
from reader.inviter_admin_bot.dm_draft_notifier import DmDraftNotifier
from reader.inviter.repository import TelegramAccountRepository
from reader.inviter_admin_bot.texts import ACCESS_DENIED_TEXT

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
ADMIN, ADMIN2, STRANGER = 100, 101, 200
CHAT = 100
RECIPIENT_ID, RECIPIENT_USERNAME = 987654321, "lead_person"
AI_TEXT = "Несколько участников пишут, что очередь около двух часов."


class _Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach = DmCampaignRepository(path), DmOutreachRepository(path)
    states, accounts = AdminBotConversationStateRepository(path), TelegramAccountRepository(path)
    clock = _Clock()
    drafts = DmDraftController(
        outreach, campaigns, states, is_trusted=lambda uid: uid in (ADMIN, ADMIN2),
        back_callback=dmc.LIST, clock=clock,
    )
    section = DmCampaignController(
        campaigns, accounts, states, is_trusted=lambda uid: uid in (ADMIN, ADMIN2),
        groups_provider=lambda: [], drafts=drafts,
    )
    yield path, campaigns, outreach, states, drafts, section, clock
    for repo in (campaigns, outreach, states, accounts):
        repo.close()


def _draft(campaigns, outreach, *, key="border_queue", message_id=1, generated=T0,
           sendability=SENDABILITY_USERNAME):
    campaign = campaigns.get_campaign_by_key(key)
    row_id = outreach.insert_candidate(
        campaign_id=campaign.id, source_chat_id=-1001, source_chat_identifier="VerhniyLars",
        source_chat_title="Верхний Ларс", source_message_id=message_id, source_message_at=generated,
        source_link=f"https://t.me/VerhniyLars/{message_id}", source_reply_to_msg_id=None,
        recipient_user_id=RECIPIENT_ID, recipient_username=RECIPIENT_USERNAME,
        source_text="Что сейчас на Ларсе?", status=STATUS_PENDING_CONTEXT, now=generated,
        draft_after_at=generated, sendability=sendability,
    )
    assert outreach.claim(row_id, generated)
    assert outreach.mark_draft(
        row_id, now=generated, context_json="{}", primary_text=AI_TEXT, follow_up_text=None,
        evidence_strength="several_consistent", used_context_refs=["S1", "A1"],
    )
    return row_id


def _press(section, action, row_id, user=ADMIN):
    return section.handle_callback(cb.encode(action, outreach_id=row_id), chat_id=user, telegram_user_id=user)


def _buttons(reply):
    return [label for row in reply.inline_rows or [] for label, _ in row]


# ---- очередь и карточка ----


def test_new_draft_appears_in_operator_queue(env):
    _, campaigns, outreach, _, _, section, _ = env
    row_id = _draft(campaigns, outreach)

    menu = section.handle_callback(cb.MENU, chat_id=CHAT, telegram_user_id=ADMIN)
    assert "🆕 Новые: 1" in menu.text
    assert _buttons(menu)[:4] == ["🆕 Новые (1)", "✅ Одобренные (0)", "⏭ Пропущенные (0)", "❌ Ошибки (0)"]

    queue = section.handle_callback(cb.encode(cb.ACTION_QUEUE, queue=cb.QUEUE_NEW), chat_id=CHAT, telegram_user_id=ADMIN)
    assert any(label.startswith(f"#{row_id} ") for label in _buttons(queue))

    card = _press(section, cb.ACTION_OPEN, row_id)
    assert _buttons(card)[:3] == [texts.APPROVE_LABEL, texts.EDIT_LABEL, texts.SKIP_LABEL]
    for expected in ("Кампания: 🚧 Очереди на границе", "Группа: Верхний Ларс", "Что сейчас на Ларсе?",
                     "Sendability: username", AI_TEXT):
        assert expected in card.text


def test_drafts_entry_is_on_dm_campaigns_screen(env):
    *_, section, _ = env
    reply = section.handle_menu(telegram_user_id=ADMIN)
    assert (texts.DRAFTS_LABEL, cb.MENU) in [b for row in reply.inline_rows for b in row]


def test_card_hides_recipient_ids_and_context_refs(env):
    _, campaigns, outreach, _, _, section, _ = env
    row_id = _draft(campaigns, outreach, sendability=SENDABILITY_PREMIUM_REQUIRED)
    card = _press(section, cb.ACTION_OPEN, row_id).text
    assert str(RECIPIENT_ID) not in card and RECIPIENT_USERNAME not in card
    assert "S1" not in card and "A1" not in card and "access_hash" not in card
    assert "premium_required" in card


# ---- действия оператора ----


def test_trusted_operator_can_approve_without_sending(env):
    _, campaigns, outreach, _, _, section, clock = env
    row_id = _draft(campaigns, outreach)
    clock.now = T0 + timedelta(minutes=5)
    reply = _press(section, cb.ACTION_APPROVE, row_id)
    row = outreach.get(row_id)
    assert (row.status, row.reviewed_by, row.reviewed_at) == (STATUS_APPROVED, ADMIN, clock.now)
    assert reply.text.startswith(texts.APPROVED_TEXT) and texts.NOT_SENT_NOTE in reply.text
    assert texts.APPROVE_LABEL not in _buttons(reply)  # больше нечего нажимать


def test_trusted_operator_can_skip_and_it_leaves_new_queue(env):
    _, campaigns, outreach, _, _, section, clock = env
    row_id = _draft(campaigns, outreach)
    reply = _press(section, cb.ACTION_SKIP, row_id, user=ADMIN2)
    row = outreach.get(row_id)
    assert (row.status, row.reviewed_by, row.reviewed_at) == (STATUS_SKIPPED, ADMIN2, T0)
    assert reply.text.startswith(texts.SKIPPED_TEXT)
    assert outreach.list_by_status(STATUS_DRAFT) == [] and outreach.count_by_status(STATUS_SKIPPED) == 1
    queue = section.handle_callback(cb.encode(cb.ACTION_QUEUE, queue=cb.QUEUE_NEW), chat_id=CHAT, telegram_user_id=ADMIN)
    assert not any(label.startswith(f"#{row_id} ") for label in _buttons(queue))


def test_trusted_operator_can_edit_and_original_is_preserved(env):
    _, campaigns, outreach, states, _, section, clock = env
    row_id = _draft(campaigns, outreach)
    prompt = _press(section, cb.ACTION_EDIT, row_id)
    assert prompt.show_cancel_button and states.get(CHAT).step == STEP_AWAITING_DM_DRAFT_EDIT

    clock.now = T0 + timedelta(minutes=3)
    reply = section.handle_state_input(states.get(CHAT), "Первая правка", chat_id=CHAT, telegram_user_id=ADMIN)
    assert states.get(CHAT) is None
    assert reply.text.startswith(texts.EDITED_TEXT)
    assert _buttons(reply)[:3] == [texts.APPROVE_LABEL, texts.EDIT_LABEL, texts.SKIP_LABEL]

    _press(section, cb.ACTION_EDIT, row_id)
    section.handle_state_input(states.get(CHAT), "Вторая правка", chat_id=CHAT, telegram_user_id=ADMIN)
    row = outreach.get(row_id)
    assert (row.status, row.primary_text, row.original_primary_text) == (STATUS_DRAFT, "Вторая правка", AI_TEXT)
    assert (row.edited_by, row.edited_at) == (ADMIN, clock.now)


def test_edit_rejects_empty_text_and_keeps_waiting(env):
    _, campaigns, outreach, states, _, section, _ = env
    row_id = _draft(campaigns, outreach)
    _press(section, cb.ACTION_EDIT, row_id)
    reply = section.handle_state_input(states.get(CHAT), "   ", chat_id=CHAT, telegram_user_id=ADMIN)
    assert reply.text.startswith("❌") and states.get(CHAT) is not None
    assert outreach.get(row_id).primary_text == AI_TEXT


# ---- права ----


def test_untrusted_user_cannot_act(env):
    _, campaigns, outreach, states, drafts, section, _ = env
    row_id = _draft(campaigns, outreach)
    for action in (cb.ACTION_APPROVE, cb.ACTION_SKIP, cb.ACTION_EDIT, cb.ACTION_OPEN):
        assert _press(section, action, row_id, user=STRANGER).text == ACCESS_DENIED_TEXT
    assert section.handle_callback(cb.MENU, chat_id=STRANGER, telegram_user_id=STRANGER).text == ACCESS_DENIED_TEXT
    assert drafts.notification_reply(outreach.get(row_id), telegram_user_id=STRANGER) is None
    assert states.get(STRANGER) is None
    row = outreach.get(row_id)
    assert (row.status, row.primary_text, row.reviewed_by) == (STATUS_DRAFT, AI_TEXT, None)


def test_edit_input_rechecks_trust(env):
    """Права проверяются и при вводе текста, а не только при нажатии ✏️."""
    _, campaigns, outreach, states, _, _, clock = env
    row_id = _draft(campaigns, outreach)
    revoked = {ADMIN}
    drafts = DmDraftController(outreach, campaigns, states, is_trusted=lambda uid: uid in revoked,
                               back_callback=dmc.LIST, clock=clock)
    drafts.handle_callback(cb.encode(cb.ACTION_EDIT, outreach_id=row_id), chat_id=CHAT, telegram_user_id=ADMIN)
    revoked.clear()
    reply = drafts.handle_state_input(states.get(CHAT), "Подмена", chat_id=CHAT, telegram_user_id=ADMIN)
    assert reply.text == ACCESS_DENIED_TEXT and outreach.get(row_id).primary_text == AI_TEXT


# ---- атомарность / повторные нажатия ----


def test_double_approve_is_prevented(env):
    _, campaigns, outreach, _, _, section, clock = env
    row_id = _draft(campaigns, outreach)
    _press(section, cb.ACTION_APPROVE, row_id, user=ADMIN)
    clock.now = T0 + timedelta(minutes=1)
    second = _press(section, cb.ACTION_APPROVE, row_id, user=ADMIN2)
    assert second.text.startswith(texts.ALREADY_PROCESSED_TEXT)
    row = outreach.get(row_id)
    assert (row.status, row.reviewed_by, row.reviewed_at) == (STATUS_APPROVED, ADMIN, T0)


def test_approve_after_skip_is_rejected(env):
    _, campaigns, outreach, _, _, section, _ = env
    row_id = _draft(campaigns, outreach)
    _press(section, cb.ACTION_SKIP, row_id)
    assert _press(section, cb.ACTION_APPROVE, row_id, user=ADMIN2).text.startswith(texts.ALREADY_PROCESSED_TEXT)
    assert outreach.get(row_id).status == STATUS_SKIPPED


def test_skip_after_approve_is_rejected(env):
    _, campaigns, outreach, _, _, section, _ = env
    row_id = _draft(campaigns, outreach)
    _press(section, cb.ACTION_APPROVE, row_id)
    assert _press(section, cb.ACTION_SKIP, row_id, user=ADMIN2).text.startswith(texts.ALREADY_PROCESSED_TEXT)
    assert outreach.get(row_id).status == STATUS_APPROVED


def test_edit_after_decision_is_rejected(env):
    _, campaigns, outreach, states, _, section, _ = env
    row_id = _draft(campaigns, outreach)
    _press(section, cb.ACTION_EDIT, row_id)          # оператор 1 открыл правку
    _press(section, cb.ACTION_SKIP, row_id, user=ADMIN2)  # оператор 2 успел пропустить
    reply = section.handle_state_input(states.get(CHAT), "Поздняя правка", chat_id=CHAT, telegram_user_id=ADMIN)
    assert reply.text.startswith(texts.ALREADY_PROCESSED_TEXT)
    row = outreach.get(row_id)
    assert (row.status, row.primary_text, row.original_primary_text) == (STATUS_SKIPPED, AI_TEXT, None)
    assert _press(section, cb.ACTION_EDIT, row_id).text.startswith(texts.ALREADY_PROCESSED_TEXT)


def test_repository_transitions_are_atomic(env):
    _, campaigns, outreach, *_ = env
    row_id = _draft(campaigns, outreach)
    assert outreach.approve(row_id, operator_id=ADMIN, now=T0) is True
    assert outreach.approve(row_id, operator_id=ADMIN2, now=T0) is False
    assert outreach.skip(row_id, operator_id=ADMIN2, now=T0) is False
    assert outreach.edit_primary_text(row_id, "x", operator_id=ADMIN2, now=T0) is False


def test_stale_and_unknown_callbacks(env):
    *_, section, _ = env
    assert section.handle_callback(b"dmd_ok:abc", chat_id=CHAT, telegram_user_id=ADMIN).text.startswith(
        texts.STALE_BUTTON_TEXT)
    assert section.handle_callback(cb.encode(cb.ACTION_OPEN, outreach_id=999), chat_id=CHAT,
                                   telegram_user_id=ADMIN).text.startswith(texts.DRAFT_NOT_FOUND_TEXT)


# ---- рассылка карточек операторам ----


async def test_notifier_sends_card_once_to_trusted_operators(env):
    _, campaigns, outreach, _, drafts, _, clock = env
    row_id = _draft(campaigns, outreach)
    sent = []

    async def send(operator_id, reply):
        sent.append((operator_id, reply))

    notifier = DmDraftNotifier(outreach, drafts, send, [ADMIN, ADMIN2, STRANGER], clock=clock)
    assert await notifier.run_once() == 1
    assert sorted(op for op, _ in sent) == [ADMIN, ADMIN2]  # не trusted — карточку не получает
    assert all(_buttons(reply)[:3] == [texts.APPROVE_LABEL, texts.EDIT_LABEL, texts.SKIP_LABEL] for _, reply in sent)
    assert all(f"#{row_id}" in reply.text for _, reply in sent)
    assert outreach.get(row_id).status == STATUS_DRAFT  # уведомление — не решение и не отправка
    assert await notifier.run_once() == 0 and len(sent) == 2  # повторно не рассылается


async def test_notifier_skips_old_drafts_and_survives_send_errors(env):
    _, campaigns, outreach, _, drafts, _, clock = env
    old_id = _draft(campaigns, outreach, message_id=1, generated=T0 - timedelta(days=2))
    new_id = _draft(campaigns, outreach, message_id=2, generated=T0)

    async def broken(operator_id, reply):
        raise RuntimeError("bot was blocked by the user")

    notifier = DmDraftNotifier(outreach, drafts, broken, [ADMIN], clock=clock)
    assert await notifier.run_once() == 1
    assert outreach.get(new_id).operator_notified_at == T0
    assert outreach.get(old_id).operator_notified_at is None  # старый — только в очереди
    assert outreach.count_by_status(STATUS_DRAFT) == 2


# ---- режим кампании ----


def test_default_campaign_mode_is_manual(env):
    _, campaigns, *_ = env
    assert {c.key: c.mode for c in campaigns.list_campaigns()} == {
        "fuel": MODE_MANUAL, "border_queue": MODE_MANUAL, "insurance": MODE_MANUAL}


def test_mode_migration_on_existing_table_defaults_to_manual(tmp_path):
    path = tmp_path / "users.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE dm_campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, "
        "title TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, scenario_name TEXT, ai_guideline TEXT, "
        "resources TEXT, source_chats TEXT, fresh_context_chats TEXT, follow_up_enabled INTEGER NOT NULL "
        "DEFAULT 0, follow_up_guideline TEXT, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO dm_campaigns (key, title, enabled, ai_guideline) VALUES ('insurance', 'S', 1, 'g')")
    conn.commit()
    conn.close()
    repo = DmCampaignRepository(path)
    try:
        insurance = repo.get_campaign_by_key("insurance")
        assert (insurance.mode, insurance.enabled, insurance.ai_guideline) == (MODE_MANUAL, True, "g")
        assert all(c.mode == MODE_MANUAL for c in repo.list_campaigns())
    finally:
        repo.close()


def test_auto_mode_is_reserved_but_cannot_be_enabled(env):
    _, campaigns, *_ = env
    campaign = campaigns.get_campaign_by_key("insurance")
    with pytest.raises(ValueError, match="ещё не реализован"):
        campaigns.set_mode(campaign.id, MODE_AUTO)
    with pytest.raises(ValueError):
        campaigns.set_mode(campaign.id, "turbo")
    assert campaigns.get_campaign(campaign.id).mode == MODE_MANUAL


def test_manual_mode_never_auto_sends(env):
    """В ручном режиме черновик остаётся draft, пока оператор не решит; у
    контроллера и рассыльщика нет ни Telegram-клиента аккаунтов, ни пути к
    отправке ЛС — approved это только отметка решения."""
    _, campaigns, outreach, _, drafts, _, _ = env
    for key in ("insurance", "fuel", "border_queue"):
        campaigns.set_enabled(campaigns.get_campaign_by_key(key).id, True)
    ids = [_draft(campaigns, outreach, key=k, message_id=i) for i, k in enumerate(("insurance", "fuel", "border_queue"), 1)]
    assert [outreach.get(i).status for i in ids] == [STATUS_DRAFT] * 3
    source = "\n".join(
        (PROJECT_ROOT / "reader" / "inviter_admin_bot" / name).read_text(encoding="utf-8")
        for name in ("dm_draft_controller.py", "dm_draft_notifier.py", "dm_draft_texts.py")
    ) + (PROJECT_ROOT / "reader" / "dm_campaigns" / "outreach_repository.py").read_text(encoding="utf-8")
    for forbidden in ("send_message", "forward_messages", "SendMessageRequest", "InputPeerUser"):
        assert forbidden not in source


# ---- @tplgee: только по теме автострахования вне insurance ----


@pytest.mark.parametrize("key,text,expected", [
    ("fuel", "Где сейчас есть 95-й перед Ларсом?", ()),
    ("border_queue", "Что сейчас на Ларсе?", ()),
    ("border_queue", "Большая очередь? И где оформить страховку на машину?", (TPLGEE,)),
    ("insurance", "Где оформить?", (TPLGEE,)),
    (None, "Что сейчас на Ларсе?", ()),
])
def test_tplgee_only_when_insurance_is_relevant(key, text, expected):
    resources = (TPLGEE, "@ProtocolGEbot", "@ProtocolTRbot")
    assert allowed_resources(resources, resource_region="ge", intent_text=text, campaign_key=key) == expected


def test_fines_still_route_protocol_bot_for_border_questions():
    resources = (TPLGEE, "@ProtocolGEbot", "@ProtocolTRbot")
    assert allowed_resources(resources, resource_region="ge", intent_text="Очередь большая? И пришёл штраф",
                             campaign_key="border_queue") == ("@ProtocolGEbot",)


async def test_drafts_existing_before_migration_are_not_pushed(tmp_path):
    """Черновики, созданные до появления ручного режима (колонки
    operator_notified_at ещё нет), после миграции карточкой не рассылаются,
    но видны в очереди; новые после миграции — рассылаются."""
    path = tmp_path / "users.db"
    campaigns, outreach = DmCampaignRepository(path), DmOutreachRepository(path)
    old_id = _draft(campaigns, outreach, message_id=1)
    outreach.close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE dm_outreach DROP COLUMN operator_notified_at")  # как до деплоя
    conn.commit()
    conn.close()

    outreach = DmOutreachRepository(path)  # миграция
    states = AdminBotConversationStateRepository(path)
    clock = _Clock()
    drafts = DmDraftController(outreach, campaigns, states, is_trusted=lambda uid: uid == ADMIN,
                               back_callback=dmc.LIST, clock=clock)
    sent = []

    async def send(operator_id, reply):
        sent.append(reply)

    notifier = DmDraftNotifier(outreach, drafts, send, [ADMIN], clock=clock)
    try:
        assert outreach.get(old_id).operator_notified_at is not None
        assert await notifier.run_once() == 0 and sent == []
        assert [r.id for r in outreach.list_by_status(STATUS_DRAFT)] == [old_id]  # в очереди виден
        new_id = _draft(campaigns, outreach, message_id=2)
        assert await notifier.run_once() == 1 and f"#{new_id}" in sent[0].text
        DmOutreachRepository(path).close()  # повторное открытие ничего не перепомечает
        assert outreach.get(new_id).operator_notified_at == T0
    finally:
        for repo in (campaigns, outreach, states):
            repo.close()
