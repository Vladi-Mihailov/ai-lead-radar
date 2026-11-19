"""
Тесты reader/turkey_bot/texts.py::format_statistics/format_debt_row/
format_debt_list_messages (см. задачи "manager Statistics / refresh для
обоих ботов" и "дополнить статистику штрафов информацией о
пользователях") — блок машины "🚗 Госномер / 🔴 Неоплаченные записи / 👤
Пользователь / 🔗 Telegram", БЕЗ даты/checked_at/"сегодня"/"вчера"/"≥"-
пометки PARTIAL."""

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

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


# ---- format_debt_row: блок госномер / задолженность / пользователь / Telegram ----


def test_debt_row_is_plate_debt_user_telegram_block():
    block = texts.format_debt_row(
        car_number="M295YB196", owners=[texts.DebtOwner(name="Иван Петров", username="ivan_petrov")],
        total_amount=Decimal(2740),
    )

    assert block == (
        f"🚗 Госномер: {format_plate_for_telegram('M295YB196')}\n"
        "🔴 Неоплаченные записи: 2 740 ₺\n"
        "👤 Пользователь: Иван Петров\n"
        "🔗 Telegram: @ivan_petrov"
    )


def test_debt_row_has_no_partial_marker_or_recency():
    block = texts.format_debt_row(
        car_number="34ABC123", owners=[texts.DebtOwner(name="Иван", username="username")], total_amount=Decimal(1250),
    )

    assert block.splitlines()[:2] == [
        f"🚗 Госномер: {format_plate_for_telegram('34ABC123')}",
        "🔴 Неоплаченные записи: 1 250 ₺",
    ]
    assert "≥" not in block
    assert "частично" not in block
    assert " — " not in block
    assert "·" not in block


def test_debt_row_lists_every_owner_and_amount_once():
    """См. задачу "Turkey manager debt statistics aggregation" — один
    physical plate у нескольких владельцев — ОДИН блок, пара 👤/🔗 на
    каждого владельца, сумма — РОВНО один раз (не дублируется на каждого
    owner)."""
    block = texts.format_debt_row(
        car_number="H401HH95",
        owners=[
            texts.DebtOwner(name="Иван Петров", username="ivan_petrov"),
            texts.DebtOwner(name="Анна", username=None),
            texts.DebtOwner(name=None, username="giorgi_b"),
        ],
        total_amount=Decimal("32664.50"),
    )

    assert block.splitlines() == [
        f"🚗 Госномер: {format_plate_for_telegram('H401HH95')}",
        "🔴 Неоплаченные записи: 32 664,50 ₺",
        "👤 Пользователь: Иван Петров",
        "🔗 Telegram: @ivan_petrov",
        "👤 Пользователь: Анна",
        "🔗 Telegram: Логин не указан",
        "👤 Пользователь: Имя не указано",
        "🔗 Telegram: @giorgi_b",
    ]
    assert block.count("32 664,50 ₺") == 1


def test_debt_row_owner_without_username_shows_login_not_specified():
    block = texts.format_debt_row(
        car_number="34ABC123", owners=[texts.DebtOwner(name="Мехмет", username=None)], total_amount=Decimal(500),
    )

    assert block.splitlines()[2:] == ["👤 Пользователь: Мехмет", "🔗 Telegram: Логин не указан"]


def test_debt_row_owner_without_name_shows_name_not_specified():
    block = texts.format_debt_row(
        car_number="34ABC123", owners=[texts.DebtOwner(name=None, username="mehmet_k")], total_amount=Decimal(500),
    )

    assert block.splitlines()[2:] == ["👤 Пользователь: Имя не указано", "🔗 Telegram: @mehmet_k"]


def test_debt_row_without_owners_shows_not_linked():
    block = texts.format_debt_row(car_number="34ABC123", owners=[], total_amount=Decimal(500))

    assert block.splitlines()[2:] == ["👤 Пользователь не привязан"]


