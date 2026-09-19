"""Разбор ASP.NET AJAX partial-rendering ("delta") ответа Sorgulama.aspx и
HTML-фрагмента результата — см. design report "KGM investigation"
(research-only, живое исследование через real HAR fixture, plate
M295YB196) и "Реализация KGM provider" (fail-closed правила классификации
ниже).

ДВА независимых уровня разбора (см. design report: тестируются отдельно):
  1. parse_delta_response() — универсальный разбор pipe-delimited
     "<byteLength>|<type>|<id>|<content>|" конвертика (см. design report:
     формат подтверждён вживую, разобран без единого лишнего байта,
     "well-documented, generic, non-obfuscated protocol") — НЕ specific
     для KGM, просто ASP.NET AJAX UpdatePanel-протокол.
  2. parse_result_panel() — разбор ОДНОГО HTML-фрагмента (значение чанка
     ("updatePanel", "pnlSayfa")) через BeautifulSoup (см. design report
     п.3: "не полагайся на fixed column positions" — для КАЖДОЙ таблицы
     строится header-name -> column-index mapping ЗАНОВО, потому что
     порядок колонок у KGM отличается от остальных 9 таблиц, подтверждено
     вживую).

FAIL-CLOSED (см. design report п.5/п.6, задача "Реализация KGM provider"):
любое несоответствие ожидаемой структуре (отсутствующая секция,
расхождение label/таблица, расхождение контрольных сумм, неразбираемая
сумма/дата) -> kind="unexpected", НИКОГДА не "no_debt"/частичный
"has_debt" по догадке. "Общий no_debt" ТРЕБУЕТ, чтобы ВСЕ 10 секций были
буквально "Kayıt yok." И три итоговых суммы были буквально нулевыми —
ПОДТВЕРЖДЕНО вживую (design report "KGM no_debt live confirmation": реальный
ответ, plate A123AA180, все 10 секций "Kayıt yok.", все три суммы 0,00 ₺,
см. tests/fixtures/kgm_sorgulama_a123aa180_no_debt.html/test_kgm_parser.py) —
код классифицировал его как "no_debt" БЕЗ единой правки. Этот же live
fixture заодно показал, что lblKgm/lblAvrasya сохраняют CSS-класс
"mtLabelHeader_bt" (обычно означающий "есть записи") ДАЖЕ в состоянии
"Kayıt yok." — сама реализация никогда не проверяла CSS-класс (только
текст label), поэтому эта особенность сайта не требует исправления."""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup
from bs4.element import Tag

from reader.turkey_bot_test.kgm.models import (
    KGM_GROUP_OPERATOR_KEY,
    OPERATOR_DISPLAY_NAMES,
    KgmDebtItem,
    KgmOperatorResult,
    KgmSubmitOutcome,
)

# Реально увиденный вживую текст ошибки CAPTCHA (см. design report: live
# submit сегодня, session hdnegrzoozne3okfs5essrqe/vjacpd0agb24zadze3bx3d1i
# — оба wrong-answer теста дали ИМЕННО этот текст) — сравнение по strip(),
# НЕ по одному факту "lblHata непусто" с произвольным содержимым (тот же
# принцип "не гадать схему", что и в gib/parser.py::
# _CAPTCHA_REJECTED_MESSAGE_TEXT).
_CAPTCHA_REJECTED_TEXT = "metin hatası"

# key -> (label_id, table_id, subtotal_id) — см. design report п.2, ВСЕ 10
# id подтверждены вживую по реальному HAR fixture (M295YB196). Порядок —
# тот же, что и на самой странице.
_OPERATOR_SECTIONS: dict[str, tuple[str, str, str]] = {
    "kgm": ("lblKgm", "gvKgm", "lblKgmCezaToplam"),
    "avrasya": ("lblAvrasya", "gvAvrasya", "lblAvrasyaCezaToplam"),
    "otoyol": ("lblOtoyol", "gvOtoyol", "lblOtoyolCezaToplam"),
    "yss": ("lblYss", "gvYss", "lblYssCezaToplam"),
    "avrupa_otoyolu": ("lblAvrupaOtoyolu", "gvAvrupaOtoyolu", "lblAvrupaOtoyoluCezaToplam"),
    "anadolu_otoyolu": ("lblAnadoluOtoyolu", "gvAnadoluOtoyolu", "lblAnadoluOtoyoluCezaToplam"),
    "ika_otoyolu": ("lblIkaOtoyolu", "gvIkaOtoyolu", "lblIkaOtoyoluCezaToplam"),
    "ankara_nigde_otoyolu": (
        "lblAnkaraNigdeOtoyolu", "gvAnkaraNigdeOtoyolu", "lblAnkaraNigdeOtoyoluCezaToplam",
    ),
    "canakkale_koprusu": (
        "lblCanakkaleKoprusu", "gvCanakkaleKoprusu", "lblCanakkaleKoprusuCezaToplam",
    ),
    "aydin_denizli": ("lblAydinDenizli", "gvAydinDenizli", "lblAydinDenizliCezaToplam"),
}

