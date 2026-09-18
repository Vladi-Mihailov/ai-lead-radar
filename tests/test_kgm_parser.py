"""
Тесты reader/turkey_bot/kgm/parser.py — см. design report "KGM
investigation"/"Реализация KGM provider"/"KGM no_debt live confirmation".
Используют САНИТИЗИРОВАННЫЕ фикстуры (tests/fixtures/kgm_*) — реальные
ответы (has_debt: plate M295YB196; no_debt: plate A123AA180, см. design
report), с вырезанными cookies/ViewState/security-adjacent значениями
(заменены на "SANITIZED"/фейковые плейсхолдеры) — сама структура
результата (id/классы/данные) осталась подлинной. Никакой реальной
сети/CAPTCHA здесь нет и не может быть.
"""

import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.kgm.parser import (  # noqa: E402
    parse_delta_response,
    parse_result_panel,
    parse_submit_response,
)

_FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"


def _read_fixture(name: str) -> str:
    return (_FIXTURES_DIR / name).read_text(encoding="utf-8")


_SUCCESS_HTML = _read_fixture("kgm_sorgulama_m295yb196_success.html")
_SUCCESS_DELTA = _read_fixture("kgm_sorgulama_m295yb196_delta.txt")
_NO_DEBT_SYNTHETIC_HTML = _read_fixture("kgm_sorgulama_no_debt_synthetic.html")
_NO_DEBT_REAL_HTML = _read_fixture("kgm_sorgulama_a123aa180_no_debt.html")
_NO_DEBT_REAL_DELTA = _read_fixture("kgm_sorgulama_a123aa180_no_debt_delta.txt")


# ---- ASP.NET AJAX delta-конвертик (см. design report: формат
# подтверждён вживую, разобран без единого лишнего байта) ----


def test_parse_delta_response_extracts_all_chunks_from_real_fixture():
    chunks = parse_delta_response(_SUCCESS_DELTA)
    assert chunks is not None
    assert ("updatePanel", "pnlSayfa") in chunks
    assert ("hiddenField", "__VIEWSTATE") in chunks
    assert chunks[("hiddenField", "__VIEWSTATE")] == "FAKE_VIEWSTATE_PLACEHOLDER_NOT_REAL"


def test_parse_delta_response_pnl_sayfa_content_matches_success_html():
    chunks = parse_delta_response(_SUCCESS_DELTA)
    assert chunks[("updatePanel", "pnlSayfa")] == _SUCCESS_HTML


def test_parse_delta_response_rejects_empty_string():
    assert parse_delta_response("") is None


def test_parse_delta_response_rejects_garbage_text():
    assert parse_delta_response("this is not a delta response at all") is None


def test_parse_delta_response_rejects_truncated_chunk():
    """Длина заявлена больше, чем реально осталось байт в строке — не
    должно приводить к IndexError/неверному срезу, только к None (см.
    design report: "malformed delta => unexpected")."""
    assert parse_delta_response("100|updatePanel|pnlSayfa|too short|") is None


def test_parse_submit_response_malformed_delta_is_unexpected():
    outcome = parse_submit_response("garbage not a delta response")
    assert outcome.kind == "unexpected"


def test_parse_submit_response_delta_without_pnlsayfa_chunk_is_unexpected():
    delta_without_panel = "1|#||4|"
    outcome = parse_submit_response(delta_without_panel)
    assert outcome.kind == "unexpected"


# ---- Успешный has_debt (см. design report: реальный HAR, M295YB196) ----


def test_has_debt_real_fixture_via_full_pipeline():
    outcome = parse_submit_response(_SUCCESS_DELTA)
    assert outcome.kind == "has_debt"


def test_has_debt_kgm_operator_two_rows_80_try():
    outcome = parse_result_panel(_SUCCESS_HTML)
    kgm = next(op for op in outcome.operators if op.operator_key == "kgm")
    assert len(kgm.items) == 2
    assert kgm.subtotal == Decimal("80.00")
    assert sum((item.payable_amount for item in kgm.items), Decimal(0)) == Decimal("80.00")


