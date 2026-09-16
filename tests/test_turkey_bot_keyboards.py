"""
Тесты reader/turkey_bot/keyboards.py — inline "❌ Отмена", персистентное
reply-меню (main_menu_keyboard) и inline-гараж (garage_keyboard, теперь с
двумя действиями на автомобиль — GIB/Avrasya, см. design report Stage 2B).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.keyboards import (  # noqa: E402
    CANCEL_CALLBACK_DATA,
    HELP_BACK_TO_MAIN_CALLBACK_DATA,
    cancel_keyboard,
    decode_garage_check_callback,
    decode_help_callback,
    encode_garage_check_callback,
    encode_help_callback,
    garage_keyboard,
    help_menu_keyboard,
    help_section_keyboard,
    main_menu_keyboard,
)
from reader.turkey_bot.models import TurkeyUserCar  # noqa: E402
from reader.turkey_bot.texts import (  # noqa: E402
    CANCEL_BUTTON_LABEL,
    CHECK_FINES_LABEL,
    CHECK_TOLLS_LABEL,
    GARAGE_LABEL,
    HELP_AVRASYA_LABEL,
    HELP_BACK_LABEL,
    HELP_GIB_LABEL,
    HELP_LABEL,
    HELP_PAYMENT_LABEL,
    HELP_TERMS_LABEL,
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


def test_main_menu_shows_fines_tolls_garage_and_help_for_everyone():
    labels = _text_labels(main_menu_keyboard(is_trusted=False))

    assert CHECK_FINES_LABEL in labels
    assert CHECK_TOLLS_LABEL in labels
    assert GARAGE_LABEL in labels
    assert HELP_LABEL in labels
    assert STATISTICS_LABEL not in labels


def test_main_menu_shows_statistics_only_for_trusted():
    """Явное требование задачи: "A normal user must not see the
    button"."""
    labels = _text_labels(main_menu_keyboard(is_trusted=True))

    assert CHECK_FINES_LABEL in labels
    assert CHECK_TOLLS_LABEL in labels
    assert GARAGE_LABEL in labels
    assert HELP_LABEL in labels
    assert STATISTICS_LABEL in labels


def test_garage_keyboard_has_three_buttons_per_car():
    """[НОМЕР] [🚔 Проверить штрафы] [🛣 Проверить платные дороги] (см.
    design report Stage 2B: "Saved cars must show both actions")."""
    cars = [_car(1, "34ABC123"), _car(2, "06XYZ999")]

    keyboard = garage_keyboard(cars)

    assert len(keyboard) == 2
    for row in keyboard:
        assert len(row) == 3


def test_garage_keyboard_shows_plate_and_both_action_labels():
    keyboard = garage_keyboard([_car(1, "34ABC123")])

    row = keyboard[0]
    assert row[0].text == "34ABC123"
    assert row[1].text == CHECK_FINES_LABEL
    assert row[2].text == CHECK_TOLLS_LABEL


def test_garage_keyboard_plate_and_fines_button_encode_the_same_gib_callback():
    """См. design report Stage 1: "Clicking the plate itself may either
    be a no-op/info action or start the same check" — здесь обе ведут на
    ОДИН и тот же (gib, car_id) callback."""
    keyboard = garage_keyboard([_car(42, "34ABC123")])

    row = keyboard[0]
    assert row[0].data == row[1].data
    assert decode_garage_check_callback(row[0].data) == ("gib", 42)


def test_garage_keyboard_tolls_button_encodes_avrasya_callback():
    keyboard = garage_keyboard([_car(42, "34ABC123")])

    row = keyboard[0]
    assert decode_garage_check_callback(row[2].data) == ("avrasya", 42)


def test_encode_decode_garage_check_roundtrip_for_each_provider():
    assert decode_garage_check_callback(encode_garage_check_callback("gib", 7)) == ("gib", 7)
    assert decode_garage_check_callback(encode_garage_check_callback("avrasya", 7)) == ("avrasya", 7)


def test_decode_garage_check_rejects_unrelated_callback_data():
    assert decode_garage_check_callback(CANCEL_CALLBACK_DATA) is None
    assert decode_garage_check_callback(b"somethingelse:1") is None
    assert decode_garage_check_callback(None) is None


def test_decode_garage_check_rejects_non_numeric_payload():
    assert decode_garage_check_callback(b"turkeygaragecheck:gib:not-a-number") is None


def test_decode_garage_check_rejects_unknown_provider():
    """См. design report: "design the internal architecture so
    additional toll-road providers can be added later" — но НЕ принимает
    произвольную строку как провайдер уже сейчас (только зарегистрированные)."""
    assert decode_garage_check_callback(b"turkeygaragecheck:unknownprovider:7") is None


def test_decode_garage_check_rejects_missing_car_id():
    assert decode_garage_check_callback(b"turkeygaragecheck:gib:") is None


# ---- ℹ️ Справка (см. design report) ----


def test_help_menu_keyboard_has_four_sections_plus_back_to_main():
    keyboard = help_menu_keyboard()

    assert len(keyboard) == 5
    for row in keyboard:
        assert len(row) == 1
    labels = [row[0].text for row in keyboard]
    assert labels == [
        HELP_TERMS_LABEL, HELP_GIB_LABEL, HELP_AVRASYA_LABEL, HELP_PAYMENT_LABEL, HELP_BACK_LABEL,
    ]


def test_help_menu_keyboard_back_button_uses_fixed_main_menu_callback():
    keyboard = help_menu_keyboard()

    back_button = keyboard[-1][0]
    assert back_button.data == HELP_BACK_TO_MAIN_CALLBACK_DATA


def test_help_menu_keyboard_section_buttons_encode_expected_callbacks():
    keyboard = help_menu_keyboard()

    assert decode_help_callback(keyboard[0][0].data) == "terms"
    assert decode_help_callback(keyboard[1][0].data) == "gib"
    assert decode_help_callback(keyboard[2][0].data) == "avrasya"
    assert decode_help_callback(keyboard[3][0].data) == "payment"


def test_help_section_keyboard_has_only_a_back_to_help_menu_button():
    keyboard = help_section_keyboard()

    assert len(keyboard) == 1
    assert len(keyboard[0]) == 1
    button = keyboard[0][0]
    assert button.text == HELP_BACK_LABEL
    assert decode_help_callback(button.data) == "menu"


def test_encode_decode_help_callback_roundtrip_for_each_section():
    for section in ("terms", "gib", "avrasya", "payment", "menu"):
        assert decode_help_callback(encode_help_callback(section)) == section


def test_decode_help_callback_rejects_unrelated_callback_data():
    assert decode_help_callback(CANCEL_CALLBACK_DATA) is None
    assert decode_help_callback(HELP_BACK_TO_MAIN_CALLBACK_DATA) is None
    assert decode_help_callback(None) is None


def test_decode_help_callback_rejects_unknown_section():
    assert decode_help_callback(b"turkeyhelp:somethingelse") is None
