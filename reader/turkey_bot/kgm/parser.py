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
текст label), поэтому эта особенность сайта не требует исправления.

ЕДИНСТВЕННОЕ исключение из "всё или ничего" — неизвестный ТЕКСТ label
отдельного оператора (ни "N kayıt", ни "Kayıt yok."; живой ответ
2026-11-12: 9 из 10 секций разобрались, у Aydın-Denizli — нет): такой
оператор НЕ ПРОВЕРЕН (его таблица не читается), остальные секции
разбираются и сверяются как обычно, результат — kind="partial" (см.
models.py::KgmSubmitOutcome). Любое другое расхождение по-прежнему
делает недоверенной всю страницу (unexpected)."""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup
from bs4.element import Tag

from reader.turkey_bot.kgm.models import (
    KGM_GROUP_OPERATOR_KEY,
    OPERATOR_DISPLAY_NAMES,
    KgmDebtItem,
    KgmDiagnostics,
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
# "Kayıt yok." — только В КОНЦЕ label (как и "N kayıt" выше): label с
# лишним текстом после него ("Kayıt yok. <ошибка>") — неизвестный, то есть
# оператор не проверен, а не "без долга". У "Avrupa Otoyolu" перед ним нет
# двоеточия, поэтому двоеточие не требуется.
_NO_DEBT_LABEL_RE = re.compile(r"kayıt yok\.?$")

# См. задачу "KGM diagnostic logging" — фиксированный словарь для
# БЕЗОПАСНОЙ классификации свободного текста сайта (lblHata, <title>
# не-delta ответа, текст ASP.NET-исключения в delta-чанке "error"): в
# diagnostics попадают только канонические имена из ключей, сам текст —
# никогда (в нём теоретически может оказаться госномер). И текст, и
# варианты сравниваются после _fold (турецкие буквы -> ASCII).
_DIAGNOSTIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "hata": ("hata", "error"),
    "zaman_asimi": ("zaman aşımı", "zaman asimi", "timeout", "timed out"),
    "bakim": ("bakım", "bakim", "maintenance"),
    "yogunluk": ("yoğun", "yogun", "busy"),
    "tekrar_deneyin": ("tekrar", "daha sonra", "try again", "later"),
    "sistem": ("sistem", "system"),
    "servis": ("servis", "service"),
    "baglanti": ("bağlantı", "baglanti", "connection"),
    "erisim": ("erişim", "erisim", "access", "denied", "forbidden", "yetki"),
    "engel": ("engel", "block", "rejected", "reddedildi"),
    "gecersiz": ("geçersiz", "gecersiz", "invalid"),
    "guvenlik": ("güvenlik", "guvenlik", "security", "captcha"),
    "plaka": ("plaka", "plate"),
    "kayit": ("kayıt", "kayit", "record"),
    "bulunamadi": ("bulunamadı", "bulunamadi", "not found"),
    "limit": ("limit", "sınır", "sinir", "çok fazla", "cok fazla", "too many"),
    "oturum": ("oturum", "session"),
    "viewstate": ("viewstate", "view state"),
    "veritabani": ("veritabanı", "veritabani", "database", "sql"),
    "sorgu": ("sorgu",),
    "yapilamadi": ("yapılamadı", "yapilamadi", "yapılamıyor", "yapilamiyor", "alınamadı", "alinamadi"),
    "ulasilamiyor": ("ulaşılamıyor", "ulasilamiyor", "unavailable", "unreachable"),
    "yok": ("yok",),
}

# Турецкие буквы -> ASCII ПОСЛЕ casefold: "ZAMAN AŞIMI".casefold() даёт
# "zaman aşimi" (I -> i, но Ş остаётся) — без этого не совпал бы ни
# "zaman aşımı", ни "zaman asimi".
_TURKISH_TO_ASCII = str.maketrans("ışğçöüâîû", "isgcouaiu")


def _fold(text: str) -> str:
    """casefold + снятие combining dot (U+0307, его даёт турецкая "İ") +
    турецкие буквы -> ASCII. Применяется и к тексту, и к вариантам
    _DIAGNOSTIC_KEYWORDS."""
    return text.casefold().replace("̇", "").translate(_TURKISH_TO_ASCII)


_FOLDED_DIAGNOSTIC_KEYWORDS = {
    name: tuple(_fold(needle) for needle in needles) for name, needles in _DIAGNOSTIC_KEYWORDS.items()
}

# Сколько символов свободного текста анализировать (ключевые слова/цифры) и
# сколько начала НЕ-delta ответа просматривать: ответ присылает сервер,
# диагностика не должна стоить дороже самого разбора.
_DIAGNOSTIC_TEXT_SCAN_CHARS = 4096
_DIAGNOSTIC_RESPONSE_SCAN_CHARS = 65536

# Протокольные типы delta-чанков ASP.NET AJAX — в diagnostics попадают
# ТОЛЬКО они (тип чанка присылает сервер); остальные лишь считаются.
_ASP_NET_DELTA_CHUNK_TYPES = frozenset({
    "#", "updatePanel", "hiddenField", "scriptBlock", "scriptStartupBlock", "scriptDispose", "expando",
    "onSubmit", "asyncPostBackControlIDs", "postBackControlIDs", "updatePanelIDs", "childUpdatePanelIDs",
    "panelsToRefreshIDs", "asyncPostBackTimeout", "formAction", "pageTitle", "focus", "pageRedirect",
    "error", "dataItem", "dataItemJson", "arrayDeclaration",
})
# Имена контролов/страниц, которые можно назвать в diagnostics: буквы и "_"
# (у id панели — максимум две цифры в конце, как "UpdatePanel1"); в
# турецком госномере цифры есть всегда, поэтому он сюда не пройдёт.
_SAFE_IDENTIFIER_RE = re.compile(r"[A-Za-z_]{1,40}[0-9]{0,2}")
_SAFE_ASPX_PAGE_RE = re.compile(r"[A-Za-z_]{1,40}\.aspx")
# Ограниченные (не "ленивые до конца текста") шаблоны — линейны даже на
# враждебном ответе вида "<title>" * 20000.
_TITLE_RE = re.compile(r"<title\b[^>]{0,200}>([^<]{0,512})", re.I)
_LBLHATA_RE = re.compile(r'id="lblHata"[^>]{0,200}>([^<]{0,512})')


def _keywords(text: str) -> str:
    folded = _fold(text[:_DIAGNOSTIC_TEXT_SCAN_CHARS])
    found = [name for name, needles in _FOLDED_DIAGNOSTIC_KEYWORDS.items() if any(n in folded for n in needles)]
    return "+".join(found) or "-"


def _text_detail(text: str) -> str:
    """Безопасная сводка свободного текста сайта: длина, число цифр в
    начале текста (подсказка, что там могли быть номер/код — без них
    самих) и ключевые слова из _DIAGNOSTIC_KEYWORDS."""
    digits = sum(ch.isdigit() for ch in text[:_DIAGNOSTIC_TEXT_SCAN_CHARS])
    return f"chars={len(text)} digits={digits} keywords={_keywords(text)}"


def _response_shape(text: str) -> str:
    """Форма НЕ-delta ответа для diagnostics (см. parse_submit_response):
    пусто / обрезанный или битый delta-ответ / HTML-страница (тогда —
    ключевые слова её <title> и lblHata, если они есть) / что-то иное.
    Сам ответ в diagnostics не попадает; просматривается только начало."""
    head = text[:_DIAGNOSTIC_RESPONSE_SCAN_CHARS].lstrip().lstrip("﻿").lstrip()
    if not head:
        return f"chars={len(text)} shape=empty"
    if re.match(r"\d{1,9}\|", head):
        return f"chars={len(text)} shape=delta_like"
    if not head.startswith("<"):
        return f"chars={len(text)} shape=other"
    title = _TITLE_RE.search(head)
    hata = _LBLHATA_RE.search(head)
    title_keywords = _keywords(title.group(1)) if title else "-"
    hata_keywords = _keywords(hata.group(1)) if hata and hata.group(1).strip() else "-"
    return f"chars={len(text)} shape=html title_keywords={title_keywords} lblhata_keywords={hata_keywords}"


def _unchecked_detail(unchecked: list[tuple[str, str]]) -> str:
    """detail для непроверенных секций: operator_key и безопасная сводка
    текста его label (см. _text_detail) — сам текст не пишется."""
    return ", ".join(f"{operator_key} {_text_detail(label_text)}" for operator_key, label_text in unchecked)


def _unexpected(message: str, *, category: str, code: str, detail: str = "") -> KgmSubmitOutcome:
    """message — прежняя короткая причина (может нести текст сайта, в лог
    не пишется); category/code/detail — безопасная диагностика, см.
    models.py::KgmDiagnostics. На kind == "unexpected" не влияет."""
    return KgmSubmitOutcome(
        kind="unexpected", message=message,
        diagnostics=KgmDiagnostics(category=category, code=code, detail=detail),
    )


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
            return _unexpected(
                f"unrecognized lblHata text: {hata_text!r}",
                category="provider_message", code="lblhata_unrecognized", detail=_text_detail(hata_text),
            )

    operator_results: list[KgmOperatorResult] = []
    # (operator_key, текст label) секций, чей label не совпал ни с одним из
    # двух реально увиденных видов — см. модуль docstring про kind="partial".
    unchecked: list[tuple[str, str]] = []

    for operator_key, (label_id, table_id, subtotal_id) in _OPERATOR_SECTIONS.items():
        label = soup.find(id=label_id)
        table = soup.find(id=table_id)
        if label is None or table is None or not isinstance(table, Tag):
            return _unexpected(
                f"missing operator section: {operator_key}",
                category="result_structure", code="section_missing", detail=operator_key,
            )

        label_text = _normalize_ws(label.get_text())
        has_debt_match = _HAS_DEBT_LABEL_RE.search(label_text)
        is_no_debt = _NO_DEBT_LABEL_RE.search(label_text.casefold()) is not None

        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if isinstance(tbody, Tag) else []

        if has_debt_match and not is_no_debt:
            declared_count = int(has_debt_match.group(1))
            if declared_count == 0 or declared_count != len(rows):
                return _unexpected(
                    f"{operator_key}: declared {declared_count} kayıt but found {len(rows)} row(s)",
                    category="result_structure", code="section_count_mismatch", detail=operator_key,
                )

            index_map = _header_index_map(table)
            if index_map is None:
                return _unexpected(
                    f"{operator_key}: unrecognized table columns",
                    category="result_structure", code="section_columns_unrecognized", detail=operator_key,
                )

            items = [
                item
                for row in rows
                if (item := _parse_row(row, index_map, operator_key=operator_key)) is not None
            ]
            if len(items) != declared_count:
                return _unexpected(
                    f"{operator_key}: {len(items)}/{declared_count} rows parsed",
                    category="result_structure", code="section_rows_unparsed", detail=operator_key,
                )

            subtotal = _parse_subtotal(soup, subtotal_id)
            items_sum = sum((item.payable_amount for item in items), Decimal(0))
            if subtotal is None:
                # Подытога нет или он не разбирается — это структура, а не
                # расхождение сумм (message — прежний, как и раньше).
                return _unexpected(
                    f"{operator_key}: subtotal does not match parsed rows",
                    category="result_structure", code="section_subtotal_unreadable", detail=operator_key,
                )
            if subtotal != items_sum:
                return _unexpected(
                    f"{operator_key}: subtotal does not match parsed rows",
                    category="checksum", code="section_subtotal_mismatch", detail=operator_key,
                )

            operator_results.append(
                KgmOperatorResult(
                    operator_key=operator_key, operator_name=OPERATOR_DISPLAY_NAMES[operator_key],
                    items=tuple(items), subtotal=subtotal,
                )
            )
        elif is_no_debt and not has_debt_match:
            if rows:
                return _unexpected(
                    f"{operator_key}: label says no records but table has rows",
                    category="result_structure", code="section_label_contradiction", detail=operator_key,
                )
        else:
            # Ни один из двух реально увиденных вживую видов label не
            # совпал (или совпали оба сразу — не должно происходить) — не
            # гадаем, есть ли у оператора долг: секция НЕ ПРОВЕРЕНА, её
            # таблица не читается вовсе (см. kind="partial" ниже).
            unchecked.append((operator_key, label_text))

    if len(unchecked) == len(_OPERATOR_SECTIONS):
        # Не проверено ни одной секции — это уже не частичный результат.
        first_key, first_text = unchecked[0]
        return _unexpected(
            f"{first_key}: unrecognized label text: {first_text!r}",
            category="result_structure", code="section_label_unrecognized", detail=_unchecked_detail(unchecked),
        )

    kgm_total = _parse_subtotal(soup, _KGM_TOTAL_ID)
    yid_total = _parse_subtotal(soup, _YID_TOTAL_ID)
    grand_total = _parse_subtotal(soup, _GRAND_TOTAL_ID)
    if kgm_total is None or yid_total is None or grand_total is None:
        missing = [
            name for name, value in (("kgm_total", kgm_total), ("yid_total", yid_total), ("grand_total", grand_total))
            if value is None
        ]
        return _unexpected(
            "missing grand totals",
            category="result_structure", code="totals_missing", detail="+".join(missing),
        )

    if kgm_total + yid_total != grand_total:
        return _unexpected(
            "kgm_total + yid_total != grand_total", category="checksum", code="totals_mismatch",
        )

    kgm_group_sum = sum(
        (op.subtotal for op in operator_results if op.operator_key == KGM_GROUP_OPERATOR_KEY),
        Decimal(0),
    )
    yid_group_sum = sum(
        (op.subtotal for op in operator_results if op.operator_key != KGM_GROUP_OPERATOR_KEY),
        Decimal(0),
    )
    # Группа, в которой есть непроверенный оператор, сверяется только как
    # верхняя граница: его долг (если сайт его знает) может входить в итог
    # группы, а разобранные секции больше итога быть не могут.
    unchecked_keys = {operator_key for operator_key, _label_text in unchecked}
    kgm_group_complete = KGM_GROUP_OPERATOR_KEY not in unchecked_keys
    yid_group_complete = not (unchecked_keys - {KGM_GROUP_OPERATOR_KEY})
    kgm_group_ok = kgm_group_sum == kgm_total if kgm_group_complete else kgm_group_sum <= kgm_total
    yid_group_ok = yid_group_sum == yid_total if yid_group_complete else yid_group_sum <= yid_total
    if not (kgm_group_ok and yid_group_ok):
        return _unexpected(
            "operator subtotals do not match KGM/YİD totals", category="checksum", code="group_totals_mismatch",
        )

    all_items_sum = sum(
        (item.payable_amount for op in operator_results for item in op.items), Decimal(0),
    )
    items_ok = all_items_sum <= grand_total if unchecked else all_items_sum == grand_total
    if not items_ok:
        return _unexpected(
            "sum of all parsed debt items != grand total", category="checksum", code="items_sum_mismatch",
        )

    if unchecked:
        # remainder=yes — итог страницы больше суммы разобранных секций:
        # сайт, похоже, знает долг непроверенного оператора (сумма в лог не
        # пишется, только признак).
        remainder = "yes" if all_items_sum < grand_total else "no"
        return KgmSubmitOutcome(
            kind="partial", operators=tuple(operator_results),
            kgm_total=kgm_total, yid_total=yid_total, grand_total=grand_total,
            message="unchecked operator sections: " + ", ".join(
                f"{operator_key}: {label_text!r}" for operator_key, label_text in unchecked
            ),
            diagnostics=KgmDiagnostics(
                category="result_structure", code="section_label_unrecognized",
                detail=f"{_unchecked_detail(unchecked)} remainder={remainder}",
            ),
            unchecked_operators=tuple(operator_key for operator_key, _label_text in unchecked),
        )

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

    return _unexpected(
        "no operator has debt but totals are non-zero", category="checksum", code="totals_nonzero_without_debt",
    )


def parse_submit_response(response_text: str) -> KgmSubmitOutcome:
    """Верхнеуровневая точка входа (см. session.py::KgmSession.submit) —
    объединяет parse_delta_response + извлечение чанка ("updatePanel",
    "pnlSayfa") + parse_result_panel (см. докстроки выше). ЛЮБОЙ сбой на
    любом из этих шагов -> kind="unexpected" (см. модуль docstring:
    "Любой malformed/unexpected transport => unexpected" — здесь именно
    ФОРМА ответа, транспорт уже успешно отработал, см. session.py)."""
    chunks = parse_delta_response(response_text)
    if chunks is None:
        return _unexpected(
            "malformed ASP.NET AJAX delta response",
            category="response_format", code="delta_malformed", detail=_response_shape(response_text),
        )

    panel_html = chunks.get(("updatePanel", "pnlSayfa"))
    if panel_html is None:
        return _unexpected_without_result_panel(chunks)

    return parse_result_panel(panel_html)


def _unexpected_without_result_panel(chunks: dict[tuple[str, str], str]) -> KgmSubmitOutcome:
    """delta разобрался, но чанка ("updatePanel", "pnlSayfa") нет —
    различаем по ТИПАМ чанков (только известные протокольные слова ASP.NET
    AJAX, см. _ASP_NET_DELTA_CHUNK_TYPES): "error" — серверное исключение
    ASP.NET (detail — его 3-значный код и ключевые слова текста, сам текст
    не пишется), "pageRedirect" — сервер перенаправил на другую страницу
    (detail — имя .aspx-страницы, только если оно похоже на имя страницы;
    сам URL не пишется). message — прежний для всех трёх."""
    message = "delta response has no updatePanel/pnlSayfa chunk"
    types = {ctype for ctype, _cid in chunks}
    if "error" in types:
        codes = sorted({
            cid for ctype, cid in chunks if ctype == "error" and cid.isascii() and cid.isdigit() and len(cid) <= 3
        })
        error_text = next(content for (ctype, _cid), content in chunks.items() if ctype == "error")
        return _unexpected(
            message, category="provider_message", code="delta_error_chunk",
            detail=f"error_codes={','.join(codes) or '-'} keywords={_keywords(error_text)}",
        )
    if "pageRedirect" in types:
        target = next(content for (ctype, _cid), content in chunks.items() if ctype == "pageRedirect")
        page = re.split(r"[?#]", target, maxsplit=1)[0].rstrip("/").rsplit("/", 1)[-1]
        return _unexpected(
            message, category="provider_message", code="delta_page_redirect",
            detail=f"target={page if _SAFE_ASPX_PAGE_RE.fullmatch(page) else 'other'}",
        )
    known = sorted(types & _ASP_NET_DELTA_CHUNK_TYPES)
    unknown = len(types - _ASP_NET_DELTA_CHUNK_TYPES)
    panels = sorted(
        cid for ctype, cid in chunks if ctype == "updatePanel" and _SAFE_IDENTIFIER_RE.fullmatch(cid)
    )[:5]
    detail = f"chunks={'+'.join(known) or '-'}"
    if unknown:
        detail += f" unknown_chunk_types={unknown}"
    if panels:
        detail += f" panels={'+'.join(panels)}"
    return _unexpected(message, category="response_format", code="delta_no_result_panel", detail=detail)
