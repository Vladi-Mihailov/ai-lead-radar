"""Тема кампании — детерминированно, без OpenAI: относится ли запрос
именно к ЭТОЙ ЛС-кампании (fuel — топливо, border_queue — проезд через
границу), а не просто содержит её ключевое слово.

Используется дважды:
- observer.py — выбор кампании для сообщения (одно сообщение — одна
  кампания): из сработавших кампаний берётся первая релевантная;
- processor.py — после шага классификации: лидовый intent, но тема не этой
  кампании -> filtered "campaign_not_relevant", генерации нет.

Отрицательные темы (посылки, продукты/таможня, страховка, штрафы, обмен
валют, SIM, доверенности, документы) проверяются только в САМОМ сообщении;
положительные — в сообщении, а если в нём темы нет вовсе (короткое
«А сейчас?») — в сообщении, на которое оно отвечает."""

import re
from collections.abc import Iterable

from reader.insurance_matching import is_insurance_text

FILTER_CAMPAIGN_NOT_RELEVANT = "campaign_not_relevant"

_I = re.IGNORECASE

# ---------------- fuel ----------------
_FUEL_RE = re.compile(
    r"(?<!\w)бенз|дизел|солярк|топлив|(?<!\w)азс(?!\w)|заправ|(?<!\w)аи[\s-]?(?:92|95|98|100)(?!\d)"
    r"|(?<![\w.,])(?:92|95|98|100)[\s-]?(?:й|го|ого|ым|ой|ый)(?!\w)",
    _I,
)

# ---------------- border_queue ----------------
# Место: граница/КПП/перевал — к нему привязываются «открыт», «обстановка»,
# «пропускают», погода.
_BORDER_PLACE_RE = re.compile(
    r"границ|(?<!\w)кпп(?!\w)|пропускн\w*\s+пункт|пункт\w*\s+пропуска|погранпереход|ларс|перевал|дариали|казбег|зарамаг|нейтралк|погранич|(?<!\w)вале(?!\w)|сарпи"
    r"|садахло|крестов",
    _I,
)
# Очередь / время прохождения / движение. Сама по себе «очередь» у АЗС — не
# граница: если в сообщении топливо и нет места-границы, очередь не считается.
_BORDER_QUEUE_RE = re.compile(
    r"очеред(?:ь|и|ью)(?!\w)|пробк|затор|много\s+машин|сколько\s+(?:сейчас\s+)?(?:стоять|ждать|проходить|времени)"
    r"|за\s+сколько|время\s+прохожд|(?<!\w)стоим(?!\w)|не\s+двига|движени",
    _I,
)
_BORDER_STATE_RE = re.compile(
    r"откры|закры|(?<!не\s)работает|обстановк|ситуаци|как\s+(?:там|дела|сейчас)|что\s+(?:там|сейчас)"
    r"|пропуска|выпуска|пускают|можно\s+(?:ли\s+)?(?:проехать|выехать|ехать|проезжать)|проед[уеёш]|проехать",
    _I,
)
# Дорога/перевал в контексте проезда («дорого» — цена — не совпадает).
_ROAD_RE = re.compile(
    r"как\s+(?:сейчас\s+)?дорог[аи](?!\w)|состояни\w*\s+дорог|что\s+с\s+дорог|дорог[аиеу]\s+(?:после|до|через|с|из|в)\s"
    r"|серпантин|(?<!\w)ям[ыа]?(?!\w)|клиренс|колея",
    _I,
)
_WEATHER_RE = re.compile(r"снег|снеж|гололед|гололёд|(?<!\w)л[её]д(?!\w)|погод|туман|лавин|метел", _I)
_TYRES_RE = re.compile(r"резин|(?<!\w)шип|липучк|(?<!\w)цеп(?:и|ях)(?!\w)|всесезон", _I)
# Не border_queue, даже рядом со словом «граница».
_BORDER_OFF_TOPIC_RE = re.compile(
    r"посылк|передать|продукт|(?<!\w)сыр|мяс[оа]|молок|колбас|провез|провоз|ввез|ввоз|вывез|вывоз|деклар"
    r"|таможенн\w*\s+(?:правил|огранич|документ)|страхов|осаго|каско|зел[её]н\w*\s+карт|штраф|оплат"
    r"|обмен|валют|рубл|(?<!\w)евро|доллар|(?<!\w)сим(?:ка|ку|ки|кой|карт)|(?<!\w)sim(?!\w)|доверенност"
    r"|документ|паспорт|загран|прописк|рожден|гражданств|(?<!\w)номер(?:а|ах|ами)(?!\w)|регион|крымск"
    r"|(?<!\w)виз[аыу](?!\w)|мультитул|оружи",
    _I,
)


