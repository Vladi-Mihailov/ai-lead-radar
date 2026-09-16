"""Разбор (status_code, body) ответа /api/debt/query — см. design report
Stage 1 (research-only, задача явно требует "do not guess response
semantics... only classify response states that have actually been
observed").

Единственная РЕАЛЬНО увиденная вживую (curl, дважды, design report
Stage 1) бизнес-форма — HTTP 400 с ошибкой валидации CAPTCHA:

    {"Messages": [{"PropertyName": "Captcha",
                    "ErrorMessage": "Güvenlik kodunu doğru girdiğinizden "
                                    "emin olunuz."}],
     "ValidationResultType": 2}

("убедитесь, что вы правильно ввели код безопасности") — классифицируется
как "rejected" ТОЛЬКО по этому конкретному, реально увиденному тексту
(тот же приём, что и reader/turkey_bot/gib/parser.py::
_CAPTCHA_REJECTED_MESSAGE_TEXT) — НЕ по одному факту status_code == 400
и НЕ по одному факту PropertyName == "Captcha" (другой, ещё не увиденный
ErrorMessage под тем же PropertyName — это НЕ доказанный тот же случай,
см. ниже).

HTTP 200 С ПОДТВЕРЖДЁННЫМ ENVELOPE (design report Stage 2C, production
turkey_toll_checks.id=3, 2026-09-16, plate M295YB196 — CAPTCHA принята,
реальная задолженность) ТЕПЕРЬ классифицируется как "has_debt" — см.
_is_confirmed_debt_envelope/_extract_debt_items ниже. РЕАЛЬНО увиденное
тело (сокращено):

    {"Response": "OK", "DcsResponse": "SUCCESSFUL",
     "Subcriptions": [
       {"ClientSubscriptionCustomerReferenceValue": "M295YB196",
        "DebtItems": [{"PrincipalTaxIncludedBalanceAmount": 330.0,
                        "TotalTaxIncludedBalanceAmount": 330.0,
                        "ExitDate": "2*************6",
                        "ExitStation": "A*****A",
                        "IsAuthenticate": false, ...}],
        "DebtorContactFullName": "*******",
        "ServiceFileTypeName": "EARLY_COLLECTION_FILE"},
       {"DebtItems": [{"PrincipalTaxIncludedBalanceAmount": 225.0,
                        "TotalFixedIncomeTaxIncludedBalanceAmount": 900.0,
                        "TotalTaxIncludedBalanceAmount": 1125.0, ...}],
        "ServiceFileTypeName": "COLLECTION_FILE"},
       {"DebtItems": [{"...": "тот же shape, тоже COLLECTION_FILE"}]}],
     "IsShowButton": false}

Классификация ТРЕБУЕТ ВСЕ пять условий (см. задачу: "Do not classify
arbitrary HTTP 200 responses as has_debt"):
  1. status_code == 200;
  2. body — распарсенный JSON-объект (dict);
  3. body["Response"] == "OK" (ТОЧНОЕ совпадение, реально увиденное значение);
  4. body["DcsResponse"] == "SUCCESSFUL" (ТОЧНОЕ совпадение);
  5. body["Subcriptions"] — НЕПУСТОЙ список (опечатка API — НЕ
     "Subscriptions" — сохраняется как есть, см. models.py::AvrasyaDebtItem
     докстрок и задачу: "do not silently assume Subscriptions is
     equivalent unless separately observed").
И ДОПОЛНИТЕЛЬНО должен найтись хотя бы один "usable" DebtItem (см.
_extract_debt_items) — структурно валидный envelope БЕЗ единой пригодной
записи (пустые/битые DebtItems везде) НЕ становится "has_debt" (см.
задачу: "malformed debt items -> conservative behavior / unexpected") —
это тоже "unexpected", а не тихое предположение о нулевой задолженности
ИЛИ о валидной структуре, которой на самом деле нет.

ExitDate/ExitStation/DebtorContactFullName/UniqueId — РЕАЛЬНО замаскированы
самим Avrasya в этом ответе (например, "2*************6", "A*****A",
"*******") для анонимного (неавторизованного) запроса — на КАЖДОЙ записи
"IsAuthenticate": false. Эти поля СОЗНАТЕЛЬНО не извлекаются вообще (см.
models.py::AvrasyaDebtItem) — показывать замаскированные значения как
настоящие было бы неверно, размаскировать их мы не пытаемся и не можем
(см. задачу).

HTTP 404 С ПУСТЫМ ТЕЛОМ (Stage 2A live-тесты, 3 независимых успешных
человеческих прохождения CAPTCHA — design report Stage 2A апдейт) ТЕПЕРЬ
классифицируется как "no_debt" — см. _is_genuinely_empty_body ниже.
Доказательная база (обе части обязательны, см. задачу: "do not infer
semantics from HTTP status alone"):

  1. Реальный фронтенд (см. /_assets/js/site/gecis_ihlali.js,
     controller.searchDebt -> viewer.showNotFoundView) обрабатывает jQuery
     jqXHR.responseJSON == undefined (jQuery не может распарсить как JSON
     пустое тело ответа) так: НИ ОДНА ветка с конкретным ResponseCode
     (9006/9010/.../4159) не совпадает (все они проверяют
     `response.responseJSON && ...`, что ложно при undefined) — код
     ПАДАЕТ в единственную безусловную else-ветку:
         viewer.showModal(SiteResources.DebtQuery.NoRecord.format(
             SiteResources.DebtQuery[state.queryType]));
     Это ОБЩЕЕ сообщение "запись по вашему запросу не найдена" — то самое,
     что реальный пользователь увидел бы на сайте для ЭТОЙ ТОЧНОЙ формы
     ответа (404 + непарсящееся/пустое тело), а не одна из
     специфичных-по-коду веток (site down/rate-limited-by-service/т.п.).
  2. ТРИ независимых live-запроса (design report Stage 2A: два для
     A123AA123, один для A777AA777), КАЖДЫЙ с правильно введённым
     человеком CAPTCHA-кодом (не отклонённая CAPTCHA — это отдельная,
     уже подтверждённая ветка "rejected" ниже), дали ОДИН И ТОТ ЖЕ:
     HTTP 404, body == "" (пустая строка, см. session.py:
     response.text на пустом теле). Совпадение по всем трём — не
     единичный случай.
  Для запроса о неоплаченных проездах "запись не найдена" означает
  именно "неоплаченных проездов не найдено" — "no_debt".

ЛЮБОЙ ДРУГОЙ 404 (непустое тело — JSON с ResponseCode, текст,
что угодно, кроме буквально пустой строки/None) остаётся "unexpected" —
реальная форма тела для КОНКРЕТНЫХ ResponseCode (9006 и т.п., см. JS выше)
так и не была увидена, и она явно относится к ДРУГИМ веткам того же
showNotFoundView (site-specific ограничения, не "долгов нет") — см.
задачу: "do not infer no_debt merely from HTTP 404" (это по-прежнему верно
для НЕ-пустых 404 — только пустое тело подтверждено выше).

HTTP 429 (rate limit) сюда вообще не попадает — session.py поднимает его
как AvrasyaRateLimitedError раньше, чем body/status_code доходят до этого
модуля (см. session.py::submit)."""