def test_has_debt_avrasya_operator_three_rows_2580_try():
    outcome = parse_result_panel(_SUCCESS_HTML)
    avrasya = next(op for op in outcome.operators if op.operator_key == "avrasya")
    assert len(avrasya.items) == 3
    assert avrasya.subtotal == Decimal("2580.00")


def test_has_debt_grand_total_2660_try():
    outcome = parse_result_panel(_SUCCESS_HTML)
    assert outcome.kgm_total == Decimal("80.00")
    assert outcome.yid_total == Decimal("2580.00")
    assert outcome.grand_total == Decimal("2660.00")


def test_has_debt_remaining_eight_operators_have_no_debt_and_are_absent():
    """См. design report: "Не показывать пустые operator sections" —
    outcome.operators содержит ТОЛЬКО kgm и avrasya, остальные 8 (все
    реально "Kayıt yok." в этой фикстуре) вообще не попадают в результат."""
    outcome = parse_result_panel(_SUCCESS_HTML)
    operator_keys = {op.operator_key for op in outcome.operators}
    assert operator_keys == {"kgm", "avrasya"}


def test_has_debt_kgm_row_fields_match_known_values():
    outcome = parse_result_panel(_SUCCESS_HTML)
    kgm = next(op for op in outcome.operators if op.operator_key == "kgm")
    first = kgm.items[0]
    assert first.date_time.isoformat() == "2026-09-15T09:39:48"
    assert first.entry_station == "HENDEK"
    assert first.exit_station == "TOPAĞAÇ SGS"
    assert first.vehicle_class == "1"
    assert first.base_toll == Decimal("40.00")
    assert first.payable_amount == Decimal("40.00")
    assert first.penalty_free_deadline.isoformat() == "2026-09-30"


def test_has_debt_avrasya_rows_have_blank_entry_station_and_vehicle_class():
    """См. design report: реально увиденное вживую &nbsp; на строках
    Avrasya — None, а не пустая строка/ошибка разбора."""
    outcome = parse_result_panel(_SUCCESS_HTML)
    avrasya = next(op for op in outcome.operators if op.operator_key == "avrasya")
    first_two = avrasya.items[:2]
    for item in first_two:
        assert item.entry_station is None
        assert item.vehicle_class is None
        assert item.exit_station in ("ASYA", "AVRUPA")
    # Третья запись Avrasya реально несёт vehicle_class="1" (см. design
    # report) — не все строки одного оператора обязаны быть одинаковыми.
    assert avrasya.items[2].vehicle_class == "1"


def test_has_debt_avrasya_base_toll_differs_from_payable_when_penalty_applied():
    """См. задачу: "Не реконструируй штраф... base_toll и payable_amount
    храни отдельно" — оба поля сохраняются как есть, даже когда они
    отличаются (реально увиденная вживую разница 225,00 -> 1.125,00)."""
    outcome = parse_result_panel(_SUCCESS_HTML)
    avrasya = next(op for op in outcome.operators if op.operator_key == "avrasya")
    first = avrasya.items[0]
    assert first.base_toll == Decimal("225.00")
    assert first.payable_amount == Decimal("1125.00")
    assert first.base_toll != first.payable_amount


def test_kgm_and_yid_tables_use_different_column_order_but_parse_correctly():
    """См. design report п.3: KGM-таблица начинается с "Bilgi, Geçiş
    Ücreti, Ödenecek Tutar, Ödeme, Çıkış Tarih, ..." — Avrasya-таблица
    начинается с "Bilgi, Çıkış Tarih, Giriş İstasyon, ...", т.е. КОЛОНКИ
    Geçiş Ücreti/Ödenecek Tutar стоят в разных позициях — оба поля,
    несмотря на это, извлекаются корректно (см. header-name mapping,
    _header_index_map) для ОБЕИХ таблиц."""
    outcome = parse_result_panel(_SUCCESS_HTML)
    kgm = next(op for op in outcome.operators if op.operator_key == "kgm")
    avrasya = next(op for op in outcome.operators if op.operator_key == "avrasya")
    assert kgm.items[0].base_toll == Decimal("40.00")
    assert avrasya.items[0].base_toll == Decimal("225.00")


# ---- rejected CAPTCHA (см. design report: реально увиденный вживую
# текст "metin hatası", live wrong-answer тест этой же сессии) ----


