"""Phase 3C: реальная отправка ЛС только по явному действию trusted-оператора
(✅ Отправить -> ✅ Да, отправить). Фейковый Telegram-клиент, без сети.

Проверяется: одна отправка на одно подтверждение, никакой отправки без
клика / для синтетики / для старых approved, выбор отправителя только из
allowlist кампании (can_send_dm, Premium, is_old, блокировки, лимиты),
резолв получателя выбранным отправителем, сверка identity, кулдаун
получателя, обработка ошибок Telegram и неопределённого транспорта."""

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from telethon import errors
from telethon.tl.types import InputPeerChannel, InputPeerUserFromMessage, User

from reader.dm_campaigns import send_service as svc
from reader.dm_campaigns.outreach_repository import (
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_DRAFT,
    STATUS_PENDING_CONTEXT,
    STATUS_SEND_FAILED,
    STATUS_SENT,
    DmOutreachRepository,
)
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.send_service import DmSendService
from reader.dm_campaigns.sender_state import DmSenderStateRepository
from reader.dm_campaigns.sendability import (
    SENDABILITY_PREMIUM_REQUIRED,
    SENDABILITY_SOURCE_MESSAGE,
    SENDABILITY_USERNAME,
)
from reader.inviter.repository import TelegramAccountRepository
from reader.inviter_admin_bot import dm_campaign_callbacks as dmc
from reader.inviter_admin_bot import dm_draft_callbacks as cb
from reader.inviter_admin_bot import dm_draft_texts as texts
from reader.inviter_admin_bot.conversation_state_repository import AdminBotConversationStateRepository
from reader.inviter_admin_bot.dm_draft_controller import DmDraftController
from reader.inviter_admin_bot.dm_draft_notifier import DmDraftNotifier
from reader.inviter_admin_bot.texts import ACCESS_DENIED_TEXT

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
ADMIN, ADMIN2, STRANGER = 100, 101, 200
RECIPIENT_ID, RECIPIENT_USERNAME = 987654321, "lead_person"
SENDER_SELF_ID = 5550001112
AI_TEXT = "Оформить ОСАГО можно онлайн у @tplgee."


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class FakeTelegram:
    """Общий "Telegram" для всех клиентов: что видит каждый отправитель."""

    def __init__(self):
        self.sent = []          # (account_id, peer, text)
        self.connects = 0
        self.authorized = True
        self.users = {RECIPIENT_USERNAME: User(id=RECIPIENT_ID, bot=False, username=RECIPIENT_USERNAME)}
        self.chats = {"VerhniyLars": InputPeerChannel(channel_id=1555, access_hash=0)}
        self.messages = {("VerhniyLars", 501): SimpleNamespace(sender_id=RECIPIENT_ID)}
        self.send_effects = []  # исключения для очередных send_message
        self.connect_exc = None
        self.send_delay = 0.0


