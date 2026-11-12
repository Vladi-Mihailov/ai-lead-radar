"""
Тесты kind == "partial" парсера KGM (см. reader/turkey_bot/kgm/parser.py,
модуль docstring) и строки лога KgmProvider для него.

Живой ответ 2026-11-12: 9 из 10 секций разобрались, текст label
Aydın-Denizli не совпал ни с "N kayıt", ни с "Kayıt yok." — раньше вся
страница становилась "unexpected". Теперь такой оператор НЕ ПРОВЕРЕН, а
остальные секции разбираются и сверяются как обычно. Любое другое
расхождение структуры по-прежнему даёт "unexpected".

Используются те же санитизированные фикстуры, что и в test_kgm_parser.py;
файл успешного ответа находится по маске, чтобы не повторять его имя.
"""

import logging
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.kgm.parser import parse_submit_response  # noqa: E402
from reader.turkey_bot.kgm.provider import KgmProvider  # noqa: E402

_FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"
_SUCCESS_FIXTURES = sorted(_FIXTURES_DIR.glob("kgm_sorgulama_*_success.html"))
assert len(_SUCCESS_FIXTURES) == 1, _SUCCESS_FIXTURES
_SUCCESS_HTML = _SUCCESS_FIXTURES[0].read_text(encoding="utf-8")
_NO_DEBT_HTML = (_FIXTURES_DIR / "kgm_sorgulama_no_debt_synthetic.html").read_text(encoding="utf-8")

_AYDIN_LABEL = "FERNAS OTOYOL İŞLETMESİ A.Ş. (AYDIN-DENİZLİ OTOYOLU): Kayıt yok."
_UNKNOWN_AYDIN_LABEL = "FERNAS OTOYOL İŞLETMESİ A.Ş. (AYDIN-DENİZLİ OTOYOLU): Sorgulama yapılamadı"
_KGM_LABEL = "KARAYOLLARI GENEL MÜDÜRLÜĞÜ: 2 kayıt"
_YID_TOTAL = 'id="lblYidToplamParaCezası" class="mtLabel">2.580,00 ₺'
_GRAND_TOTAL = 'id="lblGenelCezaToplam" class="mtLabel">2.660,00 ₺'

_KGM_TOTAL = 'id="lblKgmToplamParaCezasi" class="mtLabel">80,00 ₺'

_PLATE = "TEST0001"
_CAPTCHA = "54 32109"
_LOGGER = "reader.turkey_bot.kgm.provider"
# Тот же контракт, что в test_kgm_parser.py::test_diagnostics_never_contain_site_text_plate_or_html.
_SAFE_DIAGNOSTICS_RE = re.compile(r"[a-z_]+ [a-z_]+ [A-Za-z0-9_=+#,. -]*")
_FORBIDDEN_IN_DIAGNOSTICS = ("FERNAS", "AYDIN", "Sorgulama", "KARAYOLLARI", "AVRASYA", _PLATE, "<", ">", "'", "?")


def _replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


def _panel(html: str) -> str:
    return f"{len(html)}|updatePanel|pnlSayfa|{html}|"


def _success_with_unknown_aydin() -> str:
    return _replace_once(_SUCCESS_HTML, _AYDIN_LABEL, _UNKNOWN_AYDIN_LABEL)


def _assert_safe_diagnostics(outcome) -> None:
    diagnostics = outcome.diagnostics
    rendered = f"{diagnostics.category} {diagnostics.code} {diagnostics.detail}"
    assert _SAFE_DIAGNOSTICS_RE.fullmatch(rendered), rendered
    for forbidden in _FORBIDDEN_IN_DIAGNOSTICS:
        assert forbidden not in rendered


def test_unknown_aydin_label_on_debt_page_keeps_all_other_sections():
    outcome = parse_submit_response(_panel(_success_with_unknown_aydin()))

    assert outcome.kind == "partial"
    assert outcome.unchecked_operators == ("aydin_denizli",)
    assert [op.operator_key for op in outcome.operators] == ["kgm", "avrasya"]
    assert [len(op.items) for op in outcome.operators] == [2, 3]
    assert sum(op.subtotal for op in outcome.operators) == Decimal("2660.00")
    assert (outcome.kgm_total, outcome.yid_total, outcome.grand_total) == (
        Decimal("80.00"), Decimal("2580.00"), Decimal("2660.00"),
    )
    assert (outcome.diagnostics.category, outcome.diagnostics.code) == ("result_structure", "section_label_unrecognized")
    assert outcome.diagnostics.detail.startswith("aydin_denizli chars=")
    assert outcome.diagnostics.detail.endswith(" remainder=no")
    _assert_safe_diagnostics(outcome)


