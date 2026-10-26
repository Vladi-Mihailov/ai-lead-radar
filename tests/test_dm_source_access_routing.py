"""Выбор ЛС-отправителя с учётом доступа к исходной группе (source_message).

Production: @alenaogir (Premium) не читает @VerhniyLars (ChannelPrivateError),
а selector предлагал её для source_message — оператор жал «Отправить», доставка
падала. Теперь доступ запоминается (dm_sender_source_access) и отправитель без
доступа к группе черновика не выбирается; никого нет — no_eligible_sender_for_source,
черновик остаётся в очереди. Путь username не меняется. Фейковый Telegram, без сети."""

import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from telethon import errors

from reader.dm_campaigns import manage
from reader.dm_campaigns import send_service as svc
from reader.dm_campaigns.outreach_repository import STATUS_DRAFT, STATUS_SENT, DmOutreachRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.send_service import DmSendService
from reader.dm_campaigns.sender_selection import SenderSelector
from reader.dm_campaigns.sender_state import DmSenderStateRepository
from reader.dm_campaigns.sendability import SENDABILITY_PREMIUM_REQUIRED, SENDABILITY_SOURCE_MESSAGE
from reader.dm_campaigns.source_access import NO_ACCESS_TTL, DmSourceAccessRepository
from reader.inviter.repository import TelegramAccountRepository
from reader.inviter_admin_bot import dm_campaign_callbacks as dmc
from reader.inviter_admin_bot.conversation_state_repository import AdminBotConversationStateRepository
from reader.inviter_admin_bot.dm_draft_controller import DmDraftController
from test_dm_send import ADMIN, T0, Clock, FakeClient, FakeTelegram, click_and_confirm, draft, press
from reader.inviter_admin_bot import dm_draft_callbacks as cb

SOURCE_CHAT_ID = -1001555   # draft() в test_dm_send: VerhniyLars, chat_id=-1001555


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "users.db"
    campaigns, outreach = DmCampaignRepository(path), DmOutreachRepository(path)
    accounts, states = TelegramAccountRepository(path), DmSenderStateRepository(path)
    access, conv = DmSourceAccessRepository(path), AdminBotConversationStateRepository(path)
    tg, clock = FakeTelegram(), Clock()
    alena = accounts.create(name="@alenaogir", phone="", session_name="alenaogi", session_path="data/sessions/alenaogi",
                            enabled=True, telegram_user_id=410811386, can_send_dm=True, is_premium=True)
    insurance = campaigns.get_campaign_by_key("insurance")
    campaigns.set_campaign_account_enabled(insurance.id, alena.id, True)
    service = DmSendService(
        outreach, campaigns, accounts, states, client_factory=lambda account: FakeClient(tg, account),
        session_exists=lambda account: True, recipient_cooldown=timedelta(days=7), sender_daily_cap=5, clock=clock,
        source_access=access,
    )
    controller = DmDraftController(
        outreach, campaigns, conv, is_trusted=lambda uid: uid == ADMIN, back_callback=dmc.LIST,
        send_service=service, sender_label=lambda aid: (accounts.get(aid).name if accounts.get(aid) else None),
        clock=clock,
    )
    ns = SimpleNamespace(path=path, campaigns=campaigns, outreach=outreach, accounts=accounts, states=states,
                         access=access, tg=tg, clock=clock, service=service, controller=controller, alena=alena,
                         insurance=insurance)
    yield ns
    for repo in (campaigns, outreach, accounts, states, access, conv):
        repo.close()


def add_sender(env, name, *, premium=False):
    slug = name.lstrip("@")
    account = env.accounts.create(name=name, phone="", session_name=slug, session_path=f"data/sessions/{slug}",
                                  enabled=False, can_send_dm=True, is_premium=premium)
    env.campaigns.set_campaign_account_enabled(env.insurance.id, account.id, True)
    return account


def no_access(env, account, *, reason="ChannelPrivateError", at=None):
    env.access.record(account.id, SOURCE_CHAT_ID, has_access=False, reason=reason, now=at or T0)


def source_draft(env, **kwargs):
    return draft(env, sendability=SENDABILITY_SOURCE_MESSAGE, username=None, **kwargs)