class FakeClient:
    def __init__(self, tg: FakeTelegram, account):
        self.tg, self.account = tg, account

    async def connect(self):
        self.tg.connects += 1
        if self.tg.connect_exc:
            raise self.tg.connect_exc

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return self.tg.authorized

    async def get_entity(self, username):
        if username not in self.tg.users:
            raise ValueError(f"No user has {username} as username")
        return self.tg.users[username]

    async def get_input_entity(self, chat_ref):
        if chat_ref not in self.tg.chats:
            raise ValueError("Could not find the input entity")
        return self.tg.chats[chat_ref]

    async def get_messages(self, chat, ids):
        ref = next(k for k, v in self.tg.chats.items() if v == chat)
        return self.tg.messages.get((ref, ids))

    async def send_message(self, peer, text):
        if self.tg.send_delay:
            await asyncio.sleep(self.tg.send_delay)
        if self.tg.send_effects:
            effect = self.tg.send_effects.pop(0)
            if effect is not None:
                raise effect
        self.tg.sent.append((self.account.id, peer, text))
        return SimpleNamespace(id=777 + len(self.tg.sent))

    def __getattr__(self, name):  # любой иной метод Telegram в тесте — ошибка
        raise AssertionError(f"unexpected Telegram call: {name}")


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach = DmCampaignRepository(path), DmOutreachRepository(path)
    accounts, states = TelegramAccountRepository(path), DmSenderStateRepository(path)
    conv = AdminBotConversationStateRepository(path)
    tg, clock = FakeTelegram(), Clock()
    vladi = accounts.create(name="@Vladi_mihailov", phone="", session_name="vladi", session_path="data/sessions/vladi",
                            enabled=False, telegram_user_id=SENDER_SELF_ID, can_send_dm=True, is_premium=False)
    insurance = campaigns.get_campaign_by_key("insurance")
    campaigns.set_campaign_account_enabled(insurance.id, vladi.id, True)
    service = DmSendService(
        outreach, campaigns, accounts, states, client_factory=lambda account: FakeClient(tg, account),
        session_exists=lambda account: True, recipient_cooldown=timedelta(days=7), sender_daily_cap=5, clock=clock,
    )
    controller = DmDraftController(
        outreach, campaigns, conv, is_trusted=lambda uid: uid in (ADMIN, ADMIN2), back_callback=dmc.LIST,
        send_service=service, sender_label=lambda aid: (accounts.get(aid).name if accounts.get(aid) else None),
        clock=clock,
    )
    ns = SimpleNamespace(path=path, campaigns=campaigns, outreach=outreach, accounts=accounts, states=states,
                         tg=tg, clock=clock, service=service, controller=controller, vladi=vladi,
                         insurance=insurance)
    yield ns
    for repo in (campaigns, outreach, accounts, states, conv):
        repo.close()


_seq = iter(range(1000, 100000))


def draft(env, *, key="insurance", sendability=SENDABILITY_USERNAME, username=RECIPIENT_USERNAME,
          user_id=RECIPIENT_ID, chat_id=-1001555, message_id=501, synthetic=False, text=AI_TEXT):
    campaign = env.campaigns.get_campaign_by_key(key)
    row_id = env.outreach.insert_candidate(
        campaign_id=campaign.id, source_chat_id=chat_id, source_chat_identifier="VerhniyLars",
        source_chat_title="Верхний Ларс", source_message_id=message_id if message_id else next(_seq),
        source_message_at=T0, source_link=None, source_reply_to_msg_id=None, recipient_user_id=user_id,
        recipient_username=username, source_text="Где оформить ОСАГО?", status=STATUS_PENDING_CONTEXT, now=T0,
        draft_after_at=T0, sendability=sendability,
        contact_require_premium=sendability == SENDABILITY_PREMIUM_REQUIRED, is_synthetic=synthetic,
    )
    assert env.outreach.claim(row_id, T0)
    assert env.outreach.mark_draft(row_id, now=T0, context_json="{}", primary_text=text, follow_up_text=None,
                                   evidence_strength="none", used_context_refs=[])
    return row_id


def press(env, action, row_id, user=ADMIN):
    return env.controller.handle_callback(cb.encode(action, outreach_id=row_id), chat_id=user, telegram_user_id=user)


def confirm(env, row_id, user=ADMIN):
    return asyncio.run(env.controller.handle_send(cb.encode(cb.ACTION_SEND, outreach_id=row_id),
                                                  telegram_user_id=user))


def click_and_confirm(env, row_id, user=ADMIN):
    preview = press(env, cb.ACTION_APPROVE, row_id, user)
    return preview, confirm(env, row_id, user)


def buttons(reply):
    return [label for row in reply.inline_rows or [] for label, _ in row]


# ---------------- основной путь ----------------


