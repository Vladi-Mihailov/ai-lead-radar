"""
Тесты reader/fines/validation.py — чистые функции, без Repository/Telegram/сети.
"""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.models import FineMonitoringTask  # noqa: E402
from reader.fines.validation import (  # noqa: E402
    FineValidationError,
    looks_like_car_number,
    normalize_car_number,
    parse_date,
    resolve_monitoring_period,
    validate_no_overlap,
)


def _task(id_, start, end) -> FineMonitoringTask:
    return FineMonitoringTask(
        id=id_,
        car_number="B957MA09",
        label=None,
        start_date=start,
        end_date=end,
        status="active",
        telegram_chat_id=-100999,
        created_by_user_id=111,
        created_at=None,
        updated_at=None,
        last_checked_at=None,
        last_check_status=None,
        last_error=None,
    )


# ---- normalize_car_number ----


def test_normalize_car_number_uppercases_and_strips():
    assert normalize_car_number("  b957ma09  ") == "B957MA09"


def test_normalize_car_number_rejects_empty():
    with pytest.raises(FineValidationError):
        normalize_car_number("   ")


def test_normalize_car_number_rejects_special_characters():
    with pytest.raises(FineValidationError):
        normalize_car_number("B957?MA09")


def test_normalize_car_number_strips_internal_dashes():
    """См. design report "исправить Georgian bot" — дефисы (как и пробелы
    ниже) теперь удаляются ДО проверки формата, а не отклоняются, тот же
    принцип, что и в reader/turkey_bot/validation.py::normalize_plate."""
    assert normalize_car_number("B957-MA09") == "B957MA09"


def test_normalize_car_number_strips_internal_spaces():
    assert normalize_car_number("B957 MA09") == "B957MA09"


# ---- Кириллица -> латиница (см. design report "исправить Georgian bot:
# госномер кириллицей не проходит validation") ----


def test_normalize_car_number_cyrillic_plate_normalizes_to_latin():
    assert normalize_car_number("О687КЕ761") == "O687KE761"


def test_normalize_car_number_lowercase_cyrillic_normalizes_to_latin():
    assert normalize_car_number("о687ке761") == "O687KE761"


def test_normalize_car_number_mixed_case_cyrillic_normalizes_to_latin():
    assert normalize_car_number("о687КЕ761") == "O687KE761"


def test_normalize_car_number_cyrillic_plate_with_spaces_normalizes_to_latin():
    assert normalize_car_number("О 687 КЕ 761") == "O687KE761"


def test_normalize_car_number_cyrillic_plate_with_dashes_normalizes_to_latin():
    assert normalize_car_number("О-687-КЕ-761") == "O687KE761"


def test_normalize_car_number_latin_plate_is_unchanged():
    assert normalize_car_number("M295YB196") == "M295YB196"


# ---- looks_like_car_number (см. задачу "Improve the /start UX... Allow
# direct plate input after /start") — ЛЕНИВЫЙ pre-filter, строже голого
# normalize_car_number (который принял бы "HELLO"/"12345" — валидный
# ФОРМАТ, но явно не похоже на реальный номер), используется ТОЛЬКО чтобы
# решить, стоит ли вообще пробовать add-car flow с home-состояния, см.
# reader/public_bot/conversation.py::handle_text(). Инвариант: ЛЮБОЙ ввод,
# для которого looks_like_car_number() вернул True, ВСЕГДА проходит и
# normalize_car_number() без исключения (strict subset по char-классу) —
# см. test_looks_like_car_number_is_always_a_subset_of_normalize ниже:
# "ЕСЛИ выглядит как номер ПО ЭТИМ правилам, ЗНАЧИТ формат уже валиден"
# сегодня верно по построению, а не совпадение. ----


@pytest.mark.parametrize("plate", ["O127PX62", "M295YB196", "AA001AA", "E911EE95", "P201HX31"])
def test_looks_like_car_number_true_for_real_plate_examples(plate):
    assert looks_like_car_number(plate) is True


def test_looks_like_car_number_true_for_lowercase_and_cyrillic():
    assert looks_like_car_number("o127px62") is True
    assert looks_like_car_number("О687КЕ761") is True
    assert looks_like_car_number("О-687-КЕ-761") is True
    assert looks_like_car_number("О 687 КЕ 761") is True


@pytest.mark.parametrize("text", ["HELLO", "ПРИВЕТ", "СПАСИБО", "YES", "OK"])
def test_looks_like_car_number_false_for_words_without_digits(text):
    assert looks_like_car_number(text) is False


@pytest.mark.parametrize("text", ["12345", "0001", "999999999"])
def test_looks_like_car_number_false_for_digits_without_letters(text):
    assert looks_like_car_number(text) is False


def test_looks_like_car_number_false_for_too_short():
    assert looks_like_car_number("A1") is False
    assert looks_like_car_number("AB12") is False  # 4 chars, below the 5-char floor


def test_looks_like_car_number_false_for_too_long():
    assert looks_like_car_number("AAAAAAAAAA11111111") is False


