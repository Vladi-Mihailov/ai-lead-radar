"""
Тесты reader/public_bot/handlers.py::_send_reply — переход в Turkey-бот
(см. design report "унификация UI"): "🇹🇷 Штрафы Турции" — обычная
reply-кнопка главного меню, нажатие отвечает ОДНИМ сообщением с inline
URL-кнопкой (show_turkey_bot_link=True), никакого автоматического
companion-сообщения при показе главного меню больше нет. Реальный
Telethon не используется — event подменяется лёгким фейком, записывающим
вызовы .respond(...)/.edit(...).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.public_bot import texts  # noqa: E402
from reader.public_bot.handlers import (
    _TELEGRAM_SAFE_TEXT_LIMIT,
    _send_reply,
    _split_telegram_text,
)


class _FakeReply:
    def __init__(self, *, text, show_main_menu=False, show_turkey_bot_link=False):
        self.text = text
        self.show_main_menu = show_main_menu
        self.show_period_buttons = False
        self.show_add_client_decision_buttons = False
        self.check_now_options = None
        self.trusted_stop_options = None
        self.trusted_stop_confirm_task_id = None
        self.trusted_tasks_page = None
        self.trusted_tasks_page_options = None
        self.trusted_task_detail_id = None
        self.trusted_task_detail_page = None
        self.trusted_task_off_id = None
        self.trusted_task_off_page = None
        self.trusted_task_period_id = None
        self.trusted_task_period_page = None
        self.my_cars_page_options = None
        self.car_delete_confirm_subscription_id = None
        self.car_detail_subscription_id = None
        self.cta_buttons = None
        self.show_turkey_bot_link = show_turkey_bot_link
        self.debt_refresh_available = False
        self.debt_refresh_confirm = False
        self.debt_list_page = None
        self.debt_list_total_pages = None
        # "📸 Проверить протокол" (см. задачу "Проверить протокол") — без
        # этих двух полей _send_reply падает с AttributeError на ЛЮБОМ
        # _FakeReply, у которого ВСЕ более ранние elif-условия ложны (см.
        # reader/public_bot/handlers.py — protocol_check_* проверяются
        # одними из последних в цепочке elif).
        self.protocol_check_method_prompt = False
        self.protocol_check_back_target = None


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


async def test_main_menu_text_no_longer_sends_a_companion_message():
    """См. design report "унификация UI" — переход в Turkey-бот перенесён
    в постоянную reply-клавиатуру (см. reader/public_bot/keyboards.py::
    main_menu_keyboard), больше НЕ отправляется автоматическое
    companion-сообщение при показе главного меню."""
    reply = _FakeReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 1
    assert event.calls[0]["text"] == texts.MAIN_MENU_TEXT
    assert event.calls[0]["buttons"] is not None  # main_menu_keyboard(...)


async def test_turkey_bot_link_reply_attaches_the_inline_url_button():
    """Штатный способ перехода для reply-кнопки (см.
    reader/public_bot/conversation.py::_handle_menu_label) — ответ на
    "🇹🇷 Штрафы Турции" несёт show_turkey_bot_link=True, handlers.py
    прикрепляет turkey_bot_link_keyboard() к ЭТОМУ ЖЕ сообщению (не
    отдельным вторым, как раньше)."""
    reply = _FakeReply(text=texts.TURKEY_BOT_LINK_TEXT, show_turkey_bot_link=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 1
    assert event.calls[0]["text"] == texts.TURKEY_BOT_LINK_TEXT
    labels = _button_labels(event.calls[0]["buttons"])
    urls = _button_urls(event.calls[0]["buttons"])
    assert labels == [texts.TURKEY_BOT_LINK_LABEL]
    assert urls == {texts.TURKEY_BOT_URL}


async def test_non_main_menu_screens_do_not_get_the_turkey_bot_link():
    reply = _FakeReply(text="🔎 Проверка завершена: штрафов не найдено.", show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 1
    assert event.calls[0]["buttons"] is not None  # main_menu_keyboard(...), не turkey-link


async def test_turkey_bot_link_reply_works_on_edit_path_too():
    """prefer_edit=True (см. callback-driven "⬅️ Назад" и т.п.) — та же
    inline-кнопка прикрепляется и при редактировании существующего
    сообщения."""
    reply = _FakeReply(text=texts.TURKEY_BOT_LINK_TEXT, show_turkey_bot_link=True)
    event = _FakeEvent()

    await _send_reply(event, reply, prefer_edit=True)

    assert len(event.calls) == 1
    labels = _button_labels(event.calls[0]["buttons"])
    assert labels == [texts.TURKEY_BOT_LINK_LABEL]


async def test_turkey_bot_link_keyboard_is_pure_inline_and_does_not_crash_telethon():
    """Регрессия ровно того класса, что была найдена при реализации этой
    задачи: смешивание Button.text (reply) и Button.url (inline) в ОДНОЙ
    разметке заставляет Telethon поднять ValueError('You cannot mix
    inline with normal buttons') — здесь проверяется, что реальный
    telethon.client.buttons.ButtonMethods.build_reply_markup успешно
    строит ОБЕ разметки (главное меню и ответ на "🇹🇷 Штрафы Турции") по
    отдельности, без единого смешивания."""
    from telethon.client.buttons import ButtonMethods

    from reader.public_bot.keyboards import main_menu_keyboard, turkey_bot_link_keyboard

    menu_markup = ButtonMethods.build_reply_markup(main_menu_keyboard())
    link_markup = ButtonMethods.build_reply_markup(turkey_bot_link_keyboard())

    assert type(menu_markup).__name__ == "ReplyKeyboardMarkup"
    assert type(link_markup).__name__ == "ReplyInlineMarkup"


# ---- _split_telegram_text() — см. задачу "MessageTooLongError fix" ----


def test_split_short_text_returns_single_chunk():
    text = "короткий текст"
    assert _split_telegram_text(text) == [text]


def test_split_long_text_produces_multiple_chunks():
    # 5 параграфов по ~1000 символов каждый ("A"*1000 + "\n") — суммарно
    # заведомо больше лимита.
    text = "\n".join("A" * 1000 for _ in range(5))
    chunks = _split_telegram_text(text)
    assert len(chunks) > 1


def test_split_reproduces_original_text_exactly():
    """См. задачу: "Do not lose or duplicate any characters/content" —
    конкатенация всех chunks == исходный текст, побайтово."""
    text = "\n".join(f"строка {i}: {'x' * 200}" for i in range(50))
    chunks = _split_telegram_text(text)
    assert "".join(chunks) == text


def test_every_chunk_is_within_the_safe_limit():
    text = "\n".join(f"строка {i}: {'x' * 200}" for i in range(50))
    chunks = _split_telegram_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= _TELEGRAM_SAFE_TEXT_LIMIT


def test_split_prefers_newline_boundaries():
    """См. задачу: "Prefer splitting at newline boundaries first" —
    короткие строки НЕ разрезаются посередине, каждый chunk заканчивается
    на границе исходной строки (кроме hard-split ветки, см. отдельный
    тест ниже)."""
    lines = [f"line-{i}-{'y' * 90}" for i in range(60)]
    text = "\n".join(lines)
    chunks = _split_telegram_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        # Каждый chunk (кроме, возможно, последнего) заканчивается ровно
        # на \n — то есть разрез произошёл МЕЖДУ строками, а не внутри.
        stripped = chunk.removesuffix("\n")
        for line in stripped.split("\n"):
            assert line in lines


def test_single_extremely_long_line_hard_splits_safely():
    """Одна строка (без единого \\n) длиннее лимита — см. задачу п.2:
    "if a single block itself exceeds the limit, hard-split it safely" —
    режется жёстко, но ничего не теряется/не дублируется."""
    text = "A" * 10000
    chunks = _split_telegram_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= _TELEGRAM_SAFE_TEXT_LIMIT
    assert "".join(chunks) == text


def test_mixed_normal_lines_and_one_oversized_line():
    """Смешанный случай — обычные короткие строки ДО и ПОСЛЕ одной
    строки-гиганта, превышающей лимит сама по себе."""
    text = "короткая строка 1\n" + ("B" * 9000) + "\nкороткая строка 2"
    chunks = _split_telegram_text(text)
    assert "".join(chunks) == text
    for chunk in chunks:
        assert len(chunk) <= _TELEGRAM_SAFE_TEXT_LIMIT


# ---- _send_reply() — MessageTooLongError fix, сквозные тесты ----


async def test_send_reply_short_message_is_one_send_with_buttons():
    reply = _FakeReply(text="короткий ответ", show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 1
    assert event.calls[0]["text"] == "короткий ответ"
    assert event.calls[0]["buttons"] is not None


async def test_send_reply_long_message_sends_multiple_chunks():
    long_text = "\n".join(f"строка {i}: {'x' * 200}" for i in range(50))
    reply = _FakeReply(text=long_text, show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) > 1
    assert "".join(call["text"] for call in event.calls) == long_text


async def test_send_reply_buttons_only_on_final_chunk():
    long_text = "\n".join(f"строка {i}: {'x' * 200}" for i in range(50))
    reply = _FakeReply(text=long_text, show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) > 1
    for call in event.calls[:-1]:
        assert call["buttons"] is None
    assert event.calls[-1]["buttons"] is not None


async def test_send_reply_long_message_on_edit_path_edits_first_chunk_then_responds():
    """prefer_edit=True — первый chunk идёт через event.edit(...)
    (buttons=None, т.к. это не последний chunk), остальные — через
    event.respond(...), buttons только на самом последнем."""
    long_text = "\n".join(f"строка {i}: {'x' * 200}" for i in range(50))
    reply = _FakeReply(text=long_text, show_main_menu=True)

    class _EditCapableEvent(_FakeEvent):
        def __init__(self):
            super().__init__()
            self.edit_calls: list[dict] = []
            self.respond_calls: list[dict] = []

        async def edit(self, text, *, buttons=None):
            self.edit_calls.append({"text": text, "buttons": buttons})
            self.calls.append({"text": text, "buttons": buttons})

        async def respond(self, text, *, buttons=None):
            self.respond_calls.append({"text": text, "buttons": buttons})
            self.calls.append({"text": text, "buttons": buttons})

    event = _EditCapableEvent()
    await _send_reply(event, reply, prefer_edit=True)

    assert len(event.edit_calls) == 1
    assert event.edit_calls[0]["buttons"] is None  # не последний chunk
    assert len(event.respond_calls) >= 1
    assert event.respond_calls[-1]["buttons"] is not None  # последний chunk
    assert "".join(call["text"] for call in event.calls) == long_text


async def test_send_reply_edit_failure_falls_back_to_respond_for_all_chunks():
    """Если event.edit(...) падает (например, сообщение слишком старое
    для редактирования) — ВЕСЬ текст (включая первый chunk) уходит через
    event.respond(...), ничего не теряется и не задваивается."""
    long_text = "\n".join(f"строка {i}: {'x' * 200}" for i in range(50))
    reply = _FakeReply(text=long_text, show_main_menu=True)

    class _EditFailsEvent(_FakeEvent):
        async def edit(self, text, *, buttons=None):
            raise RuntimeError("message too old to edit")

    event = _EditFailsEvent()
    await _send_reply(event, reply, prefer_edit=True)

    assert "".join(call["text"] for call in event.calls) == long_text
    assert event.calls[-1]["buttons"] is not None
    for call in event.calls[:-1]:
        assert call["buttons"] is None
