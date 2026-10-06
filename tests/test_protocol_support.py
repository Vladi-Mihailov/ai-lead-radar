"""Живой диалог с оператором в @ProtocolGEbot и @ProtocolTRbot через группу
менеджеров. Боты подключаются через свои НАСТОЯЩИЕ register() к фейковому
Telethon-клиенту (перехватывает обработчики и все отправки) — без сети."""

import itertools
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from telethon import events

from reader.protocol_support import texts as st
from reader.protocol_support.repository import STATUS_CLOSED, ProtocolSupportRepository
from reader.protocol_support.service import BotProfile, ProtocolSupportService
from reader.protocol_support.telethon_gateway import (
    TelethonSupportGateway,
    encode_close,
    register_support_group_handlers,
)
from reader.public_bot import handlers as ge_handlers
from reader.public_bot import keyboards as ge_keyboards
from reader.public_bot import texts as ge_texts
from reader.public_bot.conversation import BotReply as GeBotReply
from reader.turkey_bot import handlers as tr_handlers
from reader.turkey_bot import keyboards as tr_keyboards
from reader.turkey_bot import texts as tr_texts
from reader.turkey_bot.conversation import BotReply as TrBotReply

GROUP = -1009876543210
MANAGER, STRANGER, USER, USER2 = 410811386, 999, 7001, 7002
TRUSTED = {MANAGER}


# ---------------- фейковый Telegram ----------------


class FakeClient:
    def __init__(self, name):
        self.name, self.handlers, self.sent = name, [], []
        self._ids = itertools.count(1000 if name == "ge" else 5000)
        self.fail_to = set()  # чаты, куда отправка "падает"

    def on(self, builder):
        def decorator(fn):
            self.handlers.append((builder, fn))
            return fn
        return decorator

    def _handler(self, kind, group):
        for builder, fn in self.handlers:
            if isinstance(builder, kind) and bool(getattr(builder, "chats", None)) == group:
                return fn
        raise AssertionError("handler not registered")

    async def _send(self, chat, text, media=None, buttons=None, reply_to=None):
        if chat in self.fail_to:
            raise ConnectionError("Telegram unavailable")
        msg = SimpleNamespace(id=next(self._ids))
        self.sent.append(dict(chat=chat, text=text, media=media, buttons=buttons, reply_to=reply_to, id=msg.id))
        return msg

    async def send_message(self, chat, text, buttons=None, reply_to=None, link_preview=None):
        return await self._send(chat, text, buttons=buttons, reply_to=reply_to)

    async def send_file(self, chat, media, caption=None, buttons=None, reply_to=None):
        return await self._send(chat, caption, media=media, buttons=buttons, reply_to=reply_to)

    def to(self, chat):
        return [s for s in self.sent if s["chat"] == chat]


def _sender(user_id, username="vova", first="Владимир", bot=False):
    return SimpleNamespace(id=user_id, username=username, first_name=first, last_name=None, bot=bot)


class PrivateEvent:
    def __init__(self, text="", *, user=USER, photo=None, document=None, sticker=None, username="vova"):
        media = photo or document or sticker
        self.raw_text, self.sender_id, self.chat_id, self.is_private = text, user, user, True
        self.message = SimpleNamespace(id=42, photo=photo, document=document or sticker, sticker=sticker, media=media)
        self._sender = _sender(user, username=username)
        self.responses = []

    async def get_sender(self):
        return self._sender

    async def respond(self, text, buttons=None, **kwargs):
        self.responses.append(dict(text=text, buttons=buttons))


class GroupEvent:
    def __init__(self, text, *, reply_to, sender=MANAGER, chat=GROUP, out=False, is_bot=False, photo=None,
                 msg_id=777):
        self.raw_text, self.chat_id, self.sender_id, self.out = text, chat, sender, out
        self.reply_to_msg_id, self.is_private = reply_to, False
        self.message = SimpleNamespace(id=msg_id, photo=photo, document=None, sticker=None, media=photo)
        self._sender = _sender(sender, bot=is_bot)

    async def get_sender(self):
        return self._sender