# Контрольные суммы страницы (см. design report п.6) — lblYidToplamParaCezası
# оканчивается турецким "ı" без точки (реально увиденное вживую написание,
# см. HAR fixture) — сохраняется буквально, не "исправляется" на "i".
_KGM_TOTAL_ID = "lblKgmToplamParaCezasi"
_YID_TOTAL_ID = "lblYidToplamParaCezası"
_GRAND_TOTAL_ID = "lblGenelCezaToplam"

_REQUIRED_COLUMNS = (
    "Çıkış Tarih",
    "Giriş İstasyon",
    "Çıkış İstasyon",
    "Araç Sınıf",
    "Cezasız Son Ödeme Tarihi",
    "Geçiş Ücreti",
    "Ödenecek Tutar",
)

_HAS_DEBT_LABEL_RE = re.compile(r":\s*(\d+)\s*kayıt\s*$")


def _unexpected(message: str) -> KgmSubmitOutcome:
    return KgmSubmitOutcome(kind="unexpected", message=message)


def parse_delta_response(text: str) -> dict[tuple[str, str], str] | None:
    """Разбор ASP.NET AJAX "1|#||4|35938|updatePanel|pnlSayfa|<html>|..."
    конвертика (см. design report: подтверждено вживую, ключ формата —
    ЧИСЛО перед каждым '|' это байтовая длина СЛЕДУЮЩЕГО поля, а НЕ
    разделитель сам по себе — поэтому '|' ВНУТРИ html-контента (которых
    там много) безопасны и не ломают разбор).

    None — структура НЕ соответствует формату вообще (см. design report
    "malformed delta => unexpected" — сюда попадает и пустая строка, и
    произвольный текст/HTML без единого валидного чанка) — вызывающий код
    (parse_submit_response) превращает это в kind="unexpected", НЕ
    поднимает исключение (сетевой транспорт уже отработал успешно к этому
    моменту, см. session.py — это ошибка ФОРМЫ ответа, не транспорта)."""
    chunks: dict[tuple[str, str], str] = {}
    pos = 0
    length = len(text)
    if length == 0:
        return None

    while pos < length:
        bar1 = text.find("|", pos)
        if bar1 == -1:
            return None
        length_text = text[pos:bar1]
        if not length_text.isdigit():
            return None
        chunk_length = int(length_text)

        bar2 = text.find("|", bar1 + 1)
        if bar2 == -1:
            return None
        ctype = text[bar1 + 1:bar2]

        bar3 = text.find("|", bar2 + 1)
        if bar3 == -1:
            return None
        cid = text[bar2 + 1:bar3]

        content_start = bar3 + 1
        content_end = content_start + chunk_length
        if content_end > length or text[content_end:content_end + 1] != "|":
            return None
        content = text[content_start:content_end]

        chunks[(ctype, cid)] = content
        pos = content_end + 1

    return chunks


def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


def _clean_cell(text: str) -> str | None:
    """None — ячейка буквально пустая (см. design report: реально
    увиденное &nbsp; на строках Avrasya для Giriş İstasyon/Araç Sınıf) —
    "\\xa0" (NBSP) НЕ считается непустым текстом."""
    cleaned = text.replace("\xa0", " ").strip()
    return cleaned or None


def _parse_try_amount(text: str) -> Decimal | None:
    """"40,00 ₺"/"1.125,00 ₺" -> Decimal (см. design report п.4: турецкий
    формат — "." разделяет тысячи, "," — дробную часть). None — пустая
    ячейка (см. design report: у "Kayıt yok." операторов lbl{Key}
    CezaToplam буквально пуст, а не "0,00 ₺" — ЭТО отличается от
    "0" и не считается ошибкой) или нераспознанный формат."""
    cleaned = _clean_cell(text)
    if cleaned is None:
        return None
    cleaned = cleaned.replace("₺", "").replace("TL", "").strip()
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _header_index_map(table: Tag) -> dict[str, int] | None:
    """См. design report п.3: "не полагайся на fixed column positions" —
    строится ЗАНОВО для КАЖДОЙ таблицы (порядок колонок у KGM отличается
    от остальных 9, подтверждено вживую). None — таблица не несёт
    <thead>/<tr> вовсе, или НЕ все _REQUIRED_COLUMNS найдены среди её
    заголовков (см. design report: "malformed => unexpected" — сюда же
    попадает изменившаяся вёрстка сайта)."""
    thead = table.find("thead")
    if thead is None or not isinstance(thead, Tag):
        return None
    header_row = thead.find("tr")
    if header_row is None or not isinstance(header_row, Tag):
        return None

    index_map: dict[str, int] = {}
    for index, th in enumerate(header_row.find_all("th")):
        index_map[_normalize_ws(th.get_text())] = index

    if not all(column in index_map for column in _REQUIRED_COLUMNS):
        return None
    return index_map


