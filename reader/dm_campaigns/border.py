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
PROTOCOL_GE_LINE = f"По штрафам в Грузии можно пользоваться {PROTOCOL_GE}."


def checkpoints_in(text: str) -> frozenset[str]:
    return frozenset(key for key, rx in CHECKPOINTS.items() if rx.search(text or ""))


def chat_checkpoint(chat_identifier: str | None) -> str | None:
    return CHAT_CHECKPOINT.get((chat_identifier or "").lstrip("@").lower())


def requested_checkpoints(text: str, reply_texts: Iterable[str] = (), chat_identifier: str | None = None) -> frozenset[str]:
    named = checkpoints_in(text)
    if named:
        return named
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
    (re.compile(r"добр\w*\s+утр", _I), "Доброе утро!"),
    (re.compile(r"добр\w*\s+д(?:ень|ня)", _I), "Добрый день!"),
    (re.compile(r"добр\w*\s+вечер", _I), "Добрый вечер!"),
    (re.compile(r"добр\w*\s+ноч", _I), "Доброй ночи!"),
    (re.compile(r"здравствуй|здрасьте", _I), "Здравствуйте!"),
    (re.compile(r"приветствую", _I), "Приветствую!"),
    (re.compile(r"(?<!\w)привет", _I), "Привет!"),
)
_STARTS_WITH_GREETING_RE = re.compile(r"^\s*(?:добр\w*\s+(?:утр|д|вечер|ноч)\w*|здравствуй\w*|привет\w*)", _I)


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
    r"|(?<!\w)данн(?:ые|ых|ым|ыми)(?!\w)|сведени|информаци|недостаточн|неясн|неизвестн|не\s+располага",
    _I,
)
_CALM_RE = re.compile(
    r"критичн\w*\s+очеред\w*\s+(?:сейчас\s+)?нет|очеред\w*\s+(?:сейчас\s+)?(?:нет|не\s+наблюда)|без\s+очеред"
    r"|спокойн|свободн|проблем\w*\s+нет", _I)
_OVERCLAIM_RE = re.compile(r"точно\s+нет|гарантированн|100\s*%", _I)


@dataclass(frozen=True)
class BorderPolicy:
    checkpoints: frozenset[str] = frozenset()
    negative_signals: tuple[str, ...] = ()
    greeting: str | None = None
    cta: tuple[str, ...] = ()
    side_topics_asked: frozenset[str] = field(default_factory=frozenset)

    def audit(self) -> dict:
        return {"checkpoints": sorted(self.checkpoints), "negative_signals": list(self.negative_signals),
                "greeting": self.greeting, "cta": bool(self.cta)}


def cta_lines(checkpoints: frozenset[str], campaign_resources: Iterable[str]) -> tuple[str, ...]:
    """Ресурсы — для поездки через Верхний Ларс (РФ↔Грузия) и только те,
    что стоят в ресурсах кампании. Другие КПП (Сарпи в Турцию…) — без них."""
    if LARS not in checkpoints:
        return ()
    handles = {r.strip().lstrip("@").lower() for r in campaign_resources}
    lines = []
    if TPLGEE.lstrip("@").lower() in handles:
        lines.append(TPLGEE_LINE)
    if PROTOCOL_GE.lstrip("@").lower() in handles:
        lines.append(PROTOCOL_GE_LINE)
    return tuple(lines)


def cta_handles(policy: BorderPolicy) -> tuple[str, ...]:
    return tuple(h for h, line in ((TPLGEE, TPLGEE_LINE), (PROTOCOL_GE, PROTOCOL_GE_LINE)) if line in policy.cta)


def border_problem(text: str, policy: BorderPolicy) -> str | None:
    if _SOURCE_DISCLOSURE_RE.search(text):
        return "source_disclosure"
    if _OVERCLAIM_RE.search(text):
        return "overclaim"
    if policy.negative_signals and _CALM_RE.search(text):
        return "contradicts_signals"
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
    tail = [line for line, handle in ((TPLGEE_LINE, TPLGEE), (PROTOCOL_GE_LINE, PROTOCOL_GE))
            if line in policy.cta and handle.lower() not in lowered]
    if tail:
        body = f"{body} {tail[0]}" + ("\n" + "\n".join(tail[1:]) if len(tail) > 1 else "")
    return body
