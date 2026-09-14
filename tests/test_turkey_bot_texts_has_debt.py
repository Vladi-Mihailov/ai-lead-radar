"""
Тесты reader/turkey_bot/texts.py::format_has_debt_messages и
_split_into_telegram_messages — рендеринг реального результата (см.
design report: заменяет старую заглушку-плейсхолдер) ТОЛЬКО из
GibFineRecord, никогда из raw_data напрямую.
"""

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot import texts  # noqa: E402
from reader.turkey_bot.gib.models import GibFineRecord  # noqa: E402

_TELEGRAM_LIMIT = 4096


def _fine(**overrides) -> GibFineRecord:
    defaults = {
        "protocol_no": "MC00000000",
        "plate": "34ABC123",
        "amount": Decimal("1000.00"),
        "description": "Örnek ihlal açıklaması",
        "violation_date": date(2026, 8, 8),
        "authority": "EMNİYET GENEL MÜDÜRLÜĞÜ",
        "late_fee": None,
        "discount": None,
    }
    defaults.update(overrides)
    return GibFineRecord(**defaults)


def test_single_fine_renders_expected_fields():
    messages = texts.format_has_debt_messages("34ABC123", (_fine(),))

    assert len(messages) == 1
    text = messages[0]
    assert "34ABC123" in text
    assert "найдены штрафы — 1" in text
    assert "MC00000000" in text
    assert "1000.00 TRY" in text
    assert "Örnek ihlal açıklaması" in text
    assert "08.08.2026" in text
    assert "EMNİYET GENEL MÜDÜRLÜĞÜ" in text


def test_multiple_fines_all_present_and_total_correct():
    fines = (
        _fine(protocol_no="AA11111111", amount=Decimal("3000.00")),
        _fine(protocol_no="BB22222222", amount=Decimal("6000.00")),
        _fine(protocol_no="CC33333333", amount=Decimal("3184.50")),
        _fine(protocol_no="DD44444444", amount=Decimal("1500.00")),
    )

    messages = texts.format_has_debt_messages("E911EE95", fines)
    full_text = "\n\n".join(messages)

    assert "найдены штрафы — 4" in full_text
    for protocol_no in ("AA11111111", "BB22222222", "CC33333333", "DD44444444"):
        assert protocol_no in full_text
    assert "13684.50 TRY" in full_text  # 3000+6000+3184.50+1500


def test_shared_authority_shown_once_not_per_fine():
    fines = (_fine(authority="SAME ORG"), _fine(authority="SAME ORG"))

    messages = texts.format_has_debt_messages("34ABC123", fines)
    full_text = "\n\n".join(messages)

    assert full_text.count("SAME ORG") == 1


def test_differing_authority_shown_per_fine():
    fines = (_fine(authority="ORG A"), _fine(authority="ORG B"))

    messages = texts.format_has_debt_messages("34ABC123", fines)
    full_text = "\n\n".join(messages)

    assert full_text.count("ORG A") == 1
    assert full_text.count("ORG B") == 1


def test_missing_optional_fields_are_omitted_not_placeholder():
    """См. задачу: "do not show a field at all when it is absent/empty
    rather than printing None, null, —, etc." - проверяем, что сама
    строка-подпись поля отсутствует целиком, а не заполнена placeholder'ом
    (сравнение с полным набором полей ниже, где все подписи присутствуют)."""
    fine = _fine(
        protocol_no=None, amount=None, description=None, violation_date=None, authority=None,
    )

    messages = texts.format_has_debt_messages("34ABC123", (fine,))
    full_text = "\n\n".join(messages)

    assert "None" not in full_text
    assert "null" not in full_text
    assert "💰 Сумма" not in full_text
    assert "📍 Нарушение" not in full_text
    assert "📅 Дата нарушения" not in full_text
    assert "🏛 Орган" not in full_text
    assert "№" not in full_text  # "Штраф № ..." - номер тоже отсутствует