def _wrap_panel_in_delta(panel_html: str) -> str:
    return f"{len(panel_html)}|updatePanel|pnlSayfa|{panel_html}|"


def test_rejected_captcha_metin_hatasi():
    panel = '<span id="lblHata" class="control-label vgRegEx_bt">metin hatası</span>'
    outcome = parse_submit_response(_wrap_panel_in_delta(panel))
    assert outcome.kind == "rejected"
    assert outcome.message == "metin hatası"


def test_unrecognized_non_empty_lblhata_is_unexpected_not_rejected():
    """См. design report/avrasya-parser: не гадать схему — ТОЛЬКО реально
    увиденный текст "metin hatası" становится "rejected", любой другой
    непустой lblHata -> unexpected."""
    panel = '<span id="lblHata">some other never-seen error text</span>'
    outcome = parse_submit_response(_wrap_panel_in_delta(panel))
    assert outcome.kind == "unexpected"


# ---- Fail-closed: отсутствующие/противоречивые секции ----


def test_missing_operator_section_is_unexpected():
    """Убираем секцию KGM целиком из настоящей фикстуры — parser должен
    отказаться классифицировать результат вместо частичного разбора."""
    broken_html = _SUCCESS_HTML.replace('id="lblKgm"', 'id="lblKgmRenamed"')
    outcome = parse_result_panel(broken_html)
    assert outcome.kind == "unexpected"


def test_label_says_records_but_table_has_wrong_row_count_is_unexpected():
    """label утверждает "2 kayıt" для KGM, но подменяем на "3 kayıt" —
    таблица физически несёт 2 строки -> расхождение -> unexpected."""
    broken_html = _SUCCESS_HTML.replace(
        "KARAYOLLARI GENEL MÜDÜRLÜĞÜ: 2 kayıt", "KARAYOLLARI GENEL MÜDÜRLÜĞÜ: 3 kayıt",
    )
    outcome = parse_result_panel(broken_html)
    assert outcome.kind == "unexpected"


def test_label_says_no_records_but_table_has_rows_is_unexpected():
    """Обратное несоответствие — превращаем "2 kayıt" в "Kayıt yok.", НЕ
    трогая саму таблицу (в ней остаются реальные 2 строки)."""
    broken_html = _SUCCESS_HTML.replace(
        "KARAYOLLARI GENEL MÜDÜRLÜĞÜ: 2 kayıt", "KARAYOLLARI GENEL MÜDÜRLÜĞÜ: Kayıt yok.",
    )
    outcome = parse_result_panel(broken_html)
    assert outcome.kind == "unexpected"


def test_checksum_mismatch_grand_total_is_unexpected():
    """Подменяем Genel Toplam на заведомо неверное значение — parser
    обязан отказаться от has_debt, несмотря на то, что все секции сами
    по себе разобрались бы корректно (см. design report п.6: "Не выдавать
    пользователю частичный результат как достоверный")."""
    broken_html = _SUCCESS_HTML.replace(
        '<span id="lblGenelCezaToplam" class="mtLabel">2.660,00 ₺</span>',
        '<span id="lblGenelCezaToplam" class="mtLabel">9.999,00 ₺</span>',
    )
    outcome = parse_result_panel(broken_html)
    assert outcome.kind == "unexpected"


def test_checksum_mismatch_operator_subtotal_is_unexpected():
    """Подменяем KGM-подытог так, что он больше не совпадает с суммой его
    собственных строк."""
    broken_html = _SUCCESS_HTML.replace(
        '<span id="lblKgmCezaToplam" class="mtLabel">80,00 ₺</span>',
        '<span id="lblKgmCezaToplam" class="mtLabel">40,00 ₺</span>',
    )
    outcome = parse_result_panel(broken_html)
    assert outcome.kind == "unexpected"


# ---- Общий no_debt — ПОДТВЕРЖДЕНО вживую (см. design report "KGM
# no_debt live confirmation": реальный ответ, plate A123AA180,
# tests/fixtures/kgm_sorgulama_a123aa180_no_debt.html/*_delta.txt) — код
# классифицировал его как "no_debt" БЕЗ единой правки. Synthetic-фикстура
# (построенная программно из реального has_debt-ответа, переворотом всех
# 10 секций в "Kayıt yok." и обнулением сумм) сохранена ниже как
# дополнительный, независимый fail-closed тест. ----


