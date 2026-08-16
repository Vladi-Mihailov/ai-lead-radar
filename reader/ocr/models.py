from dataclasses import dataclass

# Единственный источник правды для формата Telegram-сообщения "Распознано:
# ..." (см. reader/commands/insurance_ocr.py::_format_result) и для его
# обратного разбора (см. reader/checkout/parser.py) — оба модуля читают
# структуру отсюда, а не дублируют список полей/меток у себя.
#
# Три визуальных блока (см. формат сообщения):
# 1) данные страхователя + ТС + контакты;
# 2) флаг "Водитель = страхователь" и, если он "-", отдельное ФИО водителя;
# 3) то же самое для владельца.
# is_flag=True — поле рендерится как "+"/"-" (bool), а не как текст/
# "не распознано". empty_when_none=True — None рендерится как ПУСТАЯ строка
# после ":" (не как "не распознано") — только для Водитель/Владелец: их
# отсутствие означает "совпадает со страхователем", а не "не удалось
# распознать" (см. reader/commands/insurance_ocr.py::_format_result).


@dataclass(frozen=True)
class ReplyField:
    label: str
    attr: str
    is_flag: bool = False
    empty_when_none: bool = False


_INSURER_SECTION: tuple[ReplyField, ...] = (
    ReplyField("Страхователь", "policyholder_full_name"),
    ReplyField("Номер паспорта", "passport_number"),
    ReplyField("Гражданство", "citizenship"),
    ReplyField("Категория", "category"),
    ReplyField("Марка", "manufacturer"),
    ReplyField("Модель", "model"),
    ReplyField("VIN", "vin"),
    ReplyField("Номер шасси", "chassis_number"),
    ReplyField("Госномер", "registration_number"),
    ReplyField("Email", "email"),
    ReplyField("Телефон", "phone"),
    # Checkout-поля заявки (не из OCR — см. docstring OcrResult ниже и
    # reader/commands/insurance_ocr.py про default'ы): банк-эквайер, период
    # полиса, дата начала периода. Редактируются correction-reply'ем так же,
    # как остальные поля (см. reader/checkout/parser.py).
    ReplyField("Банк", "payment_bank"),
    ReplyField("Период", "policy_period"),
    ReplyField("Начало периода", "period_start"),
)
_DRIVER_SECTION: tuple[ReplyField, ...] = (
    ReplyField("Водитель = страхователь", "driver_same_as_policyholder", is_flag=True),
    ReplyField("Водитель", "driver_full_name", empty_when_none=True),
)
_OWNER_SECTION: tuple[ReplyField, ...] = (
    ReplyField("Владелец = страхователь", "owner_same_as_policyholder", is_flag=True),
    ReplyField("Владелец", "owner_full_name", empty_when_none=True),
)

# Порядок секций = порядок блоков в Telegram-сообщении (см.
# reader/commands/insurance_ocr.py::_format_result — блоки разделяются
# пустой строкой).
REPLY_SECTIONS: tuple[tuple[ReplyField, ...], ...] = (_INSURER_SECTION, _DRIVER_SECTION, _OWNER_SECTION)

# Плоский (метка, атрибут) — для парсера (см. reader/checkout/parser.py),
# которому не важна разбивка на блоки, только соответствие метки атрибуту.
REPLY_FIELD_LABELS: tuple[tuple[str, str], ...] = tuple(
    (f.label, f.attr) for section in REPLY_SECTIONS for f in section
)

# Атрибуты двух bool-флагов ("+"/"-" в тексте, а не свободное значение) —
# парсер должен обрабатывать их иначе, чем обычные текстовые поля.
FLAG_ATTRS: frozenset[str] = frozenset(f.attr for section in REPLY_SECTIONS for f in section if f.is_flag)


