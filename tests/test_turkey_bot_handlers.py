"""
Тесты reader/turkey_bot/handlers.py::_send_reply — приоритет клавиатур на
ОДНОМ сообщении и корректное прикрепление cta_buttons/show_main_menu к
ПОСЛЕДНЕМУ сообщению при наличии extra_texts (см. design report "fix:
show Turkey fine CTAs for all users" — именно отсутствие cta_buttons= в
BotReply GIB has_debt было production-регрессией; этот файл дополнительно
проверяет уровень handlers.py, а не только ConversationController, чтобы
регрессия ТАКОГО рода (например, будущая опечатка в приоритете кнопок)
не могла повториться незамеченной). Реальный Telethon не используется —
event подменяется лёгким фейком, записывающим вызовы .respond(...)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot import texts  # noqa: E402
from reader.turkey_bot.conversation import BotReply  # noqa: E402
from reader.turkey_bot.handlers import _send_reply  # noqa: E402


class _FakeEvent:
    def __init__(self):
        self.calls: list[dict] = []

    async def respond(self, text, *, buttons=None, file=None):
        self.calls.append({"text": text, "buttons": buttons, "file": file})


def _button_labels(buttons) -> list[str]:
    return [button.text for row in buttons for button in row]


def _button_urls(buttons) -> set[str]:
    return {button.url for row in buttons for button in row}


async def test_cta_buttons_attach_to_the_single_message_when_no_extra_texts():
    reply = BotReply(
        text="has_debt result", cta_buttons=(("💳 Оплатить в рублях", "https://t.me/tplgee"),),
    )
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 1
    assert event.calls[0]["text"] == "has_debt result"
    assert _button_labels(event.calls[0]["buttons"]) == ["💳 Оплатить в рублях"]
    assert _button_urls(event.calls[0]["buttons"]) == {"https://t.me/tplgee"}


async def test_cta_buttons_attach_to_the_last_message_when_extra_texts_present():
    """См. design report: cta_buttons — на ПОСЛЕДНЕМ сообщении, ТОЧНО ТАК
    ЖЕ, как show_main_menu — не на первом, где разговор ещё не завершён."""
    reply = BotReply(
        text="fine 1 of many",
        extra_texts=("fine 2", "fine 3 (last)"),
        cta_buttons=(("💳 Оплатить в рублях", "https://t.me/tplgee"), ("🚗 ОСАГО Турции", "https://t.me/tplgee")),
    )
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 3
    assert event.calls[0]["buttons"] is None
    assert event.calls[1]["buttons"] is None
    assert _button_labels(event.calls[2]["buttons"]) == ["💳 Оплатить в рублях", "🚗 ОСАГО Турции"]


async def test_cta_buttons_take_priority_over_show_main_menu_on_last_message():
    reply = BotReply(
        text="fine 1", extra_texts=("fine 2 (last)",),
        show_main_menu=True,
        cta_buttons=(("💳 Оплатить в рублях", "https://t.me/tplgee"),),
    )
    event = _FakeEvent()

    await _send_reply(event, reply)

    # Последнее сообщение получает CTA, а НЕ персистентное reply-меню (см.
    # design report: приоритет cta_buttons > show_main_menu).
    assert _button_labels(event.calls[-1]["buttons"]) == ["💳 Оплатить в рублях"]


async def test_show_main_menu_still_attaches_to_last_message_without_cta_buttons():
    """Регрессия: существующее поведение show_main_menu (без cta_buttons)
    не должно было измениться этим фиксом."""
    reply = BotReply(text="stats", extra_texts=("user list (last)",), show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply, is_trusted=True)

    assert event.calls[0]["buttons"] is None
    assert event.calls[-1]["buttons"] is not None  # main_menu_keyboard(...)


async def test_no_cta_buttons_no_regression_for_plain_reply():
    reply = BotReply(text="no_debt result", show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 1
    assert event.calls[0]["buttons"] is not None


async def test_show_cancel_button_still_wins_over_cta_buttons():
    """Приоритет show_cancel_button > cta_buttons никогда не должен был
    измениться — на практике оба одновременно не встречаются, но порядок
    должен остаться стабильным."""
    reply = BotReply(
        text="ask captcha", show_cancel_button=True,
        cta_buttons=(("💳 Оплатить в рублях", "https://t.me/tplgee"),),
    )
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert _button_labels(event.calls[0]["buttons"]) == ["❌ Отмена"]


# ---- Переход в Georgian-бот (см. design report "унификация UI") ----


async def test_welcome_text_no_longer_sends_a_companion_message():
    """См. design report "унификация UI" — переход в Georgian-бот
    перенесён в постоянную reply-клавиатуру (см.
    reader/turkey_bot/keyboards.py::main_menu_keyboard), больше НЕ
    отправляется автоматическое companion-сообщение при показе главного
    меню (/start)."""
    reply = BotReply(text=texts.WELCOME_TEXT, show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 1
    assert event.calls[0]["text"] == texts.WELCOME_TEXT
    assert event.calls[0]["buttons"] is not None  # main_menu_keyboard(...)


async def test_georgian_bot_link_reply_attaches_the_inline_url_button():
    """Штатный способ перехода для reply-кнопки (см.
    reader/turkey_bot/conversation.py::_handle_georgian_bot_link) — ответ
    на "🇬🇪 Штрафы Грузии" несёт show_georgian_bot_link=True, handlers.py
    прикрепляет georgian_bot_link_keyboard() к ЭТОМУ ЖЕ сообщению (не
    отдельным вторым, как раньше)."""
    reply = BotReply(text=texts.GEORGIAN_BOT_LINK_TEXT, show_georgian_bot_link=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 1
    assert event.calls[0]["text"] == texts.GEORGIAN_BOT_LINK_TEXT
    labels = _button_labels(event.calls[0]["buttons"])
    urls = _button_urls(event.calls[0]["buttons"])
    assert labels == [texts.GEORGIAN_BOT_LINK_LABEL]
    assert urls == {texts.GEORGIAN_BOT_URL}


async def test_non_welcome_main_menu_screens_do_not_get_the_georgian_bot_link():
    """show_main_menu=True появляется после МНОГИХ результатов (has_debt/
    no_debt/ошибки) — companion-логики больше нет вовсе, но регрессия
    (один и тот же принцип, что и раньше) стоит держать явным тестом."""
    reply = BotReply(text="✅ 34ABC123: штрафов не найдено.", show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 1
    assert event.calls[0]["buttons"] is not None  # main_menu_keyboard(...), не georgia-link


async def test_georgian_bot_link_keyboard_is_pure_inline_and_does_not_crash_telethon():
    """Регрессия ровно того класса, что была найдена при реализации этой
    задачи: смешивание Button.text (reply) и Button.url (inline) в ОДНОЙ
    разметке заставляет Telethon поднять ValueError('You cannot mix
    inline with normal buttons') — здесь проверяется, что реальный
    telethon.client.buttons.ButtonMethods.build_reply_markup успешно
    строит ОБЕ разметки (главное меню и ответ на "🇬🇪 Штрафы Грузии") по
    отдельности, без единого смешивания."""
    from telethon.client.buttons import ButtonMethods

    from reader.turkey_bot.keyboards import (
        georgian_bot_link_keyboard,
        main_menu_keyboard,
    )

    menu_markup = ButtonMethods.build_reply_markup(main_menu_keyboard())
    link_markup = ButtonMethods.build_reply_markup(georgian_bot_link_keyboard())

    assert type(menu_markup).__name__ == "ReplyKeyboardMarkup"
    assert type(link_markup).__name__ == "ReplyInlineMarkup"
