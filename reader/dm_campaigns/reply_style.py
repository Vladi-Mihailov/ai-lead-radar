"""Стиль готового ЛС-черновика (insurance / fuel / border_queue), без
изменения фактической базы:

- обязательный «подвал» с ОБОИМИ ресурсами (@tplgee, @ProtocolGEbot) —
  нативно, как совет участника чата, с вариациями формулировок (стабильно
  для одного черновика, разные — между черновиками);
- личный опыт с @tplgee с относительной датой («вчера», «позавчера»,
  «несколько дней назад»…), считается детерминированно от даты оформления;
- @tplgee — только автостраховка (медстраховку через него не предлагать);
- проверки: юридический язык insurance, @tplgee как медстраховка,
  неотвеченная часть вопроса USER (по явным признакам вопроса)."""

import re
from collections.abc import Iterable
from datetime import date

from reader.dm_campaigns.resources import PROTOCOL_GE, TPLGEE
from reader.insurance_matching import is_non_auto_insurance_text

_I = re.IGNORECASE

# Дата последнего личного оформления через @tplgee (факт от оператора).
TPLGEE_PERSONAL_PURCHASE_DATE = date(2026, 10, 5)


def personal_when(today: date, purchased: date = TPLGEE_PERSONAL_PURCHASE_DATE) -> str:
    """«вчера» / «позавчера» / «несколько дней назад» / «недавно» / "" (давно)."""
    days = (today - purchased).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    if days == 2:
        return "позавчера"
    if days <= 6:
        return "несколько дней назад"
    if days <= 30:
        return "недавно"
    return ""


def _i_did(when: str) -> str:
    return f"я {when} оформлял" if when else "я оформлял"


_ARMENIA_RE = re.compile(r"армени|ереван|armenia", _I)
_IN_GEORGIA_RE = re.compile(
    r"(?:выезжа\w*|еду|едем|возвраща\w*|выехал\w*|уезжа\w*)\s+(?:с|из)\s+грузи|(?:с|из)\s+грузии\s+(?:в|на)\s+"
    r"(?:рф|росси)|нахож\w*\s+в\s+грузи|(?:сейчас|уже)\s+в\s+грузи|по\s+грузии", _I)


def tplgee_line(source_text: str, reply_texts: Iterable[str], *, seed: int, today: date,
                purchased: date = TPLGEE_PERSONAL_PURCHASE_DATE) -> str:
    texts = [source_text or "", *reply_texts]
    did = _i_did(personal_when(today, purchased))
    if is_non_auto_insurance_text(source_text or ""):
        # вопрос только о медстраховке: @tplgee — явно про АВТОстраховку
        return f"Если едете на машине, автостраховку {did} тут: {TPLGEE}."
    if any(_ARMENIA_RE.search(t) for t in texts):
        return f"Не забудьте про страховку — для поездки в Армению её тоже можно оформить через {TPLGEE}."
    if any(_IN_GEORGIA_RE.search(t) for t in texts):
        return f"Если страховки ещё нет — без неё в Грузии штраф 100 лари; {did} тут: {TPLGEE}."
    variants = (
        f"Не забудьте про страховку — {did} тут: {TPLGEE}.",
        f"Если страховка ещё не сделана — {did} тут: {TPLGEE}.",
        f"Со страховкой лучше заранее: {did} тут: {TPLGEE}.",
    )
    return variants[seed % len(variants)]


_PROTOCOL_VARIANTS = (
    f"Штрафы по Грузии можно проверять через {PROTOCOL_GE}.",
    f"Если хотите следить за штрафами в Грузии — удобно через {PROTOCOL_GE}.",
    f"А грузинские штрафы я проверяю через {PROTOCOL_GE}.",
    f"За штрафами по Грузии можно следить через {PROTOCOL_GE}.",
)


