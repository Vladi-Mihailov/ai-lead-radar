"""Стиль ЛС-черновиков (reply_style.py): обязательное приветствие, оба ресурса
в КАЖДОМ готовом черновике insurance / fuel / border_queue, нативные и
варьирующиеся формулировки, личный опыт с @tplgee по дате, разговорный
insurance, ответ на каждую часть вопроса. Реальные production-кейсы
(#760, #847, #877, #982, #1015). Без OpenAI/Telegram."""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from _dm_reply_helpers import footer
from test_dm_campaign_relevance import Service, _draft
from test_dm_shared_intent_gate import (  # noqa: F401  (env — fixture)
    _candidate,
    _run,
    env,
)

from reader.dm_campaigns.border import greeting_for
from reader.dm_campaigns.draft_models import DmDraftOutputExpert
from reader.dm_campaigns.draft_prompt import (
    SYSTEM_PROMPT_BORDER,
    SYSTEM_PROMPT_EXPERT,
    SYSTEM_PROMPT_FUEL,
)
from reader.dm_campaigns.outreach_repository import STATUS_DRAFT
from reader.dm_campaigns.reply_style import (
    advertising_tone,
    insurance_legalese,
    personal_when,
    protocol_line,
    resource_footer,
    tplgee_as_medical,
    tplgee_line,
    unanswered_topics,
)

RES = ("@tplgee", "@ProtocolGEbot")
Q760 = ("Добрый день! Подкажите где купить страховку в Грузию на машину, официальный сайт не грузит( Или лучше "
        "после пересечения границы по факту?")
Q847 = ("Всем привет\n\nПодскажите  обязательна ли мед страховка в Грузии ? И какие последствия ее отсутствия при "
        "выезде с Грузии?")
Q982 = "Где сделать мед страховку и страховку на авто"
Q877 = "Вечер добрый. Сколько стоит дизель в Грузии?"
Q1015 = "Кстати, видел на некоторых Азс дизель на 5-7лир дешевле, залил пол бака. Пипец звук как на тракторе стал😀"


class Expert(Service):
    """insurance: генерация — схема с intent (как DmDraftOutputExpert)."""

    async def generate(self, user_text, **kwargs):
        out = await super().generate(user_text, **kwargs)
        if kwargs.get("text_format") is not None and kwargs["text_format"].__name__ == "DmIntentOutput":
            return out
        return DmDraftOutputExpert(**out.model_dump(), intent="QUESTION")


def _saved(env, oid):
    row = env[1].get(oid)
    assert row.status == STATUS_DRAFT, (row.status, row.error)
    return row.primary_text


def _both_once(text):
    assert text.count("@tplgee") == 1 and text.count("@ProtocolGEbot") == 1, text


# ---------------- 1. приветствие ----------------


@pytest.mark.parametrize("source,greeting", [
    ("Добрый вечер. Нужна страховка", "Добрый вечер!"), ("Всем привет, как обстановка?", "Привет!"),
    ("Добрый день, бензин есть?", "Добрый день!"), ("Доброе утро! Очередь есть?", "Доброе утро!"),
    ("Вечер добрый. Сколько стоит дизель?", "Добрый вечер!"), ("Сколько стоит дизель?", "Здравствуйте!"),
])
def test_every_dm_starts_with_mirrored_greeting(source, greeting):
    assert greeting_for(source) == greeting
    assert greeting_for(source, continuation=True) == greeting  # и в продолжении ветки


# ---------------- 10. личный опыт с @tplgee ----------------


@pytest.mark.parametrize("today,when", [
    (date(2026, 10, 6), "вчера"), (date(2026, 10, 7), "позавчера"), (date(2026, 10, 8), "несколько дней назад"),
    (date(2026, 10, 11), "несколько дней назад"), (date(2026, 10, 12), "недавно"), (date(2026, 11, 4), "недавно"),
    (date(2026, 11, 5), ""),
])
def test_personal_when_is_relative_to_purchase_date(today, when):
    assert personal_when(today, date(2026, 10, 5)) == when


