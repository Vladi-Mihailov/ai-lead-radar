"""Тесты раздела "✉️ ЛС-кампании" inviter_admin_bot (Phase 1): доступ,
экраны, callbacks, FSM, allowlist аккаунтов. Реальный Telethon не
используется; БД — временная (tmp_path)."""

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.dm_campaigns.repository import DmCampaignRepository
from reader.groups import Group
from reader.inviter.repository import (
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)
from reader.inviter.runtime_state_repository import InviterRuntimeStateRepository
from reader.inviter_admin_bot import conversation
from reader.inviter_admin_bot import dm_campaign_callbacks as cb
from reader.inviter_admin_bot import dm_campaign_controller
from reader.inviter_admin_bot import dm_campaign_texts as dm_texts
from reader.inviter_admin_bot import texts
from reader.inviter_admin_bot.conversation import AdminBotController
from reader.inviter_admin_bot.conversation_state_repository import (
    AdminBotConversationStateRepository,
)
from reader.inviter_admin_bot.dm_campaign_controller import DmCampaignController
from reader.inviter_admin_bot.handlers import _send_reply
from reader.inviter_admin_bot.keyboards import main_menu_keyboard
from reader.inviter_admin_bot.service import InviterAdminService

_TRUSTED_ID = 111222333
_OTHER_ID = 999888777
_CHAT_ID = 111222333

_GROUPS = [
    Group(id=None, username="VerhniyLars", title="Верхний Ларс"),
    Group(id=None, username="Sadahlo", title="Садахло"),
    Group(id=-1001234567890, username=None, title=None),
]


class _NoAuth:
    async def cancel(self, chat_id):
        return None


class _Fixture:
    def __init__(self, tmp_path):
        self.db_path = tmp_path / "users.db"
        self.accounts = TelegramAccountRepository(self.db_path)
        self.campaigns = InviteCampaignRepository(self.db_path)
        self.invites = UserCampaignInviteRepository(self.db_path)
        self.runtime_state = InviterRuntimeStateRepository(self.db_path)
        self.states = AdminBotConversationStateRepository(":memory:")
        self.dm_repo = DmCampaignRepository(self.db_path)
        self.groups = list(_GROUPS)
        self.service = InviterAdminService(
            self.accounts, self.campaigns, self.invites, self.runtime_state,
            db_path=self.db_path, trusted_admin_user_ids=frozenset({_TRUSTED_ID}),
        )
        self.dm = DmCampaignController(
            self.dm_repo, self.accounts, self.states,
            is_trusted=self.service.is_trusted, groups_provider=lambda: self.groups,
        )
        self.controller = AdminBotController(
            self.service, _NoAuth(), self.states, sync_client_factory=lambda account: None,
            dm_campaigns=self.dm,
        )

    def campaign(self, key="fuel"):
        return self.dm_repo.get_campaign_by_key(key)

    def account(self, name="@old_1", **fields):
        return self.accounts.create(
            name=name, phone="+1", session_name=name.lstrip("@"),
            session_path=f"data/sessions/{name.lstrip('@')}", **fields,
        )

    def click(self, data, user=_TRUSTED_ID):
        return self.controller.handle_dm_callback(data, chat_id=_CHAT_ID, telegram_user_id=user)

    async def say(self, text, user=_TRUSTED_ID):
        return await self.controller.handle_text(text, chat_id=_CHAT_ID, telegram_user_id=user)

    def close(self):
        for repo in (self.accounts, self.campaigns, self.invites, self.runtime_state, self.states, self.dm_repo):
            repo.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


def _buttons(reply):
    return [(label, data) for row in (reply.inline_rows or []) for label, data in row]


def _labels(reply):
    return [label for label, _ in _buttons(reply)]


def _button(reply, label_part):
    return next(data for label, data in _buttons(reply) if label_part in label)


# ---- доступ / меню ----


async def test_trusted_admin_opens_dm_campaigns_list(fx):
    reply = await fx.say(dm_texts.DM_CAMPAIGNS_LABEL)
    assert reply.text.startswith(dm_texts.LIST_HEADER)
    assert "🔴 ⛽ Бензин" in reply.text and "🔴 🚧 Очереди на границе" in reply.text and "🔴 🛡 Страховка" in reply.text
    assert _labels(reply) == ["🔴 ⛽ Бензин", "🔴 🚧 Очереди на границе", "🔴 🛡 Страховка"]
    assert dm_texts.NOT_CONNECTED_NOTE in reply.text


