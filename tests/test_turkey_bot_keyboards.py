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
    decode_toll_provider_callback,
    encode_garage_check_callback,
    encode_help_callback,
    encode_toll_provider_callback,
    garage_keyboard,
    georgian_bot_link_keyboard,
    help_menu_keyboard,
    help_section_keyboard,
    main_menu_keyboard,
    toll_provider_keyboard,
)
from reader.turkey_bot.models import TurkeyUserCar  # noqa: E402
from reader.turkey_bot.texts import (  # noqa: E402
    CANCEL_BUTTON_LABEL,
    CHECK_FINES_LABEL,
    CHECK_TOLLS_AVRASYA_LABEL,
    CHECK_TOLLS_KGM_LABEL,
    CHECK_TOLLS_LABEL,
    GARAGE_CHECK_FINES_LABEL,
    GARAGE_CHECK_TOLLS_KGM_LABEL,
    GARAGE_LABEL,
    GEORGIAN_BOT_LINK_LABEL,
    GEORGIAN_BOT_URL,
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


def test_main_menu_ordinary_user_is_a_2x2_plus_help_layout():
    """Явное требование задачи "унификация UI":
    ROW 1: 🚔 Проверить штрафы | 🛣 Проверить платные дороги
    ROW 2: 🚗 Мои авто | 🇬🇪 Штрафы Грузии
    ROW 3: ℹ️ Справка"""
    keyboard = main_menu_keyboard(is_trusted=False)

    assert len(keyboard) == 3
    assert _text_labels([keyboard[0]]) == [CHECK_FINES_LABEL, CHECK_TOLLS_LABEL]
    assert _text_labels([keyboard[1]]) == [GARAGE_LABEL, GEORGIAN_BOT_LINK_LABEL]
    assert _text_labels([keyboard[2]]) == [HELP_LABEL]
    assert STATISTICS_LABEL not in _text_labels(keyboard)


def test_main_menu_manager_puts_help_and_statistics_in_the_same_last_row():
    """Явное требование задачи: манагеру ROW 3 = ℹ️ Справка | 📊
    Статистика (одна строка, не две отдельные)."""
    keyboard = main_menu_keyboard(is_trusted=True)

    assert len(keyboard) == 3
    assert _text_labels([keyboard[0]]) == [CHECK_FINES_LABEL, CHECK_TOLLS_LABEL]
    assert _text_labels([keyboard[1]]) == [GARAGE_LABEL, GEORGIAN_BOT_LINK_LABEL]
    assert _text_labels([keyboard[2]]) == [HELP_LABEL, STATISTICS_LABEL]


def test_main_menu_keyboard_contains_the_georgian_bot_link_as_a_reply_button():
    """См. design report "унификация UI" — переход в Georgian-бот теперь
    ОБЫЧНАЯ reply-кнопка (Button.text) в главном меню, а не отдельное
    companion-сообщение (см. reader/turkey_bot/handlers.py::_send_reply/
    reader/turkey_bot/conversation.py::handle_text)."""
    labels = _text_labels(main_menu_keyboard(is_trusted=True))
    assert GEORGIAN_BOT_LINK_LABEL in labels


def test_georgian_bot_link_keyboard_has_the_expected_url_button():
    keyboard = georgian_bot_link_keyboard()

    assert len(keyboard) == 1
    assert len(keyboard[0]) == 1
    button = keyboard[0][0]
    assert button.text == GEORGIAN_BOT_LINK_LABEL
    assert button.url == GEORGIAN_BOT_URL
    assert GEORGIAN_BOT_URL == "https://t.me/ProtocolGEbot"


def test_garage_keyboard_has_four_buttons_per_car():
    """[НОМЕР] [🚔 Проверить штрафы] [🚇 Avrasya Tüneli] [🛣 KGM] (см.
    design report Stage 2B: "Saved cars must show both actions", и
    design report "Реализация KGM provider" п.10 — KGM добавлен как
    третья кнопка)."""
    cars = [_car(1, "34ABC123"), _car(2, "06XYZ999")]

    keyboard = garage_keyboard(cars)

    assert len(keyboard) == 2
    for row in keyboard:
        assert len(row) == 4


def test_garage_keyboard_shows_plate_and_all_action_labels():
    """Укороченные подписи (см. design report "унификация UI") —
    <PLATE> | 🚔 Штрафы | 🚇 Туннели | 🛣 Дороги — НЕ переиспользуют
    CHECK_FINES_LABEL/CHECK_TOLLS_KGM_LABEL (главное меню/подменю платных
    дорог их не меняли, см. GARAGE_CHECK_FINES_LABEL/
    GARAGE_CHECK_TOLLS_KGM_LABEL докстрок в texts.py)."""
    keyboard = garage_keyboard([_car(1, "34ABC123")])

    row = keyboard[0]
    assert row[0].text == "34ABC123"
    assert row[1].text == GARAGE_CHECK_FINES_LABEL == "🚔 Штрафы"
    assert row[2].text == CHECK_TOLLS_AVRASYA_LABEL == "🚇 Туннели"
    assert row[3].text == GARAGE_CHECK_TOLLS_KGM_LABEL == "🛣 Дороги"


def test_garage_keyboard_plate_and_fines_button_encode_the_same_gib_callback():
    """См. design report Stage 1: "Clicking the plate itself may either
    be a no-op/info action or start the same check" — здесь обе ведут на
    ОДИН и тот же (gib, car_id) callback."""
    keyboard = garage_keyboard([_car(42, "34ABC123")])

    row = keyboard[0]
    assert row[0].data == row[1].data
    assert decode_garage_check_callback(row[0].data) == ("gib", 42)


def test_garage_keyboard_tolls_buttons_encode_avrasya_and_kgm_callbacks():
    keyboard = garage_keyboard([_car(42, "34ABC123")])

    row = keyboard[0]
    assert decode_garage_check_callback(row[2].data) == ("avrasya", 42)
    assert decode_garage_check_callback(row[3].data) == ("kgm", 42)


def test_encode_decode_garage_check_roundtrip_for_each_provider():
    assert decode_garage_check_callback(encode_garage_check_callback("gib", 7)) == ("gib", 7)
    assert decode_garage_check_callback(encode_garage_check_callback("avrasya", 7)) == ("avrasya", 7)
    assert decode_garage_check_callback(encode_garage_check_callback("kgm", 7)) == ("kgm", 7)


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


def test_toll_provider_keyboard_has_avrasya_and_kgm_buttons():
    """См. design report "Реализация KGM provider" п.10 — CHECK_TOLLS_LABEL
    ведёт к явному выбору "🚇 Avrasya Tüneli" / "🛣 Все дороги и мосты
    (KGM)", Avrasya не удалена."""
    keyboard = toll_provider_keyboard()

    assert len(keyboard) == 2
    assert keyboard[0][0].text == CHECK_TOLLS_AVRASYA_LABEL
    assert keyboard[1][0].text == CHECK_TOLLS_KGM_LABEL
    assert decode_toll_provider_callback(keyboard[0][0].data) == "avrasya"
    assert decode_toll_provider_callback(keyboard[1][0].data) == "kgm"


def test_encode_decode_toll_provider_roundtrip():
    assert decode_toll_provider_callback(encode_toll_provider_callback("avrasya")) == "avrasya"
    assert decode_toll_provider_callback(encode_toll_provider_callback("kgm")) == "kgm"


def test_decode_toll_provider_rejects_unrelated_or_unknown_data():
    assert decode_toll_provider_callback(CANCEL_CALLBACK_DATA) is None
    assert decode_toll_provider_callback(None) is None
    # "gib" не входит в выбор платных дорог (см. модуль docstring
    # keyboards.py про _TOLL_PROVIDERS) — штрафы не имеют подменю.
    assert decode_toll_provider_callback(b"turkeytollprovider:gib") is None


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