def test_old_purchase_has_no_stale_relative_word():
    line = tplgee_line("Как очередь?", (), seed=0, today=date(2027, 1, 1))
    assert "я оформлял тут: @tplgee" in line and "позавчера" not in line and "вчера" not in line


def test_purchase_date_is_configurable():
    from reader.settings import DmOutreachSettings

    assert DmOutreachSettings().tplgee_purchase_date == date(2026, 10, 5)
    assert DmOutreachSettings(tplgee_purchase_date="2026-10-08").tplgee_purchase_date == date(2026, 10, 8)


# ---------------- 9, 11–14. ресурсы: нативно, с вариациями ----------------


def test_footer_wording_varies_between_drafts_but_is_stable_per_draft():
    footers = {resource_footer("Как очередь?", (), campaign_resources=RES, seed=s, today=date(2026, 10, 9))
               for s in range(12)}
    assert len({f[0] for f in footers}) == 3 and len({f[1] for f in footers}) == 4
    first = resource_footer("Как очередь?", (), campaign_resources=RES, seed=5, today=date(2026, 10, 9))
    assert first == resource_footer("Как очередь?", (), campaign_resources=RES, seed=5, today=date(2026, 10, 9))
    assert all("@tplgee" in f[0] and "@ProtocolGEbot" in f[1] for f in footers)


def test_protocol_line_has_four_natural_variants():
    lines = {protocol_line(seed=s) for s in range(12)}
    assert len(lines) == 4 and all("@ProtocolGEbot" in line and "грузи" in line.lower() for line in lines)


def test_medical_only_question_gets_auto_specific_tplgee():
    line = tplgee_line("Мед страховку спрашивают на границе?", (), seed=0, today=date(2026, 10, 9))
    assert line.startswith("Если едете на машине, автостраховку") and "@tplgee" in line


def test_armenia_route_gets_armenia_tplgee():
    line = tplgee_line("Едем через Грузию в Ереван, как очередь?", (), seed=0, today=date(2026, 10, 9))
    assert line == "Не забудьте про страховку — для поездки в Армению её тоже можно оформить через @tplgee."


@pytest.mark.parametrize("text", [
    "Медстраховку оформите через @tplgee.", "Медицинскую страховку можно сделать тут: @tplgee.",
])
def test_tplgee_as_medical_is_detected(text):
    assert tplgee_as_medical(text)


def test_tplgee_for_auto_next_to_medical_is_ok():
    assert not tplgee_as_medical("Медстраховка обязательна. Автостраховку оформить можно тут: @tplgee.")


@pytest.mark.parametrize("text", [
    "Наш сервис оформит полис.", "У нас можно оформить страховку.", "Предлагаем вам полис.", "Переходите в бот.",
    "Воспользуйтесь нашим ботом.",
])
def test_advertising_tone_is_detected(text):
    assert advertising_tone(text)


# ---------------- 2–3. insurance: без юридического языка ----------------


@pytest.mark.parametrize("text", [
    "Для машины на иностранных номерах автостраховка должна действовать весь срок нахождения в Грузии.",
    "Полис нужен на весь срок пребывания в стране.", "Это требование Грузии.",
    "За езду без действующего полиса предусмотрен штраф 100 лари.",
])
def test_insurance_legalese_is_detected(text):
    assert insurance_legalese(text, "Нужна страховка в Грузию?")


def test_legal_wording_allowed_when_question_is_about_term_or_plates():
    assert not insurance_legalese("Автостраховка нужна на весь срок пребывания в Грузии.",
                                  "На какой срок нужна страховка?")


def test_insurance_prompt_has_conversational_style_and_all_questions_rule():
    for rule in ("«Да, автостраховка нужна. Если её нет — штраф 100 лари.»", "«Оформить можно тут: @tplgee.»",
                 "ответь на КАЖДЫЙ", "Разговорный язык ВАЖНЕЕ примеров тона", "никогда не предлагай его для медстраховки"):
        assert rule in SYSTEM_PROMPT_EXPERT