async def test_untrusted_cannot_open_or_change(fx):
    fuel = fx.campaign()
    account = fx.account()
    assert (await fx.say(dm_texts.DM_CAMPAIGNS_LABEL, user=_OTHER_ID)).text == texts.ACCESS_DENIED_TEXT
    for data in (
        cb.LIST,
        cb.encode(cb.ACTION_OPEN, fuel.id),
        cb.encode(cb.ACTION_ENABLE, fuel.id),
        cb.encode(cb.ACTION_ACCOUNT_TOGGLE, fuel.id, account_id=account.id),
        cb.encode(cb.ACTION_GUIDELINE, fuel.id),
        b"dmc_garbage",
    ):
        reply = fx.click(data, user=_OTHER_ID)
        assert reply.text == texts.ACCESS_DENIED_TEXT
        assert reply.inline_rows is None
    assert fx.campaign().enabled is False
    assert fx.dm_repo.list_campaign_accounts(fuel.id) == []
    assert fx.states.get(_CHAT_ID) is None


async def test_untrusted_cannot_continue_fsm(fx):
    fuel = fx.campaign()
    fx.click(cb.encode(cb.ACTION_GUIDELINE, fuel.id))
    # Даже при "чужом" состоянии на том же chat_id недоверенный отказ.
    reply = await fx.say("взлом", user=_OTHER_ID)
    assert reply.text == texts.ACCESS_DENIED_TEXT
    assert fx.campaign().ai_guideline is None


def test_dm_controller_rejects_untrusted_state_input_directly(fx):
    fuel = fx.campaign()
    state = fx.states.set(_CHAT_ID, telegram_user_id=_OTHER_ID, step="awaiting_dm_guideline", payload={"campaign_id": fuel.id})
    reply = fx.dm.handle_state_input(state, "x", chat_id=_CHAT_ID, telegram_user_id=_OTHER_ID)
    assert reply.text == texts.ACCESS_DENIED_TEXT
    assert fx.campaign().ai_guideline is None


def test_main_menu_keeps_inviter_items_and_adds_dm_item():
    labels = [b.button.text for row in main_menu_keyboard() for b in row]
    for label in (texts.CAMPAIGNS_LABEL, texts.ACCOUNTS_LABEL, texts.ADD_ACCOUNT_LABEL,
                  texts.STATUS_LABEL, texts.SYNC_LABEL, texts.HELP_LABEL):
        assert label in labels
    assert dm_texts.DM_CAMPAIGNS_LABEL in labels
    assert dm_texts.DM_CAMPAIGNS_LABEL != texts.CAMPAIGNS_LABEL


async def test_inviter_campaigns_item_still_works(fx):
    reply = await fx.say(texts.CAMPAIGNS_LABEL)
    assert dm_texts.LIST_HEADER not in reply.text


def test_controller_without_dm_section_ignores_dm_callbacks(fx):
    plain = AdminBotController(fx.service, _NoAuth(), fx.states, sync_client_factory=lambda a: None)
    assert plain.handle_dm_callback(cb.LIST, chat_id=_CHAT_ID, telegram_user_id=_TRUSTED_ID) is None


def test_non_dm_callbacks_are_not_claimed(fx):
    assert fx.click(b"acc_open:1") is None
    assert fx.click(b"camp_open:1") is None
    assert fx.click(None) is None


def test_dm_steps_match_conversation_routing():
    assert conversation.DM_STEPS == {
        dm_campaign_controller.STEP_AWAITING_DM_GUIDELINE,
        dm_campaign_controller.STEP_AWAITING_DM_RESOURCES,
        dm_campaign_controller.STEP_AWAITING_DM_FOLLOW_UP,
        dm_campaign_controller.STEP_AWAITING_DM_ACCOUNT_LIMIT,
        dm_campaign_controller.STEP_AWAITING_DM_DRAFT_EDIT,
    }
    from reader.inviter_admin_bot.dm_draft_controller import STEP_AWAITING_DM_DRAFT_EDIT
    assert STEP_AWAITING_DM_DRAFT_EDIT == dm_campaign_controller.STEP_AWAITING_DM_DRAFT_EDIT


# ---- карточка / вкл-выкл ----