def _without_idioms(text: str) -> str:
    return re.sub(r"в\s+первую\s+очередь|по\s+очереди", " ", text or "", flags=_I)


# Разговорный вопрос о КПП в целом («Как Ларс?», «Что на Ларсе?», «как
# проезд?», «как граница?»): открыт ли, есть ли очередь, нормально ли
# проезжается — не только про качество асфальта.
_COLLOQUIAL_RE = re.compile(r"(?<!\w)как(?!\w)|что\s+на(?!\w)|проезд|как\s+границ", _I)
_HINT_STATE_RE = re.compile(
    r"обстановк|ситуаци|откры|закры|(?<!не\s)работает|пропуска|выпуска|пускают|как\s+(?:там|дела|сейчас)"
    r"|(?:^|[.!?]\s*)(?:а\s+)?что\s+(?:там|сейчас)",
    _I,
)
_COLLOQUIAL_NO_PLACE_RE = re.compile(r"как\s+(?:там\s+)?(?:сейчас\s+)?(?:проезд|границ|перевал)|проезд\s+(?:открыт|есть)", _I)


def _border_topic(text: str, place_hint: bool = False) -> bool:
    """place_hint — разговор и так о КПП (чат КПП / ответ на сообщение о
    КПП): тогда «какая обстановка?», «как проезд?» — тоже вопрос о проезде."""
    text = _without_idioms(text)
    named = bool(_BORDER_PLACE_RE.search(text))
    fuel = bool(_FUEL_RE.search(text))
    if _BORDER_QUEUE_RE.search(text) and (named or not fuel):
        return True
    if _ROAD_RE.search(text) or _TYRES_RE.search(text) or _COLLOQUIAL_NO_PLACE_RE.search(text):
        return True
    if named and (_BORDER_STATE_RE.search(text) or _WEATHER_RE.search(text)):
        return True
    # Чат КПП без названия места: только вопрос об обстановке/работе КПП/погоде,
    # не маршрут по России и не бензин («проехать через Элисту» — не граница).
    if place_hint and not fuel and (_HINT_STATE_RE.search(text) or _WEATHER_RE.search(text)):
        return True
    return named and bool(_COLLOQUIAL_RE.search(text))


def fuel_relevant(text: str, reply_texts: Iterable[str] = ()) -> bool:
    if _FUEL_RE.search(text or ""):
        return True
    return any(_FUEL_RE.search(t or "") for t in reply_texts)


def border_relevant(text: str, reply_texts: Iterable[str] = (), place_hint: bool = False) -> bool:
    if _BORDER_OFF_TOPIC_RE.search(text or ""):
        return False
    if _border_topic(text, place_hint):
        return True
    # Короткое продолжение без своей темы («А сейчас?») — тема из того, на что отвечает.
    if _FUEL_RE.search(text or ""):
        return False
    return any(_border_topic(t) and not _BORDER_OFF_TOPIC_RE.search(t or "") for t in reply_texts)


def insurance_relevant(text: str, reply_texts: Iterable[str] = ()) -> bool:
    return is_insurance_text(text) or any(is_insurance_text(t) for t in reply_texts)


_RULES = {"fuel": fuel_relevant, "border_queue": border_relevant, "insurance": insurance_relevant}
# Кампании, между которыми сообщение можно перенаправить по теме (observer).
ROUTABLE_CAMPAIGNS = frozenset({"fuel", "border_queue"})


