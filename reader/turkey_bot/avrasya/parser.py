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

HTTP 200 (успех, вероятно "Subcriptions" с найденными проездами) НИ РАЗУ
не был получен живым запросом — остаётся "unexpected" здесь до тех пор,
пока reader/turkey_bot/avrasya/manual_test.py не захватит реальный пример
(см. design report Stage 1, раздел "Manual live test(s) you need to
perform").

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

from reader.turkey_bot.avrasya.models import AvrasyaMessage, AvrasyaSubmitOutcome

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