def test_campaign_detail_rendering(fx):
    fuel = fx.campaign()
    reply = fx.click(cb.encode(cb.ACTION_OPEN, fuel.id))
    assert reply.text.startswith("⛽ Бензин")
    assert "Статус: 🔴 выключена" in reply.text
    assert "AI-инструкция:\nне задана" in reply.text
    assert "Ресурсы:\nне заданы" in reply.text
    assert "Источники:\nвсе отслеживаемые группы" in reply.text
    assert "Аккаунты:\nне выбраны" in reply.text
    assert "Follow-up: 🔴 выключен" in reply.text
    assert _labels(reply) == [
        dm_texts.ENABLE_LABEL, dm_texts.GUIDELINE_LABEL, dm_texts.RESOURCES_LABEL, dm_texts.SOURCES_LABEL,
        dm_texts.ACCOUNTS_LABEL, dm_texts.FOLLOW_UP_ENABLE_LABEL, dm_texts.FOLLOW_UP_TEXT_LABEL, dm_texts.BACK_LABEL,
    ]
    assert "Статистика" not in reply.text and not any("📊" in label for label in _labels(reply))


def test_enable_disable_changes_only_enabled_and_reopens_card(fx):
    fuel = fx.campaign()
    reply = fx.click(cb.encode(cb.ACTION_ENABLE, fuel.id))
    assert fx.campaign().enabled is True
    assert "Статус: 🟢 включена" in reply.text
    assert dm_texts.DISABLE_LABEL in _labels(reply)
    # Повторное нажатие устаревшей кнопки — безопасно (явное целевое состояние).
    fx.click(cb.encode(cb.ACTION_ENABLE, fuel.id))
    assert fx.campaign().enabled is True
    fx.click(cb.encode(cb.ACTION_DISABLE, fuel.id))
    after = fx.campaign()
    assert after.enabled is False
    assert (after.ai_guideline, after.resources, after.follow_up_enabled) == (None, (), False)


def test_back_returns_to_list(fx):
    reply = fx.click(cb.LIST)
    assert reply.text.startswith(dm_texts.LIST_HEADER)


# ---- FSM: инструкция / ресурсы / follow-up ----


async def test_guideline_fsm_replace_clear(fx):
    fuel = fx.campaign()
    prompt = fx.click(cb.encode(cb.ACTION_GUIDELINE, fuel.id))
    assert prompt.show_cancel_button
    assert fx.states.get(_CHAT_ID).step == "awaiting_dm_guideline"

    reply = await fx.say("Сначала ответь пользователю.\nПотом упомяни @tplgee.")
    assert fx.campaign().ai_guideline == "Сначала ответь пользователю.\nПотом упомяни @tplgee."
    assert reply.text.startswith(dm_texts.SAVED_TEXT)
    assert fx.states.get(_CHAT_ID) is None

    fx.click(cb.encode(cb.ACTION_GUIDELINE, fuel.id))
    await fx.say("-")
    assert fx.campaign().ai_guideline is None


async def test_guideline_too_long_keeps_state(fx):
    fuel = fx.campaign()
    fx.click(cb.encode(cb.ACTION_GUIDELINE, fuel.id))
    reply = await fx.say("x" * 2001)
    assert reply.text.startswith("❌")
    assert reply.show_cancel_button
    assert fx.states.get(_CHAT_ID).step == "awaiting_dm_guideline"
    assert fx.campaign().ai_guideline is None


async def test_cancel_edit(fx):
    fuel = fx.campaign()
    fx.click(cb.encode(cb.ACTION_GUIDELINE, fuel.id))
    reply = await fx.say(texts.CANCEL_BUTTON_LABEL)
    assert reply.show_main_menu
    assert fx.states.get(_CHAT_ID) is None
    assert fx.campaign().ai_guideline is None


async def test_resources_fsm_normalizes(fx):
    fuel = fx.campaign()
    fx.click(cb.encode(cb.ACTION_RESOURCES, fuel.id))
    reply = await fx.say("@tplgee, \n\n@ProtocolGEbot,@tplgee")
    assert fx.campaign().resources == ("@tplgee", "@ProtocolGEbot")
    assert "Ресурсы:\n@tplgee\n@ProtocolGEbot" in reply.text
    fx.click(cb.encode(cb.ACTION_RESOURCES, fuel.id))
    await fx.say("-")
    assert fx.campaign().resources == ()


