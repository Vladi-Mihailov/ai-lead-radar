"""Тесты ProtocolCheckProvider — typed-результат поверх сессии (см. задачу
"Проверить протокол" п.7/п.8). session — лёгкий фейк (тот же приём, что и
tests/test_police_ge_provider.py — без mocking-библиотек)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx

from reader.public_bot.protocol_check_models import (
    ProtocolCheckSessionError,
    ProtocolCheckStatus,
)
from reader.public_bot.protocol_check_provider import (
    _PROTOCOL_NO_LATIN_TO_GEORGIAN,
    ProtocolCheckProvider,
    normalize_protocol_no,
)


class _FakeSession:
    def __init__(self, *, html: str | None = None, exc: Exception | None = None):
        self._html = html
        self._exc = exc
        self.requested_fields: list[dict] = []

    async def search(self, fields: dict) -> str:
        self.requested_fields.append(fields)
        if self._exc is not None:
            raise self._exc
        return self._html


async def test_not_found_marker_returns_not_found_status():
    session = _FakeSession(html='<div class="value warning">Administrative violations have not been found.</div>')
    provider = ProtocolCheckProvider(session)

    result = await provider.check_vehicle(car_number="AA001AA", document_no="DOC1")

    assert result.status == ProtocolCheckStatus.NOT_FOUND


async def test_unrecognized_successful_html_returns_unknown_status():
    """См. задачу п.8: "не придумывай CSS selectors для FOUND" — любой
    HTML без известного NOT_FOUND-маркера — UNKNOWN, не ошибка и не
    FOUND."""
    session = _FakeSession(html="<html><body>какая-то другая страница</body></html>")
    provider = ProtocolCheckProvider(session)

    result = await provider.check_vehicle(car_number="AA001AA", document_no="DOC1")

    assert result.status == ProtocolCheckStatus.UNKNOWN


async def test_session_error_becomes_error_status_not_not_found():
    session = _FakeSession(exc=ProtocolCheckSessionError("сессия истекла"))
    provider = ProtocolCheckProvider(session)

    result = await provider.check_protocol(protocol_no="PR1", personal_no="ID1")

    assert result.status == ProtocolCheckStatus.ERROR


async def test_httpx_timeout_becomes_error_status_not_not_found():
    session = _FakeSession(exc=httpx.TimeoutException("timed out"))
    provider = ProtocolCheckProvider(session)

    result = await provider.check_vehicle(car_number="AA001AA", document_no="DOC1")

    assert result.status == ProtocolCheckStatus.ERROR


async def test_httpx_status_error_becomes_error_status_not_not_found():
    request = httpx.Request("POST", "https://videos.police.ge/submit-index.php")
    response = httpx.Response(500, request=request)
    exc = httpx.HTTPStatusError("server error", request=request, response=response)
    session = _FakeSession(exc=exc)
    provider = ProtocolCheckProvider(session)

    result = await provider.check_vehicle(car_number="AA001AA", document_no="DOC1")

    assert result.status == ProtocolCheckStatus.ERROR


async def test_check_vehicle_sends_correct_field_names():
    session = _FakeSession(html="not found: Administrative violations have not been found.")
    provider = ProtocolCheckProvider(session)

    await provider.check_vehicle(car_number="AA001AA", document_no="DOC1")

    assert session.requested_fields == [{"vehicleNo2": "AA001AA", "documentNo": "DOC1"}]


async def test_check_protocol_sends_correct_field_names():
    """protocol_no here is digits-only (no Latin letters) — deliberately
    unaffected by normalize_protocol_no() — this test is about field
    NAMES, not normalization (see the dedicated normalization tests
    below for that)."""
    session = _FakeSession(html="Administrative violations have not been found.")
    provider = ProtocolCheckProvider(session)

    await provider.check_protocol(protocol_no="9001", personal_no="ID1")

    assert session.requested_fields == [{"protocolNo": "9001", "personalNo": "ID1"}]


async def test_check_protocol_sends_normalized_protocol_no():
    """См. задачу "Проверить протокол" protocolNo normalization —
    check_protocol() должен нормализовать protocol_no (см.
    normalize_protocol_no) ПЕРЕД отправкой, personal_no не трогается."""
    session = _FakeSession(html="Administrative violations have not been found.")
    provider = ProtocolCheckProvider(session)

    await provider.check_protocol(protocol_no="eq948218", personal_no="9931694846")

    assert session.requested_fields == [{"protocolNo": "ექ948218", "personalNo": "9931694846"}]


# ---- normalize_protocol_no() — см. задачу "Проверить протокол"
# protocolNo normalization, mapping table подтверждена READ-ONLY чтением
# videos.police.ge/index.js + ручным browser-тестом (eq948218 -> ექ948218,
# "Administrative violations have not been found.") ----


def test_normalize_protocol_no_confirmed_real_example():
    assert normalize_protocol_no("eq948218") == "ექ948218"


def test_normalize_protocol_no_lowercase_t():
    assert normalize_protocol_no("t123") == "ტ123"


def test_normalize_protocol_no_uppercase_T_is_a_different_letter():
    """Доказывает регистро-чувствительность маппинга — 't' и 'T'
    сознательно дают РАЗНЫЕ грузинские буквы (см. официальный JS,
    задача: "This explicitly proves case-sensitive mapping")."""
    assert normalize_protocol_no("T123") == "თ123"
    assert normalize_protocol_no("t123") != normalize_protocol_no("T123")


def test_normalize_protocol_no_already_georgian_unchanged():
    assert normalize_protocol_no("ექ948218") == "ექ948218"


def test_normalize_protocol_no_digits_only_unchanged():
    assert normalize_protocol_no("948218") == "948218"


def test_normalize_protocol_no_unmapped_characters_pass_through_unchanged():
    """Знаки препинания и заглавные буквы БЕЗ отдельной записи в таблице
    (например 'E'/'Q' — только строчные 'e'/'q' присутствуют) остаются
    без изменений, как и в оригинальном JS (regex /[a-z]/gi матчит по
    регистру-независимому шаблону, но лукап replacement[a] — по
    точному регистру найденного символа, `|| a` — safe fallback)."""
    assert normalize_protocol_no("EQ948218") == "EQ948218"
    assert normalize_protocol_no("eq-948218") == "ექ-948218"
    assert normalize_protocol_no("") == ""
    assert normalize_protocol_no("A") == "A"  # 'A' отсутствует в таблице (только 'a')


def test_normalize_protocol_no_does_not_lowercase_or_uppercase_before_mapping():
    """См. задачу: "Do NOT lowercase before mapping / Do NOT uppercase
    before mapping" — 'a' и 'A' дают РАЗНЫЙ результат (у 'A' нет своей
    записи вовсе, у 'a' есть)."""
    assert normalize_protocol_no("a") == "ა"
    assert normalize_protocol_no("A") == "A"


def test_normalize_protocol_no_matches_every_entry_of_official_mapping_table():
    """Проверяет КАЖДУЮ запись официальной таблицы по отдельности — наш
    Python-маппинг не может незаметно разойтись с оригинальным JS."""
    for latin, georgian in _PROTOCOL_NO_LATIN_TO_GEORGIAN.items():
        assert normalize_protocol_no(latin) == georgian


def test_normalize_protocol_no_mapping_table_matches_official_js_exactly():
    """Точная, подтверждённая таблица из videos.police.ge/index.js (см.
    задачу) — 33 записи, НЕ общая грузинская транслитерация."""
    expected = {
        "a": "ა", "b": "ბ", "g": "გ", "d": "დ", "e": "ე", "v": "ვ", "z": "ზ", "T": "თ",
        "i": "ი", "k": "კ", "l": "ლ", "m": "მ", "n": "ნ", "o": "ო", "p": "პ", "J": "ჟ",
        "r": "რ", "s": "ს", "t": "ტ", "u": "უ", "f": "ფ", "q": "ქ", "R": "ღ", "y": "ყ",
        "S": "შ", "C": "ჩ", "c": "ც", "Z": "ძ", "w": "წ", "W": "ჭ", "x": "ხ", "j": "ჯ", "h": "ჰ",
    }
    assert _PROTOCOL_NO_LATIN_TO_GEORGIAN == expected
