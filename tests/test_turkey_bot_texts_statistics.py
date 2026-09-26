"""
Тесты reader/turkey_bot/texts.py::format_statistics/format_debt_row/
format_debt_list_messages (см. задачу "manager Statistics / refresh для
обоих ботов") — единый компактный формат "🚗 CAR: OWNER: AMOUNT", БЕЗ
даты/checked_at/"сегодня"/"вчера"/"≥"-пометки PARTIAL."""

import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot import texts
from reader.turkey_bot.statistics_service import TurkeyStatistics

_TELEGRAM_LIMIT = 4096


def _stats(**overrides) -> TurkeyStatistics:
    defaults = {
        "total_users": 16, "new_users_today": 2, "new_users_7d": 8, "new_users_30d": 16,
        "total_checks": 40, "checks_today": 3, "checks_7d": 12, "checks_30d": 40,
        "manual_checks": 25, "scheduled_checks": 15, "active_monitoring_subscriptions": 9,
        "provider_error_counts": {"gib": 1, "avrasya": 0, "kgm": 0},
    }
    defaults.update(overrides)
    return TurkeyStatistics(**defaults)


def _format(stats, **debt_overrides) -> str:
    defaults = {"debt_car_count": 0, "debt_total_amount": Decimal(0)}
    defaults.update(debt_overrides)
    return texts.format_statistics(stats, **defaults)


def test_format_statistics_includes_all_core_numbers():
    text = _format(_stats())

    assert "16" in text  # total_users
    assert "Новых сегодня: 2" in text
    assert "Новых за 7 дней: 8" in text
    assert "Новых за 30 дней: 16" in text
    assert "Всего: 40" in text
    assert "Сегодня: 3" in text
    assert "За 7 дней: 12" in text
    assert "За 30 дней: 40" in text
    assert "Ручных: 25" in text
    assert "По расписанию: 15" in text
    assert "Активных подписок мониторинга: 9" in text


def test_format_statistics_shows_unified_monitoring_metrics():
    text = _format(_stats()).lower()

    assert "мониторинг" in text
    assert "подписок" in text


def test_format_statistics_does_not_mention_username_list():
    assert not hasattr(texts, "format_user_list_messages")


def test_format_statistics_shows_debt_block_right_after_users():
    text = _format(_stats(), debt_car_count=3, debt_total_amount=Decimal(4500))

    users_pos = text.index("👥 Пользователи")
    debt_pos = text.index("🚨 Задолженность по последней проверке")
    checks_pos = text.index("🔎 Проверки")
    assert users_pos < debt_pos < checks_pos
    assert "Автомобилей: 3" in text
    assert "Общая сумма: 4 500 ₺" in text
    assert "С задолженностью:" not in text  # старый блок полностью убран
    assert "Без задолженности:" not in text


def test_format_statistics_never_uses_partial_marker():
    """См. задачу "manager Statistics / refresh для обоих ботов" п.1 —
    "≥"-пометка PARTIAL больше НЕ показывается (PARTIAL по-прежнему
    учитывается персистентно, см. TurkeyDebtRow.is_partial, просто больше
    не выделяется визуально)."""
    text = _format(_stats(), debt_car_count=1, debt_total_amount=Decimal(1250))

    assert "Общая сумма: 1 250 ₺" in text
    assert "≥" not in text


# ---- format_debt_row: единый компактный формат CAR: OWNER: AMOUNT ----


def test_debt_row_is_car_colon_owner_colon_amount():
    line = texts.format_debt_row(
        car_number="M295YB196", owner_display="@Mihailov_vm", total_amount=Decimal(2740),
    )

    assert line == "🚗 M295YB196: @Mihailov_vm: 2 740 ₺"


def test_debt_row_has_no_partial_marker_or_recency():
    line = texts.format_debt_row(
        car_number="34ABC123", owner_display="@username", total_amount=Decimal(1250),
    )

    assert line == "🚗 34ABC123: @username: 1 250 ₺"
    assert "≥" not in line
    assert "частично" not in line
    assert " — " not in line
    assert "·" not in line


# ---- format_debt_list_messages ----


def test_debt_list_empty_returns_no_messages():
    assert texts.format_debt_list_messages([]) == []


def test_debt_list_has_no_separate_header_and_preserves_order():
    """См. задачу п.7 — пример показывает строки СРАЗУ после аггрегата,
    без отдельного "Автомобили с задолженностью:"."""
    messages = texts.format_debt_list_messages(["🚗 A: @a: 100 ₺", "🚗 B: @b: 50 ₺"])

    assert len(messages) == 1
    assert not messages[0].startswith("Автомобили")
    assert messages[0] == "🚗 A: @a: 100 ₺\n🚗 B: @b: 50 ₺"
    assert messages[0].index("🚗 A") < messages[0].index("🚗 B")


def test_long_debt_list_splits_safely_and_keeps_every_row():
    rows = [f"🚗 CAR{i:04d}: @user{i}: {i} ₺" for i in range(500)]

    messages = texts.format_debt_list_messages(rows)

    assert len(messages) > 1
    for message in messages:
        assert len(message) <= _TELEGRAM_LIMIT

    full_text = "\n".join(messages)
    for i in range(500):
        assert f"CAR{i:04d}" in full_text  # никто не потерян/не обрезан


# ---- owner display name sanitization (задача п.2) ----


def test_owner_display_sanitizes_dot_only_name():
    display = texts.format_search_owner_display(first_name=".", last_name=None, username="Roma12312")
    assert display == "@Roma12312"


def test_owner_display_sanitizes_dash_and_underscore_names():
    display = texts.format_search_owner_display(first_name="-", last_name="_", username="someone")
    assert display == "@someone"


def test_owner_display_keeps_real_name():
    display = texts.format_search_owner_display(first_name="Иван", last_name="Иванов", username="ivan")
    assert display == "Иван Иванов (@ivan)"


def test_owner_display_dash_when_nothing_meaningful():
    display = texts.format_search_owner_display(first_name=".", last_name="-", username=None)
    assert display == "—"