def test_looks_like_car_number_false_for_ordinary_sentence_with_a_digit():
    """The exact false-positive this heuristic must avoid: an ordinary
    multi-word message that happens to contain a number. Spaces are
    stripped before the check (same as normalize_car_number's own first
    step), so this must be rejected for another reason -- here, "сегодня"
    contains Cyrillic letters outside the 12-letter car-plate mapping
    table (see _CYRILLIC_TO_LATIN), so the normalized candidate is not
    purely [A-Z0-9] and correctly fails."""
    assert looks_like_car_number("куплю гараж за 100000") is False
    assert looks_like_car_number("встречаемся в 19:00 сегодня") is False


def test_looks_like_car_number_false_for_empty_or_whitespace():
    assert looks_like_car_number("") is False
    assert looks_like_car_number("   ") is False


@pytest.mark.parametrize("candidate", [
    "O127PX62", "M295YB196", "AA001AA", "o127px62", "О687КЕ761", "A1BCD2", "AB1234", "A123456789",
])
def test_looks_like_car_number_is_always_a_subset_of_normalize(candidate):
    """Documents the intentional structural invariant (see this section's
    own header comment): under the current rules, looks_like_car_number()
    can never return True for an input that normalize_car_number() then
    rejects -- there is no "looks like a plate but is invalid" case
    reachable today. reader/public_bot/conversation.py's direct-input
    handler still goes through the real normalize_car_number() call (via
    the existing _handle_car_number_input(), unmodified) on every call, so
    if this invariant is ever broken by a future change to either
    function's rules, the existing invalid-plate error response already
    handles it correctly with no further code change needed."""
    if looks_like_car_number(candidate):
        normalize_car_number(candidate)  # must not raise


def test_normalize_car_number_all_twelve_supported_cyrillic_letters_translate_correctly():
    # А В Е К М Н -> A B E K M H, О Р С Т У Х -> O P C T Y X (см. задачу).
    assert normalize_car_number("АВЕКМН") == "ABEKMH"
    assert normalize_car_number("ОРСТУХ") == "OPCTYX"


def test_normalize_car_number_unsupported_cyrillic_letter_is_rejected():
    """Только 12 конкретных букв (тот же принцип, что и в
    reader/turkey_bot/validation.py) — любая другая кириллическая буква
    остаётся кириллицей после .translate() и не проходит [A-Z0-9]."""
    with pytest.raises(FineValidationError):
        normalize_car_number("Б957МА09")


# ---- parse_date ----


def test_parse_date_parses_valid_date():
    assert parse_date("03.08.2026") == date(2026, 8, 3)


def test_parse_date_rejects_invalid_format():
    with pytest.raises(FineValidationError):
        parse_date("2026-08-03")


def test_parse_date_rejects_garbage():
    with pytest.raises(FineValidationError):
        parse_date("не дата")


# ---- resolve_monitoring_period ----


def test_resolve_monitoring_period_defaults_to_today_plus_30_days():
    today = date(2026, 8, 3)

    start, end = resolve_monitoring_period(None, None, today=today)

    assert start == today
    assert end == date(2026, 9, 2)


def test_resolve_monitoring_period_parses_explicit_dates():
    start, end = resolve_monitoring_period(
        "03.08.2026", "13.08.2026", today=date(2026, 8, 1)
    )

    assert start == date(2026, 8, 3)
    assert end == date(2026, 8, 13)


def test_resolve_monitoring_period_rejects_end_before_start():
    with pytest.raises(FineValidationError):
        resolve_monitoring_period("13.08.2026", "03.08.2026", today=date(2026, 8, 1))


def test_resolve_monitoring_period_rejects_only_one_date_given():
    with pytest.raises(FineValidationError):
        resolve_monitoring_period("03.08.2026", None, today=date(2026, 8, 1))


def test_resolve_monitoring_period_accepts_same_start_and_end():
    start, end = resolve_monitoring_period(
        "03.08.2026", "03.08.2026", today=date(2026, 8, 1)
    )
    assert start == end == date(2026, 8, 3)


# ---- validate_no_overlap ----


def test_validate_no_overlap_passes_when_no_existing_tasks():
    validate_no_overlap(date(2026, 8, 3), date(2026, 8, 13), [])


def test_validate_no_overlap_passes_when_periods_do_not_touch():
    existing = [_task(1, date(2026, 7, 1), date(2026, 7, 31))]
    validate_no_overlap(date(2026, 8, 1), date(2026, 8, 31), existing)


def test_validate_no_overlap_rejects_overlapping_period():
    existing = [_task(1, date(2026, 8, 1), date(2026, 8, 31))]

    with pytest.raises(FineValidationError):
        validate_no_overlap(date(2026, 8, 15), date(2026, 9, 15), existing)


def test_validate_no_overlap_rejects_when_new_period_fully_inside_existing():
    existing = [_task(1, date(2026, 8, 1), date(2026, 8, 31))]

    with pytest.raises(FineValidationError):
        validate_no_overlap(date(2026, 8, 10), date(2026, 8, 20), existing)


def test_validate_no_overlap_rejects_touching_boundary():
    # end_date нового периода == start_date существующего — включительная
    # граница, тоже пересечение.
    existing = [_task(1, date(2026, 8, 10), date(2026, 8, 20))]

    with pytest.raises(FineValidationError):
        validate_no_overlap(date(2026, 8, 1), date(2026, 8, 10), existing)
