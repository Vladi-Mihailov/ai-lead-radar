"""Разбор сырого JSON-ответа payment/verification/with-mys-borc-list.

ВАЖНО (см. design report Stage 2 — задача явно требует "не гадать схему"):
уверенно можно утверждать только про ОБЩИЙ envelope апигейта ("messages":
[{"type", "text"}, ...]) — он вычитан из реального кода фронтенда (см.
design report Stage 1, модуль 14317 общей fetch-обёртки): ЛЮБОЙ ответ
ЛЮБОГО эндпоинта этого шлюза проходит через один и тот же разбор
`messages||[response]`, независимо от HTTP-статуса.

Первый реальный live-запуск (см. design report Stage 2, обновление после
ручного теста) показал, что GIB использует type="ERROR" НЕ только для
реальных ошибок/отказов — "штрафов/долга не найдено" тоже приходит как
type="ERROR":

    {"messages": [{"type": "ERROR",
                   "text": "Girdiğiniz plakaya ait borç bulunamadı."}],
     "data": null}

("borç bulunamadı" = "долг не найден"). Поэтому исходная логика ("любое
ERROR/WARNING -> rejected") была НЕВЕРНОЙ и здесь исправлена: только ЭТОТ
конкретный, реально увиденный текст сообщения классифицируется как
"no_debt". Любой ДРУГОЙ, ещё не увиденный вживую ERROR/WARNING -> НЕ
"rejected" (это была бы новая догадка того же рода, которую только что
пришлось исправлять) и НЕ "no_debt" — это "unexpected": мы действительно
не знаем, что означает текст, которого ещё не видели, и это должно
приводить к "остановиться и разобраться", а не к тихому предположению
в любую сторону.

Второй live-запуск (см. design report Stage 2, второе обновление) —
заведомо неверный код — дал ВТОРОЙ реально увиденный текст, тоже
type="ERROR":

    {"messages": [{"type": "ERROR",
                   "text": "Güvenlik kodunu yanlış girdiniz. Lütfen "
                            "kontrol ederek tekrar deneyiniz."}],
     "data": null}

("вы ввели неверный код безопасности, пожалуйста, проверьте и попробуйте
снова") — ЭТО и есть реальный текст отклонённой CAPTCHA, поэтому "rejected"
теперь тоже сопоставляется по конкретному, реально увиденному тексту, тем
же способом, что и _NO_DEBT_MESSAGE_TEXT. Любой ТРЕТИЙ, ещё не увиденный
ERROR/WARNING по-прежнему остаётся "unexpected", а не обобщается ни в
"rejected", ни в "no_debt" — см. задачу: "do not generalize other unknown
ERROR messages to rejected".

Третий live-запуск (см. design report Stage 4/live-test — принятая
CAPTCHA, реальный штраф) показал, что УСПЕШНЫЙ ответ с найденным долгом
использует СОВСЕМ ДРУГУЮ, плоскую форму — БЕЗ обёртки "data" вовсе и БЕЗ
envelope "messages": [...] (там "messages" — буквально null, не список):

    {"BORCLAR": [{"KK_ACIKLAMA": "...", "KK_BORC": "3000.00",
                  "KK_TUTANAKNO": "MC03475903", "KK_PLAKA": "E911EE95",
                  ...}, ...],
     "BORC_SORGU_TARIHI": "20260914",
     "SPOS_ISLEM_TIPI": "7",
     "messages": null,
     "pageDetail": null}

До этого фикса такой ответ падал в "unexpected" ИМЕННО через ветку
'"data" not in payload' (см. ниже) — не потому что форма была неопознана
как ошибка, а потому что успешный ответ с долгом ПРОСТО не имеет "data"
на верхнем уровне вовсе, в отличие от изначального (недоказанного, см.
Stage 1) предположения об едином envelope апигейта для ВСЕХ эндпоинтов.
"BORCLAR" ("долги" по-турецки) — реальное, увиденное вживую имя поля;
непустой список -> "has_debt", пустой -> "no_debt" (симметрично "data"
ниже) — raw_data отдаёт вызывающему коду весь payload целиком, как и у
"data"-ветки ниже. Отдельные записи ДОПОЛНИТЕЛЬНО парсятся в типизированные
GibFineRecord (см. reader/turkey_bot/gib/fine_parser.py про полный аудит
полей и то, какие из них подтверждены/неоднозначны/платёжные-и-никогда-
не-парсятся) и кладутся в GibSubmitOutcome.fines — только для kind
"has_debt", "data"-ветка ниже fines не заполняет (её реальная схема так
и не была увидена).
"""