class CallbackEvent:
    def __init__(self, data, *, sender=MANAGER):
        self.data, self.sender_id, self.answers, self.edits = data, sender, [], []

    async def answer(self, text="", alert=False):
        self.answers.append(text)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeController:
    def __init__(self, reply_cls):
        self.reply_cls, self.texts, self.resets = reply_cls, [], []

    def is_trusted(self, user_id):
        return user_id in TRUSTED

    def reset_conversation(self, chat_id):
        self.resets.append(chat_id)

    async def handle_text(self, text, **kwargs):
        self.texts.append(text)
        return self.reply_cls(text="обычная логика", show_main_menu=True)


BOTS = {
    "ge": dict(handlers=ge_handlers, keyboards=ge_keyboards, texts=ge_texts, reply=GeBotReply, flag="🇬🇪",
               username="ProtocolGEbot"),
    "tr": dict(handlers=tr_handlers, keyboards=tr_keyboards, texts=tr_texts, reply=TrBotReply, flag="🇹🇷",
               username="ProtocolTRbot"),
}


class Bot:
    """Один процесс бота: клиент + register() + сервис поддержки на общей БД."""

    def __init__(self, key, db_path, *, support_chat_id=GROUP):
        spec = BOTS[key]
        self.key, self.spec = key, spec
        self.client = FakeClient(key)
        self.controller = FakeController(spec["reply"])
        self.repo = ProtocolSupportRepository(db_path)
        self.service = ProtocolSupportService(
            self.repo, BotProfile(key=key, username=spec["username"], flag=spec["flag"]),
            TelethonSupportGateway(self.client,
                                   user_main_menu=lambda: spec["keyboards"].main_menu_keyboard(is_trusted=False)),
            support_chat_id=support_chat_id, is_trusted=lambda uid: uid in TRUSTED,
        )
        spec["handlers"].register(self.client, self.controller, None, support=self.service)
        register_support_group_handlers(self.client, self.service)

    async def private(self, event):
        await self.client._handler(events.NewMessage, False)(event)
        return event

    async def group(self, event):
        await self.client._handler(events.NewMessage, True)(event)

    async def callback(self, event):
        await self.client._handler(events.CallbackQuery, True)(event)
        return event

    def cards(self):
        return self.client.to(GROUP)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "users.db"


def _labels(rows):
    return [[b.button.text for b in row] for row in rows]


# ---------------- меню ----------------


@pytest.mark.parametrize("key", ["ge", "tr"])
def test_user_menu_operator_under_check_now_and_help_last_full_width(key):
    spec = BOTS[key]
    grid = _labels(spec["keyboards"].main_menu_keyboard(is_trusted=False))
    check_row = next(i for i, row in enumerate(grid) if spec["texts"].CHECK_NOW_LABEL in row)
    col = grid[check_row].index(spec["texts"].CHECK_NOW_LABEL)
    assert grid[check_row + 1][col] == st.OPERATOR_LABEL  # сразу под «Проверить сейчас»
    assert grid[-1] == [spec["texts"].HELP_LABEL]          # «Справка» — последняя строка целиком
    flat = [label for row in grid for label in row]
    assert flat.count(st.OPERATOR_LABEL) == 1 and flat.count(spec["texts"].HELP_LABEL) == 1


@pytest.mark.parametrize("key", ["ge", "tr"])
def test_manager_menu_has_no_operator_button(key):
    grid = _labels(BOTS[key]["keyboards"].main_menu_keyboard(is_trusted=True))
    assert st.OPERATOR_LABEL not in [label for row in grid for label in row]


def test_operator_keyboard_has_end_and_main_menu():
    from reader.protocol_support.telethon_gateway import operator_chat_keyboard
    assert _labels(operator_chat_keyboard()) == [[st.END_DIALOG_LABEL], [st.SUPPORT_MAIN_MENU_LABEL]]


# ---------------- вход и пользователь -> менеджер ----------------


@pytest.mark.parametrize("key", ["ge", "tr"])
async def test_enter_operator_chat(db, key):
    bot = Bot(key, db)
    event = await bot.private(PrivateEvent(st.OPERATOR_LABEL))
    assert event.responses[0]["text"] == st.CONNECTED_TEXT
    assert _labels(event.responses[0]["buttons"]) == [[st.END_DIALOG_LABEL], [st.SUPPORT_MAIN_MENU_LABEL]]
    assert bot.service.open_dialog(USER) is not None and bot.controller.resets == [USER]
    assert bot.controller.texts == []  # обычная логика бота не вызывалась


