"""Разбор отдельных записей "BORCLAR" (см. reader/turkey_bot/gib/parser.py)
в типизированный GibFineRecord — единственное место в кодовой базе, где
поля отдельного штрафа/долга интерпретируются (см. задачу: "do not make
conversation.py manually interpret arbitrary raw JSON" — вся эта логика
живёт в GIB-слое, conversation.py и texts.py работают только с уже
готовыми GibFineRecord).

ПОЛНЫЙ АУДИТ полей одной записи BORCLAR (см. design report — реальный
live-ответ, 4 штрафа, 20 ключей на запись, все ключи присутствуют во ВСЕХ
записях):

  Безопасные, пользовательские (парсятся сюда):
    KK_TUTANAKNO      — номер протокола/штрафа ("tutanak no");
    KK_PLAKA          — номер (совпадает с запрошенным в наблюдаемых
                         данных — хранится для сверки, не для показа);
    KK_BORC           — сумма долга ("borç" = "долг");
    KK_ACIKLAMA       — место/описание нарушения (турецкий текст,
                         "açıklama" = "описание") + встроенные
                         "HarfSeriNo:.../Ceza Tarihi:YYYY-MM-DD/
                         Ceza Maddesi:.../Madde Açıklaması:..." — во ВСЕХ
                         4 наблюдаемых записях (оба live-теста) эти 4
                         маркера присутствуют в ЭТОМ ФИКСИРОВАННОМ порядке
                         (см. design report: полный аудит перед парсингом),
                         поэтому дополнительно извлекаются структурные поля
                         (см. _parse_structured_aciklama ниже):
                           - location (текст ДО "HarfSeriNo:");
                           - law_article (значение "Ceza Maddesi:");
                           - violation_description (значение
                             "Madde Açıklaması:" — до конца строки);
                         Ceza Tarihi по-прежнему парсится отдельно (см.
                         _parse_violation_date, не тронуто) — HarfSeriNo
                         не извлекается отдельно (дублирует KK_TUTANAKNO,
                         только без дефиса). Если полный 4-маркерный
                         паттерн не совпал (маркеры отсутствуют/переставлены
                         местами/повреждены) — все три структурных поля
                         остаются None, а исходный KK_ACIKLAMA целиком
                         остаётся в description как безопасный fallback
                         (см. design report: "fail safely", "do not
                         destroy audit data") — ничего не парсится
                         частично/приблизительно;
    KK_MYS_KURUM_ADI  — орган, выдавший штраф ("kurum adı" = "название
                         учреждения"), напр. "EMNİYET GENEL MÜDÜRLÜĞÜ";
    KK_GECIKMEZAMMI   — пеня за просрочку ("gecikme zammı"), "0.00" в
                         наблюдаемых данных — парсится, показывается
                         только если > 0 (см. texts.py);
    KK_INDIRIM_MIKTARI — сумма скидки ("indirim miktarı"), "0.00" в
                         наблюдаемых данных — та же логика, что и пеня.

  ⚠️ НЕОДНОЗНАЧНОЕ поле (СОЗНАТЕЛЬНО не парсится как "срок оплаты"):
    KK_ODEMETARIHI    — буквально "дата оплаты" ("ödeme tarihi"), НО во
                         ВСЕХ наблюдаемых записях (все 4 штрафа, оба
                         live-теста) это значение ТОЧНО совпадает с
                         "BORC_SORGU_TARIHI" — датой самого запроса, а
                         не с какой-либо будущей датой. Это выглядит как
                         "дата операции/запроса", а не как дедлайн оплаты
                         — показывать это пользователю как "Оплатить до"
                         означало бы придумывать неподтверждённую
                         семантику (см. задачу: "do not invent semantics
                         for ambiguous fields"). НЕ парсится в
                         GibFineRecord вовсе на этой итерации.

  Внутренние/технические (не парсятся — не нужны пользователю):
    KK_KONTROL         — флаг ("1" во всех записях), назначение неясно;
    KK_ORGOID          — внутренний код организации GIB;
    KK_VDKODU          — код налоговой инспекции ("vergi dairesi kodu");
    KK_OZEL_PLAKA_KODU — дублирует KK_PLAKA в наблюдаемых данных;
    KK_ODEMETURLERI    — код типа платежа ("111" во всех записях),
                         не человекочитаем без справочника GIB;
    KK_MYS_ETTN        — внутренний идентификатор документа (UUID-подобный,
                         "ETTN" — Electronic Ticari Tebligat Numarası);
    KK_MYS_TAHSILAT_TURU — категория взыскания, во всех записях одна и та
                         же длинная фраза — избыточно для показа.

  Потенциально персональные (пустые в наблюдаемых данных, НЕ парсятся
  из осторожности — реальное имя/фамилия водителя, если когда-либо
  заполнены, не должны утекать в Telegram-сообщение без отдельного
  решения):
    KK_AD, KK_SOYAD    — имя/фамилия, "" (пусто) в наблюдаемых данных.

  ПЛАТЁЖНЫЕ/АВТОРИЗАЦИОННЫЕ (см. задачу — НИКОГДА не парсятся, не
  логируются, не показываются пользователю; остаются ТОЛЬКО в сыром
  raw_data для server-side audit, см. reader/turkey_bot/gib/models.py::
  GibSubmitOutcome.raw_data):
    KK_HASH, KK_KIMLIK.
"""

