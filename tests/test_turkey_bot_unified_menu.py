"""Тесты главного меню (см. design report "Перестроить UX Turkey test
bot" п.1) — layout для обычного пользователя и менеджера (trusted)."""

from reader.turkey_bot.keyboards import main_menu_keyboard
from reader.turkey_bot.texts import (
    ADD_CAR_LABEL,
    CHECK_NOW_LABEL,
    GEORGIAN_BOT_LINK_LABEL,
    HELP_LABEL,
    MY_CARS_LABEL,
    STATISTICS_LABEL,
    STOP_MONITORING_LABEL,
)


def _button_texts(rows):
    return [[btn.button.text for btn in row] for row in rows]


def test_regular_user_menu_has_no_manager_row():
    rows = main_menu_keyboard(is_trusted=False)
    texts_grid = _button_texts(rows)

    assert texts_grid[0] == [ADD_CAR_LABEL, MY_CARS_LABEL]
    assert texts_grid[1] == [CHECK_NOW_LABEL, GEORGIAN_BOT_LINK_LABEL]
    flat = [label for row in texts_grid for label in row]
    assert STATISTICS_LABEL not in flat
    assert STOP_MONITORING_LABEL not in flat
    assert HELP_LABEL in flat


def test_manager_menu_has_statistics_and_stop_monitoring_row():
    """См. задачу "Расположение кнопок manager ReplyKeyboard" — новый
    макет: ROW2 checknow/stop_monitoring, ROW3 statistics/georgian."""
    rows = main_menu_keyboard(is_trusted=True)
    texts_grid = _button_texts(rows)

    assert texts_grid[0] == [ADD_CAR_LABEL, MY_CARS_LABEL]
    assert texts_grid[1] == [CHECK_NOW_LABEL, STOP_MONITORING_LABEL]
    assert texts_grid[2] == [STATISTICS_LABEL, GEORGIAN_BOT_LINK_LABEL]
    assert texts_grid[3] == [HELP_LABEL]


def test_old_provider_specific_labels_are_not_in_main_menu():
    """См. design report п.1: убрать из главного меню отдельные 🚔/🚇/🛣."""
    for is_trusted in (False, True):
        flat = [label for row in _button_texts(main_menu_keyboard(is_trusted=is_trusted)) for label in row]
        assert "🚔 Штрафы" not in flat
        assert "🚇 Туннели" not in flat
        assert "🛣 Платные дороги" not in flat