from decimal import Decimal

from reader.turkey_bot.avrasya.models import (
    AvrasyaDebtItem,
    AvrasyaMessage,
    AvrasyaSubmitOutcome,
)

# Реально увиденный вживую текст (см. докстрок модуля) — сравнение по
# strip(), без изменения регистра (турецкий текст, регистр значим для
# İ/I/ı/i, тот же приём, что и в gib/parser.py).
_CAPTCHA_INVALID_ERROR_MESSAGE = "Güvenlik kodunu doğru girdiğinizden emin olunuz."


def _is_genuinely_empty_body(body: object) -> bool:
    """True ТОЛЬКО для буквально пустого тела (см. докстрок модуля) —
    None (защитно — session.py на практике всегда отдаёт str или dict,
    никогда None) или пустая/из пробелов строка. ЛЮБОЙ иной объект (dict,
    непустая строка) — False, даже {} (пустой JSON-объект НЕ был реально
    увиден и НЕ считается тем же случаем, что и реально увиденное пустое
    тело)."""
    if body is None:
        return True
    return isinstance(body, str) and body.strip() == ""


def _extract_messages(payload: dict) -> tuple[AvrasyaMessage, ...]:
    raw_messages = payload.get("Messages")
    if not isinstance(raw_messages, list):
        return ()
    return tuple(
        AvrasyaMessage(
            property_name=item.get("PropertyName"), error_message=item.get("ErrorMessage"),
        )
        for item in raw_messages
        if isinstance(item, dict)
    )


def _has_captcha_invalid_message(messages: tuple[AvrasyaMessage, ...]) -> bool:
    return any(
        message.property_name == "Captcha"
        and message.error_message is not None
        and message.error_message.strip() == _CAPTCHA_INVALID_ERROR_MESSAGE
        for message in messages
    )


def _is_confirmed_debt_envelope(body: dict) -> bool:
    """См. докстрок модуля — ВСЕ ТРИ условия ниже реально увидены вживую
    одновременно (Stage 2C); отдельно от наличия хотя бы одного usable
    DebtItem (см. _extract_debt_items) — тот и другой признак ОБА
    обязательны для kind == "has_debt" (см. parse_submit_response)."""
    return (
        body.get("Response") == "OK"
        and body.get("DcsResponse") == "SUCCESSFUL"
        and isinstance(body.get("Subcriptions"), list)
        and len(body["Subcriptions"]) > 0
    )


