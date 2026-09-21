"""
Тесты reader/public_bot/keyboards.py::main_menu_keyboard() — "⛔
Остановить мониторинг"/"📊 Статистика" (trusted-only, см. design report) и
"🇹🇷 Штрафы Турции" (см. design report "унификация UI" — reply-кнопка
перехода в Turkey-бот, видна ВСЕМ) добавляются в главное меню.
"""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.public_bot.keyboards import (  # noqa: E402
    decode_trusted_task_continue_callback,
    decode_trusted_task_open_callback,
    decode_trusted_task_period_callback,
    decode_trusted_task_toggle_callback,
    encode_trusted_task_continue_callback,
    encode_trusted_task_open_callback,
    encode_trusted_task_period_callback,
    encode_trusted_task_toggle_callback,
    main_menu_keyboard,
    trusted_task_detail_keyboard,
    trusted_task_off_keyboard,
    trusted_task_period_choice_keyboard,
    trusted_tasks_page_keyboard,
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


def test_main_menu_keyboard_manager_puts_statistics_and_stop_in_one_row():
    """Явное требование задачи "маленький UI-fix": ROW 3 = 📊 Статистика |
    ⛔ Остановить мониторинг (одна строка, не две отдельные)."""
    keyboard = main_menu_keyboard(is_trusted=True)

    assert len(keyboard) == 3
    assert [b.button.text for b in keyboard[0]] == [ADD_CAR_LABEL, MY_CARS_LABEL]
    assert [b.button.text for b in keyboard[1]] == [CHECK_NOW_LABEL, TURKEY_BOT_LINK_LABEL]
    assert [b.button.text for b in keyboard[2]] == [STATISTICS_LABEL, STOP_LABEL]


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


# ==== manager-facing "📋 Мои авто" ON/OFF + "▶️ Продолжить мониторинг"
# 15/30/90 дней (см. design report про per-car monitoring toggle) ====


def test_trusted_task_toggle_callback_round_trips():
    data = encode_trusted_task_toggle_callback(42, 3)
    assert decode_trusted_task_toggle_callback(data) == (42, 3)


def test_trusted_task_continue_callback_round_trips():
    data = encode_trusted_task_continue_callback(42, 3)
    assert decode_trusted_task_continue_callback(data) == (42, 3)


def test_trusted_task_period_callback_round_trips():
    data = encode_trusted_task_period_callback(42, 30, 3)
    assert decode_trusted_task_period_callback(data) == (42, 30, 3)


def test_trusted_task_period_callback_rejects_days_outside_allowlist():
    """Тот же принцип, что и у decode_period_callback — allowlist, не
    любое целое число (подделанный/чужой callback_data не должен
    пройти)."""
    forged = b"ttaskperiod:42:45:3"
    assert decode_trusted_task_period_callback(forged) is None


def test_trusted_task_period_callback_rejects_malformed_data():
    assert decode_trusted_task_period_callback(b"ttaskperiod:42:30") is None
    assert decode_trusted_task_period_callback(b"ttaskperiod:notanumber:30:3") is None
    assert decode_trusted_task_period_callback(None) is None
    assert decode_trusted_task_period_callback(b"somethingelse:42:30:3") is None


def test_trusted_task_toggle_callback_does_not_collide_with_continue_prefix():
    """Разные префиксы — decode одного никогда не путает callback другого
    (тот же принцип "коллизий по префиксу нет", см. design report)."""
    toggle_data = encode_trusted_task_toggle_callback(1, 0)
    assert decode_trusted_task_continue_callback(toggle_data) is None


def test_trusted_tasks_page_keyboard_shows_plate_and_on_off_buttons_per_row():
    """Явное требование задачи "переделываем строки" (текст в левой
    кнопке обрезался Telegram'ом): каждая машина — строка из ДВУХ кнопок,
    голый номер (открывает карточку) + "🟢 до ДД.ММ"/"⚪" — список
    БОЛЬШЕ НЕ дублируется текстом (см. design report: Telegram не
    позволяет inline-кнопку справа от строки текста)."""
    keyboard = trusted_tasks_page_keyboard(
        [
            (1, "Y111CA18", True, date(2026, 12, 20)),
            (2, "B641XE89", False, date(2026, 8, 8)),
        ],
        page=0, total_pages=1,
    )

    assert len(keyboard) == 2  # без пагинации (total_pages=1)
    row0 = [b.text for b in keyboard[0]]
    row1 = [b.text for b in keyboard[1]]
    assert row0 == ["Y111CA18", "🟢 до 20.12"]
    assert row1 == ["B641XE89", "⚪"]


def test_trusted_tasks_page_keyboard_left_button_is_bare_car_number():
    """Явное требование задачи: ЛЕВАЯ кнопка — ТОЛЬКО номер, без 🚗 и без
    "· до ДД.ММ" (иначе длинные номера обрезаются Telegram'ом)."""
    keyboard = trusted_tasks_page_keyboard(
        [(1, "K892AC126", True, date(2026, 9, 4))], page=0, total_pages=1,
    )

    left_label = keyboard[0][0].text
    assert left_label == "K892AC126"
    assert "🚗" not in left_label
    assert "до" not in left_label
    assert "2026" not in left_label


def test_trusted_tasks_page_keyboard_right_button_on_shows_short_end_date():
    """ON: "🟢 до ДД.ММ" — короткая дата (день.месяц), без года, без слова
    "ON"."""
    keyboard = trusted_tasks_page_keyboard(
        [(1, "K892AC126", True, date(2026, 9, 4))], page=0, total_pages=1,
    )

    right_label = keyboard[0][1].text
    assert right_label == "🟢 до 04.09"
    assert "2026" not in right_label
    assert "ON" not in right_label


def test_trusted_tasks_page_keyboard_right_button_off_is_just_grey_circle():
    """OFF: голый "⚪" — БЕЗ слова "OFF" и БЕЗ даты, даже если у задачи
    есть последний сохранённый end_date (см. design report: "Для OFF дату
    НЕ показывать")."""
    keyboard = trusted_tasks_page_keyboard(
        [(2, "K892AC126", False, date(2026, 9, 4))], page=0, total_pages=1,
    )

    assert keyboard[0][0].text == "K892AC126"
    assert keyboard[0][1].text == "⚪"


def test_trusted_tasks_page_keyboard_plate_button_opens_task_detail():
    """Кнопка-номер больше НЕ no-op (как раньше) — кодирует
    encode_trusted_task_open_callback(task_id, page), открывающий
    карточку этой конкретной машины."""
    keyboard = trusted_tasks_page_keyboard(
        [(42, "M295YB196", True, date(2026, 9, 4))], page=3, total_pages=1,
    )

    plate_button = keyboard[0][0]
    assert decode_trusted_task_open_callback(plate_button.data) == (42, 3)


def test_trusted_tasks_page_keyboard_adds_pagination_row_when_multiple_pages():
    keyboard = trusted_tasks_page_keyboard(
        [(1, "M295YB196", True, date(2026, 9, 4))], page=0, total_pages=2,
    )

    assert len(keyboard) == 2  # 1 car row + 1 pagination row
    assert len(keyboard[-1]) == 3


def test_trusted_task_detail_keyboard_has_only_back():
    keyboard = trusted_task_detail_keyboard(page=1)

    labels = [b.text for row in keyboard for b in row]
    assert labels == ["⬅️ Назад"]


def test_trusted_task_open_callback_round_trips():
    data = encode_trusted_task_open_callback(42, 3)
    assert decode_trusted_task_open_callback(data) == (42, 3)


def test_trusted_task_off_keyboard_has_continue_and_back():
    keyboard = trusted_task_off_keyboard(42, page=1)

    labels = [b.text for row in keyboard for b in row]
    assert labels == ["▶️ Продолжить мониторинг", "⬅️ Назад"]


def test_trusted_task_period_choice_keyboard_has_15_30_90_and_back():
    keyboard = trusted_task_period_choice_keyboard(42, page=1)

    labels = [b.text for row in keyboard for b in row]
    assert labels == ["15 дней", "30 дней", "90 дней", "⬅️ Назад"]


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
