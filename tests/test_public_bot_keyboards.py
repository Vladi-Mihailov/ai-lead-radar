"""
Тесты reader/public_bot/keyboards.py::main_menu_keyboard() — "📊
Статистика" добавляется в главное меню ТОЛЬКО когда include_statistics=True
(см. reader/public_bot/handlers.py — единственный вызывающий код, решающий
это по ConversationController.is_trusted(event.sender_id))."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.public_bot.keyboards import main_menu_keyboard  # noqa: E402
from reader.public_bot.texts import STATISTICS_LABEL  # noqa: E402


def _labels(keyboard) -> list[str]:
    # Button.text(...) возвращает обёртку telethon.tl.custom.button.Button —
    # реальный текст лежит на её внутреннем .button (types.KeyboardButton),
    # не на самой обёртке (там .text — это classmethod-конструктор).
    return [row_button.button.text for row in keyboard for row_button in row]


def test_main_menu_keyboard_without_statistics_by_default():
    keyboard = main_menu_keyboard()

    assert STATISTICS_LABEL not in _labels(keyboard)


def test_main_menu_keyboard_excludes_statistics_for_ordinary_user():
    keyboard = main_menu_keyboard(include_statistics=False)

    assert STATISTICS_LABEL not in _labels(keyboard)


def test_main_menu_keyboard_includes_statistics_for_trusted_operator():
    keyboard = main_menu_keyboard(include_statistics=True)

    assert STATISTICS_LABEL in _labels(keyboard)


def test_main_menu_keyboard_does_not_change_existing_buttons():
    """Явное требование задачи: "не менять существующие кнопки и flow"."""
    without = _labels(main_menu_keyboard(include_statistics=False))
    with_stats = _labels(main_menu_keyboard(include_statistics=True))

    assert without == with_stats[: len(without)]
    assert len(with_stats) == len(without) + 1
