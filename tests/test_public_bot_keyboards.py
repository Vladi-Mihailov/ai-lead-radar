"""
Тесты reader/public_bot/keyboards.py::main_menu_keyboard() — "⛔
Остановить мониторинг"/"📊 Статистика" (trusted-only, см. design report) и
"🇹🇷 Штрафы Турции" (см. design report "унификация UI" — reply-кнопка
перехода в Turkey-бот, видна ВСЕМ) добавляются в главное меню.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.public_bot.keyboards import (  # noqa: E402
    main_menu_keyboard,
    turkey_bot_link_keyboard,
)
from reader.public_bot.texts import (  # noqa: E402
    ADD_CAR_LABEL,
    CHECK_NOW_LABEL,
    MY_CARS_LABEL,
    STATISTICS_LABEL,
    STOP_LABEL,
    TURKEY_BOT_LINK_LABEL,
    TURKEY_BOT_URL,
)


def _labels(keyboard) -> list[str]:
    # Button.text(...) возвращает обёртку telethon.tl.custom.button.Button —
    # реальный текст лежит на её внутреннем .button (types.KeyboardButton),
    # не на самой обёртке (там .text — это classmethod-конструктор).
    return [row_button.button.text for row in keyboard for row_button in row]


def test_main_menu_keyboard_without_trusted_buttons_by_default():
    keyboard = main_menu_keyboard()

    labels = _labels(keyboard)
    assert STATISTICS_LABEL not in labels
    assert STOP_LABEL not in labels


def test_main_menu_keyboard_ordinary_user_is_a_2x2_layout():
    """Явное требование задачи "унификация UI":
    ROW 1: ➕ Добавить авто | 📋 Мои авто
    ROW 2: 🔎 Проверить сейчас | 🇹🇷 Штрафы Турции"""
    keyboard = main_menu_keyboard(is_trusted=False)

    assert len(keyboard) == 2
    assert [b.button.text for b in keyboard[0]] == [ADD_CAR_LABEL, MY_CARS_LABEL]
    assert [b.button.text for b in keyboard[1]] == [CHECK_NOW_LABEL, TURKEY_BOT_LINK_LABEL]


def test_main_menu_keyboard_includes_trusted_buttons_for_trusted_operator():
    keyboard = main_menu_keyboard(is_trusted=True)

    labels = _labels(keyboard)
    assert STATISTICS_LABEL in labels
    assert STOP_LABEL in labels


def test_main_menu_keyboard_manager_adds_statistics_row_and_keeps_stop_row():
    """Задача явно не указала "⛔ Остановить мониторинг" в новом макете —
    по решению пользователя кнопка сохранена ОТДЕЛЬНОЙ строкой ПОСЛЕ
    заданных 3 строк (task-level admin flow не должен стать недостижим
    из главного меню, см. design report)."""
    keyboard = main_menu_keyboard(is_trusted=True)

    assert len(keyboard) == 4
    assert [b.button.text for b in keyboard[0]] == [ADD_CAR_LABEL, MY_CARS_LABEL]
    assert [b.button.text for b in keyboard[1]] == [CHECK_NOW_LABEL, TURKEY_BOT_LINK_LABEL]
    assert [b.button.text for b in keyboard[2]] == [STATISTICS_LABEL]
    assert [b.button.text for b in keyboard[3]] == [STOP_LABEL]


def test_main_menu_keyboard_does_not_change_ordinary_user_buttons():
    """Явное требование задачи: "не менять существующие кнопки и flow" —
    ADD_CAR/MY_CARS/CHECK_NOW остаются теми же тремя кнопками независимо
    от is_trusted (см. design report: старая "⛔ Остановить мониторинг"
    убрана из главного меню ТОЛЬКО для обычного пользователя, п.8)."""
    without = _labels(main_menu_keyboard(is_trusted=False))
    with_trusted = _labels(main_menu_keyboard(is_trusted=True))

    for label in (ADD_CAR_LABEL, MY_CARS_LABEL, CHECK_NOW_LABEL):
        assert label in without
        assert label in with_trusted
    assert len(with_trusted) == len(without) + 2  # + STATISTICS_LABEL + STOP_LABEL


def test_main_menu_keyboard_contains_the_turkey_bot_link_as_a_reply_button():
    """См. design report "унификация UI" — переход в Turkey-бот теперь
    ОБЫЧНАЯ reply-кнопка (Button.text) в главном меню, а не отдельное
    companion-сообщение (см. reader/public_bot/handlers.py::_send_reply/
    reader/public_bot/conversation.py::_handle_menu_label)."""
    labels = _labels(main_menu_keyboard(is_trusted=True))
    assert TURKEY_BOT_LINK_LABEL in labels


def test_turkey_bot_link_keyboard_has_the_expected_url_button():
    keyboard = turkey_bot_link_keyboard()

    assert len(keyboard) == 1
    assert len(keyboard[0]) == 1
    button = keyboard[0][0]
    assert button.text == TURKEY_BOT_LINK_LABEL
    assert button.url == TURKEY_BOT_URL
    assert TURKEY_BOT_URL == "https://t.me/ProtocolTRbot"
