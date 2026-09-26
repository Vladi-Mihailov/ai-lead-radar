"""Тонкая обёртка над VideosPoliceGeSession (см. protocol_check_session.py) —
превращает HTML/исключения в typed ProtocolCheckResult (см.
protocol_check_models.py), тот же трёхслойный приём (session/provider),
что и у reader/fines/police_ge_provider.py, НО typed-результат вместо
исключения на выходе (см. задачу п.7: "Provider должен возвращать typed
result... никогда не превращать timeout/HTTP error/session error в
NOT_FOUND") — поэтому ВСЕ сетевые/сессионные ошибки ловятся ИМЕННО здесь и
превращаются в ProtocolCheckStatus.ERROR, ConversationController никогда
не видит ни ProtocolCheckSessionError, ни httpx-исключение напрямую."""

import httpx

from reader.public_bot.protocol_check_models import (
    ProtocolCheckResult,
    ProtocolCheckSessionError,
    ProtocolCheckStatus,
)
from reader.public_bot.protocol_check_session import VideosPoliceGeSession

# Точная, подтверждённая READ-ONLY диагностикой строка (см. задачу п.7:
# "NOT_FOUND только при точном подтверждённом marker") — НЕ regex/fuzzy
# match, ровно эта подстрока.
_NOT_FOUND_MARKER = "Administrative violations have not been found."

# См. задачу "Проверить протокол" — protocolNo normalization — ТОЧНАЯ
# таблица из videos.police.ge/index.js (подтверждена READ-ONLY чтением
# официального JS + ручным browser-тестом: eq948218 -> ექ948218), НЕ
# общая грузинская транслитерация и НЕ догадка. На реальном сайте
# срабатывает form.protocolNo.onblur — который ВСЕГДА выполняет ИМЕННО
# этот посимвольный regex-replace (form.protocolNo.onkeypress тоже
# существует, но onblur — единственный путь, который гарантированно
# отрабатывает независимо от того, как значение попало в поле —
# keypressWorks там установлена в true через var ВНУТРИ обработчика
# keypress, что создаёт новую, локальную переменную и никогда не
# трогает внешнюю — поэтому внешний keypressWorks остаётся false
# навсегда, и `if (!keypressWorks)` в onblur всегда истинно). Ключи —
# ИМЕННО те регистры, что в оригинале (строчные для большинства букв,
# 7 отдельных заглавных — T/J/R/S/C/Z/W — со своим собственным
# соответствием) — НЕ lower()/upper() до маппинга, НЕ придуманные
# заглавные варианты для остальных букв (см. задачу: "Do NOT invent
# mappings for uppercase letters not present in official JS").
_PROTOCOL_NO_LATIN_TO_GEORGIAN: dict[str, str] = {
    "a": "ა", "b": "ბ", "g": "გ", "d": "დ", "e": "ე", "v": "ვ", "z": "ზ", "T": "თ",
    "i": "ი", "k": "კ", "l": "ლ", "m": "მ", "n": "ნ", "o": "ო", "p": "პ", "J": "ჟ",
    "r": "რ", "s": "ს", "t": "ტ", "u": "უ", "f": "ფ", "q": "ქ", "R": "ღ", "y": "ყ",
    "S": "შ", "C": "ჩ", "c": "ც", "Z": "ძ", "w": "წ", "W": "ჭ", "x": "ხ", "j": "ჯ", "h": "ჰ",
}


def normalize_protocol_no(raw: str) -> str:
    """Посимвольно повторяет videos.police.ge/index.js's onblur fallback
    для поля protocolNo (см. модульный комментарий выше про
    _PROTOCOL_NO_LATIN_TO_GEORGIAN) — символ есть в таблице (тем же
    регистром, что введён) -> заменяется на грузинскую букву; иначе
    (цифры, уже грузинский текст, пунктуация, регистры без
    соответствия) — остаётся БЕЗ ИЗМЕНЕНИЙ. ТОЛЬКО protocolNo — НЕ
    применяется к personalNo/documentNo/vehicleNo2 (см. index.js — у
    них своя, отдельная, чисто upper()-семантика, см.
    _handle_protocol_check_*_input в conversation.py, не меняется)."""
    return "".join(_PROTOCOL_NO_LATIN_TO_GEORGIAN.get(ch, ch) for ch in raw)


class ProtocolCheckProvider:
    def __init__(self, session: VideosPoliceGeSession):
        self._session = session

    async def check_vehicle(self, *, car_number: str, document_no: str) -> ProtocolCheckResult:
        return await self._check({"vehicleNo2": car_number, "documentNo": document_no})

    async def check_protocol(self, *, protocol_no: str, personal_no: str) -> ProtocolCheckResult:
        return await self._check({"protocolNo": normalize_protocol_no(protocol_no), "personalNo": personal_no})

    async def _check(self, fields: dict[str, str]) -> ProtocolCheckResult:
        try:
            html = await self._session.search(fields)
        except (ProtocolCheckSessionError, httpx.HTTPError):
            # httpx.HTTPError покрывает и сетевые/timeout-ошибки (RequestError,
            # включая TimeoutException), и HTTP-статус-ошибки (HTTPStatusError) —
            # ни то, ни другое не должно превратиться в NOT_FOUND (см. задачу
            # п.7).
            return ProtocolCheckResult(status=ProtocolCheckStatus.ERROR)
        return _parse_result(html)


def _parse_result(html: str) -> ProtocolCheckResult:
    if _NOT_FOUND_MARKER in html:
        return ProtocolCheckResult(status=ProtocolCheckStatus.NOT_FOUND)
    # FOUND-структура ответа НЕ наблюдалась ни разу (см. задачу п.8) —
    # намеренно не угадываем selectors/поля; любой другой технически
    # успешный HTML (сессия не истекла, NOT_FOUND-маркера нет) — UNKNOWN
    # (UNPARSED_SUCCESS), а не ошибка и не "не найдено".
    return ProtocolCheckResult(status=ProtocolCheckStatus.UNKNOWN)