def test_debt_owner_name_drops_placeholder_names_and_username_is_never_built_from_name():
    assert texts.debt_owner_name("Иван", "Петров") == "Иван Петров"
    assert texts.debt_owner_name(".", None) is None
    assert texts.debt_owner_name("-", "_") is None
    block = texts.format_debt_row(
        car_number="34ABC123", owners=[texts.DebtOwner(name=texts.debt_owner_name("Иван", "Петров"), username=None)],
        total_amount=Decimal(500),
    )
    assert "@Иван" not in block
    assert "🔗 Telegram: Логин не указан" in block


def test_debt_row_owner_text_cannot_inject_telegram_markdown():
    from telethon.extensions import markdown
    from telethon.tl.types import MessageEntityBold

    block = texts.format_debt_row(
        car_number="AB123CD",
        owners=[texts.DebtOwner(name="**Мехмет** `x` [Нажми](http://evil.example)", username="evil__user")],
        total_amount=Decimal(500),
    )

    plain_text, entities = markdown.parse(block)
    assert all(isinstance(entity, MessageEntityBold) for entity in entities)
    assert len(entities) == 2  # только буквы госномера: AB и CD
    visible = plain_text.replace("​", "")
    assert "👤 Пользователь: **Мехмет** ˋxˋ [Нажми](http://evil.example)" in visible
    assert "🔗 Telegram: Логин не указан" in visible  # "__" — не настоящий Telegram username


def test_debt_row_valid_username_is_inserted_unchanged():
    """Настоящий username вставляется как есть — без невидимых символов,
    чтобы упоминание оставалось кликабельным и копировалось точно."""
    block = texts.format_debt_row(
        car_number="34ABC123", owners=[texts.DebtOwner(name=None, username="@ivan_petrov_77")],
        total_amount=Decimal(500),
    )

    assert block.splitlines()[-1] == "🔗 Telegram: @ivan_petrov_77"


# ---- format_debt_list_messages ----


def test_debt_list_empty_returns_no_messages():
    assert texts.format_debt_list_messages([]) == []


def test_debt_list_has_no_separate_header_and_preserves_order():
    """См. задачу п.7 — пример показывает строки СРАЗУ после аггрегата,
    без отдельного "Автомобили с задолженностью:"."""
    rows = ["🚗 Госномер: A\n💰 Задолженность: 100 ₺", "🚗 Госномер: B\n💰 Задолженность: 50 ₺"]

    messages = texts.format_debt_list_messages(rows)

    assert len(messages) == 1
    assert not messages[0].startswith("Автомобили")
    # Многострочные блоки машин разделены пустой строкой.
    assert messages[0] == "🚗 Госномер: A\n💰 Задолженность: 100 ₺\n\n🚗 Госномер: B\n💰 Задолженность: 50 ₺"
    assert messages[0].index("🚗 Госномер: A") < messages[0].index("🚗 Госномер: B")


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


def _state(
    provider, freshness, amount=Decimal(0), *, last_success_at=None, last_attempt_at=None, error_type=None,
    debt_count=0,
):
    return TurkeyProviderDebtState(
        provider=provider, freshness=freshness, amount=amount,
        last_success_at=last_success_at, last_attempt_at=last_attempt_at, last_attempt_error_type=error_type,
        debt_count=debt_count,
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


_OWNER = texts.DebtOwner(name="Иван Петров", username="ivan_petrov")


def test_debt_row_with_all_providers_current_has_no_provider_lines():
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(2740), last_success_at=_NOV_11, last_attempt_at=_NOV_11),
        _state("kgm", TurkeyProviderFreshness.CURRENT, last_success_at=_NOV_11, last_attempt_at=_NOV_11),
    )

    block = texts.format_debt_row(
        car_number="M295YB196", owners=[_OWNER], total_amount=Decimal(2740), provider_states=states,
    )

    assert block.splitlines() == [
        f"🚗 Госномер: {format_plate_for_telegram('M295YB196')}",
        "🔴 Неоплаченные записи: 2 740 ₺",
        "👤 Пользователь: Иван Петров",
        "🔗 Telegram: @ivan_petrov",
    ]