def test_unknown_aydin_label_on_no_debt_page_is_partial_not_no_debt():
    outcome = parse_submit_response(_panel(_replace_once(_NO_DEBT_HTML, _AYDIN_LABEL, _UNKNOWN_AYDIN_LABEL)))

    assert outcome.kind == "partial"
    assert outcome.operators == ()
    assert outcome.unchecked_operators == ("aydin_denizli",)
    assert outcome.grand_total == Decimal(0)
    assert outcome.diagnostics.detail == (
        f"aydin_denizli chars={len(_UNKNOWN_AYDIN_LABEL)} digits=0 keywords=sorgu+yapilamadi remainder=no"
    )
    _assert_safe_diagnostics(outcome)


def test_page_total_above_parsed_sections_marks_remainder():
    html = _replace_once(_success_with_unknown_aydin(), _YID_TOTAL, _YID_TOTAL.replace("2.580,00", "2.680,00"))
    html = _replace_once(html, _GRAND_TOTAL, _GRAND_TOTAL.replace("2.660,00", "2.760,00"))

    outcome = parse_submit_response(_panel(html))

    assert outcome.kind == "partial"
    assert sum(op.subtotal for op in outcome.operators) == Decimal("2660.00")
    assert outcome.grand_total == Decimal("2760.00")
    assert outcome.diagnostics.detail.endswith(" remainder=yes")


def test_parsed_sections_above_page_total_are_unexpected():
    html = _replace_once(_success_with_unknown_aydin(), _YID_TOTAL, _YID_TOTAL.replace("2.580,00", "2.000,00"))
    html = _replace_once(html, _GRAND_TOTAL, _GRAND_TOTAL.replace("2.660,00", "2.080,00"))

    outcome = parse_submit_response(_panel(html))

    assert outcome.kind == "unexpected"
    assert (outcome.diagnostics.category, outcome.diagnostics.code) == ("checksum", "group_totals_mismatch")
    assert outcome.unchecked_operators == ()


def test_complete_group_is_still_checked_exactly_on_a_partial_page():
    """Непроверен только Aydın-Denizli (группа YİD) — группа KGM полная и
    должна совпадать с lblKgmToplamParaCezasi буквально."""
    html = _replace_once(_success_with_unknown_aydin(), _KGM_TOTAL, _KGM_TOTAL.replace("80,00", "100,00"))
    html = _replace_once(html, _GRAND_TOTAL, _GRAND_TOTAL.replace("2.660,00", "2.680,00"))

    outcome = parse_submit_response(_panel(html))

    assert outcome.kind == "unexpected"
    assert (outcome.diagnostics.category, outcome.diagnostics.code) == ("checksum", "group_totals_mismatch")


def test_totals_identity_is_still_required_on_a_partial_page():
    html = _replace_once(_success_with_unknown_aydin(), _GRAND_TOTAL, _GRAND_TOTAL.replace("2.660,00", "2.700,00"))

    outcome = parse_submit_response(_panel(html))

    assert outcome.kind == "unexpected"
    assert (outcome.diagnostics.category, outcome.diagnostics.code) == ("checksum", "totals_mismatch")


def test_missing_totals_on_a_partial_page_are_unexpected():
    html = _replace_once(_success_with_unknown_aydin(), 'id="lblGenelCezaToplam"', 'id="lblGenelCezaToplamRenamed"')

    outcome = parse_submit_response(_panel(html))

    assert outcome.kind == "unexpected"
    assert (outcome.diagnostics.code, outcome.diagnostics.detail) == ("totals_missing", "grand_total")


def test_no_records_label_with_extra_text_is_unchecked_not_no_debt():
    """Раньше "Kayıt yok" где угодно в label давало "без долга" — то есть
    ложный ноль по оператору, если после него шла ошибка."""
    label = _AYDIN_LABEL + " Sorgulama yapılamadı"
    outcome = parse_submit_response(_panel(_replace_once(_NO_DEBT_HTML, _AYDIN_LABEL, label)))

    assert outcome.kind == "partial"
    assert outcome.unchecked_operators == ("aydin_denizli",)
    assert "keywords=kayit+sorgu+yapilamadi+yok" in outcome.diagnostics.detail


def test_hostile_unknown_label_leaks_nothing_into_diagnostics():
    hostile = f"FERNAS (AYDIN): <b>Hata</b> '{_PLATE}' ? 12345 <script>x</script>"
    outcome = parse_submit_response(_panel(_replace_once(_NO_DEBT_HTML, _AYDIN_LABEL, hostile)))

    assert outcome.kind == "partial"
    _assert_safe_diagnostics(outcome)
    assert "digits=" in outcome.diagnostics.detail
    assert "script" not in outcome.diagnostics.detail