@pytest.mark.parametrize("key", ["ge", "tr"])
async def test_user_text_goes_to_group_with_visible_source(db, key):
    bot = Bot(key, db)
    await bot.private(PrivateEvent(st.OPERATOR_LABEL))
    await bot.private(PrivateEvent("Как оплатить штраф?"))
    (card,) = bot.cards()
    spec = BOTS[key]
    assert card["text"] == (f"{spec['flag']} Новое обращение из @{spec['username']}\n\nИмя: Владимир\n"
                             "Username: @vova\n\nСообщение:\nКак оплатить штраф?")
    assert card["buttons"][0][0].text == st.CLOSE_DIALOG_BUTTON
    assert bot.controller.texts == []  # не ушло в проверку штрафов/дорог


@pytest.mark.parametrize("key", ["ge", "tr"])
@pytest.mark.parametrize("kind", ["photo", "document"])
async def test_user_media_is_resent_as_is(db, key, kind):
    bot = Bot(key, db)
    await bot.private(PrivateEvent(st.OPERATOR_LABEL))
    media = SimpleNamespace(name=f"{kind}-media")
    await bot.private(PrivateEvent("Вот квитанция", **{kind: media}))
    (card,) = bot.cards()
    assert card["media"] is media  # то же Telegram-медиа, без скачивания
    assert card["text"].endswith("Сообщение:\nВот квитанция") and BOTS[key]["flag"] in card["text"]


@pytest.mark.parametrize("key", ["ge", "tr"])
async def test_media_without_caption_and_missing_username(db, key):
    bot = Bot(key, db)
    await bot.private(PrivateEvent(st.OPERATOR_LABEL, username=None))
    await bot.private(PrivateEvent("", photo=SimpleNamespace(name="p"), username=None))
    (card,) = bot.cards()
    assert "Username: —" in card["text"] and "Сообщение" not in card["text"]


async def test_follow_up_messages_thread_under_the_first_card(db):
    bot = Bot("ge", db)
    await bot.private(PrivateEvent(st.OPERATOR_LABEL))
    await bot.private(PrivateEvent("Как оплатить штраф?"))
    await bot.private(PrivateEvent("А сколько ждать?"))
    first, second = bot.cards()
    assert second["reply_to"] == first["id"] and second["text"].startswith("🇬🇪 Сообщение из @ProtocolGEbot")


async def test_unsupported_user_message_gets_hint(db):
    bot = Bot("ge", db)
    await bot.private(PrivateEvent(st.OPERATOR_LABEL))
    event = await bot.private(PrivateEvent("", sticker=SimpleNamespace(name="st")))
    assert event.responses[0]["text"] == st.UNSUPPORTED_USER_TEXT and bot.cards() == []


# ---------------- менеджер -> пользователь ----------------


async def _opened(bot, user=USER, text="Как оплатить штраф?"):
    await bot.private(PrivateEvent(st.OPERATOR_LABEL, user=user))
    await bot.private(PrivateEvent(text, user=user))
    return bot.cards()[-1]["id"]


@pytest.mark.parametrize("key", ["ge", "tr"])
async def test_manager_reply_reaches_user_via_same_bot(db, key):
    bot = Bot(key, db)
    card = await _opened(bot)
    await bot.group(GroupEvent("Сначала штраф должен появиться в базе.", reply_to=card))
    (to_user,) = bot.client.to(USER)
    assert to_user["text"] == "👨‍💼 Оператор:\n\nСначала штраф должен появиться в базе."


async def test_live_dialog_continues_both_ways(db):
    bot = Bot("ge", db)
    card = await _opened(bot)
    await bot.group(GroupEvent("Сначала штраф должен появиться в базе.", reply_to=card, msg_id=801))
    await bot.private(PrivateEvent("А сколько ждать?"))
    second_card = bot.cards()[-1]["id"]
    await bot.group(GroupEvent("Обычно несколько дней.", reply_to=second_card, msg_id=802))
    await bot.group(GroupEvent("И ещё: бот пришлёт уведомление.", reply_to=801, msg_id=803))  # reply на свой ответ
    assert [m["text"].split("\n\n")[1] for m in bot.client.to(USER)] == [
        "Сначала штраф должен появиться в базе.", "Обычно несколько дней.", "И ещё: бот пришлёт уведомление."]