def protocol_line(*, seed: int) -> str:
    return _PROTOCOL_VARIANTS[(seed // 3) % len(_PROTOCOL_VARIANTS)]


def resource_footer(source_text: str, reply_texts: Iterable[str], *, campaign_resources: Iterable[str], seed: int,
                    today: date, purchased: date = TPLGEE_PERSONAL_PURCHASE_DATE) -> tuple[str, ...]:
    """Обязательный подвал insurance / fuel / border_queue: оба ресурса (из
    настроек кампании), 1–2 коротких предложения."""
    replies = tuple(reply_texts)
    handles = {r.strip().lstrip("@").lower() for r in campaign_resources}
    lines = []
    if TPLGEE.lstrip("@").lower() in handles:
        lines.append(tplgee_line(source_text, replies, seed=seed, today=today, purchased=purchased))
    if PROTOCOL_GE.lstrip("@").lower() in handles:
        lines.append(protocol_line(seed=seed))
    return tuple(lines)


# ---------------- проверки стиля и полноты ----------------

# Юридические обороты insurance — только если сам вопрос о сроке/номерах.
_INSURANCE_LEGALESE_RE = re.compile(
    r"для\s+(?:машин\w*|автомобил\w*)\s+на\s+иностранн\w*\s+номер|(?:весь|на\s+весь)\s+срок\s+(?:нахождени|пребывани)"
    r"|это\s+требование\s+(?:действует|грузии)|требование\s+действует|предусмотрен\w*\s+штраф"
    r"|за\s+езду\s+без\s+(?:действующ|обязательн)",
    _I,
)
_LEGAL_QUESTION_RE = re.compile(r"(?<!\w)срок|на\s+сколько\s+(?:дней|времени)|какой\s+срок|номер|регистрац", _I)


def insurance_legalese(text: str, source_text: str) -> bool:
    return bool(_INSURANCE_LEGALESE_RE.search(text or "")) and not _LEGAL_QUESTION_RE.search(source_text or "")


_MEDICAL_RE = re.compile(r"(?<!\w)мед\w*\.?\s*страх|медстрах|медицин", _I)
_AUTO_RE = re.compile(r"авто|машин|осаго|каско", _I)


def tplgee_as_medical(text: str) -> bool:
    """@tplgee в одном предложении с медстраховкой без указания на авто."""
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        if "@tplgee" in sentence.lower() and _MEDICAL_RE.search(sentence) and not _AUTO_RE.search(sentence):
            return True
    return False


# Явные признаки части вопроса -> что должно быть в ответе по этой части.
_QUESTION_TOPICS = (
    ("медстраховка",
     re.compile(r"(?<!\w)мед\w*\.?\s*страх|медстрах|медицинск\w*\s+страх", _I),
     re.compile(r"мед|медицин", _I)),
    ("последствия",
     re.compile(r"последстви|что\s+будет|чем\s+грозит|какой\s+штраф|будет\s+ли\s+штраф|штраф\w*\s+или", _I),
     re.compile(r"штраф|последств|грозит|не\s+выпуст|не\s+пуст|сведени|информаци", _I)),
    ("до/после границы",
     re.compile(r"(?:лучше|или)\s+(?:\w+\s+){0,2}(?:до|после)\s+(?:пересечени|границ|таможн|въезд)"
                r"|лучше\s+(?:после|заранее|на\s+границе)|на\s+границе\s+(?:купить|сделать|оформить)", _I),
     re.compile(r"заранее|до\s+(?:поездки|границы|пересечения|въезда|выезда)|после\s+(?:границы|пересечения|таможни)"
                r"|на\s+границе|онлайн|не\s+обязательно\s+ждать", _I)),
    ("цена",
     re.compile(r"сколько\s+стоит|(?<!\w)цен[аыу](?!\w)|почём|почем|стоимост", _I),
     re.compile(r"цен|стоим|стоит|лари|₾|руб|\d", _I)),
    ("погода",
     re.compile(r"погод", _I),
     re.compile(r"погод|снег|дожд|тепл|холод|мороз|ясн|солн|туман|гололед|гололёд|осадк", _I)),
    ("до каких пор",
     re.compile(r"до\s+каких\s+пор|до\s+какого\s+(?:числа|времени|месяца)|до\s+какой\s+даты|как\s+долго", _I),
     re.compile(r"до\s+\w+|ноябр|декабр|октябр|январ|числ|недел|месяц|пока|сроки|заранее", _I)),
    ("резина",
     re.compile(r"летн\w*\s+или\s+(?:на\s+)?зимн|зимн\w*\s+или\s+(?:на\s+)?летн"
                r"|на\s+летн\w*(?:\s+резин\w*)?[^?.]{0,30}?(?:проеду|можно|пройду|нормально)"
                r"|какую\s+резину|(?:нужн\w*|ставить)\s+(?:ли\s+)?зимн", _I),
     re.compile(r"резин|шин|зимн|летн|липучк", _I)),
)


def unanswered_topics(source_text: str, answer: str) -> tuple[str, ...]:
    """Части вопроса USER (по явным признакам), на которые в ответе нет даже
    упоминания. Неполный, но безопасный детерминированный контроль: что не
    распознано — проверяет промпт и одна исправляющая попытка."""
    questions = " ".join(_question_parts(source_text))
    missing = []
    for name, asked, answered in _QUESTION_TOPICS:
        if asked.search(questions) and not answered.search(answer or ""):
            missing.append(name)
    # «мед страховку И страховку на авто» — обе части
    if _QUESTION_TOPICS[0][1].search(questions) and re.search(
            r"(?:и|а)\s+(?:страховк\w*\s+)?(?:на\s+)?(?:авто|машин)|автострах", questions, _I) \
            and not _AUTO_RE.search(answer or ""):
        missing.append("автостраховка")
    return tuple(missing)


_QUESTION_START_RE = re.compile(
    # вопросительное слово в начале — допускается после обращения («Ребята, как по погоде…»)
    r"^\s*(?:(?:ребят\w*|друзья|народ|люди|всем\s+привет|привет|здравствуйте|добр\w+\s+\w+)[,!.\s]+)?"
    r"(?:и\s+|а\s+|или\s+)?(?:подскаж|скаж|как|где|сколько|почем|почём|обязательн|какие|какой|какая|можно|нужн"
    r"|есть\s+ли|будет\s+ли|до\s+каких|когда|куда|зачем|почему|что|ехать\s+на)", _I)


def _question_parts(text: str) -> list[str]:
    """Предложения-вопросы USER (со «?» или с вопросительного слова) —
    рассказ «а до границы далеко» вопросом не считается."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [p for p in parts if p.strip() and ("?" in p or _QUESTION_START_RE.search(p))]


# Рекламный тон: мы советуем как участник чата, а не продаём.
_AD_TONE_RE = re.compile(
    r"наш\w*\s+(?:сервис|бот|компани|партн)|у\s+нас\s+можно|предлагаем|переходите|воспользуйтесь\s+нашим"
    r"|выгодн\w*\s+предложени|скидк", _I)


def advertising_tone(text: str) -> bool:
    return bool(_AD_TONE_RE.search(text or ""))
