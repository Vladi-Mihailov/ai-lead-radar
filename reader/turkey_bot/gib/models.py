"""Минимальные модели GIB-слоя (dijital.gib.gov.tr) — см. design report
Stage 1/2. Никакого мониторинга/подписок здесь нет и не будет: Турция —
одноразовая проверка (см. задачу), эти модели описывают ровно один цикл
"получить CAPTCHA -> отправить код -> получить исход".
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CaptchaChallenge:
    """Результат start()/refresh_captcha() — image_id это то же самое, что
    GIB называет "cid" в ответе getnewcaptcha и "imageId" в запросе
    with-mys-borc-list (см. design report Stage 1: подтверждено чтением
    реального кода фронтенда, не догадка)."""

    image_id: str
    image_png: bytes


# Намеренно ГРУБАЯ классификация (см. reader/turkey_bot/gib/parser.py) —
# полей под конкретные штрафы/задолженности здесь нет и не должно быть, пока
# реальный "has_debt"-ответ не будет увиден через manual_test.py (см. design
# report Stage 2). "no_debt" уже подтверждён живым запуском — ВАЖНО: GIB
# присылает его как type="ERROR" (текст "Girdiğiniz plakaya ait borç
# bulunamadı." = "долг не найден"), поэтому type="ERROR" сам по себе НЕ
# означает "rejected" (см. parser.py про исправление этой ошибки).
GibSubmitKind = Literal["no_debt", "has_debt", "rejected", "unexpected"]


@dataclass(frozen=True)
class GibMessage:
    """Один элемент "messages" в общем envelope апигейта GIB (см. design
    report Stage 1: тот же envelope используется ЛЮБЫМ эндпоинтом этого
    шлюза, включая getnewcaptcha, — подтверждено чтением кода общей
    fetch-обёртки фронтенда, модуль 14317)."""

    type: str | None
    text: str | None


@dataclass(frozen=True)
class GibSubmitOutcome:
    """Результат parser.parse_submit_response().

    kind:
      - "no_debt"    — либо "data" пуст, либо GIB вернул конкретный,
        реально увиденный текст "Girdiğiniz plakaya ait borç bulunamadı."
        (даже с type="ERROR" — см. design report Stage 2: первый live-
        запуск показал, что GIB использует ERROR и для этого легитимного
        исхода, не только для настоящих ошибок);
      - "has_debt"    — либо "data" непуст (изначальное, недоказанное
        предположение об envelope апигейта — так и не подтверждено вживую),
        либо (реально увиденная, см. parser.py, третий live-запуск) плоская
        форма с непустым "BORCLAR" — БЕЗ обёртки "data" вовсе. В обоих
        случаях конкретные поля отдельного штрафа/долга (KK_ACIKLAMA/
        KK_BORC/KK_TUTANAKNO/... для формы с "BORCLAR") сознательно НЕ
        парсятся в отдельную модель (см. design report: "smallest rule
        necessary") — raw_data отдаётся как есть вызывающему коду;
      - "rejected"    — GIB вернул конкретный, реально увиденный (второй
        live-запуск, заведомо неверный код, см. design report Stage 2)
        текст "Güvenlik kodunu yanlış girdiniz. Lütfen kontrol ederek
        tekrar deneyiniz." ("вы ввели неверный код безопасности") — тоже
        с type="ERROR". Сопоставляется по конкретному тексту, ТАК ЖЕ, как
        и "no_debt" выше — исходная идея "любой ERROR/WARNING = rejected"
        оказалась неверной один раз и НЕ обобщается снова: любой ДРУГОЙ,
        ещё не увиденный ERROR/WARNING остаётся "unexpected", а не
        "rejected" (см. задачу: "do not generalize other unknown ERROR
        messages to rejected");
      - "unexpected"  — форма ответа (или конкретный текст message) не
        совпадает ни с одним из известных случаев (см. parser.py) — сигнал
        "остановиться и разобраться" (например, попробовать заведомо
        неверный код через manual_test.py и сообщить, что реально пришло),
        а не тихо предполагать что-либо о содержимом.

    raw_data — то, что реально лежало под "data" (или весь payload целиком
    для "unexpected") — НЕ содержит cookies/captcha_code (см. задачу про
    то, что нельзя логировать) и предназначено для точечной отладки через
    manual_test.py, а не для показа конечному пользователю как есть.
    """

    kind: GibSubmitKind
    messages: tuple[GibMessage, ...]
    raw_data: object | None