def campaign_relevant(campaign_key: str | None, text: str, reply_texts: Iterable[str] = (), *,
                      place_hint: bool = False) -> bool:
    """True — запрос по теме кампании; кампании без правила — всегда True.
    place_hint (только border_queue) — см. border.place_hint."""
    rule = _RULES.get(campaign_key or "")
    if rule is None:
        return True
    if rule is border_relevant:
        return border_relevant(text or "", tuple(reply_texts), place_hint)
    return rule(text or "", tuple(reply_texts))


# ---------------- border_queue: тематический фильтр ответа ----------------
# Соседние темы, которые в ответ border_queue не попадают, если USER сам о
# них не спрашивал (свежие сообщения группы обсуждают всё подряд).
_SIDE_TOPICS = {
    "бензин": _FUEL_RE,
    "страховка": re.compile(r"страхов|осаго|каско|зел[её]н\w*\s+карт|(?<!\w)полис", _I),
    "штрафы": re.compile(r"штраф", _I),
    "посылки": re.compile(r"посылк|посылоч|передать\s+(?:\w+\s+){0,2}(?:пакет|посылк)", _I),
    "документы": re.compile(
        r"доверенност|документ|паспорт|загран|прописк|рожден|гражданств|(?<!\w)виз[аыу](?!\w)|(?<!\w)стс(?!\w)"
        r"|(?<!\w)ву(?!\w)|штамп", _I),
    "номера": re.compile(r"(?<!\w)номер(?:а|ах|ами)(?!\w)|регион|крымск", _I),
    "обмен валют": re.compile(r"обмен|валют|рубл|(?<!\w)евро(?!\w)|доллар", _I),
    "ввоз вещей": re.compile(
        r"продукт|(?<!\w)сыр|мяс[оа]|молок|колбас|провез|провоз|ввез|ввоз|вывез|вывоз|деклар|литр", _I),
    "SIM": re.compile(r"(?<!\w)сим(?:ка|ку|ки|кой|карт)|(?<!\w)sim(?!\w)", _I),
    "животные": re.compile(r"собак|кошк|животн|(?<!\w)чип(?!\w)", _I),
}
# Сообщение о прохождении границы/КПП (отчёт, а не вопрос): место + действие/время.
_BORDER_REPORT_RE = re.compile(
    r"прош(?:ли|ёл|ел|ла)|проех|заех|выех|минут|(?<!\w)час(?:а|ов)?(?!\w)|окн[аоу]|пересменк|шлагбаум|стоял|стоим"
    r"|тоннел|откры|закры",
    _I,
)


def side_topics(text: str) -> frozenset[str]:
    """Соседние (не border_queue) темы в тексте."""
    return frozenset(name for name, rx in _SIDE_TOPICS.items() if rx.search(text or ""))


def border_evidence_relevant(text: str, asked: frozenset[str] = frozenset()) -> bool:
    """Свежее сообщение — источник для ответа border_queue: о проезде
    (очередь, время, открыт/закрыт, дорога, погода) или о соседней теме, о
    которой USER спросил сам (asked = side_topics вопроса)."""
    topics = side_topics(text)
    if topics and topics <= asked:
        return True
    text = _without_idioms(text)
    return _border_topic(text) or bool(_BORDER_PLACE_RE.search(text) and _BORDER_REPORT_RE.search(text))


def strip_side_topics(answer: str, asked: frozenset[str]) -> tuple[str, frozenset[str]]:
    """Вырезает из ответа border_queue предложения с соседними темами, о
    которых USER не спрашивал. -> (очищенный текст, вырезанные темы)."""
    removed: set[str] = set()
    paragraphs = []
    for paragraph in re.split(r"\n\s*\n", answer.strip()):
        kept = []
        for sentence in re.split(r"(?<=[.!?…])\s+", paragraph.strip()):
            leaked = side_topics(sentence) - asked
            if leaked:
                removed |= leaked
                continue
            kept.append(sentence)
        if kept:
            paragraphs.append(" ".join(kept))
    return "\n\n".join(paragraphs), frozenset(removed)