async def test_follow_up_toggle_and_text(fx):
    fuel = fx.campaign()
    reply = fx.click(cb.encode(cb.ACTION_FOLLOW_UP_ENABLE, fuel.id))
    assert fx.campaign().follow_up_enabled is True
    assert "Follow-up: 🟢 включён" in reply.text
    fx.click(cb.encode(cb.ACTION_FOLLOW_UP_TEXT, fuel.id))
    assert fx.states.get(_CHAT_ID).step == "awaiting_dm_follow_up_guideline"
    await fx.say("*За отсутствие медицинской страховки штрафа нет.")
    assert fx.campaign().follow_up_guideline == "*За отсутствие медицинской страховки штрафа нет."
    fx.click(cb.encode(cb.ACTION_FOLLOW_UP_DISABLE, fuel.id))
    assert fx.campaign().follow_up_enabled is False


async def test_menu_label_during_fsm_navigates_and_clears_state(fx):
    fuel = fx.campaign()
    fx.click(cb.encode(cb.ACTION_GUIDELINE, fuel.id))
    reply = await fx.say(dm_texts.DM_CAMPAIGNS_LABEL)
    assert reply.text.startswith(dm_texts.LIST_HEADER)
    assert fx.states.get(_CHAT_ID) is None
    assert fx.campaign().ai_guideline is None


async def test_fsm_for_deleted_campaign_is_safe(fx):
    fx.states.set(_CHAT_ID, telegram_user_id=_TRUSTED_ID, step="awaiting_dm_guideline", payload={"campaign_id": 9999})
    reply = await fx.say("текст")
    assert dm_texts.CAMPAIGN_NOT_FOUND_TEXT in reply.text
    assert fx.states.get(_CHAT_ID) is None


# ---- источники ----


def test_sources_screen_lists_groups_yaml_and_toggles(fx):
    fuel = fx.campaign()
    reply = fx.click(cb.encode(cb.ACTION_SOURCES, fuel.id))
    assert "Сейчас: все отслеживаемые группы" in reply.text
    assert _labels(reply)[:3] == ["☐ Верхний Ларс", "☐ Садахло", "☐ -1001234567890"]

    reply = fx.click(_button(reply, "Верхний Ларс"))
    assert fx.campaign().source_chats == ("VerhniyLars",)
    assert "☑ Верхний Ларс" in _labels(reply)
    fx.click(cb.encode(cb.ACTION_SOURCE_TOGGLE, fuel.id, source="-1001234567890"))
    assert fx.campaign().source_chats == ("VerhniyLars", "-1001234567890")

    card = fx.click(cb.encode(cb.ACTION_OPEN, fuel.id))
    assert "Источники:\nВерхний Ларс\n-1001234567890" in card.text

    reply = fx.click(cb.encode(cb.ACTION_SOURCES_ALL, fuel.id))
    assert fx.campaign().source_chats == ()
    assert "Сейчас: все отслеживаемые группы" in reply.text


def test_source_toggle_rejects_unknown_group(fx):
    fuel = fx.campaign()
    reply = fx.click(cb.encode(cb.ACTION_SOURCE_TOGGLE, fuel.id, source="evil_group"))
    assert fx.campaign().source_chats == ()
    assert dm_texts.STALE_BUTTON_TEXT in reply.text


def test_source_removed_from_groups_yaml_still_shown_and_removable(fx):
    fuel = fx.campaign()
    fx.dm_repo.update_source_chats(fuel.id, ["old_group"])
    reply = fx.click(cb.encode(cb.ACTION_SOURCES, fuel.id))
    assert "☑ old_group — нет в groups.yaml" in _labels(reply)
    card = fx.click(cb.encode(cb.ACTION_OPEN, fuel.id))
    assert "old_group (нет в groups.yaml)" in card.text
    fx.click(_button(reply, "old_group"))
    assert fx.campaign().source_chats == ()


def test_sources_with_broken_groups_provider(fx):
    def broken():
        raise RuntimeError("no file")
    fx.dm._groups_provider = broken
    reply = fx.click(cb.encode(cb.ACTION_SOURCES, fx.campaign().id))
    assert _labels(reply) == [dm_texts.ALL_GROUPS_LABEL, dm_texts.DONE_LABEL]


# ---- аккаунты ----