def test_debt_row_stale_kgm_reads_as_last_known_with_dates():
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(13336), last_success_at=_NOV_11, last_attempt_at=_NOV_11),
        _state(
            "kgm", TurkeyProviderFreshness.STALE, Decimal("20192.50"),
            last_success_at=_NOV_04, last_attempt_at=_NOV_11, error_type="unexpected",
        ),
    )

    text = texts.format_debt_row(
        car_number="34ABC123", owners=[_OWNER], total_amount=Decimal("33528.50"), provider_states=states,
    )

    # Детализация по провайдерам — сразу под суммой, владельцы — после неё.
    assert text.splitlines() == [
        f"🚗 Госномер: {format_plate_for_telegram('34ABC123')}",
        "🔴 Неоплаченные записи: ⚠️ 33 528,50 ₺",
        "   ✅ GİB: 13 336 ₺ — проверка 11.11.2026",
        "   ⚠️ KGM: 20 192,50 ₺ — последняя известная (успешная проверка 04.11.2026), не ответил 11.11.2026",
        "👤 Пользователь: Иван Петров",
        "🔗 Telegram: @ivan_petrov",
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
        car_number="34ABC123", owners=[], total_amount=Decimal(18000), provider_states=attempted,
    )
    never_text = texts.format_debt_row(
        car_number="34ABC123", owners=[], total_amount=Decimal(18000), provider_states=never_checked,
    )

    assert attempted_text.splitlines()[1] == "🔴 Неоплаченные записи: ⚠️ 18 000 ₺"
    assert attempted_text.splitlines()[-2] == "   ❔ KGM: нет достоверных данных (не ответил 11.11.2026)"
    assert never_text.splitlines()[-2] == "   ❔ KGM: нет достоверных данных (не проверялся)"
    assert never_text.splitlines()[-1] == "👤 Пользователь не привязан"


def test_debt_row_zero_amount_provider_is_shown_as_no_debt():
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, last_success_at=_NOV_11, last_attempt_at=_NOV_11),
        _state("kgm", TurkeyProviderFreshness.STALE, Decimal(3788), last_success_at=_NOV_04, last_attempt_at=_NOV_11),
    )

    text = texts.format_debt_row(
        car_number="34ABC123", owners=[], total_amount=Decimal(3788), provider_states=states,
    )

    assert "   ✅ GİB: без долга — проверка 11.11.2026" in text.splitlines()


def test_debt_row_dates_use_istanbul_calendar_day():
    late_evening_utc = datetime(2026, 11, 4, 22, 30, tzinfo=timezone.utc)  # 05.11 01:30 в Стамбуле
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, last_success_at=_NOV_11, last_attempt_at=_NOV_11),
        _state("kgm", TurkeyProviderFreshness.STALE, Decimal(59), last_success_at=late_evening_utc, last_attempt_at=_NOV_11),
    )

    text = texts.format_debt_row(car_number="34ABC123", owners=[], total_amount=Decimal(59), provider_states=states)

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


def test_refresh_summary_shows_failed_provider_and_its_last_successful_check_date():
    """Ревью: при частичной проверке нужна и дата последней успешной
    проверки упавшего провайдера — номер мог после refresh выпасть из
    списка совсем, и тогда строка списка с датой не показывается вовсе."""
    outcome = TurkeyRefreshOutcome(
        checked=2, failed=2, total_plates=2, partial_plates=2,
        partial_car_numbers=("34ABC123", "06XYZ789"), partial_failed_providers=(("kgm",), ("gib", "kgm")),
        partial_last_success_at=(
            (datetime(2026, 11, 4, 18, 12, tzinfo=timezone.utc),),
            (None, datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)),
        ),
    )

    summary = texts.format_debt_refresh_summary(outcome)

    assert (
        f"Частично: {format_plate_for_telegram('34ABC123')} (KGM — последняя успешная проверка 04.11.2026), "
        f"{format_plate_for_telegram('06XYZ789')} "
        "(GİB — успешных проверок не было; KGM — последняя успешная проверка 06.11.2026)"
    ) in summary


