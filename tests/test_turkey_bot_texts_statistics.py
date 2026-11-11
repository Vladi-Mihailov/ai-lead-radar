"""
Тесты reader/turkey_bot/texts.py::format_statistics/format_debt_row/
format_debt_list_messages (см. задачу "manager Statistics / refresh для
обоих ботов") — единый компактный формат "🚗 CAR: OWNER: AMOUNT", БЕЗ
даты/checked_at/"сегодня"/"вчера"/"≥"-пометки PARTIAL."""

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.plate_display import format_plate_for_telegram
from reader.turkey_bot import texts
from reader.turkey_bot.debt_refresh_service import TurkeyRefreshOutcome
from reader.turkey_bot.statistics_service import (
    TurkeyProviderDebtState,
    TurkeyProviderFreshness,
    TurkeyStatistics,
)

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
        car_number="M295YB196", owner_displays=["@Mihailov_vm"], total_amount=Decimal(2740),
    )

    assert line == f"🚗 {format_plate_for_telegram('M295YB196')}: @Mihailov_vm: 2 740 ₺"


def test_debt_row_has_no_partial_marker_or_recency():
    line = texts.format_debt_row(
        car_number="34ABC123", owner_displays=["@username"], total_amount=Decimal(1250),
    )

    assert line == f"🚗 {format_plate_for_telegram('34ABC123')}: @username: 1 250 ₺"
    assert "≥" not in line
    assert "частично" not in line
    assert " — " not in line
    assert "·" not in line


def test_debt_row_multiple_owners_joined_with_pipe_and_amount_once():
    """См. задачу "Turkey manager debt statistics aggregation" — один
    physical plate у нескольких владельцев печатается ОДНОЙ строкой,
    все owners через " | ", сумма — РОВНО один раз в конце (не
    дублируется на каждого owner)."""
    line = texts.format_debt_row(
        car_number="H401HH95",
        owner_displays=["V K (@vdhl29)", "Алена (@alenaogir)", "Иброхим (@ibrohim_abu_romadan)"],
        total_amount=Decimal("32664.50"),
    )

    assert line == (
        f"🚗 {format_plate_for_telegram('H401HH95')}: "
        "V K (@vdhl29) | Алена (@alenaogir) | Иброхим (@ibrohim_abu_romadan): 32 664,50 ₺"
    )
    assert line.count("32 664,50 ₺") == 1


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


# ---- подтверждённая / последняя известная / нет данных (см. задачу
# "Turkey debt list: последний успешный результат отдельно по каждому
# провайдеру и госномеру") ----

_NOV_04 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
_NOV_11 = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def _state(provider, freshness, amount=Decimal(0), *, last_success_at=None, last_attempt_at=None, error_type=None):
    return TurkeyProviderDebtState(
        provider=provider, freshness=freshness, amount=amount,
        last_success_at=last_success_at, last_attempt_at=last_attempt_at, last_attempt_error_type=error_type,
    )


def test_debt_summary_block_unchanged_when_nothing_is_last_known():
    expected = "🚨 Задолженность по последней проверке\nАвтомобилей: 2\nОбщая сумма: 1 500 ₺"

    assert texts.format_debt_summary_block(debt_car_count=2, debt_total_amount=Decimal(1500)) == expected
    assert texts.format_debt_summary_block(
        debt_car_count=2, debt_total_amount=Decimal(1500), confirmed_amount=Decimal(1500),
        last_known_amount=Decimal(0), no_data_car_count=0,
    ) == expected


def test_debt_summary_block_separates_confirmed_and_last_known():
    text = texts.format_debt_summary_block(
        debt_car_count=17, debt_total_amount=Decimal("120601.50"),
        confirmed_amount=Decimal(43000), last_known_amount=Decimal("77601.50"),
    )

    assert text.splitlines() == [
        "🚨 Задолженность по последней проверке",
        "Автомобилей: 17",
        "Общая сумма: 120 601,50 ₺",
        "✅ Подтверждено последними проверками: 43 000 ₺",
        "⚠️ Последняя известная (провайдер не ответил): 77 601,50 ₺",
    ]


def test_debt_summary_block_derives_confirmed_amount_when_not_given():
    text = texts.format_debt_summary_block(
        debt_car_count=1, debt_total_amount=Decimal(1400), last_known_amount=Decimal(500),
    )

    assert "✅ Подтверждено последними проверками: 900 ₺" in text


def test_debt_summary_block_shows_cars_without_reliable_data():
    text = texts.format_debt_summary_block(debt_car_count=0, debt_total_amount=Decimal(0), no_data_car_count=3)

    assert text.splitlines()[-1] == "❔ Нет достоверных данных GİB/KGM: 3 авто"
    assert "Подтверждено" not in text