import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from reader.turkey_bot.gib.models import GibFineRecord

logger = logging.getLogger(__name__)

# "Ceza Tarihi:2026-08-08" встроено в KK_ACIKLAMA (см. модуль docstring) —
# единственный реально подтверждённый источник ДАТЫ НАРУШЕНИЯ (отдельной
# от неоднозначной KK_ODEMETARIHI).
_VIOLATION_DATE_RE = re.compile(r"Ceza Tarihi:(\d{4})-(\d{2})-(\d{2})")

# Полный, реально наблюдавшийся (см. design report: 4/4 записей, оба
# live-теста) паттерн KK_ACIKLAMA — ВСЕ 4 маркера в этом фиксированном
# порядке. re.DOTALL не нужен (турецкий текст без переносов строк в
# наблюдаемых данных), но не вредит. Нежадный location (.+?) — чтобы
# остановиться на ПЕРВОМ "HarfSeriNo:", а не на последнем вхождении чего-
# либо похожего. Если этот паттерн НЕ совпадает целиком (любой маркер
# отсутствует/переставлен/повреждён) — .match() вернёт None и все три
# структурных поля останутся None (см. _parse_structured_aciklama).
_STRUCTURED_ACIKLAMA_RE = re.compile(
    r"^(?P<location>.+?)\s*HarfSeriNo:\S+\s*"
    r"Ceza Tarihi:\d{4}-\d{2}-\d{2}\s*"
    r"Ceza Maddesi:(?P<law_article>\S+)\s*"
    r"Madde Açıklaması:(?P<violation_description>.+)$"
)


def _clean_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _parse_amount(value: object) -> Decimal | None:
    """None при чём угодно непарсимом — см. задачу: "malformed amount
    ... fails safely" — никогда не бросает исключение наружу."""
    if value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        logger.warning("Turkey GIB: fine amount value could not be parsed as a number")
        return None


def _parse_violation_date(description: str | None) -> date | None:
    """None если описание отсутствует, не содержит "Ceza Tarihi:...", или
    содержит его в повреждённом/невалидном виде (см. задачу: "malformed
    ... date fails safely") — никогда не бросает исключение наружу."""
    if not description:
        return None
    match = _VIOLATION_DATE_RE.search(description)
    if not match:
        return None
    try:
        year, month, day = (int(part) for part in match.groups())
        return date(year, month, day)
    except ValueError:
        logger.warning("Turkey GIB: violation date extracted from description was invalid")
        return None


def _parse_structured_aciklama(description: str | None) -> tuple[str | None, str | None, str | None]:
    """(location, law_article, violation_description) — все три None, если
    описание отсутствует или полный 4-маркерный паттерн не совпал целиком
    (см. _STRUCTURED_ACIKLAMA_RE и модуль docstring про "fail safely, do
    not invent partial parsing"). Никогда не бросает исключение наружу."""
    if not description:
        return None, None, None
    match = _STRUCTURED_ACIKLAMA_RE.match(description)
    if not match:
        return None, None, None
    location = _clean_str(match.group("location"))
    law_article = _clean_str(match.group("law_article"))
    violation_description = _clean_str(match.group("violation_description"))
    return location, law_article, violation_description


def _parse_one(item: dict) -> GibFineRecord:
    description = _clean_str(item.get("KK_ACIKLAMA"))
    location, law_article, violation_description = _parse_structured_aciklama(description)
    return GibFineRecord(
        protocol_no=_clean_str(item.get("KK_TUTANAKNO")),
        plate=_clean_str(item.get("KK_PLAKA")),
        amount=_parse_amount(item.get("KK_BORC")),
        description=description,
        violation_date=_parse_violation_date(description),
        authority=_clean_str(item.get("KK_MYS_KURUM_ADI")),
        late_fee=_parse_amount(item.get("KK_GECIKMEZAMMI")),
        discount=_parse_amount(item.get("KK_INDIRIM_MIKTARI")),
        location=location,
        law_article=law_article,
        violation_description=violation_description,
    )


def parse_fine_records(borclar: object) -> tuple[GibFineRecord, ...]:
    """borclar — payload["BORCLAR"] как есть. Не-list вход, и не-dict
    элементы внутри списка, пропускаются защитно (тот же принцип, что и
    parser.py::_extract_messages) — никогда не бросает исключение наружу,
    так что один повреждённый элемент не портит остальные штрафы."""
    if not isinstance(borclar, list):
        return ()

    return tuple(_parse_one(item) for item in borclar if isinstance(item, dict))
