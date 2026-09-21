"""Тесты reader/inviter_admin_bot/handlers.py::_send_reply — та же схема,
что и tests/test_public_bot_handlers.py: реальный Telethon не используется,
event подменяется лёгким fake, записывающим вызовы .respond(...)/.edit(...)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter_admin_bot import texts
from reader.inviter_admin_bot.handlers import _send_reply


class _FakeReply:
    def __init__(self, *, text, **fields):
        self.text = text
        self.show_main_menu = False
        self.show_cancel_button = False
        self.accounts_page_options = None
        self.account_card_id = None
        self.account_card_enabled = None
        self.limit_choice_account_id = None
        for key, value in fields.items():
            setattr(self, key, value)


class _FakeEvent:
    def __init__(self):
        self.calls: list[dict] = []

    async def respond(self, text, *, buttons=None):
        self.calls.append({"text": text, "buttons": buttons})

    async def edit(self, text, *, buttons=None):
        self.calls.append({"text": text, "buttons": buttons})


def _labels(buttons) -> list[str]:
    # Button.text(...) (reply-клавиатура) оборачивает в telethon.tl.custom.
    # button.Button — реальный текст на .button.text; Button.inline(...)
    # возвращает types.KeyboardButtonCallback напрямую — .text (тот же
    # приём различения, что и в tests/test_public_bot_keyboards.py::_labels).
    return [getattr(b, "button", b).text for row in buttons for b in row]


async def test_main_menu_reply_attaches_main_menu_keyboard():
    reply = _FakeReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 1
    labels = _labels(event.calls[0]["buttons"])
    assert texts.ACCOUNTS_LABEL in labels
    assert texts.ADD_ACCOUNT_LABEL in labels
    assert texts.START_LABEL in labels
    assert texts.PAUSE_LABEL in labels


async def test_access_denied_reply_has_no_buttons_at_all():
    """⛔ Нет доступа — НИ ОДНОЙ кнопки, никаких административных данных
    (см. design "ДОСТУП К ADMIN BOT")."""
    reply = _FakeReply(text=texts.ACCESS_DENIED_TEXT, show_main_menu=True)  # даже если controller ошибочно попросит меню
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert event.calls[0]["buttons"] is None


async def test_accounts_page_reply_attaches_account_rows():
    reply = _FakeReply(text=texts.ACCOUNTS_HEADER, accounts_page_options=[(1, "@vvz982", True), (2, "@ib85gnat", False)])
    event = _FakeEvent()

    await _send_reply(event, reply)

    labels = _labels(event.calls[0]["buttons"])
    assert labels == ["@vvz982", "🟢", "@ib85gnat", "⚪"]


async def test_account_card_reply_shows_turn_off_when_enabled():
    reply = _FakeReply(text="card", account_card_id=1, account_card_enabled=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    labels = _labels(event.calls[0]["buttons"])
    assert texts.TURN_OFF_ACCOUNT_LABEL in labels
    assert texts.TURN_ON_ACCOUNT_LABEL not in labels


async def test_account_card_reply_shows_turn_on_when_disabled():
    reply = _FakeReply(text="card", account_card_id=1, account_card_enabled=False)
    event = _FakeEvent()

    await _send_reply(event, reply)

    labels = _labels(event.calls[0]["buttons"])
    assert texts.TURN_ON_ACCOUNT_LABEL in labels
    assert texts.TURN_OFF_ACCOUNT_LABEL not in labels


async def test_limit_choice_reply_attaches_limit_keyboard():
    reply = _FakeReply(text="limit", limit_choice_account_id=1)
    event = _FakeEvent()

    await _send_reply(event, reply)

    labels = _labels(event.calls[0]["buttons"])
    assert "15" in labels
    assert texts.MANUAL_LIMIT_LABEL in labels


async def test_cancel_button_reply_attaches_cancel_keyboard():
    reply = _FakeReply(text=texts.PHONE_PROMPT, show_cancel_button=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    labels = _labels(event.calls[0]["buttons"])
    assert labels == [texts.CANCEL_BUTTON_LABEL]


async def test_prefer_edit_falls_back_to_respond_when_edit_fails():
    class _FailingEditEvent(_FakeEvent):
        async def edit(self, text, *, buttons=None):
            raise RuntimeError("message too old to edit")

    reply = _FakeReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)
    event = _FailingEditEvent()

    await _send_reply(event, reply, prefer_edit=True)

    assert len(event.calls) == 1
    assert event.calls[0]["text"] == texts.MAIN_MENU_TEXT