def test_border_and_fuel_prompts_require_answering_every_question():
    assert "Найди ВСЕ самостоятельные вопросы USER и ответь на КАЖДЫЙ" in SYSTEM_PROMPT_BORDER
    assert "Найди ВСЕ самостоятельные вопросы USER и ответь на КАЖДЫЙ" in SYSTEM_PROMPT_FUEL


# ---------------- 4–6. ответ на каждую часть вопроса ----------------


@pytest.mark.parametrize("source,answer,missing", [
    (Q847, "Да, медстраховка обязательна.", ("последствия",)),
    (Q847, "Да, медстраховка обязательна. По последствиям на выезде проверенной информации у меня нет.", ()),
    (Q982, "Медстраховка обязательна.", ("автостраховка",)),
    (Q982, "Медстраховка обязательна. Автостраховку оформить можно тут: @tplgee.", ()),
    (Q760, "Да, автостраховка нужна.", ("до/после границы",)),
    (Q760, "Ждать границы не обязательно — оформить можно онлайн заранее: @tplgee.", ()),
    (Q877, "Заправляйтесь заранее.", ("цена",)),
    (Q877, "Точную цену по Грузии не назову — она отличается по сетям; в каком вы городе?", ()),
    ("Как по погоде на Верхнем Ларсе?", "Ларс открыт, очереди нет.", ("погода",)),
    ("На летней резине примерно до каких пор безопасно Ларс проезжать?", "Сейчас снега нет.", ("до каких пор",)),
    ("Бензина почти не осталось, а до границы далеко", "Ближайшие АЗС работают.", ()),  # рассказ — не вопрос
])
def test_unanswered_question_parts(source, answer, missing):
    assert unanswered_topics(source, answer) == missing


# ---------------- 7, 17. оба ресурса в СОХРАНЁННОМ тексте всех трёх кампаний ----------------


def test_760_insurance_conversational_answer_covers_both_parts_and_resources(env):
    legal = ("Для машины на иностранных номерах автостраховка должна действовать весь срок нахождения в Грузии. "
             "Оформляйте полис онлайн через @tplgee.")
    good = ("Ждать границы не обязательно — автостраховку можно оформить онлайн заранее. Оформить можно тут: "
            "@tplgee.")
    oid = _candidate(env, "insurance", Q760, msg_id=801)
    service = Expert("HELP_REQUEST", _draft(legal), _draft(good))
    _run(env, service)
    text = _saved(env, oid)
    assert text.startswith("Добрый день! Ждать границы не обязательно")
    assert "иностранных номерах" not in text and "весь срок" not in text
    _both_once(text)
    assert len(service.generation_inputs) == 2 and "без юридического языка" in service.generation_inputs[1]


def test_847_insurance_both_questions_answered(env):
    partial = "Да, с 1 января 2026 года медстраховка для въезда в Грузию обязательна."
    full = partial + " По последствиям на выезде проверенной информации у меня нет."
    oid = _candidate(env, "insurance", Q847, msg_id=802)
    service = Expert("QUESTION", _draft(partial), _draft(full))
    _run(env, service)
    text = _saved(env, oid)
    assert text.startswith(f"Привет! {full}") and "ответь на КАЖДУЮ часть" in service.generation_inputs[1]
    _both_once(text)
    assert "Если едете на машине, автостраховку" in text  # @tplgee — явно про авто


def test_982_insurance_medical_and_auto_both_answered(env):
    partial = "С 1 января 2026 года медицинская страховка для въезда в Грузию обязательна."
    full = partial + " Автостраховку оформить можно тут: @tplgee."
    oid = _candidate(env, "insurance", Q982, msg_id=803)
    service = Expert("HELP_REQUEST", _draft(partial), _draft(full))
    _run(env, service)
    text = _saved(env, oid)
    assert text.startswith(f"Здравствуйте! {full}")
    _both_once(text)
    assert not tplgee_as_medical(text)