from reader.turkey_bot.gib.fine_parser import parse_fine_records
from reader.turkey_bot.gib.models import GibMessage, GibSubmitOutcome

_BLOCKING_MESSAGE_TYPES = frozenset({"ERROR", "WARNING"})

# Оба текста ниже — РЕАЛЬНО наблюдавшиеся (live, см. design report Stage 2),
# не догадки. Сравнение — по strip(), без изменения регистра (турецкий
# текст, регистр значим для İ/I/ı/i).
_NO_DEBT_MESSAGE_TEXT = "Girdiğiniz plakaya ait borç bulunamadı."
_CAPTCHA_REJECTED_MESSAGE_TEXT = (
    "Güvenlik kodunu yanlış girdiniz. Lütfen kontrol ederek tekrar deneyiniz."
)


def _extract_messages(payload: dict) -> tuple[GibMessage, ...]:
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        return ()
    return tuple(
        GibMessage(type=item.get("type"), text=item.get("text"))
        for item in raw_messages
        if isinstance(item, dict)
    )


def _has_message_text(messages: tuple[GibMessage, ...], known_text: str) -> bool:
    return any(
        message.text is not None and message.text.strip() == known_text
        for message in messages
    )


def _is_empty(data: object) -> bool:
    if data is None:
        return True
    if isinstance(data, (list, dict, str)):
        return len(data) == 0
    # Скаляр (число/bool/...) под "data" — ни разу не наблюдался; не пустой
    # по определению, но это тоже кандидат на "unexpected" в будущем, если
    # реальный ответ окажется таким (см. докстрок модуля).
    return False


def parse_submit_response(payload: object) -> GibSubmitOutcome:
    if not isinstance(payload, dict):
        return GibSubmitOutcome(kind="unexpected", messages=(), raw_data=payload)

    messages = _extract_messages(payload)

    if _has_message_text(messages, _NO_DEBT_MESSAGE_TEXT):
        return GibSubmitOutcome(kind="no_debt", messages=messages, raw_data=payload.get("data"))

    if _has_message_text(messages, _CAPTCHA_REJECTED_MESSAGE_TEXT):
        return GibSubmitOutcome(kind="rejected", messages=messages, raw_data=payload.get("data"))

    if any(message.type in _BLOCKING_MESSAGE_TYPES for message in messages):
        # ERROR/WARNING, но НЕ один из двух известных текстов выше — см.
        # докстрок модуля: реальный смысл неизвестен — "unexpected", не
        # догадка (см. задачу: "do not generalize other unknown ERROR
        # messages to rejected").
        return GibSubmitOutcome(kind="unexpected", messages=messages, raw_data=payload.get("data"))

    if "BORCLAR" in payload:
        # Реальная, плоская форма успешного ответа с результатом проверки
        # долга (см. докстрок модуля, третий live-запуск) — БЕЗ обёртки
        # "data". Проверяется ДО ветки "data" ниже: у этой формы "data"
        # никогда нет, а "BORCLAR" — самый надёжный (реально увиденный)
        # признак именно этого случая.
        borclar = payload["BORCLAR"]
        kind = "no_debt" if _is_empty(borclar) else "has_debt"
        fines = parse_fine_records(borclar) if kind == "has_debt" else ()
        return GibSubmitOutcome(kind=kind, messages=messages, raw_data=payload, fines=fines)

    if "data" not in payload:
        # Ни известного сообщения, ни "BORCLAR", ни "data" вовсе — форма,
        # которую мы никогда не видели (см. докстрок модуля) —
        # "unexpected", а не молчаливое предположение "штрафов нет".
        return GibSubmitOutcome(kind="unexpected", messages=messages, raw_data=payload)

    data = payload["data"]
    kind = "no_debt" if _is_empty(data) else "has_debt"
    return GibSubmitOutcome(kind=kind, messages=messages, raw_data=data)