def _parse_decimal_amount(value: object) -> Decimal | None:
    """None для отсутствующего/неподходящего типа (см. задачу: "conservative
    behavior" для битых записей) — str(value) ПЕРЕД Decimal(...) (не
    Decimal(value) напрямую) намеренно: превращает float 330.0 в "330.0" ->
    Decimal("330.0") БЕЗ артефактов двоичного float (см. задачу:
    "decimal-safe monetary handling; do not introduce floating-point
    display artifacts") — тот же приём был бы неверен для Decimal(330.0)
    напрямую (даёт длинный неточный хвост для некоторых значений)."""
    if isinstance(value, bool):  # bool — подкласс int, но не сумма денег.
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    return None


def _extract_debt_item(raw_item: object, *, service_file_type: str) -> AvrasyaDebtItem | None:
    """None — запись НЕ usable (см. докстрок модуля: "malformed debt items
    -> unexpected") — принципиально обязательны ТОЛЬКО
    PrincipalTaxIncludedBalanceAmount и TotalTaxIncludedBalanceAmount
    (реально присутствуют на КАЖДОЙ увиденной вживую записи);
    TotalFixedIncomeTaxIncludedBalanceAmount опционален (см. задачу: "when
    present") — отсутствие -> penalty_amount=None, НЕ 0 и НЕ
    total-principal (см. models.py::AvrasyaDebtItem)."""
    if not isinstance(raw_item, dict):
        return None
    principal = _parse_decimal_amount(raw_item.get("PrincipalTaxIncludedBalanceAmount"))
    total = _parse_decimal_amount(raw_item.get("TotalTaxIncludedBalanceAmount"))
    if principal is None or total is None:
        return None
    penalty = _parse_decimal_amount(raw_item.get("TotalFixedIncomeTaxIncludedBalanceAmount"))
    return AvrasyaDebtItem(
        principal_amount=principal, total_amount=total,
        service_file_type=service_file_type, penalty_amount=penalty,
    )


def _extract_debt_items(body: dict) -> tuple[AvrasyaDebtItem, ...]:
    """Проходит ВСЕ Subcriptions[].DebtItems[] (см. докстрок модуля про
    опечатку "Subcriptions") — записи, не прошедшие _extract_debt_item
    (см. выше), просто пропускаются (не прерывают разбор остальных) —
    итоговый kind остаётся "has_debt", только если получилась хотя бы
    ОДНА usable запись (см. parse_submit_response)."""
    subscriptions = body.get("Subcriptions")
    if not isinstance(subscriptions, list):
        return ()

    items: list[AvrasyaDebtItem] = []
    for subscription in subscriptions:
        if not isinstance(subscription, dict):
            continue
        service_file_type = subscription.get("ServiceFileTypeName")
        if not isinstance(service_file_type, str):
            continue
        debt_items = subscription.get("DebtItems")
        if not isinstance(debt_items, list):
            continue
        for raw_item in debt_items:
            item = _extract_debt_item(raw_item, service_file_type=service_file_type)
            if item is not None:
                items.append(item)

    return tuple(items)


def parse_submit_response(status_code: int, body: object) -> AvrasyaSubmitOutcome:
    if status_code == 400 and isinstance(body, dict):
        messages = _extract_messages(body)
        if _has_captcha_invalid_message(messages):
            return AvrasyaSubmitOutcome(
                kind="rejected", status_code=status_code, messages=messages, raw_data=body,
            )
        # 400 с ДРУГИМ (ещё не увиденным вживую) содержимым Messages —
        # НЕ обобщается в "rejected" (см. докстрок модуля и тот же принцип
        # в gib/parser.py: "do not generalize other unknown ERROR messages").
        return AvrasyaSubmitOutcome(
            kind="unexpected", status_code=status_code, messages=messages, raw_data=body,
        )

    if status_code == 200 and isinstance(body, dict) and _is_confirmed_debt_envelope(body):
        debt_items = _extract_debt_items(body)
        if debt_items:
            return AvrasyaSubmitOutcome(
                kind="has_debt", status_code=status_code, messages=(), raw_data=body,
                debt_items=debt_items,
            )
        # Envelope структурно подтверждён, но НИ ОДНОЙ usable записи (см.
        # докстрок модуля) — не становится "has_debt" по одной догадке.
        return AvrasyaSubmitOutcome(
            kind="unexpected", status_code=status_code, messages=(), raw_data=body,
        )

    if status_code == 404 and _is_genuinely_empty_body(body):
        # Реально увиденная вживую (3/3 live-теста, см. докстрок модуля)
        # форма "нет записи" -> "нет неоплаченных проездов".
        return AvrasyaSubmitOutcome(
            kind="no_debt", status_code=status_code, messages=(), raw_data=body,
        )

    # 200/непустой 404/500/любая другая форма — ни разу не увидены вживую
    # (см. докстрок модуля) — "unexpected", а не догадка в любую сторону.
    return AvrasyaSubmitOutcome(
        kind="unexpected", status_code=status_code, messages=(), raw_data=body,
    )
