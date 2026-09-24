"""
Тесты reader/turkey_bot/keyboards.py — ПЕРЕНЕСЕНО из reader/turkey_bot_test/
(см. задачу "Перенос Unified Turkey функционала в production") — старая
версия этого файла (garage_keyboard/toll_provider_keyboard/
decode_garage_check_callback — provider-by-provider CAPTCHA-visible UX)
заменена целиком новым car-centric unified UX, поэтому старые тесты
удалены вместе со старым кодом, который они проверяли (см.
test_turkey_bot_unified_menu.py/test_turkey_bot_my_cars.py/
test_turkey_bot_monitoring_toggle.py для conversation.py-уровня
интеграционных тестов car-centric flow — здесь только сама сборка
клавиатур/кодирование callback_data)."""

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.keyboards import (
    BACK_TO_MY_CARS_CALLBACK_DATA,
    CANCEL_CALLBACK_DATA,
    HELP_BACK_TO_MAIN_CALLBACK_DATA,
    add_car_confirmation_keyboard,
    cancel_keyboard,
    car_card_keyboard,
    car_delete_confirm_keyboard,
    check_now_picker_keyboard,
    decode_car_action_callback,
    decode_car_open_by_plate_callback,
    decode_car_open_callback,
    decode_help_callback,
    encode_car_action_callback,
    encode_car_open_by_plate_callback,
    encode_car_open_callback,
    encode_help_callback,
    georgian_bot_link_keyboard,
    help_menu_keyboard,
    help_section_keyboard,
    main_menu_keyboard,
    my_cars_list_keyboard,
)
from reader.turkey_bot.models import TurkeyUserCar
from reader.turkey_bot.texts import (
    ADD_CAR_LABEL,
    CANCEL_BUTTON_LABEL,
    CHECK_NOW_LABEL,
    DELETE_CANCEL_LABEL,
    DELETE_CAR_CONFIRM_BUTTON_LABEL,
    DELETE_CAR_LABEL,
    DISABLE_MONITORING_LABEL,
    ENABLE_MONITORING_LABEL,
    GEORGIAN_BOT_LINK_LABEL,
    GEORGIAN_BOT_URL,
    HELP_BACK_LABEL,
    HELP_LABEL,
    HISTORY_LABEL,
    MY_CARS_LABEL,
    SEARCH_LABEL,
    STATISTICS_LABEL,
    STOP_MONITORING_LABEL,
)


def _button_texts(rows):
    return [[btn.text for btn in row] for row in rows]


def _reply_button_texts(rows):
    """Button.text(...) (reply-keyboard, см. main_menu_keyboard) оборачивает
    подпись иначе, чем Button.inline(...) — доступ через .button.text (тот
    же приём, что и в test_turkey_bot_unified_menu.py)."""
    return [[btn.button.text for btn in row] for row in rows]


def _car(car_id: int = 1, car_number: str = "34ABC123") -> TurkeyUserCar:
    now = datetime.now(timezone.utc)
    return TurkeyUserCar(
        id=car_id, telegram_user_id=222, car_number=car_number,
        created_at=now, last_checked_at=now,
        last_overall_status=None, last_total_amount=None,
    )


def test_cancel_keyboard_uses_fixed_callback():
    keyboard = cancel_keyboard()
    assert keyboard[0][0].text == CANCEL_BUTTON_LABEL
    assert keyboard[0][0].data == CANCEL_CALLBACK_DATA


def test_regular_user_main_menu_layout():
    rows = main_menu_keyboard(is_trusted=False)
    grid = _reply_button_texts(rows)

    assert grid[0] == [ADD_CAR_LABEL, MY_CARS_LABEL]
    assert grid[1] == [CHECK_NOW_LABEL, GEORGIAN_BOT_LINK_LABEL]
    assert grid[2] == [HELP_LABEL]
    flat = [label for row in grid for label in row]
    assert STATISTICS_LABEL not in flat
    assert STOP_MONITORING_LABEL not in flat


def test_manager_main_menu_layout():
    """См. задачу "manager/trusted Search": SEARCH_LABEL заменяет бывшую
    STOP_MONITORING_LABEL в этой строке (константа/underlying flow
    остаются, см. reader/turkey_bot/keyboards.py::main_menu_keyboard)."""
    rows = main_menu_keyboard(is_trusted=True)
    grid = _reply_button_texts(rows)

    assert grid[0] == [ADD_CAR_LABEL, MY_CARS_LABEL]
    assert grid[1] == [CHECK_NOW_LABEL, SEARCH_LABEL]
    assert grid[2] == [STATISTICS_LABEL, GEORGIAN_BOT_LINK_LABEL]
    assert grid[3] == [HELP_LABEL]
    flat = [label for row in grid for label in row]
    assert STOP_MONITORING_LABEL not in flat


def test_georgian_bot_link_keyboard_is_a_url_button():
    keyboard = georgian_bot_link_keyboard()
    button = keyboard[0][0]
    assert button.text == GEORGIAN_BOT_LINK_LABEL
    assert button.url == GEORGIAN_BOT_URL


def test_car_open_callback_roundtrip():
    data = encode_car_open_callback(42)
    assert decode_car_open_callback(data) == 42


