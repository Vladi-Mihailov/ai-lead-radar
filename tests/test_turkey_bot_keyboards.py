"""
Тесты reader/turkey_bot/keyboards.py — inline "❌ Отмена", персистентное
reply-меню (main_menu_keyboard) и inline-гараж (garage_keyboard).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.keyboards import (  # noqa: E402
    CANCEL_CALLBACK_DATA,
    cancel_keyboard,
    decode_garage_check_callback,
    encode_garage_check_callback,
    garage_keyboard,
    main_menu_keyboard,
)
from reader.turkey_bot.models import TurkeyUserCar  # noqa: E402
from reader.turkey_bot.texts import (  # noqa: E402
    CANCEL_BUTTON_LABEL,
    GARAGE_CHECK_BUTTON_LABEL,
    GARAGE_LABEL,
    STATISTICS_LABEL,
)


def _text_labels(keyboard) -> list[str]:
    # Button.text(...) возвращает обёртку telethon.tl.custom.button.Button -
    # реальный текст лежит на её внутреннем .button (types.KeyboardButton),
    # не на самой обёртке (см. tests/test_public_bot_keyboards.py про тот
    # же нюанс).
    return [row_button.button.text for row in keyboard for row_button in row]


def _car(car_id: int, car_number: str) -> TurkeyUserCar:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return TurkeyUserCar(
        id=car_id, telegram_user_id=1, car_number=car_number, created_at=now, last_checked_at=now,
    )


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


def test_main_menu_shows_garage_for_everyone():
    labels = _text_labels(main_menu_keyboard(is_trusted=False))

    assert GARAGE_LABEL in labels
    assert STATISTICS_LABEL not in labels


def test_main_menu_shows_statistics_only_for_trusted():
    """Явное требование задачи: "A normal user must not see the
    button"."""
    labels = _text_labels(main_menu_keyboard(is_trusted=True))

    assert GARAGE_LABEL in labels
    assert STATISTICS_LABEL in labels


def test_garage_keyboard_has_two_buttons_per_car():
    cars = [_car(1, "34ABC123"), _car(2, "06XYZ999")]

    keyboard = garage_keyboard(cars)

    assert len(keyboard) == 2
    for row in keyboard:
        assert len(row) == 2


def test_garage_keyboard_shows_plate_and_check_button_labels():
    keyboard = garage_keyboard([_car(1, "34ABC123")])

    row = keyboard[0]
    assert row[0].text == "34ABC123"
    assert row[1].text == GARAGE_CHECK_BUTTON_LABEL


def test_garage_keyboard_both_buttons_encode_same_car_id():
    """См. design report: "Clicking the plate itself may either be a
    no-op/info action or start the same check" - здесь обе кнопки ведут
    на ОДИН и тот же callback."""
    keyboard = garage_keyboard([_car(42, "34ABC123")])

    row = keyboard[0]
    assert row[0].data == row[1].data
    assert decode_garage_check_callback(row[0].data) == 42


def test_encode_decode_garage_check_roundtrip():
    data = encode_garage_check_callback(7)

    assert decode_garage_check_callback(data) == 7


def test_decode_garage_check_rejects_unrelated_callback_data():
    assert decode_garage_check_callback(CANCEL_CALLBACK_DATA) is None
    assert decode_garage_check_callback(b"somethingelse:1") is None
    assert decode_garage_check_callback(None) is None


def test_decode_garage_check_rejects_non_numeric_payload():
    assert decode_garage_check_callback(b"turkeygaragecheck:not-a-number") is None