def test_unknown_kgm_label_keeps_yid_sections_and_skips_only_kgm_group_check():
    """Бывший кейс "section-label-unknown" из test_kgm_parser.py: раньше
    unexpected на всю страницу."""
    html = _replace_once(_SUCCESS_HTML, _KGM_LABEL, "KARAYOLLARI GENEL MÜDÜRLÜĞÜ: iki kayıt")

    outcome = parse_submit_response(_panel(html))

    assert outcome.kind == "partial"
    assert outcome.unchecked_operators == ("kgm",)
    assert [op.operator_key for op in outcome.operators] == ["avrasya"]
    assert outcome.diagnostics.detail.startswith("kgm chars=")
    assert outcome.diagnostics.detail.endswith(" remainder=yes")
    assert outcome.message == "unchecked operator sections: kgm: 'KARAYOLLARI GENEL MÜDÜRLÜĞÜ: iki kayıt'"
    _assert_safe_diagnostics(outcome)


def test_rows_of_an_unchecked_section_are_never_read():
    html = _replace_once(_SUCCESS_HTML, "(AVRASYA TÜNELİ): 3 kayıt", "(AVRASYA TÜNELİ): Sorgulama yapılamadı")

    outcome = parse_submit_response(_panel(html))

    assert outcome.kind == "partial"
    assert outcome.unchecked_operators == ("avrasya",)
    assert [op.operator_key for op in outcome.operators] == ["kgm"]
    assert sum(len(op.items) for op in outcome.operators) == 2


def test_all_sections_unknown_is_unexpected_not_partial():
    html = re.sub(r"Kayıt yok\.", "Sorgulama yapılamadı", _NO_DEBT_HTML)
    assert html.count("Sorgulama yapılamadı") == 10

    outcome = parse_submit_response(_panel(html))

    assert outcome.kind == "unexpected"
    assert (outcome.diagnostics.category, outcome.diagnostics.code) == ("result_structure", "section_label_unrecognized")
    assert [part.split(" ")[0] for part in outcome.diagnostics.detail.split(", ")] == [
        "kgm", "avrasya", "otoyol", "yss", "avrupa_otoyolu", "anadolu_otoyolu", "ika_otoyolu",
        "ankara_nigde_otoyolu", "canakkale_koprusu", "aydin_denizli",
    ]
    assert outcome.operators == ()
    assert outcome.grand_total is None
    _assert_safe_diagnostics(outcome)


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        (_KGM_LABEL, "KARAYOLLARI GENEL MÜDÜRLÜĞÜ: 3 kayıt", "section_count_mismatch"),
        (_KGM_LABEL, "KARAYOLLARI GENEL MÜDÜRLÜĞÜ: Kayıt yok.", "section_label_contradiction"),
        ('id="lblKgm"', 'id="lblKgmRenamed"', "section_missing"),
    ],
)
def test_structural_errors_in_other_sections_still_make_the_page_unexpected(old, new, code):
    outcome = parse_submit_response(_panel(_replace_once(_success_with_unknown_aydin(), old, new)))

    assert outcome.kind == "unexpected"
    assert outcome.diagnostics.code == code
    assert outcome.unchecked_operators == ()


def test_complete_pages_are_unchanged():
    assert parse_submit_response(_panel(_SUCCESS_HTML)).kind == "has_debt"
    assert parse_submit_response(_panel(_NO_DEBT_HTML)).kind == "no_debt"
    assert parse_submit_response(_panel(_SUCCESS_HTML)).unchecked_operators == ()


class _FakeSession:
    def __init__(self, response_text: str):
        self._response_text = response_text
        self.last_submit_seconds: float | None = None

    async def submit(self, *, plate: str, captcha_code: str) -> str:
        self.last_submit_seconds = 10.17
        return self._response_text


async def test_provider_logs_one_safe_line_for_partial_and_returns_outcome_unchanged(caplog):
    response = _panel(_success_with_unknown_aydin())
    provider = KgmProvider(_FakeSession(response))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        outcome = await provider.submit(plate=_PLATE, captcha_code=_CAPTCHA)

    assert outcome == parse_submit_response(response)
    warnings = [r.getMessage() for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING]
    assert warnings == [
        "KGM partial response: category=result_structure code=section_label_unrecognized "
        f"detail={outcome.diagnostics.detail} submit_seconds=10.17 response_chars={len(response)}"
    ]
    for forbidden in (_PLATE, _CAPTCHA, "FERNAS", "Sorgulama", "AYDIN"):
        assert forbidden not in warnings[0]