def test_manual_click_sends_once_with_confirmation(env):
    row_id = draft(env)
    preview = press(env, cb.ACTION_APPROVE, row_id)
    assert env.tg.sent == [] and env.tg.connects == 0           # "✅ Отправить" — без сети
    assert "Отправитель: @Vladi_mihailov" in preview.text and buttons(preview)[0] == texts.CONFIRM_SEND_LABEL
    assert env.outreach.get(row_id).status == STATUS_DRAFT

    env.clock.now = T0 + timedelta(minutes=2)
    reply = confirm(env, row_id)
    assert len(env.tg.sent) == 1
    account_id, peer, text = env.tg.sent[0]
    assert (account_id, peer.id, text) == (env.vladi.id, RECIPIENT_ID, AI_TEXT)
    row = env.outreach.get(row_id)
    assert (row.status, row.sender_account_id, row.approved_by, row.telegram_message_id) == (
        STATUS_SENT, env.vladi.id, ADMIN, 778)
    assert (row.approved_at, row.send_started_at, row.sent_at) == (env.clock.now,) * 3
    assert reply.text.startswith(texts.SENT_TEXT) and "Отправитель: @Vladi_mihailov" in reply.text
    assert str(SENDER_SELF_ID) not in reply.text and str(RECIPIENT_ID) not in reply.text
    assert texts.APPROVE_LABEL not in buttons(reply)


def test_no_click_no_send(env):
    draft(env)
    notifier_sent = []

    async def send(op, reply):
        notifier_sent.append(op)

    asyncio.run(DmDraftNotifier(env.outreach, env.controller, send, [ADMIN], clock=env.clock).run_once())
    asyncio.run(asyncio.sleep(0))
    assert notifier_sent == [ADMIN] and env.tg.sent == [] and env.tg.connects == 0


def test_edited_text_is_sent_and_original_kept(env):
    row_id = draft(env)
    assert env.outreach.edit_primary_text(row_id, "Исправленный текст", operator_id=ADMIN, now=T0)
    click_and_confirm(env, row_id)
    assert [t for _, _, t in env.tg.sent] == ["Исправленный текст"]
    assert env.outreach.get(row_id).original_primary_text == AI_TEXT


def test_source_message_resolves_via_selected_sender(env):
    row_id = draft(env, sendability=SENDABILITY_SOURCE_MESSAGE, username=None)
    click_and_confirm(env, row_id)
    (account_id, peer, _), = env.tg.sent
    assert account_id == env.vladi.id and isinstance(peer, InputPeerUserFromMessage)
    assert (peer.user_id, peer.msg_id, peer.peer.channel_id) == (RECIPIENT_ID, 501, 1555)
    assert env.outreach.get(row_id).status == STATUS_SENT


def test_source_message_sender_without_access_is_blocked(env):
    env.tg.chats.clear()
    row_id = draft(env, sendability=SENDABILITY_SOURCE_MESSAGE, username=None)
    _, reply = click_and_confirm(env, row_id)
    row = env.outreach.get(row_id)
    assert (row.status, row.send_error) == (STATUS_BLOCKED, svc.NOT_IN_SOURCE_CHAT) and env.tg.sent == []
    assert texts.NOT_SENT_TEXT in reply.text and "нет доступа к исходной группе" in reply.text


def test_source_message_author_mismatch_is_blocked(env):
    env.tg.messages[("VerhniyLars", 501)] = SimpleNamespace(sender_id=111)
    row_id = draft(env, sendability=SENDABILITY_SOURCE_MESSAGE, username=None)
    click_and_confirm(env, row_id)
    assert env.outreach.get(row_id).send_error == svc.IDENTITY_MISMATCH and env.tg.sent == []


def test_username_identity_mismatch_no_send(env):
    env.tg.users[RECIPIENT_USERNAME] = User(id=42, bot=False, username=RECIPIENT_USERNAME)
    row_id = draft(env)
    click_and_confirm(env, row_id)
    row = env.outreach.get(row_id)
    assert (row.status, row.send_error) == (STATUS_BLOCKED, svc.IDENTITY_MISMATCH) and env.tg.sent == []


# ---------------- никогда не отправляется ----------------


