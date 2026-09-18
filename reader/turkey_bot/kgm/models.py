"""Модели KGM (webihlaltakip.kgm.gov.tr) — см. design report "KGM
investigation": страница агрегирует РОВНО 10 фиксированных операторов в
одном ответе (подтверждено вживую, real HAR fixture, plate M295YB196).

OPERATOR_DISPLAY_ORDER/OPERATOR_DISPLAY_NAMES — статический словарь, а НЕ
парсинг названия из самого label (см. parser.py) — сайт САМ непоследователен
в пунктуации (у "Avrupa Otoyolu" перед "Kayıt yok." нет двоеточия, у
остальных 9 есть, см. design report), поэтому названия операторов
надёжнее зафиксировать здесь, чем разбирать вживую увиденную
непоследовательность."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

# Порядок — тот же, что и на самой странице (см. design report,
# "10 фиксированных секций") — используется и parser.py (порядок обхода
# секций), и texts.py (порядок вывода пользователю).
OPERATOR_DISPLAY_NAMES: dict[str, str] = {
    "kgm": "KGM",
    "avrasya": "Avrasya Tüneli",
    "otoyol": "Otoyol (Gebze-Orhangazi-İzmir)",
    "yss": "ICA (YSS Köprüsü ve Kuzey Çevre Otoyolu)",
    "avrupa_otoyolu": "Avrupa Otoyolu",
    "anadolu_otoyolu": "KMO Anadolu Otoyolu",
    "ika_otoyolu": "Kuzey Ege Otoyolu",
    "ankara_nigde_otoyolu": "Ankara-Niğde Otoyolu",
    "canakkale_koprusu": "Çanakkale Otoyol ve Köprüsü",
    "aydin_denizli": "Aydın-Denizli Otoyolu",
}

# operator_key == "kgm" — единственный оператор, входящий в "Ödenecek Tutar
# (KGM)" (см. parser.py про lblKgmToplamParaCezasi/lblYidToplamParaCezası);
# ВСЕ остальные 9 (включая Avrasya) относятся к "YİD" (Yap-İşlet-Devret —
# приватные операторы, см. design report) группе для контрольной суммы.
KGM_GROUP_OPERATOR_KEY = "kgm"


@dataclass(frozen=True)
class KgmCaptchaChallenge:
    """См. reader/turkey_bot/avrasya/models.py::AvrasyaCaptchaChallenge —
    тот же приём: без image_id, ожидаемый код живёт исключительно в
    серверной сессии (ASP.NET_SessionId cookie, см. session.py)."""

    image_png: bytes


@dataclass(frozen=True)
class KgmDebtItem:
    """Одна строка таблицы ОДНОГО оператора (см. design report: "Не
    реконструируй штраф, если сайт его явно не отдаёт... base_toll и
    payable_amount храни отдельно") — penalty НИГДЕ не вычисляется и не
    хранится отдельным полем, даже когда base_toll != payable_amount (см.
    parser.py).

    entry_station/exit_station/vehicle_class — None означает "ячейка была
    буквально пустой (&nbsp;) на сайте" (реально увиденное вживую поведение
    для строк Avrasya, см. design report) — НЕ означает "не удалось
    распарсить" (не usable-строка вообще не становится KgmDebtItem, см.
    parser.py)."""

    operator: str
    date_time: datetime
    entry_station: str | None
    exit_station: str | None
    vehicle_class: str | None
    base_toll: Decimal
    payable_amount: Decimal
    penalty_free_deadline: date | None


@dataclass(frozen=True)
class KgmOperatorResult:
    """Один из 10 операторов, у которого реально найдена задолженность
    (см. models.py::KgmSubmitOutcome.operators — операторы без
    задолженности сюда вообще не попадают, "Не показывать пустые operator
    sections" — design report). subtotal — распарсенный
    lbl{Key}CezaToplam (см. parser.py), уже сверенный с
    sum(item.payable_amount for item in items) на этапе parser.py (см.
    design report: "если суммы расходятся — unexpected", поэтому здесь
    subtotal ГАРАНТИРОВАННО согласован с items, а не просто "показан рядом
    без проверки")."""

    operator_key: str
    operator_name: str
    items: tuple[KgmDebtItem, ...]
    subtotal: Decimal


KgmSubmitKind = Literal["has_debt", "no_debt", "rejected", "unexpected"]


@dataclass(frozen=True)
class KgmSubmitOutcome:
    """Результат parser.py::parse_submit_response().

    operators — НЕПУСТОЙ ТОЛЬКО при kind == "has_debt", и содержит ТОЛЬКО
    операторов с реальной задолженностью (см. design report: "Не
    показывать пустые operator sections") — для любого другого kind
    остаётся пустым tuple.

    kgm_total/yid_total/grand_total — три контрольные суммы страницы (см.
    design report п.6: lblKgmToplamParaCezasi/lblYidToplamParaCezası/
    lblGenelCezaToplam) — None ТОЛЬКО когда kind == "rejected"/"unexpected"
    (страница либо не дошла до результата, либо сама структура не
    доверена, см. parser.py про fail-closed поведение). Для kind ==
    "has_debt"/"no_debt" ВСЕГДА присутствуют и уже сверены со всеми
    контрольными суммами (см. design report п.6 — все 4 проверки),
    иначе parser.py вернул бы "unexpected" вместо этого.

    message — ТОЛЬКО диагностика (см. design report): реально увиденный
    вживую текст "metin hatası" для kind == "rejected" (см. parser.py::
    _CAPTCHA_REJECTED_TEXT), короткая причина для kind == "unexpected"
    (НИКОГДА не сырой HTML/delta-ответ целиком — это НЕ raw_data, того
    самого понятия здесь нет вовсе, см. design report: "не выдавать
    пользователю частичный результат как достоверный" — сюда никогда не
    попадает то, что могло бы стать текстом пользователю напрямую, см.
    reader/turkey_bot/texts.py)."""

    kind: KgmSubmitKind
    operators: tuple[KgmOperatorResult, ...] = ()
    kgm_total: Decimal | None = None
    yid_total: Decimal | None = None
    grand_total: Decimal | None = None
    message: str | None = None