def _parse_row(row: Tag, index_map: dict[str, int], *, operator_key: str) -> KgmDebtItem | None:
    """None — строка НЕ usable (см. design report: "malformed debt items
    -> unexpected", тот же принцип, что и avrasya/parser.py::
    _extract_debt_item) — date_time/base_toll/payable_amount ОБЯЗАТЕЛЬНЫ,
    entry_station/exit_station/vehicle_class/penalty_free_deadline могут
    быть None (см. models.py::KgmDebtItem)."""
    cells = row.find_all("td")

    def cell(column: str) -> str:
        return cells[index_map[column]].get_text()

    try:
        # Наивный datetime — сайт отдаёт локальное турецкое время без
        # часового пояса (см. design report), присваивать tzinfo самим
        # означало бы догадку, которую задача явно запрещает.
        date_time = datetime.strptime(  # noqa: DTZ007
            _normalize_ws(cell("Çıkış Tarih")), "%d.%m.%Y %H:%M:%S",
        )
    except (IndexError, ValueError):
        return None

    base_toll = _parse_try_amount(cell("Geçiş Ücreti"))
    payable_amount = _parse_try_amount(cell("Ödenecek Tutar"))
    if base_toll is None or payable_amount is None:
        return None

    deadline_text = _clean_cell(cell("Cezasız Son Ödeme Tarihi"))
    penalty_free_deadline: date | None = None
    if deadline_text is not None:
        try:
            penalty_free_deadline = datetime.strptime(deadline_text, "%d.%m.%Y").date()  # noqa: DTZ007
        except ValueError:
            return None

    return KgmDebtItem(
        operator=operator_key,
        date_time=date_time,
        entry_station=_clean_cell(cell("Giriş İstasyon")),
        exit_station=_clean_cell(cell("Çıkış İstasyon")),
        vehicle_class=_clean_cell(cell("Araç Sınıf")),
        base_toll=base_toll,
        payable_amount=payable_amount,
        penalty_free_deadline=penalty_free_deadline,
    )


def _parse_subtotal(soup: BeautifulSoup, element_id: str) -> Decimal | None:
    node = soup.find(id=element_id)
    if node is None:
        return None
    return _parse_try_amount(node.get_text())


