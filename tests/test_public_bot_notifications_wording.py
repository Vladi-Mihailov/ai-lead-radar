"""Тесты задачи "remove the word мониторинг from user-facing UI" —
normal-user-facing per-car ON/OFF toggle (reader/public_bot/keyboards.py::
car_detail_keyboard, reader/public_bot/texts.py::format_car_details/
format_turn_on_success/format_turn_off_success/format_no_fines_found).

Trusted/admin-only screens (format_trusted_task_detail, format_stop_success,
format_delegated_add_car_summary, "▶️ Продолжить мониторинг" resume flow,
MANUAL_BOOTSTRAP_*, etc.) are explicitly out of scope for this task (see its
own "Do NOT blindly rename internal/admin/debug terminology" requirement)
and are deliberately NOT touched or asserted against here."""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.public_bot.keyboards import (  # noqa: E402
    CHECK_NOW_LABEL,
    car_detail_keyboard,
    decode_check_now_callback,
    decode_my_car_turn_off_callback,
    decode_my_car_turn_on_callback,
    encode_my_car_turn_off_callback,
    encode_my_car_turn_on_callback,
)
from reader.public_bot.models import FineMonitoringSubscription  # noqa: E402
from reader.public_bot.texts import (  # noqa: E402
    DELETE_CAR_BUTTON_LABEL,
    TURN_OFF_BUTTON_LABEL,
    TURN_ON_BUTTON_LABEL,
    format_car_details,
    format_no_fines_found,
    format_turn_off_success,
    format_turn_on_success,
)

_TODAY = date(2026, 10, 26)


def _subscription(*, status: str, end_date: date) -> FineMonitoringSubscription:
    return FineMonitoringSubscription(
        id=1, monitoring_task_id=1, car_number="O127PX62",
        telegram_user_id=42, telegram_chat_id=42, telegram_username=None,
        status=status, start_date=date(2026, 1, 1), end_date=end_date, source="self_service",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        stopped_at=None,
    )


def _labels(keyboard) -> list[str]:
    # car_detail_keyboard() builds raw Button.inline(...) rows -- unlike
    # main_menu_keyboard's Button.text(...) reply buttons (see
    # test_public_bot_keyboards.py's own _labels()), these are plain
    # types.KeyboardButtonCallback with .text/.data directly, no .button
    # wrapper level.
    return [row_button.text for row in keyboard for row_button in row]


# ---- 1/3. button labels -- no more "Добавить/Снять с мониторинга" ----

def test_turn_on_button_label_is_enable_notifications():
    assert TURN_ON_BUTTON_LABEL == "🔔 Включить уведомления"
    assert "мониторинг" not in TURN_ON_BUTTON_LABEL.lower()


def test_turn_off_button_label_is_disable_notifications():
    assert TURN_OFF_BUTTON_LABEL == "🔕 Отключить уведомления"
    assert "мониторинг" not in TURN_OFF_BUTTON_LABEL.lower()


def test_car_detail_keyboard_off_shows_enable_notifications_button():
    """monitoring OFF -> button says 🔔 Включить уведомления."""
    rows = car_detail_keyboard(1, monitoring_state="OFF", page=0)
    assert TURN_ON_BUTTON_LABEL in _labels(rows)
    assert TURN_OFF_BUTTON_LABEL not in _labels(rows)


def test_car_detail_keyboard_on_shows_disable_notifications_button():
    """monitoring ON -> button says 🔕 Отключить уведомления."""
    rows = car_detail_keyboard(1, monitoring_state="ON", page=0)
    assert TURN_OFF_BUTTON_LABEL in _labels(rows)
    assert TURN_ON_BUTTON_LABEL not in _labels(rows)


def test_car_detail_keyboard_expired_shows_neither_toggle_button():
    """ИСТЁК has no active ON/OFF toggle (reactivation needs a new period,
    see car_detail_keyboard's own docstring) -- unaffected by this task."""
    rows = car_detail_keyboard(1, monitoring_state="ИСТЁК", page=0)
    labels = _labels(rows)
    assert TURN_ON_BUTTON_LABEL not in labels
    assert TURN_OFF_BUTTON_LABEL not in labels
    assert DELETE_CAR_BUTTON_LABEL in labels


