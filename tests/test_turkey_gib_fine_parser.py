"""
Тесты reader/turkey_bot/gib/fine_parser.py::parse_fine_records.

Фикстуры ниже сохраняют РЕАЛЬНЫЕ, увиденные вживую имена полей одной
записи BORCLAR (см. design report Stage 4/live-test), но НЕ реальные
значения — hash/kimlik/суммы/описания фиктивные (см. задачу: "tests must
use fabricated/sanitized values only").
"""

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.gib.fine_parser import parse_fine_records  # noqa: E402


def _sanitized_item(**overrides) -> dict:
    item = {
        "KK_ACIKLAMA": (
            "Örnek konum açıklaması HarfSeriNo:XX00000000 Ceza Tarihi:2026-08-08 "
            "Ceza Maddesi:51/2-B-2 Madde Açıklaması:Örnek ihlal açıklaması"
        ),
        "KK_AD": "",
        "KK_BORC": "1000.00",
        "KK_GECIKMEZAMMI": "0.00",
        "KK_HASH": "SANITIZED-PLACEHOLDER-HASH",
        "KK_INDIRIM_MIKTARI": "0.00",
        "KK_KIMLIK": "SANITIZED-PLACEHOLDER-KIMLIK",
        "KK_KONTROL": "1",
        "KK_MIKTARODENEN": "1000.00",
        "KK_MYS_ETTN": "00000000-0000-0000-0000-000000000000",
        "KK_MYS_KURUM_ADI": "EMNİYET GENEL MÜDÜRLÜĞÜ",
        "KK_MYS_TAHSILAT_TURU": (
            "Yabancı Plakalı Araç ve Sürücüsüne Düzenlenen İdari Para Cezası Karar Tutanakları"
        ),
        "KK_ODEMETARIHI": "20260101",
        "KK_ODEMETURLERI": "111",
        "KK_ORGOID": "00000000000000",
        "KK_OZEL_PLAKA_KODU": "34ABC123",
        "KK_PLAKA": "34ABC123",
        "KK_SOYAD": "",
        "KK_TUTANAKNO": "XX00000000",
        "KK_VDKODU": "000000",
    }
    item.update(overrides)
    return item


def test_parses_all_confirmed_fields_from_one_record():
    fines = parse_fine_records([_sanitized_item()])

    assert len(fines) == 1
    fine = fines[0]
    assert fine.protocol_no == "XX00000000"
    assert fine.plate == "34ABC123"
    assert fine.amount == Decimal("1000.00")
    assert fine.authority == "EMNİYET GENEL MÜDÜRLÜĞÜ"
    assert fine.violation_date == date(2026, 8, 8)
    assert "Örnek konum açıklaması" in fine.description


def test_extracts_structured_location_article_and_violation_description():
    """См. design report: полный аудит подтвердил 4-маркерный паттерн на
    4/4 реальных записях - location/law_article/violation_description
    извлекаются структурно, ДОПОЛНИТЕЛЬНО к сырому description."""
    fines = parse_fine_records([_sanitized_item()])

    fine = fines[0]
    assert fine.location == "Örnek konum açıklaması"
    assert fine.law_article == "51/2-B-2"
    assert fine.violation_description == "Örnek ihlal açıklaması"
    # description (сырой, полный KK_ACIKLAMA) сохраняется как есть -
    # см. задачу: "do not destroy audit data".
    assert "HarfSeriNo:" in fine.description
    assert "Ceza Tarihi:" in fine.description


def test_translated_fields_default_to_none_until_translation_runs():
    fines = parse_fine_records([_sanitized_item()])

    assert fines[0].location_ru is None
    assert fines[0].violation_description_ru is None


def test_missing_harfserino_marker_fails_safely_to_none_for_structured_fields():
    item = _sanitized_item(
        KK_ACIKLAMA="Some location text Ceza Tarihi:2026-08-08 Ceza Maddesi:51/2-B-2 "
        "Madde Açıklaması:Some violation text"
    )

    fines = parse_fine_records([item])

    fine = fines[0]
    assert fine.location is None
    assert fine.law_article is None
    assert fine.violation_description is None
    assert fine.description is not None  # raw description всё ещё доступен