def parse_result_panel(html: str) -> KgmSubmitOutcome:
    """Разбор ОДНОГО HTML-фрагмента (значение чанка ("updatePanel",
    "pnlSayfa"), см. parse_delta_response) — см. модуль docstring про
    fail-closed правила. rejected проверяется РАНЬШЕ секций (см. design
    report: подтверждено вживую — при rejected секции не несут результата
    вовсе, поэтому их разбор здесь даже не начинается)."""
    soup = BeautifulSoup(html, "html.parser")

    hata_node = soup.find(id="lblHata")
    if hata_node is not None:
        hata_text = hata_node.get_text(strip=True)
        if hata_text == _CAPTCHA_REJECTED_TEXT:
            return KgmSubmitOutcome(kind="rejected", message=hata_text)
        if hata_text:
            # Непустой lblHata, но НЕ реально увиденный текст (см. design
            # report: "не гадать схему") — тоже unexpected, не "rejected".
            return _unexpected(f"unrecognized lblHata text: {hata_text!r}")

    operator_results: list[KgmOperatorResult] = []

    for operator_key, (label_id, table_id, subtotal_id) in _OPERATOR_SECTIONS.items():
        label = soup.find(id=label_id)
        table = soup.find(id=table_id)
        if label is None or table is None or not isinstance(table, Tag):
            return _unexpected(f"missing operator section: {operator_key}")

        label_text = _normalize_ws(label.get_text())
        has_debt_match = _HAS_DEBT_LABEL_RE.search(label_text)
        is_no_debt = "kayıt yok" in label_text.casefold()

        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if isinstance(tbody, Tag) else []

        if has_debt_match and not is_no_debt:
            declared_count = int(has_debt_match.group(1))
            if declared_count == 0 or declared_count != len(rows):
                return _unexpected(
                    f"{operator_key}: declared {declared_count} kayıt but found {len(rows)} row(s)"
                )

            index_map = _header_index_map(table)
            if index_map is None:
                return _unexpected(f"{operator_key}: unrecognized table columns")

            items = [
                item
                for row in rows
                if (item := _parse_row(row, index_map, operator_key=operator_key)) is not None
            ]
            if len(items) != declared_count:
                return _unexpected(f"{operator_key}: {len(items)}/{declared_count} rows parsed")

            subtotal = _parse_subtotal(soup, subtotal_id)
            items_sum = sum((item.payable_amount for item in items), Decimal(0))
            if subtotal is None or subtotal != items_sum:
                return _unexpected(f"{operator_key}: subtotal does not match parsed rows")

            operator_results.append(
                KgmOperatorResult(
                    operator_key=operator_key, operator_name=OPERATOR_DISPLAY_NAMES[operator_key],
                    items=tuple(items), subtotal=subtotal,
                )
            )
        elif is_no_debt and not has_debt_match:
            if rows:
                return _unexpected(f"{operator_key}: label says no records but table has rows")
        else:
            # Ни один из двух реально увиденных вживую видов label не
            # совпал (или совпали оба сразу — не должно происходить) — не
            # гадаем, какой из них "правильный" (см. design report:
            # "label/table противоречат друг другу => unexpected").
            return _unexpected(f"{operator_key}: unrecognized label text: {label_text!r}")

    kgm_total = _parse_subtotal(soup, _KGM_TOTAL_ID)
    yid_total = _parse_subtotal(soup, _YID_TOTAL_ID)
    grand_total = _parse_subtotal(soup, _GRAND_TOTAL_ID)
    if kgm_total is None or yid_total is None or grand_total is None:
        return _unexpected("missing grand totals")

    if kgm_total + yid_total != grand_total:
        return _unexpected("kgm_total + yid_total != grand_total")

    kgm_group_sum = sum(
        (op.subtotal for op in operator_results if op.operator_key == KGM_GROUP_OPERATOR_KEY),
        Decimal(0),
    )
    yid_group_sum = sum(
        (op.subtotal for op in operator_results if op.operator_key != KGM_GROUP_OPERATOR_KEY),
        Decimal(0),
    )
    if kgm_group_sum != kgm_total or yid_group_sum != yid_total:
        return _unexpected("operator subtotals do not match KGM/YİD totals")

    all_items_sum = sum(
        (item.payable_amount for op in operator_results for item in op.items), Decimal(0),
    )
    if all_items_sum != grand_total:
        return _unexpected("sum of all parsed debt items != grand total")

    if operator_results:
        return KgmSubmitOutcome(
            kind="has_debt", operators=tuple(operator_results),
            kgm_total=kgm_total, yid_total=yid_total, grand_total=grand_total,
        )

    # Ни у одного оператора нет задолженности — "общий no_debt" ТОЛЬКО
    # если ВСЕ 10 секций буквально "Kayıt yok." (уже гарантировано выше —
    # иначе была бы unexpected) И суммы буквально нулевые — ПОДТВЕРЖДЕНО
    # вживую (см. модуль docstring и tests/fixtures/
    # kgm_sorgulama_a123aa180_no_debt.html).
    if kgm_total == 0 and yid_total == 0 and grand_total == 0:
        return KgmSubmitOutcome(
            kind="no_debt", kgm_total=kgm_total, yid_total=yid_total, grand_total=grand_total,
        )

    return _unexpected("no operator has debt but totals are non-zero")


def parse_submit_response(response_text: str) -> KgmSubmitOutcome:
    """Верхнеуровневая точка входа (см. session.py::KgmSession.submit) —
    объединяет parse_delta_response + извлечение чанка ("updatePanel",
    "pnlSayfa") + parse_result_panel (см. докстроки выше). ЛЮБОЙ сбой на
    любом из этих шагов -> kind="unexpected" (см. модуль docstring:
    "Любой malformed/unexpected transport => unexpected" — здесь именно
    ФОРМА ответа, транспорт уже успешно отработал, см. session.py)."""
    chunks = parse_delta_response(response_text)
    if chunks is None:
        return _unexpected("malformed ASP.NET AJAX delta response")

    panel_html = chunks.get(("updatePanel", "pnlSayfa"))
    if panel_html is None:
        return _unexpected("delta response has no updatePanel/pnlSayfa chunk")

    return parse_result_panel(panel_html)