def test_car_open_callback_decode_rejects_unrelated_data():
    assert decode_car_open_callback(b"turkeycancel") is None
    assert decode_car_open_callback(None) is None
    assert decode_car_open_callback(b"turkeycaropen:not-a-number") is None


def test_car_open_by_plate_callback_roundtrip():
    data = encode_car_open_by_plate_callback("34ABC123")
    assert decode_car_open_by_plate_callback(data) == "34ABC123"


def test_car_action_callback_roundtrip_for_every_known_action():
    for action in ("check", "monitor_on", "monitor_off", "history", "delete_prompt", "delete_confirm", "delete_cancel"):
        data = encode_car_action_callback(action, 7)
        assert decode_car_action_callback(data) == (action, 7)


def test_car_action_callback_rejects_unknown_action():
    """Явное требование задачи п.9 (унаследовано из предыдущего этапа) —
    вручную сформированный callback с посторонним action не должен
    маршрутизироваться никуда, а не выполнять произвольное действие."""
    forged = b"turkeycaraction:delete:7"  # "delete" больше не в _CAR_ACTIONS
    assert decode_car_action_callback(forged) is None


def test_car_action_callback_rejects_malformed_car_id():
    forged = b"turkeycaraction:check:not-a-number"
    assert decode_car_action_callback(forged) is None


def test_my_cars_list_keyboard_shows_on_off_and_opens_correct_car():
    cars = [_car(1, "A123AA123"), _car(2, "B456BB456")]
    keyboard = my_cars_list_keyboard(cars, {1: True, 2: False})

    assert keyboard[0][0].text == "🟢 A123AA123 — ON"
    assert keyboard[1][0].text == "⚪ B456BB456 — OFF"
    assert decode_car_open_callback(keyboard[0][0].data) == 1
    assert decode_car_open_callback(keyboard[1][0].data) == 2


def test_my_cars_list_keyboard_defaults_missing_state_to_off():
    """monitoring_active без записи для car.id -> OFF, никогда ON по
    умолчанию (см. задачу: не показывать мониторинг активным без
    подтверждённого active=1 в БД)."""
    cars = [_car(1, "A123AA123")]
    keyboard = my_cars_list_keyboard(cars, {})

    assert keyboard[0][0].text == "⚪ A123AA123 — OFF"


def test_check_now_picker_keyboard_uses_plain_plate_label():
    cars = [_car(5, "A123AA123")]
    keyboard = check_now_picker_keyboard(cars)

    assert keyboard[0][0].text == "🚗 A123AA123"
    assert decode_car_action_callback(keyboard[0][0].data) == ("check", 5)


def test_off_card_shows_enable_button_history_delete_back():
    keyboard = car_card_keyboard(_car(3), monitoring_active=False)
    labels = [row[0].text for row in keyboard]

    assert labels == [CHECK_NOW_LABEL, ENABLE_MONITORING_LABEL, HISTORY_LABEL, DELETE_CAR_LABEL, "⬅️ Назад"]
    assert decode_car_action_callback(keyboard[1][0].data) == ("monitor_on", 3)
    assert decode_car_action_callback(keyboard[3][0].data) == ("delete_prompt", 3)
    assert keyboard[4][0].data == BACK_TO_MY_CARS_CALLBACK_DATA


def test_on_card_shows_disable_button():
    keyboard = car_card_keyboard(_car(3), monitoring_active=True)
    labels = [row[0].text for row in keyboard]

    assert labels[1] == DISABLE_MONITORING_LABEL
    assert decode_car_action_callback(keyboard[1][0].data) == ("monitor_off", 3)


def test_car_delete_confirm_keyboard_has_confirm_and_cancel():
    keyboard = car_delete_confirm_keyboard(9)
    row = keyboard[0]

    assert row[0].text == DELETE_CAR_CONFIRM_BUTTON_LABEL
    assert decode_car_action_callback(row[0].data) == ("delete_confirm", 9)
    assert row[1].text == DELETE_CANCEL_LABEL
    assert decode_car_action_callback(row[1].data) == ("delete_cancel", 9)


def test_add_car_confirmation_keyboard_offers_check_now():
    keyboard = add_car_confirmation_keyboard(11)
    assert keyboard[0][0].text == CHECK_NOW_LABEL
    assert decode_car_action_callback(keyboard[0][0].data) == ("check", 11)


def test_help_callback_roundtrip_for_known_sections():
    for section in ("menu", "terms", "gib", "avrasya", "payment"):
        assert decode_help_callback(encode_help_callback(section)) == section


def test_help_callback_rejects_unknown_section():
    assert decode_help_callback(encode_help_callback("unknown")) is None


def test_help_menu_keyboard_back_uses_fixed_main_menu_callback():
    keyboard = help_menu_keyboard()
    back_button = keyboard[-1][0]
    assert back_button.text == HELP_BACK_LABEL
    assert back_button.data == HELP_BACK_TO_MAIN_CALLBACK_DATA


def test_help_section_keyboard_back_returns_to_help_menu():
    keyboard = help_section_keyboard()
    assert decode_help_callback(keyboard[0][0].data) == "menu"