def test_debt_list_packs_multi_line_rows_so_handler_never_resplits():
    """Ревью: строки стали многострочными, а reader/turkey_bot/handlers.py::
    _split_telegram_text заново режет всё длиннее своего безопасного предела
    по границам строк — строка провайдера не должна уехать в отдельное
    сообщение без номера машины."""
    from reader.turkey_bot.handlers import _TELEGRAM_SAFE_TEXT_LIMIT, _split_telegram_text

    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(13336),
               last_success_at=datetime(2026, 11, 11, 15, 55, tzinfo=timezone.utc),
               last_attempt_at=datetime(2026, 11, 11, 15, 55, tzinfo=timezone.utc)),
        _state("kgm", TurkeyProviderFreshness.STALE, Decimal("20192.50"),
               last_success_at=datetime(2026, 11, 4, 15, 43, tzinfo=timezone.utc),
               last_attempt_at=datetime(2026, 11, 11, 15, 55, tzinfo=timezone.utc), error_type="transport_error"),
    )
    rows = [
        texts.format_debt_row(
            car_number=f"34ABC{i:03d}",
            owners=[texts.DebtOwner(name=f"Владелец{'я' * (i % 7)}", username=f"user{i}")] * (1 + i % 3),
            total_amount=Decimal("33528.50"), provider_states=states,
        )
        for i in range(60)
    ]

    messages = texts.format_debt_list_messages(rows)

    assert len(messages) > 1
    assert texts._DEBT_LIST_MESSAGE_LIMIT <= _TELEGRAM_SAFE_TEXT_LIMIT
    for message in messages:
        assert _split_telegram_text(message) == [message]
        assert message.startswith("🚗 Госномер:")
    assert "\n\n".join(messages) == "\n\n".join(rows)


def test_refresh_summary_without_failed_providers_lists_plates_only():
    outcome = TurkeyRefreshOutcome(
        checked=1, failed=1, total_plates=1, partial_plates=1, partial_car_numbers=("34ABC123",),
    )

    summary = texts.format_debt_refresh_summary(outcome)

    assert f"Частично: {format_plate_for_telegram('34ABC123')}" in summary.splitlines()


# ---- число неоплаченных штрафов в строке машины (задача "показывать по
# каждой машине количество и сумму отдельно"): сумма debt_count тех же
# состояний GİB и KGM, из которых сложена сумма; строки "оплачено" нет ----


def test_debt_row_shows_count_of_gib_and_kgm_records_behind_the_amount():
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(80), last_success_at=_NOV_11, last_attempt_at=_NOV_11,
               debt_count=2),
        _state("kgm", TurkeyProviderFreshness.CURRENT, Decimal(2660), last_success_at=_NOV_11,
               last_attempt_at=_NOV_11, debt_count=3),
    )

    block = texts.format_debt_row(
        car_number="34ABC123", owners=[_OWNER], total_amount=Decimal(2740), provider_states=states,
    )

    assert block.splitlines() == [
        f"🚗 Госномер: {format_plate_for_telegram('34ABC123')}",
        "🔴 Неоплаченные записи: 2 740 ₺ — 2 штрафа GİB, 3 неоплаченных проезда KGM",
        "👤 Пользователь: Иван Петров",
        "🔗 Telegram: @ivan_petrov",
    ]