async def test_manager_photo_is_resent_to_user(db):
    bot = Bot("tr", db)
    card = await _opened(bot)
    photo = SimpleNamespace(name="screenshot")
    await bot.group(GroupEvent("Смотрите", reply_to=card, photo=photo))
    (to_user,) = bot.client.to(USER)
    assert to_user["media"] is photo and to_user["text"] == "👨‍💼 Оператор:\n\nСмотрите"


async def test_same_user_two_bots_never_mixed(db):
    ge, tr = Bot("ge", db), Bot("tr", db)
    ge_card = await _opened(ge, text="GE вопрос")
    tr_card = await _opened(tr, text="TR вопрос")
    assert ge.service.open_dialog(USER).id != tr.service.open_dialog(USER).id
    # общая группа: оба процесса видят оба reply, но каждый доставляет только свой
    for bot in (ge, tr):
        await bot.group(GroupEvent("ответ GE", reply_to=ge_card, msg_id=901))
        await bot.group(GroupEvent("ответ TR", reply_to=tr_card, msg_id=902))
    assert [m["text"] for m in ge.client.to(USER)] == ["👨‍💼 Оператор:\n\nответ GE"]
    assert [m["text"] for m in tr.client.to(USER)] == ["👨‍💼 Оператор:\n\nответ TR"]


async def test_two_users_never_mixed(db):
    bot = Bot("ge", db)
    card1 = await _opened(bot, user=USER, text="вопрос 1")
    card2 = await _opened(bot, user=USER2, text="вопрос 2")
    await bot.group(GroupEvent("для второго", reply_to=card2, msg_id=910))
    await bot.group(GroupEvent("для первого", reply_to=card1, msg_id=911))
    assert [m["text"] for m in bot.client.to(USER)] == ["👨‍💼 Оператор:\n\nдля первого"]
    assert [m["text"] for m in bot.client.to(USER2)] == ["👨‍💼 Оператор:\n\nдля второго"]


# ---------------- один открытый диалог / persistence ----------------


async def test_repeated_operator_press_reuses_open_dialog(db):
    bot = Bot("ge", db)
    await bot.private(PrivateEvent(st.OPERATOR_LABEL))
    first = bot.service.open_dialog(USER).id
    event = await bot.private(PrivateEvent(st.OPERATOR_LABEL))
    assert bot.service.open_dialog(USER).id == first and event.responses[0]["text"] == st.CONNECTED_TEXT
    count = bot.repo._conn.execute("SELECT COUNT(*) FROM protocol_support_dialogs").fetchone()[0]
    assert count == 1


async def test_mapping_survives_restart(db):
    before = Bot("ge", db)
    card = await _opened(before)
    before.repo.close()
    after = Bot("ge", db)  # «рестарт» процесса: новые репозиторий/сервис/клиент
    await after.group(GroupEvent("После рестарта", reply_to=card))
    assert [m["text"] for m in after.client.to(USER)] == ["👨‍💼 Оператор:\n\nПосле рестарта"]
    assert after.service.open_dialog(USER) is not None


# ---------------- закрытие ----------------


@pytest.mark.parametrize("key", ["ge", "tr"])
async def test_user_closes_dialog(db, key):
    bot = Bot(key, db)
    await _opened(bot)
    event = await bot.private(PrivateEvent(st.END_DIALOG_LABEL))
    assert event.responses[0]["text"] == st.CLOSED_BY_USER_TEXT
    assert st.OPERATOR_LABEL in [label for row in _labels(event.responses[0]["buttons"]) for label in row]
    assert bot.service.open_dialog(USER) is None
    await bot.private(PrivateEvent("AA123BB"))  # дальше — обычная логика бота
    assert bot.controller.texts == ["AA123BB"]


@pytest.mark.parametrize("label", [st.SUPPORT_MAIN_MENU_LABEL, "/start"])
async def test_main_menu_exit_closes_dialog(db, label):
    bot = Bot("tr", db)
    await _opened(bot)
    event = await bot.private(PrivateEvent(label))
    assert event.responses[0]["text"] == tr_texts.MAIN_MENU_TEXT and bot.service.open_dialog(USER) is None
    assert bot.controller.texts == []