def test_accounts_screen_shows_statuses_and_toggles(fx):
    fuel = fx.campaign()
    ok = fx.account("@old_1")
    disabled = fx.account("@old_2", enabled=False)
    blocked = fx.account("@new_3")
    fx.accounts.update(blocked.id, blocked_until=datetime.now(timezone.utc) + timedelta(hours=5), blocked_reason="peer_flood")
    hidden_old = fx.account("@dup", is_old=True, old_reason="duplicate_telegram_user_id")

    reply = fx.click(cb.encode(cb.ACTION_ACCOUNTS, fuel.id))
    assert "☐ @old_1" in reply.text
    assert "☐ @old_2 — отключён" in reply.text
    assert re.search(r"☐ @new_3 — заблокирован до", reply.text)
    assert "@dup" not in reply.text  # is_old — как и на других экранах

    fx.click(cb.encode(cb.ACTION_ACCOUNT_TOGGLE, fuel.id, account_id=ok.id))
    reply = fx.click(cb.encode(cb.ACTION_ACCOUNT_TOGGLE, fuel.id, account_id=disabled.id))
    assert {a.account_id for a in fx.dm_repo.list_campaign_accounts(fuel.id)} == {ok.id, disabled.id}
    assert "☑ @old_1 — лимит ЛС: не задан" in reply.text
    assert "☑ @old_2 — отключён, лимит ЛС: не задан" in reply.text
    # Allowlist не меняет сам аккаунт.
    assert fx.accounts.get(disabled.id).enabled is False

    card = fx.click(cb.encode(cb.ACTION_OPEN, fuel.id))
    assert "Аккаунты:\n2 выбрано" in card.text

    fx.click(cb.encode(cb.ACTION_ACCOUNT_TOGGLE, fuel.id, account_id=ok.id))
    assert {a.account_id for a in fx.dm_repo.list_campaign_accounts(fuel.id)} == {disabled.id}
    # Другая кампания не затронута.
    assert fx.dm_repo.list_campaign_accounts(fx.campaign("insurance").id) == []
    assert hidden_old.id not in {a.account_id for a in fx.dm_repo.list_campaign_accounts(fuel.id)}


def test_is_old_account_cannot_be_added(fx):
    fuel = fx.campaign()
    old = fx.account("@dup", is_old=True, old_reason="duplicate_telegram_user_id")
    reply = fx.click(cb.encode(cb.ACTION_ACCOUNT_TOGGLE, fuel.id, account_id=old.id))
    assert "нельзя" in reply.text
    assert fx.dm_repo.list_campaign_accounts(fuel.id) == []


def test_allowed_is_old_account_is_shown_and_removable(fx):
    fuel = fx.campaign()
    acc = fx.account("@was_ok")
    fx.dm_repo.set_campaign_account_enabled(fuel.id, acc.id, True)
    fx.accounts.update(acc.id, is_old=True, old_reason="duplicate_telegram_user_id")
    reply = fx.click(cb.encode(cb.ACTION_ACCOUNTS, fuel.id))
    assert "☑ @was_ok — устаревшая запись" in reply.text
    fx.click(cb.encode(cb.ACTION_ACCOUNT_TOGGLE, fuel.id, account_id=acc.id))
    assert fx.dm_repo.list_campaign_accounts(fuel.id) == []


def test_toggle_nonexistent_account(fx):
    fuel = fx.campaign()
    reply = fx.click(cb.encode(cb.ACTION_ACCOUNT_TOGGLE, fuel.id, account_id=4242))
    assert "Аккаунт не найден" in reply.text
    assert fx.dm_repo.list_campaign_accounts(fuel.id) == []


async def test_daily_limit_edit(fx):
    fuel = fx.campaign()
    acc = fx.account("@old_1")
    fx.click(cb.encode(cb.ACTION_ACCOUNT_TOGGLE, fuel.id, account_id=acc.id))

    prompt = fx.click(cb.encode(cb.ACTION_ACCOUNT_LIMIT, fuel.id, account_id=acc.id))
    assert prompt.show_cancel_button
    assert "Сейчас: не задан" in prompt.text

    for bad in ("abc", "0", "-5", "51"):
        reply = await fx.say(bad)
        assert reply.text.startswith("❌")
        assert fx.dm_repo.get_campaign_account(fuel.id, acc.id).daily_limit is None

    reply = await fx.say("7")
    assert fx.dm_repo.get_campaign_account(fuel.id, acc.id).daily_limit == 7
    assert reply.text.startswith(dm_texts.SAVED_TEXT)
    assert "🔢 7" in _labels(reply)
    assert fx.states.get(_CHAT_ID) is None

    fx.click(cb.encode(cb.ACTION_ACCOUNT_LIMIT, fuel.id, account_id=acc.id))
    await fx.say("-")
    assert fx.dm_repo.get_campaign_account(fuel.id, acc.id).daily_limit is None
    assert fx.accounts.get(acc.id).daily_limit == 30