def test_debt_row_count_keeps_the_warning_mark_and_provider_lines():
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(13336), last_success_at=_NOV_11,
               last_attempt_at=_NOV_11, debt_count=1),
        _state(
            "kgm", TurkeyProviderFreshness.STALE, Decimal("20192.50"),
            last_success_at=_NOV_04, last_attempt_at=_NOV_11, error_type="unexpected", debt_count=3,
        ),
    )

    text = texts.format_debt_row(
        car_number="34ABC123", owners=[], total_amount=Decimal("33528.50"), provider_states=states,
    )

    assert text.splitlines() == [
        f"🚗 Госномер: {format_plate_for_telegram('34ABC123')}",
        "🔴 Неоплаченные записи: ⚠️ 33 528,50 ₺ — 1 штраф GİB, 3 неоплаченных проезда KGM",
        "   ✅ GİB: 13 336 ₺ — проверка 11.11.2026",
        "   ⚠️ KGM: 20 192,50 ₺ — последняя известная (успешная проверка 04.11.2026), не ответил 11.11.2026",
        "👤 Пользователь не привязан",
    ]


def test_debt_row_count_skips_a_provider_without_reliable_data():
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(18000), last_success_at=_NOV_11,
               last_attempt_at=_NOV_11, debt_count=1),
        _state("kgm", TurkeyProviderFreshness.NO_DATA, last_attempt_at=_NOV_11, error_type="transport_error"),
    )

    text = texts.format_debt_row(car_number="34ABC123", owners=[], total_amount=Decimal(18000), provider_states=states)

    assert text.splitlines()[1] == "🔴 Неоплаченные записи: ⚠️ 18 000 ₺ — 1 штраф GİB"


def test_debt_row_without_a_known_count_shows_the_amount_only():
    """Состояния без числа (или без состояний вовсе) — только сумма: число
    не выдумывается."""
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(500), last_success_at=_NOV_11, last_attempt_at=_NOV_11),
        _state("kgm", TurkeyProviderFreshness.CURRENT, last_success_at=_NOV_11, last_attempt_at=_NOV_11),
    )

    with_states = texts.format_debt_row(
        car_number="34ABC123", owners=[], total_amount=Decimal(500), provider_states=states,
    )
    without_states = texts.format_debt_row(car_number="34ABC123", owners=[], total_amount=Decimal(500))

    assert with_states.splitlines()[1] == "🔴 Неоплаченные записи: 500 ₺"
    assert without_states.splitlines()[1] == "🔴 Неоплаченные записи: 500 ₺"


def test_debt_row_shows_no_count_when_a_provider_with_debt_has_none():
    """Сумма KGM без числа записей — число неполное, поэтому только сумма
    (а не «1 штраф» за всю сумму GİB + KGM)."""
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(100), last_success_at=_NOV_11,
               last_attempt_at=_NOV_11, debt_count=1),
        _state("kgm", TurkeyProviderFreshness.CURRENT, Decimal(50), last_success_at=_NOV_11, last_attempt_at=_NOV_11),
    )

    text = texts.format_debt_row(car_number="34ABC123", owners=[], total_amount=Decimal(150), provider_states=states)

    assert text.splitlines()[1] == "🔴 Неоплаченные записи: 150 ₺"


@pytest.mark.parametrize("count, gib_text, kgm_text", [
    (1, "1 штраф", "1 неоплаченный проезд"), (2, "2 штрафа", "2 неоплаченных проезда"),
    (4, "4 штрафа", "4 неоплаченных проезда"), (5, "5 штрафов", "5 неоплаченных проездов"),
    (11, "11 штрафов", "11 неоплаченных проездов"), (12, "12 штрафов", "12 неоплаченных проездов"),
    (14, "14 штрафов", "14 неоплаченных проездов"), (21, "21 штраф", "21 неоплаченный проезд"),
    (22, "22 штрафа", "22 неоплаченных проезда"), (111, "111 штрафов", "111 неоплаченных проездов"),
    (112, "112 штрафов", "112 неоплаченных проездов"), (122, "122 штрафа", "122 неоплаченных проезда"),
])
def test_debt_row_count_uses_the_russian_plural(count, gib_text, kgm_text):
    """GİB — штрафы, KGM — неоплаченные проезды: у каждого провайдера своё
    слово с нужной формой."""
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(100), last_success_at=_NOV_11,
               last_attempt_at=_NOV_11, debt_count=count),
        _state("kgm", TurkeyProviderFreshness.CURRENT, Decimal(200), last_success_at=_NOV_11,
               last_attempt_at=_NOV_11, debt_count=count),
    )

    text = texts.format_debt_row(car_number="34ABC123", owners=[], total_amount=Decimal(300), provider_states=states)

    assert text.splitlines()[1] == f"🔴 Неоплаченные записи: 300 ₺ — {gib_text} GİB, {kgm_text} KGM"