def sent_by(env):
    return [account_id for account_id, _, _ in env.tg.sent]


# ---------------- 4 сценария задачи ----------------


def test_username_draft_goes_to_alena_even_without_source_access(env):
    no_access(env, env.alena)
    env.tg.read_errors[env.alena.id] = errors.ChannelPrivateError(request=None)
    row_id = draft(env)                                               # sendability=username
    preview, _ = click_and_confirm(env, row_id)
    assert "Отправитель: @alenaogir" in preview.text
    assert sent_by(env) == [env.alena.id] and env.tg.source_reads == []
    assert env.outreach.get(row_id).status == STATUS_SENT


def test_source_message_without_access_excludes_alena_without_network(env):
    no_access(env, env.alena)
    row_id = source_draft(env)
    preview = press(env, cb.ACTION_APPROVE, row_id)
    assert "@alenaogir" not in preview.text and "ни у одного отправителя" in preview.text
    assert env.tg.connects == 0 and env.tg.source_reads == [] and env.tg.sent == []
    row = env.outreach.get(row_id)
    assert row.status == STATUS_DRAFT                                 # остаётся в очереди, не blocked


def test_source_message_goes_to_sender_with_access(env):
    other = add_sender(env, "@reader_of_lars")
    no_access(env, env.alena)
    row_id = source_draft(env)
    preview, reply = click_and_confirm(env, row_id)
    assert "Отправитель: @reader_of_lars" in preview.text
    assert sent_by(env) == [other.id] and env.tg.source_reads == [other.id]   # Алёну даже не пробовали
    assert (env.outreach.get(row_id).status, env.outreach.get(row_id).sender_account_id) == (STATUS_SENT, other.id)


def test_nobody_with_source_access_returns_no_eligible_sender_for_source(env):
    other = add_sender(env, "@second")
    no_access(env, env.alena)
    no_access(env, other, reason="UserNotParticipantError")
    row_id = source_draft(env)
    assert env.service.plan(row_id).reason == svc.NO_SENDER_FOR_SOURCE == "no_eligible_sender_for_source"
    outcome = env.service.preview(row_id, operator_id=ADMIN)
    assert (outcome.kind, outcome.reason) == (svc.KIND_DRAFT, svc.NO_SENDER_FOR_SOURCE)
    outcome = asyncio.run(env.service.send(row_id, operator_id=ADMIN))
    assert (outcome.kind, outcome.reason) == (svc.KIND_DRAFT, svc.NO_SENDER_FOR_SOURCE)
    assert env.tg.connects == 0 and env.tg.sent == [] and env.outreach.get(row_id).status == STATUS_DRAFT


# ---------------- premium_required ----------------


def test_premium_required_with_username_still_goes_to_alena(env):
    no_access(env, env.alena)
    row_id = draft(env, sendability=SENDABILITY_PREMIUM_REQUIRED)    # username есть
    click_and_confirm(env, row_id)
    assert sent_by(env) == [env.alena.id] and env.tg.source_reads == []


def test_premium_required_without_username_needs_source_access(env):
    no_access(env, env.alena)
    add_sender(env, "@not_premium")                                    # не Premium — не подходит
    row_id = draft(env, sendability=SENDABILITY_PREMIUM_REQUIRED, username=None)
    assert env.service.plan(row_id).reason == svc.NO_SENDER_FOR_SOURCE
    assert env.tg.connects == 0


# ---------------- как доступ узнаётся ----------------


def test_failed_read_is_remembered_and_next_draft_skips_alena(env):
    other = add_sender(env, "@second")
    env.tg.read_errors[env.alena.id] = errors.ChannelPrivateError(request=None)
    first = source_draft(env, message_id=501)
    preview, _ = click_and_confirm(env, first)
    assert "Отправитель: @alenaogir" in preview.text                 # доступ ещё не известен
    assert sent_by(env) == [other.id]
    alena_access = env.access.get(env.alena.id, SOURCE_CHAT_ID)
    assert (alena_access.has_access, alena_access.reason) == (False, "ChannelPrivateError")
    assert env.access.get(other.id, SOURCE_CHAT_ID).has_access is True

    env.tg.messages[("VerhniyLars", 502)] = SimpleNamespace(sender_id=111222)
    env.tg.source_reads.clear()
    second = source_draft(env, message_id=502, user_id=111222)
    preview, _ = click_and_confirm(env, second)
    assert "Отправитель: @second" in preview.text and env.tg.source_reads == [other.id]


