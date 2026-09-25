"""
Тесты reader/turkey_bot/texts.py::format_statistics/format_debt_row/
format_debt_recency/format_debt_list_messages (см. задачу "доработать
📊 Статистика Turkey bot" — список username удалён целиком, старый
misleading блок "С задолженностью/Без задолженности/Частично/Ошибка"
(исторические turkey_check_runs, не текущее состояние, см. задачу п.10)
заменён на "🚨 Задолженность по последней проверке")."""

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot import texts
from reader.turkey_bot.statistics_service import TurkeyStatistics

_TELEGRAM_LIMIT = 4096
_TZ = ZoneInfo("UTC")
_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


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
    defaults = {"debt_car_count": 0, "debt_total_amount": Decimal(0), "debt_has_partial": False}
    defaults.update(debt_overrides)
    return texts.format_statistics(stats, **defaults)


def test_format_statistics_includes_all_core_numbers():
    """См. задачу "Перенос Unified Turkey функционала в production" —
    ПЕРЕНЕСЕНО из reader/turkey_bot_test/texts.py::format_statistics
    (unified-архитектура: manual/scheduled/active_monitoring_subscriptions/
    provider_error_counts — НОВЫЕ поля относительно старой GIB-only
    версии)."""
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
    """ОБРАТНОЕ прежнему test_format_statistics_does_not_mention_georgian_
    only_concepts — эта задача ЯВНО требует monitoring ON/OFF/scheduler в
    production (см. задачу п.1: "monitoring ON/OFF", "scheduler"),
    поэтому "мониторинг"/"подписок" ТЕПЕРЬ ожидаемо присутствуют."""
    text = _format(_stats()).lower()

    assert "мониторинг" in text
    assert "подписок" in text


def test_format_statistics_does_not_mention_username_list():
    """См. задачу п.1: "Полностью убрать этот список пользователей" —
    список @username больше НЕ формируется вообще, format_statistics —
    единственная точка сборки текста, ей физически неоткуда взять
    отдельные логины (format_user_list_messages удалена целиком)."""
    assert not hasattr(texts, "format_user_list_messages")


def test_format_statistics_shows_debt_block_right_after_users():
    """См. задачу п.2: "Сразу после блока 👥 Пользователи" + честная
    замена старого misleading блока (см. задачу п.10)."""
    text = _format(_stats(), debt_car_count=3, debt_total_amount=Decimal(4500))

    users_pos = text.index("👥 Пользователи")
    debt_pos = text.index("🚨 Задолженность по последней проверке")
    checks_pos = text.index("🔎 Проверки")
    assert users_pos < debt_pos < checks_pos
    assert "Автомобилей: 3" in text
    assert "Общая сумма: 4 500 ₺" in text
    assert "С задолженностью:" not in text  # старый блок полностью убран
    assert "Без задолженности:" not in text


def test_format_statistics_marks_total_as_partial_when_any_row_is_partial():
    text = _format(_stats(), debt_car_count=1, debt_total_amount=Decimal(1250), debt_has_partial=True)

    assert "Общая сумма: ≥ 1 250 ₺" in text


# ---- format_debt_recency ----


def test_debt_recency_today():
    assert texts.format_debt_recency(_NOW, now=_NOW, tz=_TZ) == "сегодня"


def test_debt_recency_yesterday():
    from datetime import timedelta

    assert texts.format_debt_recency(_NOW - timedelta(days=1), now=_NOW, tz=_TZ) == "вчера"


def test_debt_recency_plural_forms():
    from datetime import timedelta

    assert texts.format_debt_recency(_NOW - timedelta(days=2), now=_NOW, tz=_TZ) == "2 дня назад"
    assert texts.format_debt_recency(_NOW - timedelta(days=5), now=_NOW, tz=_TZ) == "5 дней назад"
    assert texts.format_debt_recency(_NOW - timedelta(days=18), now=_NOW, tz=_TZ) == "18 дней назад"
    assert texts.format_debt_recency(_NOW - timedelta(days=21), now=_NOW, tz=_TZ) == "21 день назад"
    assert texts.format_debt_recency(_NOW - timedelta(days=100), now=_NOW, tz=_TZ) == "100 дней назад"


# ---- format_debt_row ----


def test_debt_row_shows_car_owner_amount_and_recency():
    line = texts.format_debt_row(
        car_number="M295YB196", owner_display="@Mihailov_vm", total_amount=Decimal(2740),
        is_partial=False, recency="сегодня",
    )

    assert line == "🚗 M295YB196 — @Mihailov_vm — 2 740 ₺ · сегодня"


def test_debt_row_marks_partial_amount_and_label():
    line = texts.format_debt_row(
        car_number="34ABC123", owner_display="@username", total_amount=Decimal(1250),
        is_partial=True, recency="2 дня назад",
    )

    assert line == "🚗 34ABC123 — @username — ≥ 1 250 ₺ · частично · 2 дня назад"


# ---- format_debt_list_messages ----


def test_debt_list_empty_returns_no_messages():
    assert texts.format_debt_list_messages([]) == []


def test_debt_list_header_and_order_preserved():
    messages = texts.format_debt_list_messages(["🚗 A — @a — 100 ₺ · сегодня", "🚗 B — @b — 50 ₺ · вчера"])

    assert len(messages) == 1
    assert messages[0].startswith("Автомобили с задолженностью:")
    assert messages[0].index("🚗 A") < messages[0].index("🚗 B")


def test_long_debt_list_splits_safely_and_keeps_every_row():
    rows = [f"🚗 CAR{i:04d} — @user{i} — {i} ₺ · сегодня" for i in range(500)]

    messages = texts.format_debt_list_messages(rows)

    assert len(messages) > 1
    for message in messages:
        assert len(message) <= _TELEGRAM_LIMIT

    full_text = "\n".join(messages)
    for i in range(500):
        assert f"CAR{i:04d}" in full_text  # никто не потерян/не обрезан