@dataclass(frozen=True)
class OcrResult:
    """Результат распознавания документов автомобиля + состояние checkout-
    заявки tpl.ge (одна и та же структура — см. reader/checkout/parser.py,
    который восстанавливает её из текста Telegram-сообщения).

    Источники ФИО (см. reader/ocr/prompt.py) — паспорт/ID страхователя ЛИБО
    водительское удостоверение (для водителя) и техпаспорт/свидетельство
    регистрации ТС (для страхователя), плюс доверенность (для отдельного
    владельца):
    - policyholder_full_name — из техпаспорта. Если паспорт/права и
      техпаспорт распознаны и ФИО совпадает — то же самое ФИО, driver
      same_as-флаг True. Если ФИО различаются — policyholder берётся из
      техпаспорта, driver_full_name — из паспорта/прав,
      driver_same_as_policyholder=False. Если техпаспортное ФИО не
      распознано — policyholder_full_name=None (ФИО одного паспорта не
      может заменить страхователя — это другая бизнес-роль).
    - driver_same_as_policyholder/owner_same_as_policyholder — bool
      (никогда None): ~99% случаев водитель и владелец совпадают со
      страхователем, поэтому это два явных флага, а не отдельная
      обязательная роль. driver_full_name/owner_full_name заполняются
      только когда соответствующий флаг False.
    - owner_same_as_policyholder/owner_full_name — определяются ТОЛЬКО
      доверенностью (техпаспорт для этого повторно не используется, см.
      reader/ocr/prompt.py): если среди документов есть доверенность и
      модель уверенно определила в ней ФИО владельца/доверителя для этой
      операции — owner_same_as_policyholder=False, owner_full_name — это
      ФИО. Иначе (доверенности нет, или лицо нельзя определить уверенно) —
      True/None. Оба поля можно скорректировать через correction-reply (см.
      reader/checkout/parser.py) — исправленный оператором reply
      authoritative для конкретного checkout.

    passport_number/citizenship — из паспорта страхователя. category/
    manufacturer/model/vin/chassis_number/registration_number — из
    техпаспорта. email/phone/payment_bank/policy_period/period_start — НЕ
    распознаются OCR (не часть Structured Output, см. reader/ocr/service.py);
    попадают в Telegram-сообщение как default-значения заявки (см.
    reader/commands/insurance_ocr.py) и могут быть изменены оператором через
    correction-reply, как и остальные поля:
    - payment_bank — "bog"/"liberty" (default "bog"), см.
      reader/checkout/mapping.py::resolve_payment_bank про реальный
      PaymentBank/tpl.ge bank ID;
    - policy_period — "15"/"30"/"90" (default "15", "1-Y" в Telegram-flow не
      поддерживается), см. reader/checkout/mapping.py::resolve_policy_period;
    - period_start — "DD.MM.YYYY" (default — календарная дата создания OCR-
      заявки, вычисленная РОВНО ОДИН РАЗ при формировании черновика и с тех
      пор хранящаяся в самом тексте Telegram-сообщения — checkout НЕ берёт
      today() заново при "pay", см. reader/checkout/mapping.py::
      resolve_period_start).

    None у текстового поля — "не найдено/не распознано", а не
    предположение."""

    policyholder_full_name: str | None
    driver_same_as_policyholder: bool
    driver_full_name: str | None
    owner_same_as_policyholder: bool
    owner_full_name: str | None
    passport_number: str | None
    citizenship: str | None
    category: str | None
    manufacturer: str | None
    model: str | None
    vin: str | None
    chassis_number: str | None
    registration_number: str | None
    email: str | None
    phone: str | None
    payment_bank: str | None
    policy_period: str | None
    period_start: str | None

    @property
    def fields_found_count(self) -> int:
        """Только поля, которые реально распознаёт/не распознаёт OCR — без
        двух bool-флагов (у них всегда есть значение) и без email/phone/
        payment_bank/policy_period/period_start (это config/draft-defaults, а
        не результат распознавания документа), см. docstring класса."""
        return sum(
            1
            for value in (
                self.policyholder_full_name, self.driver_full_name, self.owner_full_name,
                self.passport_number, self.citizenship,
                self.category, self.registration_number, self.vin, self.chassis_number,
                self.manufacturer, self.model,
            )
            if value
        )
