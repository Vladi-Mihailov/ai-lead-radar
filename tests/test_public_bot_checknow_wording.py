"""Тесты задачи "Make two small user-facing UX/text changes" —

1. 🔎 Проверить сейчас -> 🔎 Проверить штрафы (label only, callback_data/
   handlers unchanged) + vehicle-detail explanation made explicit that the
   check covers ALL currently-outstanding fines (including earlier-issued
   ones), NOT a full paid/closed-fine history.
2. HELP_TEXT's ОСАГО bullet now points to the @tplgee group.

Business logic, callback_data, DB schema, notification semantics, REDNET
routing, dedupe, and scheduling are untouched by this task -- not
re-verified here beyond the callback_data-unchanged assertions below (the
hourglass/duplicate-click-guard regression suites already cover those and
are run alongside this file, not duplicated here)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.public_bot.keyboards import (  # noqa: E402
    CHECK_NOW_LABEL,
    car_detail_keyboard,
    decode_check_now_callback,
    decode_my_car_turn_off_callback,
    decode_my_car_turn_on_callback,
    encode_my_car_turn_off_callback,
    encode_my_car_turn_on_callback,
)
from reader.public_bot.texts import (  # noqa: E402
    HELP_TEXT,
    TURN_OFF_BUTTON_LABEL,
    TURN_ON_BUTTON_LABEL,
    format_car_details,
)
from test_public_bot_notifications_wording import _subscription, _labels  # noqa: E402

from datetime import date  # noqa: E402

_TODAY = date(2026, 10, 26)


# ---- 1. Check Now label ----

def test_check_now_label_is_proverit_shtrafy():
    assert CHECK_NOW_LABEL == "🔎 Проверить штрафы"


def test_check_now_button_shown_with_new_label():
    for state in ("ON", "OFF", "ИСТЁК"):
        assert CHECK_NOW_LABEL in _labels(car_detail_keyboard(1, monitoring_state=state, page=0))


def test_check_now_callback_data_unchanged_by_label_rename():
    """Label-only change -- the encoded subscription_id must round-trip
    exactly as before."""
    rows = car_detail_keyboard(42, monitoring_state="ON", page=0)
    check_now_button = next(row_button for row in rows for row_button in row if row_button.text == CHECK_NOW_LABEL)
    assert decode_check_now_callback(check_now_button.data) == 42


def test_turn_on_off_callback_data_still_unchanged():
    """Regression guard: this task must not have touched the toggle
    callbacks either."""
    assert decode_my_car_turn_on_callback(encode_my_car_turn_on_callback(5, 1)) == (5, 1)
    assert decode_my_car_turn_off_callback(encode_my_car_turn_off_callback(5, 1)) == (5, 1)


# ---- vehicle detail explanation: "all current unpaid fines, including earlier-issued ones" ----

_EXACT_EXPLANATION = (
    "🔎 Проверить штрафы — покажем все неоплаченные штрафы, которые сейчас числятся "
    "по автомобилю, в том числе выписанные ранее."
)


def test_car_details_contains_exact_new_explanation_on():
    text = format_car_details(_subscription(status="active", end_date=date(2026, 12, 1)), _TODAY)
    assert _EXACT_EXPLANATION in text


def test_car_details_contains_exact_new_explanation_off():
    text = format_car_details(_subscription(status="stopped", end_date=date(2026, 12, 1)), _TODAY)
    assert _EXACT_EXPLANATION in text


def test_car_details_contains_exact_new_explanation_expired():
    text = format_car_details(_subscription(status="active", end_date=date(2026, 1, 1)), _TODAY)
    assert _EXACT_EXPLANATION in text


def test_car_details_never_implies_full_paid_fine_history():
    for status, end_date in (("active", date(2026, 12, 1)), ("stopped", date(2026, 12, 1)), ("active", date(2026, 1, 1))):
        text = format_car_details(_subscription(status=status, end_date=end_date), _TODAY).lower()
        for banned in ("вся история штрафов", "все штрафы за всё время", "старые штрафы"):
            assert banned not in text


# ---- notification labels unchanged (regression from the previous task) ----

def test_notification_labels_unchanged():
    assert TURN_ON_BUTTON_LABEL == "🔔 Включить уведомления"
    assert TURN_OFF_BUTTON_LABEL == "🔕 Отключить уведомления"


# ---- 2. Help OSAGO line ----

def test_help_contains_exact_osago_tplgee_line():
    assert "• 🛡 ОСАГО Грузия — оформить страховку для поездки по Грузии в группе @tplgee." in HELP_TEXT


def test_help_osago_line_uses_tplgee_not_osago_g():
    assert "@osago_g" not in HELP_TEXT
    assert "@tplgee" in HELP_TEXT


def test_help_check_now_bullet_no_longer_says_proverit_seychas():
    assert "Проверить сейчас" not in HELP_TEXT
    assert "🔎 Проверить штрафы" in HELP_TEXT