@pytest.mark.parametrize("key", ["ge", "tr"])
async def test_manager_closes_dialog_idempotently(db, key):
    bot = Bot(key, db)
    await _opened(bot)
    dialog_id = bot.service.open_dialog(USER).id
    first = await bot.callback(CallbackEvent(encode_close(dialog_id)))
    assert first.answers == [st.DIALOG_CLOSED_NOTE] and first.edits == [{"buttons": None}]
    (note,) = bot.client.to(USER)
    assert note["text"] == st.CLOSED_BY_MANAGER_TEXT and note["buttons"] is not None  # главное меню бота
    again = await bot.callback(CallbackEvent(encode_close(dialog_id)))
    assert again.answers == ["Диалог уже закрыт"] and len(bot.client.to(USER)) == 1
    assert bot.repo.get(dialog_id).status == STATUS_CLOSED


async def test_untrusted_cannot_close(db):
    bot = Bot("ge", db)
    await _opened(bot)
    dialog_id = bot.service.open_dialog(USER).id
    event = await bot.callback(CallbackEvent(encode_close(dialog_id), sender=STRANGER))
    assert event.answers == ["Нет доступа"] and bot.service.open_dialog(USER) is not None


# ---------------- безопасность ----------------


@pytest.mark.parametrize("case", ["untrusted", "not_reply", "unknown_message", "bot", "outgoing", "other_chat"])
async def test_ignored_group_messages(db, case):
    bot = Bot("ge", db)
    card = await _opened(bot)
    kwargs = dict(reply_to=card)
    if case == "untrusted":
        kwargs["sender"] = STRANGER
    elif case == "not_reply":
        kwargs["reply_to"] = None
    elif case == "unknown_message":
        kwargs["reply_to"] = 123456
    elif case == "bot":
        kwargs["is_bot"] = True
    elif case == "outgoing":
        kwargs["out"] = True
    elif case == "other_chat":
        kwargs["chat"] = -100111
    await bot.group(GroupEvent("текст", **kwargs))
    assert bot.client.to(USER) == []


async def test_reply_to_closed_dialog_is_ignored(db):
    bot = Bot("ge", db)
    card = await _opened(bot)
    await bot.private(PrivateEvent(st.END_DIALOG_LABEL))
    await bot.group(GroupEvent("поздний ответ", reply_to=card))
    assert bot.client.to(USER) == []


async def test_disabled_support_answers_unavailable(db):
    bot = Bot("ge", db, support_chat_id=None)
    event = await bot.private(PrivateEvent(st.OPERATOR_LABEL))
    assert event.responses[0]["text"] == st.UNAVAILABLE_TEXT and bot.cards() == []


def test_settings_require_numeric_support_chat_id():
    from reader.settings import _support_chat_id
    assert _support_chat_id("-1001234567890") == -1001234567890 and _support_chat_id(None) is None
    with pytest.raises(ValueError):
        _support_chat_id("@tplgee")


# ---------------- ошибки Telegram ----------------


async def test_user_blocked_bot_manager_is_told(db):
    bot = Bot("ge", db)
    card = await _opened(bot)
    bot.client.fail_to.add(USER)
    await bot.group(GroupEvent("ответ", reply_to=card, msg_id=950))
    warning = bot.cards()[-1]
    assert warning["text"] == st.DELIVERY_FAILED_TEXT and warning["reply_to"] == 950


async def test_support_group_unavailable_user_is_told(db):
    bot = Bot("tr", db)
    await bot.private(PrivateEvent(st.OPERATOR_LABEL))
    bot.client.fail_to.add(GROUP)
    event = await bot.private(PrivateEvent("Вопрос"))
    assert event.responses[0]["text"] == st.UNAVAILABLE_TEXT
    assert bot.service.open_dialog(USER) is not None  # диалог не теряется, можно повторить


async def test_media_copy_failure_user_is_told(db):
    bot = Bot("ge", db)
    await bot.private(PrivateEvent(st.OPERATOR_LABEL))
    bot.client.fail_to.add(GROUP)
    event = await bot.private(PrivateEvent("фото", photo=SimpleNamespace(name="p")))
    assert event.responses[0]["text"] == st.UNAVAILABLE_TEXT


async def test_normal_flow_untouched_without_dialog(db):
    bot = Bot("ge", db)
    await bot.private(PrivateEvent("AA123BB"))
    assert bot.controller.texts == ["AA123BB"] and bot.cards() == []