def test_limit_for_not_allowed_account_refused(fx):
    fuel = fx.campaign()
    acc = fx.account("@old_1")
    reply = fx.click(cb.encode(cb.ACTION_ACCOUNT_LIMIT, fuel.id, account_id=acc.id))
    assert "не разрешён" in reply.text
    assert fx.states.get(_CHAT_ID) is None


async def test_limit_fsm_after_account_removed(fx):
    fuel = fx.campaign()
    acc = fx.account("@old_1")
    fx.click(cb.encode(cb.ACTION_ACCOUNT_TOGGLE, fuel.id, account_id=acc.id))
    fx.click(cb.encode(cb.ACTION_ACCOUNT_LIMIT, fuel.id, account_id=acc.id))
    fx.dm_repo.set_campaign_account_enabled(fuel.id, acc.id, False)
    reply = await fx.say("5")
    assert "больше не разрешён" in reply.text
    assert fx.states.get(_CHAT_ID) is None


# ---- устаревшие / битые callbacks ----


def test_malformed_and_stale_callbacks(fx):
    for data in (b"dmc_open:abc", b"dmc_unknown:1", b"dmc_acct:1", b"dmc_srct:1:", b"dmc_open", b"dmc_\xff\xfe"):
        reply = fx.click(data)
        assert dm_texts.STALE_BUTTON_TEXT in reply.text
    reply = fx.click(cb.encode(cb.ACTION_OPEN, 9999))
    assert dm_texts.CAMPAIGN_NOT_FOUND_TEXT in reply.text
    reply = fx.click(cb.encode(cb.ACTION_ENABLE, 9999))
    assert dm_texts.CAMPAIGN_NOT_FOUND_TEXT in reply.text


def test_callback_encoding_roundtrip_and_limit():
    data = cb.encode(cb.ACTION_SOURCE_TOGGLE, 3, source="a" * 32)
    assert len(data) <= cb.MAX_CALLBACK_BYTES
    assert cb.decode(data) == cb.DmCallback(action=cb.ACTION_SOURCE_TOGGLE, campaign_id=3, source="a" * 32)
    assert cb.decode(cb.encode(cb.ACTION_ACCOUNT_LIMIT, 2, account_id=17)) == cb.DmCallback(
        action=cb.ACTION_ACCOUNT_LIMIT, campaign_id=2, account_id=17,
    )
    with pytest.raises(ValueError):
        cb.encode(cb.ACTION_SOURCE_TOGGLE, 1, source="x" * 80)
    with pytest.raises(ValueError):
        cb.encode("bogus", 1)
    # Не пересекается с существующими префиксами admin-бота.
    for existing in (b"acc_open:1", b"camp_on:1", b"limits_open:1", b"accounts_back", b"campaigns_back"):
        assert not cb.is_dm_callback(existing)


# ---- handlers: inline_rows -> Button ----


class _FakeEvent:
    def __init__(self):
        self.calls = []

    async def respond(self, text, *, buttons=None):
        self.calls.append({"text": text, "buttons": buttons})

    async def edit(self, text, *, buttons=None):
        self.calls.append({"text": text, "buttons": buttons})


async def test_send_reply_builds_inline_buttons(fx):
    reply = fx.click(cb.encode(cb.ACTION_OPEN, fx.campaign().id))
    event = _FakeEvent()
    await _send_reply(event, reply)
    buttons = event.calls[0]["buttons"]
    assert [b.text for row in buttons for b in row] == _labels(reply)
    assert [b.data for row in buttons for b in row] == [d for _, d in _buttons(reply)]


# ---- guardrail: в коде фичи нет пути к лидам ----


_FEATURE_FILES = (
    "reader/dm_campaigns/models.py",
    "reader/dm_campaigns/repository.py",
    "reader/inviter_admin_bot/dm_campaign_controller.py",
    "reader/inviter_admin_bot/dm_campaign_callbacks.py",
    "reader/inviter_admin_bot/dm_campaign_texts.py",
)
_FORBIDDEN = (
    "send_message", "get_entity", "TelegramClient", "AsyncOpenAI", "responses.parse",
    "InviteToChannelRequest", "iter_messages", "get_messages", "import telethon", "from telethon",
    "import openai", "from openai",
)


def test_feature_code_has_no_send_or_openai_path():
    for relative in _FEATURE_FILES:
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        for token in _FORBIDDEN:
            assert token not in source, f"{relative}: {token}"