def test_reordered_markers_fail_safely_to_none():
    """См. задачу: "parsing must fail safely if markers are missing or
    reordered" - маркеры в НЕПРАВИЛЬНОМ порядке не должны частично
    распарситься."""
    item = _sanitized_item(
        KK_ACIKLAMA="Madde Açıklaması:Some violation text HarfSeriNo:XX00000000 "
        "Ceza Tarihi:2026-08-08 Ceza Maddesi:51/2-B-2 Some location text"
    )

    fines = parse_fine_records([item])

    fine = fines[0]
    assert fine.location is None
    assert fine.law_article is None
    assert fine.violation_description is None


def test_malformed_description_missing_all_markers_fails_safely():
    item = _sanitized_item(KK_ACIKLAMA="Just some plain text with no structured markers at all")

    fines = parse_fine_records([item])

    fine = fines[0]
    assert fine.location is None
    assert fine.law_article is None
    assert fine.violation_description is None
    assert fine.description == "Just some plain text with no structured markers at all"


def test_parses_multiple_records_independently():
    fines = parse_fine_records([
        _sanitized_item(KK_TUTANAKNO="AA11111111", KK_BORC="500.00"),
        _sanitized_item(KK_TUTANAKNO="BB22222222", KK_BORC="750.50"),
    ])

    assert len(fines) == 2
    assert fines[0].protocol_no == "AA11111111"
    assert fines[0].amount == Decimal("500.00")
    assert fines[1].protocol_no == "BB22222222"
    assert fines[1].amount == Decimal("750.50")


def test_never_exposes_hash_or_kimlik_fields():
    """Явное требование задачи: KK_HASH/KK_KIMLIK никогда не должны
    попадать в GibFineRecord - этих полей в dataclass нет вообще (см.
    design report), проверяем это структурно, а не просто "не равно"."""
    fines = parse_fine_records([_sanitized_item()])

    fine = fines[0]
    field_names = {f for f in fine.__dataclass_fields__}
    assert "KK_HASH" not in field_names
    assert "KK_KIMLIK" not in field_names
    # И на всякий случай - фиктивные значения точно не просочились в
    # какое-либо СУЩЕСТВУЮЩЕЕ поле (защита от копипаст-ошибки).
    rendered_values = [str(v) for v in (fine.protocol_no, fine.description, fine.authority)]
    assert not any("SANITIZED-PLACEHOLDER" in v for v in rendered_values)


def test_non_numeric_amount_fails_safely_to_none():
    fines = parse_fine_records([_sanitized_item(KK_BORC="not-a-number")])

    assert fines[0].amount is None


def test_missing_amount_field_is_none():
    item = _sanitized_item()
    del item["KK_BORC"]

    fines = parse_fine_records([item])

    assert fines[0].amount is None


def test_description_without_ceza_tarihi_gives_no_violation_date():
    fines = parse_fine_records([_sanitized_item(KK_ACIKLAMA="No structured date here at all")])

    assert fines[0].violation_date is None


def test_malformed_ceza_tarihi_fails_safely_to_none():
    fines = parse_fine_records([
        _sanitized_item(KK_ACIKLAMA="Some text Ceza Tarihi:2026-13-99 more text")
    ])

    assert fines[0].violation_date is None


def test_empty_description_gives_no_violation_date_and_no_description():
    fines = parse_fine_records([_sanitized_item(KK_ACIKLAMA="")])

    assert fines[0].description is None
    assert fines[0].violation_date is None


def test_missing_authority_is_none():
    item = _sanitized_item()
    del item["KK_MYS_KURUM_ADI"]

    fines = parse_fine_records([item])

    assert fines[0].authority is None


def test_late_fee_and_discount_parsed_when_present():
    fines = parse_fine_records([
        _sanitized_item(KK_GECIKMEZAMMI="50.00", KK_INDIRIM_MIKTARI="25.00")
    ])

    assert fines[0].late_fee == Decimal("50.00")
    assert fines[0].discount == Decimal("25.00")


def test_non_list_input_returns_empty_tuple():
    assert parse_fine_records(None) == ()
    assert parse_fine_records({"not": "a list"}) == ()


def test_non_dict_items_are_skipped_defensively():
    fines = parse_fine_records(["not-a-dict", 123, _sanitized_item(KK_TUTANAKNO="OK000000")])

    assert len(fines) == 1
    assert fines[0].protocol_no == "OK000000"