def test_car_detail_keyboard_always_offers_check_now():
    for state in ("ON", "OFF", "ИСТЁК"):
        assert CHECK_NOW_LABEL in _labels(car_detail_keyboard(1, monitoring_state=state, page=0))


# ---- Check Now / toggle callback_data unchanged (encode/decode round trip) ----

def test_check_now_callback_data_unchanged():
    rows = car_detail_keyboard(7, monitoring_state="ON", page=0)
    check_now_button = next(
        row_button for row in rows for row_button in row if row_button.text == CHECK_NOW_LABEL
    )
    assert decode_check_now_callback(check_now_button.data) == 7


def test_turn_on_off_callback_data_round_trips_unchanged():
    assert decode_my_car_turn_on_callback(encode_my_car_turn_on_callback(7, 2)) == (7, 2)
    assert decode_my_car_turn_off_callback(encode_my_car_turn_off_callback(7, 2)) == (7, 2)


# ---- 2. vehicle detail explanation ----

def test_car_details_on_distinguishes_check_now_from_future_notifications():
    text = format_car_details(_subscription(status="active", end_date=date(2026, 12, 1)), _TODAY)
    assert "мониторинг" not in text.lower()
    assert "все неоплаченные штрафы" in text  # 🔎 Проверить штрафы: current unpaid fines
    assert "выписанные ранее" in text  # no fake "paid fines archive" implied, see banned list below
    assert "появятся новые штрафы" in text  # 🔔 Уведомления: future new fines
    assert "включены" in text
    for banned in ("вся история штрафов", "все штрафы за всё время", "старые штрафы", "история штрафов", "штрафы за прошлый период"):
        assert banned not in text.lower()


def test_car_details_off_distinguishes_check_now_from_future_notifications():
    text = format_car_details(_subscription(status="stopped", end_date=date(2026, 12, 1)), _TODAY)
    assert "мониторинг" not in text.lower()
    assert "все неоплаченные штрафы" in text
    assert "выписанные ранее" in text
    assert "выключены" in text
    for banned in ("вся история штрафов", "все штрафы за всё время", "старые штрафы", "история штрафов", "штрафы за прошлый период"):
        assert banned not in text.lower()


def test_car_details_expired_has_no_on_off_claim():
    text = format_car_details(_subscription(status="active", end_date=date(2026, 1, 1)), _TODAY)
    assert "мониторинг" not in text.lower()
    assert "включены" not in text and "выключены" not in text
    assert "⏱" in text


def test_car_details_plate_is_dynamic():
    sub = _subscription(status="active", end_date=date(2026, 12, 1))
    text = format_car_details(sub, _TODAY)
    assert sub.car_number in text.replace("*", "")  # format_plate_for_telegram may bold-markup digits/letters


# ---- 6. notification enabled/disabled confirmation ----

def test_turn_on_success_text_mentions_future_notifications_not_monitoring():
    text = format_turn_on_success("O127PX62")
    assert "мониторинг" not in text.lower()
    assert "включены" in text
    assert "сообщим" in text  # "Мы сообщим, если появятся новые штрафы..."


def test_turn_off_success_text_does_not_imply_fines_are_deleted():
    text = format_turn_off_success("O127PX62")
    assert "мониторинг" not in text.lower()
    assert "отключены" in text
    assert "сохранён" in text  # car stays in "Мои авто", nothing deleted
    for banned in ("удал", "недоступ"):
        assert banned not in text.lower()


# ---- 5. zero-fines wording ----

def test_no_fines_found_wording():
    text = format_no_fines_found("O127PX62")
    assert "штрафов не найдено" in text
    assert "неоплаченных" in text
    assert "мониторинг" not in text.lower()
    for banned in ("старые штрафы", "история штрафов", "штрафы за прошлый период"):
        assert banned not in text.lower()
