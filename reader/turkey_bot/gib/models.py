"""Минимальные модели GIB-слоя (dijital.gib.gov.tr) — см. design report
Stage 1/2. Никакого мониторинга/подписок здесь нет и не будет: Турция —
одноразовая проверка (см. задачу), эти модели описывают ровно один цикл
"получить CAPTCHA -> отправить код -> получить исход".
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
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
        форма с непустым "BORCLAR" — БЕЗ обёртки "data" вовсе. Для формы с
        "BORCLAR" отдельные записи ДОПОЛНИТЕЛЬНО парсятся в `fines` (см.
        GibFineRecord ниже и reader/turkey_bot/gib/fine_parser.py) — только
        поля с подтверждённой семантикой, см. докстрок fine_parser.py про
        полный аудит; raw_data по-прежнему отдаёт ВЕСЬ payload как есть
        (для формы "data" — только "data") для audit-хранения как раньше;
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
    для "unexpected"/формы с "BORCLAR") — НЕ содержит cookies/captcha_code
    (см. задачу про то, что нельзя логировать), НО МОЖЕТ содержать
    KK_HASH/KK_KIMLIK (платёжные токены GIB, см. fine_parser.py) — это
    ok ТОЛЬКО для server-side audit-хранения (turkey_fine_checks.
    raw_response, см. задачу: "may continue storing the complete server-
    side GIB response"), raw_data НИКОГДА не должен попадать в
    пользовательский текст/обычные логи приложения — см. conversation.py.

    fines — типизированные записи из "BORCLAR" (см. GibFineRecord ниже),
    непусто ТОЛЬКО когда kind == "has_debt" и форма ответа была "BORCLAR"
    (форма "data" fines не заполняет — её реальная схема так и не была
    увидена, см. выше). НИКОГДА не содержит KK_HASH/KK_KIMLIK — см.
    fine_parser.py про то, какие поля вообще парсятся.
    """

    kind: GibSubmitKind
    messages: tuple[GibMessage, ...]
    raw_data: object | None
    fines: tuple["GibFineRecord", ...] = ()


@dataclass(frozen=True)
class GibFineRecord:
    """Одна запись из "BORCLAR" — см. fine_parser.py про парсинг и полный
    аудит полей реального ответа GIB (design report). Только поля с
    ПОДТВЕРЖДЁННОЙ семантикой:

      - protocol_no    — KK_TUTANAKNO (номер протокола/штрафа);
      - plate          — KK_PLAKA (в наблюдаемых данных совпадает с
        запрошенным номером — хранится для сверки, не для показа рядом с
        каждым штрафом, см. texts.py: он и так уже в заголовке сообщения);
      - amount         — KK_BORC, сумма долга (Decimal, распарсено
        безопасно — см. fine_parser._parse_amount);
      - description    — KK_ACIKLAMA как есть, турецкий текст БЕЗ перевода
        на этой итерации (см. задачу п.4 — не вводить внешнюю зависимость
        перевода сейчас);
      - violation_date — дата НАРУШЕНИЯ (не оплаты!), извлечена из
        "Ceza Tarihi:YYYY-MM-DD" внутри description — см. fine_parser.py
        про то, почему KK_ODEMETARIHI ("дата оплаты" буквально) СОЗНАТЕЛЬНО
        не используется как срок оплаты (неподтверждённая, вероятно ложная
        семантика — см. design report);
      - authority      — KK_MYS_KURUM_ADI (орган, выдавший штраф);
      - late_fee       — KK_GECIKMEZAMMI (пеня за просрочку), если > 0;
      - discount       — KK_INDIRIM_MIKTARI (сумма скидки), если > 0.

    НИКОГДА не содержит KK_HASH/KK_KIMLIK (платёжные токены) — этих полей
    в этом dataclass нет вообще, не только "не заполнены"."""

    protocol_no: str | None
    plate: str | None
    amount: Decimal | None
    description: str | None
    violation_date: date | None
    authority: str | None
    late_fee: Decimal | None
    discount: Decimal | None
