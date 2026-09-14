"""
Тесты reader/turkey_bot/keyboards.py — единственная клавиатура, inline
"❌ Отмена".
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.keyboards import (  # noqa: E402
    CANCEL_CALLBACK_DATA,
    cancel_keyboard,
)
from reader.turkey_bot.texts import CANCEL_BUTTON_LABEL  # noqa: E402


def test_cancel_keyboard_has_one_button_with_expected_label_and_data():
    keyboard = cancel_keyboard()

    assert len(keyboard) == 1
    assert len(keyboard[0]) == 1
    button = keyboard[0][0]
    assert button.text == CANCEL_BUTTON_LABEL
    assert button.data == CANCEL_CALLBACK_DATA


def test_cancel_callback_data_is_a_fixed_constant_without_dynamic_content():
    """См. design report Stage 3: callback_data здесь НЕ несёт
    session_token/subscription_id — идентичность chat_id решается
    исключительно event.chat_id самого нажатия (см.
    reader/turkey_bot/handlers.py)."""
    assert CANCEL_CALLBACK_DATA == b"turkeycancel"