def test_debt_row_with_all_providers_current_stays_one_line():
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(2740), last_success_at=_NOV_11, last_attempt_at=_NOV_11),
        _state("kgm", TurkeyProviderFreshness.CURRENT, last_success_at=_NOV_11, last_attempt_at=_NOV_11),
    )

    line = texts.format_debt_row(
        car_number="M295YB196", owner_displays=["@Mihailov_vm"], total_amount=Decimal(2740), provider_states=states,
    )

    assert line == f"🚗 {format_plate_for_telegram('M295YB196')}: @Mihailov_vm: 2 740 ₺"


def test_debt_row_stale_kgm_reads_as_last_known_with_dates():
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(13336), last_success_at=_NOV_11, last_attempt_at=_NOV_11),
        _state(
            "kgm", TurkeyProviderFreshness.STALE, Decimal("20192.50"),
            last_success_at=_NOV_04, last_attempt_at=_NOV_11, error_type="unexpected",
        ),
    )

    text = texts.format_debt_row(
        car_number="34ABC123", owner_displays=["@user"], total_amount=Decimal("33528.50"), provider_states=states,
    )

    assert text.splitlines() == [
        f"🚗 {format_plate_for_telegram('34ABC123')}: @user: ⚠️ 33 528,50 ₺",
        "   ✅ GİB: 13 336 ₺ — проверка 11.11.2026",
        "   ⚠️ KGM: 20 192,50 ₺ — последняя известная (успешная проверка 04.11.2026), не ответил 11.11.2026",
    ]


def test_debt_row_provider_without_reliable_data():
    attempted = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(18000), last_success_at=_NOV_11, last_attempt_at=_NOV_11),
        _state("kgm", TurkeyProviderFreshness.NO_DATA, last_attempt_at=_NOV_11, error_type="transport_error"),
    )
    never_checked = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(18000), last_success_at=_NOV_11, last_attempt_at=_NOV_11),
        _state("kgm", TurkeyProviderFreshness.NO_DATA),
    )

    attempted_text = texts.format_debt_row(
        car_number="34ABC123", owner_displays=["—"], total_amount=Decimal(18000), provider_states=attempted,
    )
    never_text = texts.format_debt_row(
        car_number="34ABC123", owner_displays=["—"], total_amount=Decimal(18000), provider_states=never_checked,
    )

    assert attempted_text.splitlines()[0].endswith(": ⚠️ 18 000 ₺")
    assert attempted_text.splitlines()[-1] == "   ❔ KGM: нет достоверных данных (не ответил 11.11.2026)"
    assert never_text.splitlines()[-1] == "   ❔ KGM: нет достоверных данных (не проверялся)"


def test_debt_row_zero_amount_provider_is_shown_as_no_debt():
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, last_success_at=_NOV_11, last_attempt_at=_NOV_11),
        _state("kgm", TurkeyProviderFreshness.STALE, Decimal(3788), last_success_at=_NOV_04, last_attempt_at=_NOV_11),
    )

    text = texts.format_debt_row(
        car_number="34ABC123", owner_displays=["—"], total_amount=Decimal(3788), provider_states=states,
    )

    assert "   ✅ GİB: без долга — проверка 11.11.2026" in text.splitlines()


def test_debt_row_dates_use_istanbul_calendar_day():
    late_evening_utc = datetime(2026, 11, 4, 22, 30, tzinfo=timezone.utc)  # 05.11 01:30 в Стамбуле
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, last_success_at=_NOV_11, last_attempt_at=_NOV_11),
        _state("kgm", TurkeyProviderFreshness.STALE, Decimal(59), last_success_at=late_evening_utc, last_attempt_at=_NOV_11),
    )

    text = texts.format_debt_row(car_number="34ABC123", owner_displays=["—"], total_amount=Decimal(59), provider_states=states)

    assert "(успешная проверка 05.11.2026)" in text


def test_refresh_summary_shows_which_providers_did_not_answer():
    outcome = TurkeyRefreshOutcome(
        checked=2, failed=2, total_plates=2, partial_plates=2,
        partial_car_numbers=("34ABC123", "06XYZ789"), partial_failed_providers=(("kgm",), ("gib", "kgm")),
    )

    summary = texts.format_debt_refresh_summary(outcome)

    assert (
        f"Частично: {format_plate_for_telegram('34ABC123')} (KGM), {format_plate_for_telegram('06XYZ789')} (GİB, KGM)"
        in summary
    )


def test_refresh_summary_without_failed_providers_lists_plates_only():
    outcome = TurkeyRefreshOutcome(
        checked=1, failed=1, total_plates=1, partial_plates=1, partial_car_numbers=("34ABC123",),
    )

    summary = texts.format_debt_refresh_summary(outcome)

    assert f"Частично: {format_plate_for_telegram('34ABC123')}" in summary.splitlines()


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