def test_debt_row_does_not_count_records_of_a_provider_whose_amount_is_not_in_the_sum():
    """Ревью: штрафы GİB без разобранной суммы (amount 0) за суммой строки не
    стоят — считаются только проезды KGM, чья сумма в ней."""
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(0), last_success_at=_NOV_11,
               last_attempt_at=_NOV_11, debt_count=2),
        _state("kgm", TurkeyProviderFreshness.CURRENT, Decimal(615), last_success_at=_NOV_11,
               last_attempt_at=_NOV_11, debt_count=3),
    )

    line = texts.format_debt_row(
        car_number="34ABC123", owners=[], total_amount=Decimal(615), provider_states=states,
    ).splitlines()[1]

    assert line == "🔴 Неоплаченные записи: 615 ₺ — 3 неоплаченных проезда KGM"


def test_debt_row_kgm_only_never_calls_passages_fines():
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, last_success_at=_NOV_11, last_attempt_at=_NOV_11),
        _state("kgm", TurkeyProviderFreshness.CURRENT, Decimal(60), last_success_at=_NOV_11,
               last_attempt_at=_NOV_11, debt_count=1),
    )

    line = texts.format_debt_row(
        car_number="34ABC123", owners=[], total_amount=Decimal(60), provider_states=states,
    ).splitlines()[1]

    assert line == "🔴 Неоплаченные записи: 60 ₺ — 1 неоплаченный проезд KGM"
    assert "штраф" not in line


@pytest.mark.parametrize("count, expected", [(1, "1 запись"), (2, "2 записи"), (5, "5 записей")])
def test_debt_row_unknown_provider_records_are_called_records(count, expected):
    """Провайдер без своего слова (сейчас таких нет) — нейтральное «записи»."""
    states = (
        _state("other", TurkeyProviderFreshness.CURRENT, Decimal(30), last_success_at=_NOV_11,
               last_attempt_at=_NOV_11, debt_count=count),
    )

    line = texts.format_debt_row(
        car_number="34ABC123", owners=[], total_amount=Decimal(30), provider_states=states,
    ).splitlines()[1]

    assert line == f"🔴 Неоплаченные записи: 30 ₺ — {expected} other"


@pytest.mark.parametrize("kgm_freshness", list(TurkeyProviderFreshness))
def test_debt_row_never_has_a_paid_line(kgm_freshness):
    """Turkey: источники не сообщают об оплате — строки 🟢/"оплачено" нет ни
    при каких состояниях провайдеров."""
    states = (
        _state("gib", TurkeyProviderFreshness.CURRENT, Decimal(100), last_success_at=_NOV_11,
               last_attempt_at=_NOV_11, debt_count=1),
        _state("kgm", kgm_freshness, Decimal(50) if kgm_freshness != TurkeyProviderFreshness.NO_DATA else Decimal(0),
               last_success_at=_NOV_04 if kgm_freshness != TurkeyProviderFreshness.NO_DATA else None,
               last_attempt_at=_NOV_11, debt_count=2 if kgm_freshness != TurkeyProviderFreshness.NO_DATA else 0),
    )

    text = texts.format_debt_row(car_number="34ABC123", owners=[], total_amount=Decimal(150), provider_states=states)

    assert "🟢" not in text
    assert "оплачено" not in text.lower()  # «Неоплаченные записи» — не «оплачено»


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