def test_real_no_debt_fixture_all_ten_operators_empty_and_zero_totals():
    outcome = parse_result_panel(_NO_DEBT_REAL_HTML)
    assert outcome.kind == "no_debt"
    assert outcome.operators == ()
    assert outcome.kgm_total == Decimal(0)
    assert outcome.yid_total == Decimal(0)
    assert outcome.grand_total == Decimal(0)


def test_real_no_debt_fixture_via_full_pipeline():
    outcome = parse_submit_response(_NO_DEBT_REAL_DELTA)
    assert outcome.kind == "no_debt"


def test_real_no_debt_fixture_kgm_and_avrasya_labels_keep_bt_class_but_still_no_debt():
    """Реальный live-ответ (A123AA180) показал особенность сайта, не
    увиденную в исходном (has_debt) фикстуре: lblKgm/lblAvrasya сохраняют
    CSS-класс "mtLabelHeader_bt" (обычно означающий "есть записи") ДАЖЕ
    когда текст говорит "Kayıt yok." — parser.py никогда не проверял
    class (только текст label), поэтому классификация остаётся корректной
    без единой правки кода (см. design report)."""
    assert 'id="lblKgm" class="mtLabelHeader_bt">KARAYOLLARI GENEL MÜDÜRLÜĞÜ: Kayıt yok.' in (
        _NO_DEBT_REAL_HTML
    )
    outcome = parse_result_panel(_NO_DEBT_REAL_HTML)
    assert outcome.kind == "no_debt"


def test_synthetic_overall_no_debt_all_ten_operators_empty_and_zero_totals():
    outcome = parse_result_panel(_NO_DEBT_SYNTHETIC_HTML)
    assert outcome.kind == "no_debt"
    assert outcome.operators == ()
    assert outcome.kgm_total == Decimal(0)
    assert outcome.yid_total == Decimal(0)
    assert outcome.grand_total == Decimal(0)


def test_no_debt_requires_all_ten_sections_not_just_zero_totals():
    """Fail-closed: если totals обнулены, но хотя бы одна секция всё ещё
    (некорректно) утверждает задолженность без соответствующих строк —
    unexpected, а не тихий no_debt."""
    inconsistent_html = _NO_DEBT_SYNTHETIC_HTML.replace(
        "KARAYOLLARI GENEL MÜDÜRLÜĞÜ: Kayıt yok.", "KARAYOLLARI GENEL MÜDÜRLÜĞÜ: 1 kayıt",
    )
    outcome = parse_result_panel(inconsistent_html)
    assert outcome.kind == "unexpected"


def test_real_no_debt_requires_all_ten_sections_not_just_zero_totals():
    """Тот же fail-closed тест, что и выше, но на РЕАЛЬНОМ (не synthetic)
    fixture — доказывает, что fail-closed правило не было ослаблено при
    добавлении реального fixture."""
    inconsistent_html = _NO_DEBT_REAL_HTML.replace(
        "KARAYOLLARI GENEL MÜDÜRLÜĞÜ: Kayıt yok.", "KARAYOLLARI GENEL MÜDÜRLÜĞÜ: 1 kayıt",
    )
    outcome = parse_result_panel(inconsistent_html)
    assert outcome.kind == "unexpected"


# ---- Турецкий денежный формат -> Decimal ----


def test_turkish_amount_format_parses_to_decimal():
    from reader.turkey_bot.kgm.parser import _parse_try_amount

    assert _parse_try_amount("40,00 ₺") == Decimal("40.00")
    assert _parse_try_amount("1.125,00 ₺") == Decimal("1125.00")
    assert _parse_try_amount("2.660,00 ₺") == Decimal("2660.00")


def test_turkish_amount_format_blank_is_none():
    from reader.turkey_bot.kgm.parser import _parse_try_amount

    assert _parse_try_amount("") is None
    assert _parse_try_amount("\xa0") is None


def test_turkish_amount_format_unparseable_is_none():
    from reader.turkey_bot.kgm.parser import _parse_try_amount

    assert _parse_try_amount("not a number ₺") is None