def test_explicit_synthetic_is_never_sent(env):
    row_id = draft(env, synthetic=True)
    assert env.outreach.get(row_id).is_synthetic is True
    reply = press(env, cb.ACTION_APPROVE, row_id)
    assert "синтетическая" in reply.text
    assert confirm(env, row_id).text.startswith(texts.ALREADY_PROCESSED_TEXT)
    assert env.outreach.get(row_id).status == STATUS_BLOCKED and env.tg.sent == [] and env.tg.connects == 0


def test_sql_claim_rejects_synthetic_row_even_bypassing_checks(env):
    """Вторая линия защиты: прямой claim (минуя plan()) синтетику не захватывает."""
    row_id = draft(env, synthetic=True)
    assert env.outreach.claim_for_send(row_id, operator_id=ADMIN, sender_account_id=env.vladi.id, now=T0) is False
    row = env.outreach.get(row_id)
    assert (row.status, row.sender_account_id) == (STATUS_DRAFT, None)
    normal = draft(env, message_id=502)
    assert env.outreach.claim_for_send(normal, operator_id=ADMIN, sender_account_id=env.vladi.id, now=T0) is True


def test_unusual_chat_id_alone_does_not_make_row_synthetic(env):
    """Обычный кандидат (как из Reader — без is_synthetic) всегда 0, какой бы
    ни был chat id: синтетика выставляется только явно."""
    row_id = draft(env, chat_id=-1)
    assert env.outreach.get(row_id).is_synthetic is False
    click_and_confirm(env, row_id)
    assert env.outreach.get(row_id).status == STATUS_SENT and len(env.tg.sent) == 1


def test_reader_pipeline_does_not_set_synthetic():
    observer = (PROJECT_ROOT / "reader" / "dm_campaigns" / "observer.py").read_text(encoding="utf-8")
    assert "is_synthetic" not in observer  # кандидат Reader — значение по умолчанию 0