def test_insurance_without_any_resource_gets_both_from_server(env):
    oid = _candidate(env, "insurance", "Добрый вечер. Нужна страховка в Грузию?", msg_id=804)
    _run(env, Expert("QUESTION", _draft("Да, автостраховка нужна. Если её нет — штраф 100 лари.")))
    text = _saved(env, oid)
    assert text == ("Добрый вечер! Да, автостраховка нужна. Если её нет — штраф 100 лари. "
                    f"{footer(oid, 'Добрый вечер. Нужна страховка в Грузию?')}")
    _both_once(text)


def test_877_fuel_price_answered_or_limited_honestly_with_resources(env):
    honest = "Точную цену дизеля по Грузии не назову — она отличается по сетям и городам; в каком вы городе?"
    oid = _candidate(env, "fuel", Q877, msg_id=805)
    service = Service("QUESTION", _draft("Заправляйтесь заранее и не доводите запас до минимума."), _draft(honest))
    _run(env, service)
    text = _saved(env, oid)
    assert text == f"Добрый вечер! {honest} {footer(oid, Q877)}"
    _both_once(text)


def test_1015_fuel_outside_georgia_route_still_gets_greeting_and_both_resources(env):
    answer = "Дешёвый дизель на отдельных АЗС бывает худшего качества — заправляйтесь на проверенных."
    oid = _candidate(env, "fuel", Q1015, msg_id=806)
    _run(env, Service("PERSONAL_PROBLEM", _draft(answer)))
    text = _saved(env, oid)
    assert text.startswith("Здравствуйте! ")
    _both_once(text)


@pytest.mark.parametrize("key,source,answer", [
    ("border_queue", "Какая сейчас очередь на Ларсе?", "Верхний Ларс открыт, критичных очередей сейчас нет."),
    ("fuel", "Где заправиться дизелем на М4?", "На М4 заправки работают, заправляйтесь заранее."),
    ("insurance", "Нужна страховка на машину?", "Да, автостраховка нужна. Если её нет — штраф 100 лари."),
])
def test_every_campaign_final_text_contains_both_resources(env, key, source, answer):
    oid = _candidate(env, key, source, msg_id=810 + len(key))
    service = Expert("QUESTION", _draft(answer)) if key == "insurance" else Service("QUESTION", _draft(answer))
    _run(env, service)
    final_text = _saved(env, oid)
    assert "@tplgee" in final_text
    assert "@ProtocolGEbot" in final_text
    _both_once(final_text)
    assert final_text.index(answer) < final_text.index("@tplgee")  # ответ раньше ресурсов


def test_advertising_tone_gets_one_repair(env):
    oid = _candidate(env, "fuel", "Где заправиться дизелем на М4?", msg_id=830)
    service = Service("QUESTION", _draft("На М4 заправки работают. Наш сервис подскажет лучшие АЗС."),
                      _draft("На М4 заправки работают, заправляйтесь заранее."))
    _run(env, service)
    assert "Наш сервис" not in _saved(env, oid) and "без рекламного тона" in service.generation_inputs[1]


@pytest.mark.parametrize("source,answer,missing", [
    ("Ребята как по погоде на верхнем Ларсе", "Ларс открыт, очереди нет.", ("погода",)),  # вопрос без «?»
    ("В начале ноября и середина ноября на Ларсе бывает снег? Ехать на летней или зимней резине ?",
     "Про снег в начале и середине ноября прямо сейчас не подскажу.", ("резина",)),
    ("В начале ноября и середина ноября на Ларсе бывает снег? Ехать на летней или зимней резине ?",
     "Про снег в ноябре не подскажу; надёжнее ехать на зимней резине.", ()),
    ("Добрый вечер !на летней резине (полный привод )проеду ?", "Ларс открыт.", ("резина",)),
    ("Кстати, видел на некоторых Азс дизель дешевле, залил пол бака. Звук как на тракторе стал", "Ок.", ()),
])
def test_unanswered_question_parts_from_production(source, answer, missing):
    assert unanswered_topics(source, answer) == missing
