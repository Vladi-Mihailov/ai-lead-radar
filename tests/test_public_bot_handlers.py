"""
Тесты reader/public_bot/handlers.py::_send_reply — переход в Turkey-бот
(см. design report "связать Georgian bot и Turkey bot взаимными кнопками
перехода"). Реальный Telethon не используется — event подменяется лёгким
фейком, записывающим вызовы .respond(...)/.edit(...).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.public_bot import texts  # noqa: E402
from reader.public_bot.handlers import _send_reply  # noqa: E402


class _FakeReply:
    def __init__(self, *, text, show_main_menu=False):
        self.text = text
        self.show_main_menu = show_main_menu
        self.show_period_buttons = False
        self.show_add_client_decision_buttons = False
        self.check_now_options = None
        self.trusted_stop_options = None
        self.trusted_stop_confirm_task_id = None
        self.trusted_tasks_page = None
        self.my_cars_page_options = None
        self.car_delete_confirm_subscription_id = None
        self.car_detail_subscription_id = None
        self.cta_buttons = None


class _FakeEvent:
    def __init__(self):
        self.calls: list[dict] = []

    async def respond(self, text, *, buttons=None):
        self.calls.append({"text": text, "buttons": buttons})

    async def edit(self, text, *, buttons=None):
        self.calls.append({"text": text, "buttons": buttons})


def _button_labels(buttons) -> list[str]:
    return [button.text for row in buttons for button in row]


def _button_urls(buttons) -> set[str]:
    return {button.url for row in buttons for button in row}


async def test_main_menu_text_sends_a_companion_message_with_turkey_bot_link():
    """Главное меню (MAIN_MENU_TEXT, см. ConversationController.start) —
    ЕДИНСТВЕННОЕ место, где появляется переход в Turkey-бот, ОТДЕЛЬНЫМ
    сообщением (Telethon не позволяет смешать reply- и inline-кнопки в
    одной разметке, см. reader/public_bot/keyboards.py::
    main_menu_keyboard докстрок)."""
    reply = _FakeReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 2
    assert event.calls[0]["text"] == texts.MAIN_MENU_TEXT
    assert event.calls[0]["buttons"] is not None  # main_menu_keyboard(...)
    assert event.calls[1]["text"] == texts.TURKEY_BOT_LINK_TEXT
    labels = _button_labels(event.calls[1]["buttons"])
    urls = _button_urls(event.calls[1]["buttons"])
    assert labels == [texts.TURKEY_BOT_LINK_LABEL]
    assert urls == {texts.TURKEY_BOT_URL}


async def test_non_main_menu_screens_do_not_get_the_turkey_bot_link():
    """Явное требование задачи: "не добавлять переход в каждый экран —
    только в главное меню" — show_main_menu=True появляется после МНОГИХ
    результатов (статистика/проверки/ошибки), но companion-сообщение не
    должно отправляться, если это не буквально экран главного меню."""
    reply = _FakeReply(text="🔎 Проверка завершена: штрафов не найдено.", show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 1


async def test_main_menu_text_link_sent_even_on_edit_path():
    """prefer_edit=True (см. callback-driven "⬅️ Назад" и т.п.) — companion-
    сообщение всё равно отправляется отдельным .respond(...), а не теряется
    из-за того, что основной текст был отредактирован, а не отправлен
    заново."""
    reply = _FakeReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply, prefer_edit=True)

    assert len(event.calls) == 2
    assert event.calls[1]["text"] == texts.TURKEY_BOT_LINK_TEXT


async def test_turkey_bot_link_keyboard_is_pure_inline_and_does_not_crash_telethon():
    """Регрессия ровно того класса, что была найдена при реализации этой
    задачи: смешивание Button.text (reply) и Button.url (inline) в ОДНОЙ
    разметке заставляет Telethon поднять ValueError('You cannot mix
    inline with normal buttons') — здесь проверяется, что реальный
    telethon.client.buttons.ButtonMethods.build_reply_markup успешно
    строит ОБЕ разметки (главное меню и companion-сообщение) по
    отдельности, без единого смешивания."""
    from telethon.client.buttons import ButtonMethods

    from reader.public_bot.keyboards import main_menu_keyboard, turkey_bot_link_keyboard

    menu_markup = ButtonMethods.build_reply_markup(main_menu_keyboard())
    link_markup = ButtonMethods.build_reply_markup(turkey_bot_link_keyboard())

    assert type(menu_markup).__name__ == "ReplyKeyboardMarkup"
    assert type(link_markup).__name__ == "ReplyInlineMarkup"
