"""Минимальные модели Avrasya Tüneli слоя (avrasyatuneli.com) — см. design
report Stage 1 (research-only, живое исследование через curl/чтение
реального JS-бандла /_assets/js/site/gecis_ihlali.js, НЕ догадка).

Намеренное отличие от reader/turkey_bot_test/gib/models.py::CaptchaChallenge:
здесь нет image_id — у Avrasya CAPTCHA не имеет собственного
идентификатора вовсе, ожидаемый код живёт ИСКЛЮЧИТЕЛЬНО в серверной
сессии (ASP.NET_SessionId cookie) под фиксированным ключом
uid=DebtQueryByPlate (см. session.py) — один и тот же httpx.AsyncClient
на весь цикл проверки обязателен, но передавать какой-либо id между
fetch CAPTCHA и submit() не нужно и нечего.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class AvrasyaCaptchaChallenge:
    """Результат start()/refresh_captcha() — только сами байты картинки
    (JPEG, ~180x50, см. design report Stage 1), без image_id (см. докстрок
    модуля)."""

    image_png: bytes


@dataclass(frozen=True)
class AvrasyaMessage:
    """Один элемент "Messages" в ответе /api/debt/query при HTTP 400 (см.
    design report Stage 1 — РЕАЛЬНО увиденная вживую форма, не догадка):

        {"Messages": [{"PropertyName": "Captcha",
                        "ErrorMessage": "Güvenlik kodunu doğru "
                                        "girdiğinizden emin olunuz."}],
         "ValidationResultType": 2}

    Поля названы так же, как в реальном JSON (PropertyName/ErrorMessage) —
    НЕ переименованы под GibMessage.type/text, потому что это другой,
    самостоятельный протокол (см. design report: "do not force Avrasya
    into GIB-specific abstractions")."""

    property_name: str | None
    error_message: str | None


# Намеренно ГРУБАЯ классификация, ровно как и у GibSubmitKind (см.
# reader/turkey_bot_test/gib/models.py) — "не гадать схему" (см. design report
# Stage 1/2A/2C отчёты, разделы "Confirmed response states"):
#   - "rejected"   — HTTP 400 с Messages[].PropertyName == "Captcha" и
#     ИМЕННО этим, реально увиденным текстом ошибки (см. parser.py) —
#     ПОДТВЕРЖДЕНО вживую (дважды, curl).
#   - "no_debt"    — HTTP 404 с ГЕНУИННО пустым телом (см. parser.py::
#     _is_genuinely_empty_body) — ПОДТВЕРЖДЕНО вживую (Stage 2A: 3
#     независимых успешных human-CAPTCHA прохождения, все дали ровно эту
#     форму) И подтверждено чтением реального фронтенда (см. parser.py
#     докстрок: этот ответ падает в единственную generic
#     "запись не найдена" ветку showNotFoundView). ЛЮБОЙ ДРУГОЙ (непустой)
#     404 остаётся "unexpected" — см. parser.py.
#   - "has_debt"   — HTTP 200 с ПОДТВЕРЖДЁННЫМ envelope (см. parser.py::
#     _is_confirmed_debt_envelope) — ПОДТВЕРЖДЕНО вживую (Stage 2C:
#     production turkey_toll_checks.id=3, 2026-09-16, plate M295YB196,
#     реальная задолженность, 3 DebtItems, ExitDate/ExitStation/
#     DebtorContactFullName замаскированы самим Avrasya для анонимного
#     запроса — см. debt_items/AvrasyaDebtItem ниже про то, какие поля
#     из этого реально извлекаются).
#   - "unexpected" — всё остальное: неизвестный код/форма, HTTP 500,
#     непустой 404, HTTP 200 БЕЗ подтверждённого envelope (см. parser.py),
#     HTTP 200 с подтверждённым envelope, но БЕЗ единого распознанного
#     DebtItem (см. parser.py: "malformed debt items -> unexpected, не
#     догадка") — любой ответ, не совпадающий ни с одним реально
#     увиденным случаем.
#
# HTTP 429 (rate limit, см. design report Stage 1: "API calls quota
# exceeded! maximum admitted 1 per Second.") ЗДЕСЬ НЕ ПРЕДСТАВЛЕН вовсе —
# это транспортная ошибка (см. session.py::AvrasyaRateLimitedError),
# parser.py её никогда не видит и не классифицирует как бизнес-исход.
AvrasyaSubmitKind = Literal["no_debt", "has_debt", "rejected", "unexpected"]


@dataclass(frozen=True)
class AvrasyaDebtItem:
    """Одна запись из "DebtItems" внутри одного элемента "Subcriptions"
    (опечатка API — НЕ "Subscriptions" — сохраняется как есть, см. design
    report Stage 2C: "Preserve the API's actual misspelling Subcriptions;
    do not silently assume Subscriptions is equivalent unless separately
    observed") — РЕАЛЬНО увиденная вживую форма (production
    turkey_toll_checks.id=3, 2026-09-16, см. parser.py).

    ТОЛЬКО поля с подтверждённой, безопасной для показа семантикой —
    ExitDate/ExitStation/DebtorContactFullName/UniqueId ЗДЕСЬ НЕТ ВООБЩЕ
    (не только "не показаны"): реальный production-ответ содержит их
    промаскированными самим Avrasya для анонимного (неавторизованного)
    запроса (например, "2*************6", "A*****A", "*******" — на
    каждой реальной записи также стоит "IsAuthenticate": false) —
    показывать их пользователю как настоящие значения было бы неверно, а
    "размаскировать" их мы не пытаемся и не можем (см. задачу).

    penalty_amount — None означает "поле TotalFixedIncomeTaxIncludedBalanceAmount
    отсутствовало в этой записи" (см. design report: "A missing penalty
    field should behave as zero/not supplied for that item" — НЕ
    восстанавливается как total - principal, см. parser.py)."""

    principal_amount: Decimal
    total_amount: Decimal
    service_file_type: str
    penalty_amount: Decimal | None = None


@dataclass(frozen=True)
class AvrasyaSubmitOutcome:
    """Результат parser.parse_submit_response().

    status_code — исходный HTTP-статус ответа /api/debt/query: в отличие
    от GIB (единый envelope, статус не важен, см. gib/parser.py), у
    Avrasya HTTP-статус САМ ПО СЕБЕ несёт часть смысла (400 всегда
    сопровождает Captcha-ошибку, 200+confirmed envelope — задолженность,
    см. parser.py) — сохраняется для audit/отладки и на случай будущего
    уточнения парсинга.

    raw_data — сырой распарсенный JSON-ответа (или исходный текст, если
    тело не было JSON, см. session.py) КАК ЕСТЬ — НИКОГДА не должен
    попадать в пользовательский Telegram-текст напрямую (см. design
    report Stage 1: "Do not expose raw technical JSON directly to
    Telegram users") — только для server-side audit-хранения (см.
    reader/turkey_bot_test/toll_check_repository.py); пользователь видит ТОЛЬКО
    то, что texts.py::format_avrasya_has_debt_message строит из
    debt_items ниже.

    messages — типизированные AvrasyaMessage из "Messages" (см. выше),
    непусто только для реально увиденной формы HTTP 400/Captcha; для
    ЛЮБОГО другого kind остаётся пустым кортежем, а не предположением о
    структуре, которой мы не видели.

    debt_items — типизированные AvrasyaDebtItem (см. выше), непусто
    ТОЛЬКО когда kind == "has_debt" (см. parser.py) — для любого другого
    kind пустой кортеж, НИКОГДА не домысливается."""

    kind: AvrasyaSubmitKind
    status_code: int
    messages: tuple[AvrasyaMessage, ...]
    raw_data: object | None
    debt_items: tuple[AvrasyaDebtItem, ...] = ()