@pytest.mark.parametrize("problem", ["deleted_message", "msg_id_invalid"])
def test_message_level_problems_are_not_group_access(env, problem):
    if problem == "deleted_message":
        env.tg.hidden_messages.add(env.alena.id)
    else:
        env.tg.read_errors[env.alena.id] = errors.MsgIdInvalidError(request=None)
    row_id = source_draft(env)
    click_and_confirm(env, row_id)
    assert env.outreach.get(row_id).send_error == svc.NO_SENDER_FOR_SOURCE
    assert env.access.get(env.alena.id, SOURCE_CHAT_ID) is None       # про группу ничего не записано


def test_unresolvable_group_is_recorded_as_no_access(env):
    env.tg.entity_errors[env.alena.id] = errors.ChannelPrivateError(request=None)
    click_and_confirm(env, source_draft(env))
    assert env.access.get(env.alena.id, SOURCE_CHAT_ID).has_access is False


def test_confirmed_access_sender_is_preferred_over_unknown(env):
    other = add_sender(env, "@second")
    env.access.record(other.id, SOURCE_CHAT_ID, has_access=True, reason=None, now=T0)
    plan = env.service.plan(source_draft(env))
    assert [a.id for a in plan.candidates] == [other.id, env.alena.id]


def test_no_access_expires_after_ttl(env):
    no_access(env, env.alena, at=T0 - NO_ACCESS_TTL - timedelta(minutes=1))
    plan = env.service.plan(source_draft(env))
    assert plan.ok and plan.sender.id == env.alena.id


def test_access_of_other_group_does_not_matter(env):
    env.access.record(env.alena.id, -1009999, has_access=False, reason="ChannelPrivateError", now=T0)
    assert env.service.plan(source_draft(env)).sender.id == env.alena.id


def test_expected_sender_for_draft_prompt_respects_source_access(env):
    no_access(env, env.alena)
    selector = SenderSelector(env.outreach, env.campaigns, env.accounts, env.states,
                              session_exists=lambda a: True, source_access=env.access)
    source_item = env.outreach.get(source_draft(env))
    username_item = env.outreach.get(draft(env, message_id=900))
    assert selector.expected_sender(source_item, T0, cap=5) is None
    assert selector.expected_sender(username_item, T0, cap=5).id == env.alena.id


def test_without_repository_behaviour_is_unchanged(env):
    no_access(env, env.alena)
    selector = SenderSelector(env.outreach, env.campaigns, env.accounts, env.states, session_exists=lambda a: True)
    assert selector.expected_sender(env.outreach.get(source_draft(env)), T0, cap=5).id == env.alena.id


# ---------------- manage check-source-access (только чтение, без send_message) ----------------


def test_check_source_access_dry_run_and_apply(env):
    env.tg.read_errors[env.alena.id] = errors.ChannelPrivateError(request=None)
    item = env.outreach.get(source_draft(env))
    client = FakeClient(env.tg, env.alena)
    text = asyncio.run(manage.check_source_access(client, item, env.alena.id, env.access, apply=False, now=T0))
    assert text.startswith("DRY RUN") and "НЕТ ДОСТУПА (ChannelPrivateError)" in text
    assert env.access.get(env.alena.id, SOURCE_CHAT_ID) is None
    text = asyncio.run(manage.check_source_access(client, item, env.alena.id, env.access, apply=True, now=T0))
    assert text.startswith("APPLIED") and env.access.get(env.alena.id, SOURCE_CHAT_ID).has_access is False
    assert env.tg.sent == []


def test_check_source_access_refuses_username_draft(env):
    item = env.outreach.get(draft(env))
    with pytest.raises(SystemExit):
        asyncio.run(manage.check_source_access(FakeClient(env.tg, env.alena), item, env.alena.id, env.access,
                                               apply=True, now=T0))
    assert env.tg.connects == 0
