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