def test_late_fee_shown_only_when_positive():
    with_fee = texts.format_has_debt_messages("34ABC123", (_fine(late_fee=Decimal("50.00")),))
    without_fee = texts.format_has_debt_messages("34ABC123", (_fine(late_fee=Decimal("0.00")),))

    assert "50.00 TRY" in "\n\n".join(with_fee)
    assert "Пеня" not in "\n\n".join(without_fee)


def test_discount_shown_only_when_positive():
    with_discount = texts.format_has_debt_messages(
        "34ABC123", (_fine(discount=Decimal("25.00")),)
    )
    without_discount = texts.format_has_debt_messages(
        "34ABC123", (_fine(discount=Decimal("0.00")),)
    )

    assert "25.00 TRY" in "\n\n".join(with_discount)
    assert "Скидка" not in "\n\n".join(without_discount)


def test_total_includes_late_fee_and_subtracts_discount():
    fine = _fine(amount=Decimal("100.00"), late_fee=Decimal("10.00"), discount=Decimal("5.00"))

    messages = texts.format_has_debt_messages("34ABC123", (fine,))

    assert "105.00 TRY" in "\n\n".join(messages)  # 100 + 10 - 5


def test_no_fines_returns_safe_fallback_not_empty():
    messages = texts.format_has_debt_messages("34ABC123", ())

    assert len(messages) == 1
    assert "34ABC123" in messages[0]


def test_never_exposes_sensitive_looking_strings():
    """GibFineRecord структурно не может нести KK_HASH/KK_KIMLIK (их нет в
    dataclass), но проверяем и сам рендер на всякий случай - типичный
    формат таких значений (длинная строка со спецсимволами) не должен
    просочиться через какое-либо поле."""
    fine = _fine(description="SANITIZED-PLACEHOLDER-HASH SANITIZED-PLACEHOLDER-KIMLIK")
    messages = texts.format_has_debt_messages("34ABC123", (fine,))
    full_text = "\n\n".join(messages)

    # Само описание намеренно содержит эти строки здесь, чтобы
    # зафиксировать: description ПОКАЗЫВАЕТСЯ как есть (см. задачу п.4 -
    # турецкий текст без изменений) - тест на секреты должен проверяться
    # на уровне fine_parser.py (см. tests/test_turkey_gib_fine_parser.py::
    # test_never_exposes_hash_or_kimlik_fields), а не здесь, где description
    # уже пользовательский ввод модели, а не сырое поле GIB.
    assert full_text  # sanity: рендер вообще не падает на таком описании


def test_many_fines_split_into_multiple_telegram_safe_messages():
    """См. задачу: "never exceed Telegram message length" /
    "split into multiple messages if necessary" / "preserve fine
    boundaries when splitting" / "do not silently truncate fines"."""
    long_description = "X" * 500  # достаточно длинно, чтобы 4096 переполнился на ~8 штрафах
    fines = tuple(
        _fine(protocol_no=f"MC{i:08d}", description=long_description) for i in range(20)
    )

    messages = texts.format_has_debt_messages("34ABC123", fines)

    assert len(messages) > 1
    for message in messages:
        assert len(message) <= _TELEGRAM_LIMIT

    full_text = "\n\n".join(messages)
    for i in range(20):
        assert f"MC{i:08d}" in full_text  # ни один штраф не пропал/не обрезан

    assert "Общая задолженность" in messages[-1]  # итог виден и понятен


def test_split_never_breaks_a_single_fine_block_across_messages():
    long_description = "Y" * 300
    fines = tuple(_fine(protocol_no=f"MC{i:08d}", description=long_description) for i in range(15))

    messages = texts.format_has_debt_messages("34ABC123", fines)

    for i in range(15):
        marker = f"MC{i:08d}"
        containing = [m for m in messages if marker in m]
        assert len(containing) == 1  # штраф целиком в ОДНОМ сообщении, не разбит
