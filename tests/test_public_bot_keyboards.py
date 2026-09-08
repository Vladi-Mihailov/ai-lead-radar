"""
Тесты reader/public_bot/keyboards.py::main_menu_keyboard() — "⛔
Остановить мониторинг" (task-level, trusted-only после переработки UX,
см. design report) и "📊 Статистика" добавляются в главное меню ТОЛЬКО
когда is_trusted=True (см. reader/public_bot/handlers.py — единственный
вызывающий код, решающий это по ConversationController.is_trusted(
event.sender_id)).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.public_bot.keyboards import main_menu_keyboard  # noqa: E402
from reader.public_bot.texts import (  # noqa: E402
    ADD_CAR_LABEL,
    CHECK_NOW_LABEL,
    MY_CARS_LABEL,
    STATISTICS_LABEL,
    STOP_LABEL,
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


def test_main_menu_keyboard_excludes_trusted_buttons_for_ordinary_user():
    keyboard = main_menu_keyboard(is_trusted=False)

    labels = _labels(keyboard)
    assert labels == [ADD_CAR_LABEL, MY_CARS_LABEL, CHECK_NOW_LABEL]


def test_main_menu_keyboard_includes_trusted_buttons_for_trusted_operator():
    keyboard = main_menu_keyboard(is_trusted=True)

    labels = _labels(keyboard)
    assert STATISTICS_LABEL in labels
    assert STOP_LABEL in labels


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
    assert len(with_trusted) == len(without) + 2  # + STOP_LABEL + STATISTICS_LABEL