def test_existing_synthetic_rows_are_marked_by_migration(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach = DmCampaignRepository(path), DmOutreachRepository(path)
    rows = []
    # #32 (chat -1, маркер) -> 1; маркер в обычном чате -> 1; ненастоящий
    # chat id БЕЗ маркера -> 0 (числового правила нет); обычная строка -> 0.
    cases = ((-1, '{"synthetic": true}'), (-1001555, '{"synthetic": true}'), (-1, "{}"), (-1001555, "{}"))
    for chat_id, ctx in cases:
        rid = outreach.insert_candidate(
            campaign_id=campaigns.get_campaign_by_key("insurance").id, source_chat_id=chat_id,
            source_chat_identifier=None, source_chat_title=None, source_message_id=len(rows) + 1,
            source_message_at=T0, source_link=None, source_reply_to_msg_id=None, recipient_user_id=None,
            recipient_username=None, source_text="x", status=STATUS_PENDING_CONTEXT, now=T0, draft_after_at=T0)
        outreach.claim(rid, T0)
        outreach.mark_draft(rid, now=T0, context_json=ctx, primary_text="x", follow_up_text=None,
                            evidence_strength="none", used_context_refs=[])
        rows.append(rid)
    outreach.close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE dm_outreach DROP COLUMN is_synthetic")  # как до Phase 3C
    conn.commit()
    conn.close()
    outreach = DmOutreachRepository(path)
    try:
        assert [outreach.get(r).is_synthetic for r in rows] == [True, True, False, False]
        assert outreach.claim_for_send(rows[0], operator_id=ADMIN, sender_account_id=1, now=T0) is False
    finally:
        outreach.close()
        campaigns.close()


def test_untrusted_operator_cannot_send(env):
    row_id = draft(env)
    assert press(env, cb.ACTION_APPROVE, row_id, user=STRANGER).text == ACCESS_DENIED_TEXT
    assert confirm(env, row_id, user=STRANGER).text == ACCESS_DENIED_TEXT
    assert env.outreach.get(row_id).status == STATUS_DRAFT and env.tg.sent == [] and env.tg.connects == 0


def test_old_approved_rows_are_never_sent(env):
    row_id = draft(env)
    assert env.outreach.approve(row_id, operator_id=ADMIN, now=T0)  # решение до Phase 3C
    assert confirm(env, row_id).text.startswith(texts.ALREADY_PROCESSED_TEXT)
    assert press(env, cb.ACTION_APPROVE, row_id).text.startswith(texts.ALREADY_PROCESSED_TEXT)
    asyncio.run(DmDraftNotifier(env.outreach, env.controller, lambda *a: asyncio.sleep(0), [ADMIN],
                                clock=env.clock).run_once())
    assert env.outreach.get(row_id).status == STATUS_APPROVED and env.tg.sent == []


def test_send_callback_is_not_handled_by_sync_path(env):
    row_id = draft(env)
    reply = press(env, cb.ACTION_SEND, row_id)  # "send" через синхронный путь — устаревшая кнопка
    assert reply.text.startswith(texts.STALE_BUTTON_TEXT) and env.tg.sent == []


# ---------------- выбор отправителя ----------------


def test_sender_not_in_campaign_allowlist_no_send(env):
    env.campaigns.set_campaign_account_enabled(env.insurance.id, env.vladi.id, False)
    other = env.accounts.create(name="@other_sender", phone="", session_name="o", session_path="data/sessions/o",
                                can_send_dm=True)
    fuel = env.campaigns.get_campaign_by_key("fuel")
    env.campaigns.set_campaign_account_enabled(fuel.id, other.id, True)  # в allowlist ДРУГОЙ кампании
    row_id = draft(env)
    reply = press(env, cb.ACTION_APPROVE, row_id)
    assert env.outreach.get(row_id).send_error == svc.NO_ELIGIBLE_SENDER and env.tg.sent == []
    assert "нет подходящего отправителя" in reply.text


def test_can_send_dm_zero_no_send(env):
    env.accounts.update(env.vladi.id, can_send_dm=False)
    row_id = draft(env)
    press(env, cb.ACTION_APPROVE, row_id)
    assert env.outreach.get(row_id).send_error == svc.NO_ELIGIBLE_SENDER and env.tg.sent == []


def test_old_account_or_telegram_block_is_not_selected(env):
    env.accounts.update(env.vladi.id, blocked_until=T0 + timedelta(hours=1))
    first = draft(env)
    press(env, cb.ACTION_APPROVE, first)
    assert env.outreach.get(first).send_error == svc.NO_ELIGIBLE_SENDER
    env.accounts.update(env.vladi.id, blocked_until=None, is_old=True)
    second = draft(env, message_id=502)
    press(env, cb.ACTION_APPROVE, second)
    assert env.outreach.get(second).send_error == svc.NO_ELIGIBLE_SENDER and env.tg.sent == []


def test_premium_required_with_non_premium_sender_no_send(env):
    row_id = draft(env, sendability=SENDABILITY_PREMIUM_REQUIRED)
    reply = press(env, cb.ACTION_APPROVE, row_id)
    row = env.outreach.get(row_id)
    assert (row.status, row.send_error) == (STATUS_BLOCKED, svc.NO_PREMIUM_SENDER) and env.tg.sent == []
    assert "Premium" in reply.text


def test_premium_required_with_premium_sender_sends(env):
    premium = env.accounts.create(name="@alenaogi", phone="", session_name="a", session_path="data/sessions/a",
                                  enabled=False, can_send_dm=True, is_premium=True)
    env.campaigns.set_campaign_account_enabled(env.insurance.id, premium.id, True)
    row_id = draft(env, sendability=SENDABILITY_PREMIUM_REQUIRED)
    click_and_confirm(env, row_id)
    assert [a for a, _, _ in env.tg.sent] == [premium.id]


def test_campaign_not_manual_no_send(env):
    conn = sqlite3.connect(env.path)
    conn.execute("UPDATE dm_campaigns SET mode = 'auto' WHERE key = 'insurance'")
    conn.commit()
    conn.close()
    row_id = draft(env)
    press(env, cb.ACTION_APPROVE, row_id)
    assert env.outreach.get(row_id).send_error == svc.CAMPAIGN_NOT_MANUAL and env.tg.sent == []


# ---------------- атомарность ----------------


def test_double_click_one_rpc_only(env):
    row_id = draft(env)
    env.tg.send_delay = 0.05

    async def both():
        data = cb.encode(cb.ACTION_SEND, outreach_id=row_id)
        return await asyncio.gather(env.controller.handle_send(data, telegram_user_id=ADMIN),
                                    env.controller.handle_send(data, telegram_user_id=ADMIN2))

    first, second = asyncio.run(both())
    assert len(env.tg.sent) == 1 and env.tg.connects == 1
    assert first.text.startswith(texts.SENT_TEXT) and second.text.startswith(texts.ALREADY_PROCESSED_TEXT)
    assert confirm(env, row_id).text.startswith(texts.ALREADY_PROCESSED_TEXT) and len(env.tg.sent) == 1


def test_transport_uncertainty_does_not_auto_retry(env):
    row_id = draft(env)
    env.tg.send_effects = [ConnectionError("Server closed the connection")]
    _, reply = click_and_confirm(env, row_id)
    row = env.outreach.get(row_id)
    assert row.status == STATUS_SEND_FAILED and row.send_error.startswith("uncertain")
    assert "неизвестно, дошло ли сообщение" in reply.text
    assert confirm(env, row_id).text.startswith(texts.ALREADY_PROCESSED_TEXT)
    assert env.tg.connects == 1 and env.tg.sent == []
    # неопределённая отправка считается для кулдауна: второй черновик тому же человеку не уходит
    again = draft(env, message_id=502)
    press(env, cb.ACTION_APPROVE, again)
    assert env.outreach.get(again).send_error == svc.RECENT_DM


def test_transport_error_before_send_returns_to_draft(env):
    row_id = draft(env)
    env.tg.connect_exc = ConnectionError("network down")
    _, reply = click_and_confirm(env, row_id)
    row = env.outreach.get(row_id)
    assert row.status == STATUS_DRAFT and row.send_error.startswith("transport_before_send")
    assert "не отправлено" in reply.text.lower() and env.tg.sent == []
    env.tg.connect_exc = None
    click_and_confirm(env, row_id)
    assert env.outreach.get(row_id).status == STATUS_SENT and len(env.tg.sent) == 1


# ---------------- защита получателя и лимиты ----------------


def test_recipient_cooldown_blocks_repeat_across_campaigns(env):
    click_and_confirm(env, draft(env))
    fuel = env.campaigns.get_campaign_by_key("fuel")
    env.campaigns.set_campaign_account_enabled(fuel.id, env.vladi.id, True)
    second = draft(env, key="fuel", message_id=502)
    env.clock.now = T0 + timedelta(days=6)
    reply = press(env, cb.ACTION_APPROVE, second)
    row = env.outreach.get(second)
    assert (row.status, row.send_error) == (STATUS_BLOCKED, svc.RECENT_DM) and len(env.tg.sent) == 1
    assert "уже писали недавно" in reply.text
    env.clock.now = T0 + timedelta(days=8)
    third = draft(env, key="fuel", message_id=503)
    click_and_confirm(env, third)
    assert env.outreach.get(third).status == STATUS_SENT and len(env.tg.sent) == 2


def test_daily_cap_blocks_excess_but_keeps_draft(env):
    env.service._cap = 2
    for i in range(2):
        env.tg.users[f"user{i}"] = User(id=1000 + i, bot=False, username=f"user{i}")
        click_and_confirm(env, draft(env, username=f"user{i}", user_id=1000 + i, message_id=600 + i))
    env.tg.users["user9"] = User(id=1009, bot=False, username="user9")
    extra = draft(env, username="user9", user_id=1009, message_id=699)
    reply = press(env, cb.ACTION_APPROVE, extra)
    assert env.outreach.get(extra).status == STATUS_DRAFT and len(env.tg.sent) == 2
    assert "дневной лимит" in reply.text
    assert confirm(env, extra).text.find("дневной лимит") >= 0 and len(env.tg.sent) == 2
    env.clock.now = T0 + timedelta(hours=25)
    click_and_confirm(env, extra)
    assert env.outreach.get(extra).status == STATUS_SENT


def test_campaign_account_limit_applies(env):
    env.campaigns.set_campaign_account_daily_limit(env.insurance.id, env.vladi.id, 1)
    click_and_confirm(env, draft(env))
    env.tg.users["user2"] = User(id=2002, bot=False, username="user2")
    second = draft(env, username="user2", user_id=2002, message_id=502)
    press(env, cb.ACTION_APPROVE, second)
    assert env.outreach.get(second).status == STATUS_DRAFT and len(env.tg.sent) == 1


def test_default_has_no_global_daily_cap_but_keeps_recipient_cooldown():
    from reader.settings import DmOutreachSettings
    settings = DmOutreachSettings()
    assert (settings.sender_daily_cap, settings.recipient_cooldown_days) == (None, 7)
    assert DmOutreachSettings(sender_daily_cap=0).sender_daily_cap == 0  # 0 — тоже «без потолка»
    assert DmOutreachSettings(sender_daily_cap=5).sender_daily_cap == 5  # включается конфигом


@pytest.mark.parametrize("cap", [None, 0])
def test_twenty_manual_sends_are_not_blocked_by_global_cap(env, cap):
    """6-я, 10-я, 20-я ручная отправка за сутки — без «дневной лимит»."""
    env.service._cap = cap
    for i in range(20):
        env.tg.users[f"u{i}"] = User(id=3000 + i, bot=False, username=f"u{i}")
        row_id = draft(env, username=f"u{i}", user_id=3000 + i, message_id=900 + i)
        click_and_confirm(env, row_id)
        row = env.outreach.get(row_id)
        assert (row.status, row.send_error) == (STATUS_SENT, None), (i + 1, row.send_error)
    assert len(env.tg.sent) == 20


def test_flood_wait_still_pauses_sender_without_global_cap(env):
    env.service._cap = None
    row_id = draft(env)
    env.tg.send_effects = [errors.FloodWaitError(request=None, capture=300)]
    click_and_confirm(env, row_id)
    assert (env.outreach.get(row_id).status, env.tg.sent) == (STATUS_DRAFT, [])
    assert env.states.get(env.vladi.id).flood_wait_until == T0 + timedelta(seconds=300)


def test_peer_flood_still_blocks_sender_without_global_cap(env):
    env.service._cap = None
    row_id = draft(env)
    env.tg.send_effects = [errors.PeerFloodError(request=None)]
    click_and_confirm(env, row_id)
    assert env.states.get(env.vladi.id).blocked_reason == svc.PEER_FLOOD and env.tg.sent == []


def test_recipient_cooldown_still_applies_without_global_cap(env):
    env.service._cap = None
    click_and_confirm(env, draft(env))
    second = draft(env, message_id=502)
    press(env, cb.ACTION_APPROVE, second)
    assert env.outreach.get(second).send_error == svc.RECENT_DM and len(env.tg.sent) == 1


# ---------------- ошибки Telegram ----------------


def test_flood_wait_pauses_sender_and_keeps_draft(env):
    row_id = draft(env)
    env.tg.send_effects = [errors.FloodWaitError(request=None, capture=600)]
    _, reply = click_and_confirm(env, row_id)
    row = env.outreach.get(row_id)
    assert (row.status, row.send_error) == (STATUS_DRAFT, "flood_wait:600") and env.tg.sent == []
    assert "подождать 600" in reply.text
    assert env.states.get(env.vladi.id).flood_wait_until == T0 + timedelta(seconds=600)
    again = press(env, cb.ACTION_APPROVE, row_id)  # пока пауза — без сети, черновик в очереди
    assert "FloodWait" in again.text and env.tg.connects == 1
    env.clock.now = T0 + timedelta(seconds=601)
    click_and_confirm(env, row_id)
    assert env.outreach.get(row_id).status == STATUS_SENT


def test_peer_flood_blocks_sender_until_manual_check(env):
    row_id = draft(env)
    env.tg.send_effects = [errors.PeerFloodError(request=None)]
    _, reply = click_and_confirm(env, row_id)
    assert env.outreach.get(row_id).status == STATUS_DRAFT and "PeerFlood" in reply.text
    assert env.states.get(env.vladi.id).blocked_reason == svc.PEER_FLOOD
    env.clock.now = T0 + timedelta(days=3)
    press(env, cb.ACTION_APPROVE, row_id)
    assert env.outreach.get(row_id).send_error == svc.NO_ELIGIBLE_SENDER and env.tg.sent == []


@pytest.mark.parametrize("exc,reason", [
    (errors.UserPrivacyRestrictedError(request=None), svc.PRIVACY),
    (errors.UserIsBlockedError(request=None), svc.USER_BLOCKED),
    (errors.ChatWriteForbiddenError(request=None), svc.CHAT_WRITE_FORBIDDEN),
    (errors.PeerIdInvalidError(request=None), svc.PEER_INVALID),
    (errors.InputUserDeactivatedError(request=None), svc.USER_DEACTIVATED),
])
def test_recipient_errors_are_blocked(env, exc, reason):
    row_id = draft(env)
    env.tg.send_effects = [exc]
    _, reply = click_and_confirm(env, row_id)
    row = env.outreach.get(row_id)
    assert (row.status, row.send_error) == (STATUS_BLOCKED, reason) and env.tg.sent == []
    assert reply.text.startswith(texts.NOT_SENT_TEXT)


def test_username_not_found_is_blocked(env):
    row_id = draft(env, username="ghost_user")
    click_and_confirm(env, row_id)
    assert env.outreach.get(row_id).send_error == svc.USERNAME_NOT_FOUND and env.tg.sent == []


def test_other_rpc_error_is_send_failed(env):
    row_id = draft(env)
    env.tg.send_effects = [errors.RPCError(request=None, message="SOME_ERROR", code=400)]
    click_and_confirm(env, row_id)
    row = env.outreach.get(row_id)
    assert row.status == STATUS_SEND_FAILED and row.send_error == "rpc_error:RPCError"


def test_unauthorized_session_blocks_sender_and_keeps_draft(env):
    env.tg.authorized = False
    row_id = draft(env)
    click_and_confirm(env, row_id)
    assert env.outreach.get(row_id).status == STATUS_DRAFT and env.tg.sent == []
    assert env.states.get(env.vladi.id).blocked_reason == svc.SENDER_UNAUTHORIZED


def test_no_recipient_is_blocked(env):
    row_id = draft(env, sendability="unresolved", username=None, user_id=None)
    press(env, cb.ACTION_APPROVE, row_id)
    assert env.outreach.get(row_id).send_error == svc.NO_RECIPIENT and env.tg.sent == []


def test_sender_client_factory_is_safe_by_construction(tmp_path):
    """Клиент отправителя: без сна на FloodWait, без авто-повтора запросов,
    сессия только в памяти (файл не меняется)."""
    from telethon.sessions import StringSession

    from reader.dm_campaigns.sender_client import build_sender_client_factory
    session = tmp_path / "data" / "sessions" / "vladi.session"
    session.parent.mkdir(parents=True)
    conn = sqlite3.connect(session)
    conn.execute("CREATE TABLE sessions (dc_id INTEGER, server_address TEXT, port INTEGER, auth_key BLOB)")
    conn.execute("INSERT INTO sessions VALUES (2, '149.154.167.51', 443, ?)", (b"k" * 256,))
    conn.commit()
    conn.close()
    before = session.read_bytes()
    client = build_sender_client_factory(1, "hash", tmp_path)(SimpleNamespace(session_path="data/sessions/vladi"))
    assert client.flood_sleep_threshold == 0 and client._request_retries == 1
    assert isinstance(client.session, StringSession) and session.read_bytes() == before
