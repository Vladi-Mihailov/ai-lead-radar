"""border_queue — экспертный ответ о проезде через КПП (детерминированная
часть; модель пишет только сам ответ по сути):

- КПП, о котором спрашивают: названный в сообщении, иначе в цепочке ответов,
  иначе — КПП чата (группа «Верхний Ларс» -> Ларс). Свежие сведения и
  обсуждение — только про этот КПП (Сарпи/Вале не попадают в ответ о Ларсе);
- негативные сигналы (закрытие, ограничение, большая очередь, снег/лёд,
  авария) считаются здесь по свежим сообщениям-отчётам (не по вопросам).
  Нет сигналов — бизнес-логика активного чата: «открыт, критичных очередей
  нет»; есть — ответ обязан их отразить;
- пользователю источник не раскрывается («в группе пишут», «по свежим
  сообщениям», «нет данных» — отклоняется);
- приветствие в ответ на приветствие; для поездки через Верхний Ларс —
  короткий блок наших ресурсов в конце (@tplgee, @ProtocolGEbot)."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from reader.dm_campaigns.resources import PROTOCOL_GE, TPLGEE

_I = re.IGNORECASE

LARS = "lars"
CHECKPOINTS = {
    LARS: re.compile(r"ларс|дариал|казбег|степанцминд|крестов\w*\s+перевал|гудаур|зарамаг|lars", _I),
    "sarpi": re.compile(r"сарпи|sarpi", _I),
    "vale": re.compile(r"(?<!\w)вал[еэ](?!\w)|(?<!\w)vale(?!\w)|пософ", _I),
    "sadakhlo": re.compile(r"садахло|sadakhlo|баграташен", _I),
}
CHECKPOINT_TITLES = {LARS: "Верхний Ларс", "sarpi": "Сарпи", "vale": "Вале", "sadakhlo": "Садахло"}
# Группы, посвящённые одному КПП (config/groups.yaml).
CHAT_CHECKPOINT = {
    "verhniylars": LARS, "geolars": LARS, "verkhniy_lars_dariali": LARS,
    "sarpi_ge": "sarpi", "sadahlo": "sadakhlo",
}

TPLGEE_LINE = f"Не забудьте про страховку — я делал тут {TPLGEE}."
# Человек уже в Грузии / выезжает из Грузии на машине: напоминание с
# утверждённым фактом (штраф 100 лари за езду без обязательной
# автостраховки) — без момента/места оформления штрафа.
TPLGEE_REMINDER_LINE = (
    "Если страховка ещё не оформлена, обратите на это внимание — за езду без обязательной "
    f"автостраховки предусмотрен штраф 100 лари. Оформить можно через {TPLGEE}."
)
PROTOCOL_GE_LINE = f"По штрафам в Грузии можно пользоваться {PROTOCOL_GE}."
_HANDLE_RE = re.compile(r"@[A-Za-z0-9_]{4,32}")


def checkpoints_in(text: str) -> frozenset[str]:
    return frozenset(key for key, rx in CHECKPOINTS.items() if rx.search(text or ""))


# КПП, по которому у нас нет оперативных сведений («пропускной пункт РБ-РФ
# Красный камень»): его статус не выдумывается и Ларсом не подменяется.
_CHECKPOINT_WORD = (r"(?:[Кк][Пп][Пп]|[Пп]ропускн\w*\s+пункт\w*|[Пп]ункт\w*\s+пропуска|[Пп]огранпереход\w*"
                    r"|[Пп]ограничн\w*\s+переход\w*|[Пп]ереход\w*)")
_NAMED_CHECKPOINT_RE = re.compile(
    _CHECKPOINT_WORD + r"\s+(?:[A-ZА-ЯЁ]{2,3}\s*[-–]\s*[A-ZА-ЯЁ]{2,3}\s+)?[«\"]?"
    r"([А-ЯЁ][а-яё]+(?:ый|ий|ой|ая|ое|ие)\s+(?:[А-ЯЁ][а-яё]+|камень|мост|брод)(?!\w)"
    r"|[А-ЯЁ][а-яё]+(?:[-–][А-ЯЁ][а-яё]+)?)"
)
_KNOWN_UNSUPPORTED_RE = re.compile(r"красн\w*\s+кам\w*|яраг|казмаляр|(?<!\w)самур(?!\w)|бугаздро|гугути", _I)


def unsupported_checkpoint(text: str) -> str | None:
    """Явно названный в сообщении КПП, который мы не отслеживаем, -> его имя.
    Если в сообщении назван и отслеживаемый КПП (Ларс, Сарпи…) — None."""
    text = text or ""
    if checkpoints_in(text):
        return None
    match = _NAMED_CHECKPOINT_RE.search(text)
    if match and not checkpoints_in(match.group(1)):
        return match.group(1).strip()
    known = _KNOWN_UNSUPPORTED_RE.search(text)
    return known.group(0).strip() if known else None


def chat_checkpoint(chat_identifier: str | None) -> str | None:
    return CHAT_CHECKPOINT.get((chat_identifier or "").lstrip("@").lower())


def requested_checkpoints(text: str, reply_texts: Iterable[str] = (), chat_identifier: str | None = None) -> frozenset[str]:
    """Объект вопроса: явный КПП в сообщении > в цепочке ответов > КПП чата.
    Явно названный неотслеживаемый КПП («Красный камень») — объект вопроса:
    КПП чата (Ларс) тогда НЕ подставляется."""
    named = checkpoints_in(text)
    if named:
        return named
    if unsupported_checkpoint(text):
        return frozenset()
    for reply in reply_texts:
        named = checkpoints_in(reply)
        if named:
            return named
    chat = chat_checkpoint(chat_identifier)
    return frozenset({chat}) if chat else frozenset()


def place_hint(chat_identifier: str | None, reply_texts: Iterable[str] = ()) -> bool:
    """Разговор и так о КПП (чат КПП или ответ на сообщение о КПП): «как
    дорога?», «какая обстановка?» — вопрос о проезде."""
    return chat_checkpoint(chat_identifier) is not None or any(checkpoints_in(t) for t in reply_texts)


def checkpoint_evidence_ok(text: str, chat_identifier: str | None, requested: frozenset[str]) -> bool:
    """Сообщение годится как сведения о запрошенном КПП: названный в нём КПП
    — запрошенный; без названия — из чата этого КПП (или из общего чата)."""
    if not requested:
        return True
    named = checkpoints_in(text)
    if named:
        return bool(named & requested)
    chat = chat_checkpoint(chat_identifier)
    return chat is None or chat in requested


# ---------------- негативные сигналы ----------------

_QUESTION_RE = re.compile(
    r"\?|^\s*(?:подскажите|скажите|кто\s|как\s|какая|какой|какие|есть\s+ли|правда\s+ли|а\s+как|что\s+там)", _I)
NEGATIVE_SIGNALS = {
    "закрытие/ограничение проезда": re.compile(
        r"(?<!не\s)закры(?:ли|т|та|то|ты|вают)(?!\w)|перекры|не\s+пуска|не\s+выпуска|(?<!\w)не\s+работа"
        r"|движени\w*\s+(?:нет|останов|ограничен|перекр|закр)|ни\s+туда\s+ни\s+обратно|стоит\s+намертво", _I),
    "большая очередь": re.compile(
        r"(?<!думал\s)(?<!думали\s)(?<!думала\s)(?:больш|огромн|длинн|километров)\w*\s+(?:очеред|пробк)"
        r"|(?:очеред|пробк)\w*\s+(?:очень\s+|сейчас\s+)?(?:больш|огромн|длинн)"
        r"|очеред\w*\s+(?:на\s+|в\s+)?(?:\d+|несколько)\s*(?:км|километр|час)"
        r"|(?:\d+|несколько)\s*(?:км|километр)\w*\s+(?:очеред|пробк)"
        r"|сто(?:им|яли|ят|ял|яла)\s+(?:уже\s+)?(?:\d+|два|три|четыре|пять|шесть|несколько)\s*(?:-?\s*\d+\s*)?час"
        r"|(?:\d+|два|три|четыре|пять|шесть)\s*час\w*\s+(?:стоим|стояли|стоят|в\s+очеред)", _I),
    "снег/лёд на дороге": re.compile(
        r"снегопад|метел|гололед|гололёд|лавин|(?<!\w)цеп(?:и|ях)(?!\w)|только\s+(?:на\s+)?зимн|сильн\w*\s+снег"
        r"|снег\w*\s+(?:идёт|идет|валит|замело)", _I),
    "авария/остановка движения": re.compile(r"авари|(?<!\w)дтп(?!\w)|столкнов", _I),
}


def negative_signals(texts: Iterable[str]) -> tuple[str, ...]:
    """Сигналы из сообщений-отчётов (вопросы «закрыли?» — не сигнал)."""
    found: list[str] = []
    for text in texts:
        if _QUESTION_RE.search(text or ""):
            continue
        for name, rx in NEGATIVE_SIGNALS.items():
            if rx.search(text or "") and name not in found:
                found.append(name)
    return tuple(found)


# ---------------- приветствие ----------------

_GREETINGS = (
    (re.compile(r"добр\w*\s+утр|утр\w*\s+добр", _I), "Доброе утро!"),
    (re.compile(r"добр\w*\s+д(?:ень|ня)|(?<!\w)день\s+добр", _I), "Добрый день!"),
    (re.compile(r"добр\w*\s+вечер|вечер\w*\s+добр", _I), "Добрый вечер!"),
    (re.compile(r"добр\w*\s+ноч|ноч\w*\s+добр", _I), "Доброй ночи!"),
    (re.compile(r"здравствуй|здрасьте", _I), "Здравствуйте!"),
    (re.compile(r"приветствую", _I), "Приветствую!"),
    (re.compile(r"(?<!\w)привет", _I), "Привет!"),
)
_STARTS_WITH_GREETING_RE = re.compile(
    r"^\s*(?:добр\w*\s+(?:утр|д|вечер|ноч)\w*|(?:утр|день|вечер|ноч)\w*\s+добр\w*|здравствуй\w*|привет\w*)", _I)


def greeting_for(source_text: str, *, continuation: bool) -> str | None:
    """Приветствие ответа: зеркально приветствию USER; без приветствия —
    «Здравствуйте!», кроме продолжения разговора (ответ в ветке)."""
    for rx, greeting in _GREETINGS:
        if rx.search(source_text or ""):
            return greeting
    return None if continuation else "Здравствуйте!"


# ---------------- проверки и оформление ответа ----------------

_SOURCE_DISCLOSURE_RE = re.compile(
    r"свеж\w*\s+(?:сообщени|данн|сведени|отзыв|информаци)|(?:в|из|по)\s+(?:этой\s+|нашей\s+)?(?:групп|чат)\w*"
    r"|в\s+обсуждени|участник\w*|по\s+сообщени|сообща(?:ют|ет|ли)(?!\w)|(?<!\w)пишут|(?<!\w)писали|написал"
    r"|подтверд\w*\s+не\s+мог|не\s+мог\w*\s+(?:точно\s+)?подтверд|данных\s+нет|нет\s+(?:точных\s+|подтвержд\w*\s+|"
    r"свежих\s+)?(?:данных|сведений|информаци)|(?<!\w)отзыв|по\s+информаци"
    # любые «данные/сведения/информация», «недостаточно», «неясно» — это
    # рассказ о механике, а не ответ сервиса («сведений о снеге пока нет»,
    # «информации недостаточно, статус неясен», «есть и данные о…»)
    r"|(?<!\w)данн(?:ые|ых|ым|ыми)(?!\w)|сведени|информаци|недостаточн|неясн|неизвестн|не\s+располага"
    # неуверенность вместо ответа («Не уверен в текущем наличии бензина»)
    r"|не\s+уверен|не\s+знаю|сложно\s+сказать|трудно\s+сказать|затрудняюсь"
    # «конкретных указаний не поступало», «сообщений не было» — тоже механика
    r"|не\s+поступал|не\s+поступило|сообщений\s+не\s+было|не\s+сообщал",
    _I,
)
_CALM_RE = re.compile(
    r"критичн\w*\s+очеред\w*\s+(?:сейчас\s+)?нет|очеред\w*\s+(?:сейчас\s+)?(?:нет|не\s+наблюда)|без\s+очеред"
    r"|спокойн|свободн|проблем\w*\s+нет", _I)
_OVERCLAIM_RE = re.compile(
    r"точно\s+(?:нет|есть|будет|будут|заправ\w*|пропуст\w*)|гарантированн|100\s*%|всегда\s+(?:есть|бывает)", _I)


# ---------------- поездка в/через Грузию (подвал с ресурсами) ----------------

_GEORGIA_RE = re.compile(
    r"грузи|georgia|ларс|дариал|казбег|степанцминд|тбилис|батуми|кутаис|гудаур|мцхет|ахалцих|зугдиди", _I)
_RUSSIA_RE = re.compile(r"росси|(?<!\w)рф(?!\w)|russia", _I)
_ARMENIA_TURKEY_RE = re.compile(r"армени|ереван|турци|armenia|turkey", _I)
# Маршрут только по России («из Ростова 95 поймать?», «М4 или через Волгоград»).
_RUSSIA_INTERNAL_RE = re.compile(
    r"москв|питер|петербург|(?<!\w)спб(?!\w)|ростов|волгоград|краснодар|(?<!\w)м-?\s?(?:4|11)(?!\d)|элист"
    r"|ставропол|саратов|воронеж|самар|казан|нижн\w*\s+новгород|ярослав|пятигорск|минвод|(?<!\w)кмв(?!\w)"
    r"|невинномыс|оренбург|челябинск|екатеринбург|уф[аеуы](?!\w)", _I)
# Подъезд к Верхнему Ларсу (Владикавказ, Северная Осетия) — уже поездка в Грузию.
_LARS_APPROACH_RE = re.compile(r"владикавказ|владик(?!\w)|беслан|осети|алагир|(?<!\w)чми(?!\w)", _I)
_IN_GEORGIA_RE = re.compile(
    r"(?:выезжа\w*|еду|едем|возвраща\w*|выехал\w*|уезжа\w*)\s+(?:с|из)\s+грузи|(?:с|из)\s+грузии\s+(?:в|на)\s+"
    r"(?:рф|росси)|нахож\w*\s+в\s+грузи|(?:сейчас|уже)\s+в\s+грузи|по\s+грузии", _I)
_GEORGIA_GATEWAYS = frozenset({LARS, "sadakhlo"})


def georgia_trip(text: str, reply_texts: Iterable[str] = (), chat_identifier: str | None = None,
                 checkpoints: frozenset[str] = frozenset()) -> bool:
    """Поездка в/через/по Грузии: названа Грузия (или Ларс, Тбилиси…), маршрут
    Россия↔Армения/Турция (транзит через Грузию), либо — если маршрут не
    чисто российский — КПП Ларс/Садахло или чат этих КПП. Сарпи/Вале «в
    сторону Турции» без упоминания Грузии — нет."""
    texts = [text or "", *reply_texts]
    if any(_GEORGIA_RE.search(t) for t in texts):
        return True
    if any((_RUSSIA_RE.search(t) or _RUSSIA_INTERNAL_RE.search(t)) and _ARMENIA_TURKEY_RE.search(t) for t in texts):
        return True
    if any(_LARS_APPROACH_RE.search(t) for t in texts):
        return True
    if _RUSSIA_INTERNAL_RE.search(text or ""):
        return False
    return bool(checkpoints & _GEORGIA_GATEWAYS) or chat_checkpoint(chat_identifier) in _GEORGIA_GATEWAYS


def footer_lines(text: str, reply_texts: Iterable[str], *, georgia: bool,
                 campaign_resources: Iterable[str]) -> tuple[str, ...]:
    """Обязательный подвал fuel/border_queue для поездки, связанной с
    Грузией: @tplgee (для уже находящихся в Грузии — напоминание со штрафом
    100 лари) и @ProtocolGEbot — только ресурсы из настроек кампании."""
    if not georgia:
        return ()
    handles = {r.strip().lstrip("@").lower() for r in campaign_resources}
    in_georgia = any(_IN_GEORGIA_RE.search(t or "") for t in [text, *reply_texts])
    lines = []
    if TPLGEE.lstrip("@").lower() in handles:
        lines.append(TPLGEE_REMINDER_LINE if in_georgia else TPLGEE_LINE)
    if PROTOCOL_GE.lstrip("@").lower() in handles:
        lines.append(PROTOCOL_GE_LINE)
    return tuple(lines)


@dataclass(frozen=True)
class BorderPolicy:
    """Оформление и проверка ответа ЛС-кампании (kind = ключ кампании):
    border_queue — всё; fuel — без раскрытия источника + приветствие + подвал;
    insurance — только приветствие (ресурсы обязательны по правилам кампании)."""

    kind: str = "border_queue"
    checkpoints: frozenset[str] = frozenset()
    negative_signals: tuple[str, ...] = ()
    greeting: str | None = None
    cta: tuple[str, ...] = ()
    side_topics_asked: frozenset[str] = field(default_factory=frozenset)
    # Всё, что видит модель (сообщение USER, обсуждение, свежие сведения), —
    # конкретные места, трассы и цифры ответа должны отсюда подтверждаться.
    context_text: str = ""
    # Явно названный неотслеживаемый КПП: ответ — честное «не подскажу» без
    # подмены другим КПП (unsupported_checkpoint_answer, модель не вызывается).
    unsupported_checkpoint: str | None = None
    # Сети АЗС, перечисленные USER («Лукойл, Роснефть, Газпром?»): ответ
    # обязан работать с ними, а не отделываться общей фразой.
    question_options: tuple[str, ...] = ()

    def audit(self) -> dict:
        audit = {"checkpoints": sorted(self.checkpoints), "negative_signals": list(self.negative_signals),
                 "greeting": self.greeting, "cta": bool(self.cta)}
        if self.unsupported_checkpoint:
            audit["unsupported_checkpoint"] = self.unsupported_checkpoint
        if self.question_options:
            audit["question_options"] = list(self.question_options)
        return audit

    @property
    def expert_dynamic(self) -> bool:
        """border_queue / fuel: экспертный тон без раскрытия источника."""
        return self.kind in ("border_queue", "fuel")


def cta_lines(checkpoints: frozenset[str], campaign_resources: Iterable[str]) -> tuple[str, ...]:
    """Совместимость: подвал по одному лишь КПП (Ларс/Садахло)."""
    return footer_lines("", (), georgia=bool(checkpoints & _GEORGIA_GATEWAYS), campaign_resources=campaign_resources)


def cta_handles(policy: BorderPolicy) -> tuple[str, ...]:
    return tuple(dict.fromkeys(h for line in policy.cta for h in _HANDLE_RE.findall(line)))


# Предположение под видом факта («обычно направляют», «чаще всего решают на
# месте», «проблем не предвидится») — не ответ сервиса.
_GENERALIZATION_RE = re.compile(
    r"(?<!\w)обычно(?!\w)|чаще\s+всего|как\s+правило|как\s+обычно|скорее\s+всего|не\s+предвид|(?<!\w)вероятно(?!\w)"
    r"|по\s+опыту|как\s+показывает\s+практика|как\s+водится|в\s+большинстве\s+случаев|по\s+идее|должно\s+быть\s+нормально",
    _I,
)
# Слова, которые в ответе о поездке через КПП допустимы всегда (это сам
# предмет разговора), — остальные имена собственные должны быть в контексте.
_ALWAYS_KNOWN = ("верхн", "ларс", "грузи", "росси", "кпп", "азс", "армени", "турци", "крест")
_PLACE_ALIASES = (
    ("питер", "петербург", "спб", "санкт"),
    ("москв", "мск"),
    ("владикавказ", "владик"),
    ("минвод", "минеральн", "кмв"),
    ("тбилис", "тбилиси"),
    ("ереван", "армени"),
)
_PROPER_RE = re.compile(r"(?<![\w-])[А-ЯЁ][а-яё]{3,}")
_ROAD_CODE_RE = re.compile(r"(?<!\w)[МРАMPA]-?\s?(\d{1,3})(?!\d)")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def unsupported_detail(text: str, context_text: str) -> str | None:
    """Конкретная деталь ответа (место, трасса, цифра), которой нет ни в
    вопросе, ни в обсуждении, ни в свежих сведениях, -> её значение."""
    known = (context_text or "").lower()
    for variants in _PLACE_ALIASES:  # «с Питера» подтверждает «из Петербурга»
        if any(v in known for v in variants):
            known += " " + " ".join(variants)
    for sentence in re.split(r"(?<=[.!?…])\s+", text.strip()):
        body = sentence.strip()
        for match in _PROPER_RE.finditer(body):
            if match.start() == 0:  # первое слово предложения — не имя собственное
                continue
            word = match.group(0)
            stem = word.lower()[:5]
            if not stem.startswith(_ALWAYS_KNOWN) and stem not in known:
                return word
    for digits in _ROAD_CODE_RE.findall(text):
        if digits not in known:
            return f"М{digits}"
    for number in _NUMBER_RE.findall(text):
        if number not in known and number.replace(",", ".") not in known:
            return number
    return None


_FUEL_BRANDS = {
    "Лукойл": r"лукойл|лукоил|lukoil", "Роснефть": r"роснефт|rosneft", "Газпром": r"газпром|gazprom",
    "Татнефть": r"татнефт", "Shell": r"shell|шелл", "Teboil": r"teboil|тебойл", "SOCAR": r"socar|сокар",
    "Wissol": r"wissol|виссол", "Gulf": r"(?<!\w)gulf|галф", "Rompetrol": r"rompetrol|ромпетрол",
    "ННК": r"(?<!\w)ннк(?!\w)", "Нефтьмагистраль": r"нефтьмагистрал", "Опти": r"(?<!\w)опти(?!\w)",
}


def question_options(text: str) -> tuple[str, ...]:
    """Сети АЗС, перечисленные USER в вопросе."""
    return tuple(name for name, rx in _FUEL_BRANDS.items() if re.search(rx, text or "", _I))


def unsupported_checkpoint_answer(name: str) -> str:
    return (f"По переходу «{name}» сейчас не буду вводить вас в заблуждение — точную текущую обстановку "
            "по нему не подскажу.")


def border_problem(text: str, policy: BorderPolicy) -> str | None:
    if _SOURCE_DISCLOSURE_RE.search(text):
        return "source_disclosure"
    if _OVERCLAIM_RE.search(text):
        return "overclaim"
    if policy.negative_signals and _CALM_RE.search(text):
        return "contradicts_signals"
    if _GENERALIZATION_RE.search(text):
        return "unsupported_generalization"
    if policy.question_options and not any(
            re.search(_FUEL_BRANDS[name], text, _I) for name in policy.question_options):
        return f"ignored_question_options:{','.join(policy.question_options)}"
    detail =unsupported_detail(text, policy.context_text) if policy.context_text else None
    if detail:
        return f"unsupported_detail:{detail}"
    return None


def other_checkpoint_sentences(text: str, checkpoints: frozenset[str]) -> tuple[str, frozenset[str]]:
    """Вырезает предложения про КПП, о которых не спрашивали."""
    if not checkpoints:
        return text, frozenset()
    removed: set[str] = set()
    paragraphs = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        kept = []
        for sentence in re.split(r"(?<=[.!?…])\s+", paragraph.strip()):
            other = checkpoints_in(sentence) - checkpoints
            if other:
                removed |= other
                continue
            kept.append(sentence)
        if kept:
            paragraphs.append(" ".join(kept))
    return "\n\n".join(paragraphs), frozenset(CHECKPOINT_TITLES[c] for c in removed)


def decorate(text: str, policy: BorderPolicy) -> str:
    """Приветствие в начале (если модель его не написала) + ресурсы в конце
    (те, что ещё не упомянуты)."""
    body = text.strip()
    if policy.greeting and not _STARTS_WITH_GREETING_RE.search(body):
        body = f"{policy.greeting} {body}"
    lowered = body.lower()
    tail = [line for line in policy.cta if not any(h.lower() in lowered for h in _HANDLE_RE.findall(line))]
    if tail:
        body = f"{body} {tail[0]}" + ("\n" + "\n".join(tail[1:]) if len(tail) > 1 else "")
    return body
