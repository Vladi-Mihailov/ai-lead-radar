"""Обязательный подвал с ресурсами (fuel / border_queue) для поездки,
связанной с Грузией, приветствие во всех ЛС-кампаниях и порядок «сначала
ответ, потом ресурсы» (reader/dm_campaigns/border.py). Реальные production-
сообщения (обезличены). Без OpenAI/Telegram: модель — фейк."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from test_dm_border_expert import _insert
from test_dm_campaign_relevance import Service, _draft
from test_dm_shared_intent_gate import _candidate, _run, env  # noqa: F401  (env — fixture)

from reader.dm_campaigns.border import (
    PROTOCOL_GE_LINE,
    TPLGEE_LINE,
    TPLGEE_REMINDER_LINE,
    BorderPolicy,
    footer_lines,
    georgia_trip,
)
from reader.dm_campaigns.draft_models import DmDraftOutputExpert, normalize_draft
from reader.dm_campaigns.draft_prompt import SYSTEM_PROMPT_EXPERT, SYSTEM_PROMPT_FUEL, system_prompt_for
from reader.dm_campaigns.outreach_repository import STATUS_DRAFT

FOOTER = f"{TPLGEE_LINE}\n{PROTOCOL_GE_LINE}"
REMINDER_FOOTER = f"{TPLGEE_REMINDER_LINE}\n{PROTOCOL_GE_LINE}"
RESOURCES = ("@tplgee", "@ProtocolGEbot", "@ProtocolTRbot")

Q394 = ("Ребята, привет! Те кто на автодомах подскажите по какой очереди вы проходили, нас на РФ отправили с "
        "грузовиками проходить")
Q393 = "Доброе утро. Подскажите пожалуйста как обстановка на въезд в Россию? Большая очередь?"
Q387 = "Утро доброе всем! Выезжаю с Грузии 🇬🇪 подскажите пожалуйста как дорога, и пробки!"
Q350 = "Здравствуйте подскажите пожалуйста на дорогу как бензин есть или нет??"
Q338 = "Всех приветствую как дела с бензином хочу с Питера выехать в Грузию спасибо заранее 🤲"
Q386 = "Подскажите в каком ларьке дешевле страховка после грузинской таможни"


def _fuel(env, text, msg_id):
    return _candidate(env, "fuel", text, msg_id=msg_id)


def _text(env, oid):
    row = env[1].get(oid)
    assert row.status == STATUS_DRAFT, (row.status, row.error)
    return row.primary_text


def _answer_before_cta(text, answer):
    first_handle = min(i for i in (text.find("@tplgee"), text.find("@ProtocolGEbot")) if i >= 0)
    return text.index(answer) < first_handle


# ---------------- border_queue ----------------


def test_394_motorhome_queue_gets_answer_then_footer(env):
    answer = ("Если вас уже направили в грузовую очередь, проходите по ней — для автодомов порядок пропуска может "
              "зависеть от категории автомобиля.")
    oid = _insert(env, Q394, msg_id=601)
    _run(env, Service("QUESTION", _draft(answer)))
    text = _text(env, oid)
    assert text == f"Привет! {answer} {FOOTER}"
    assert _answer_before_cta(text, answer)


def test_393_entry_to_russia_gets_greeting_and_footer(env):
    answer = "Верхний Ларс открыт, критичной очереди на въезд в Россию сейчас нет."
    oid = _insert(env, Q393, msg_id=602)
    _run(env, Service("QUESTION", _draft(answer)))
    assert _text(env, oid) == f"Доброе утро! {answer} {FOOTER}"


def test_387_leaving_georgia_gets_insurance_reminder_with_approved_fine(env):
    answer = "Верхний Ларс открыт, дорога проезжая, критичных пробок сейчас нет."
    oid = _insert(env, Q387, msg_id=603)
    _run(env, Service("QUESTION", _draft(answer)))
    text = _text(env, oid)
    assert text == f"Доброе утро! {answer} {REMINDER_FOOTER}"
    assert "штраф 100 лари" in text and "@tplgee" in text and "@ProtocolGEbot" in text
    for unapproved in ("гарантир", "обязательно выпишут", "на выезде", "на границе выпиш"):
        assert unapproved not in text


def test_387_model_sentence_about_fine_mechanics_is_removed(env):
    oid = _insert(env, Q387, msg_id=604)
    _run(env, Service("QUESTION", _draft("Ларс открыт, дорога проезжая. На выезде штраф выпишут обязательно.")))
    text = _text(env, oid)
    assert "выпишут" not in text and text.endswith(REMINDER_FOOTER)


# ---------------- fuel ----------------


def test_350_fuel_on_the_way_gets_footer(env):
    answer = ("По дороге в Грузию заправки есть, но на отдельных участках с нужным бензином бывают перебои. Лучше "
              "заправляться заранее и не доводить запас топлива до минимума.")
    oid = _fuel(env, Q350, 605)
    _run(env, Service("QUESTION", _draft(answer)))
    text = _text(env, oid)
    assert text == f"Здравствуйте! {answer} {FOOTER}"
    assert _answer_before_cta(text, answer)


def test_338_fuel_from_piter_to_georgia_gets_footer(env):
    answer = ("По маршруту из Петербурга в Грузию заправки есть. На отдельных участках лучше заправляться заранее и "
              "держать нормальный запас хода.")
    oid = _fuel(env, Q338, 606)
    _run(env, Service("QUESTION", _draft(answer)))
    assert _text(env, oid) == f"Приветствую! {answer} {FOOTER}"


def test_fuel_uses_expert_prompt_and_rejects_source_disclosure(env):
    assert system_prompt_for("fuel") is SYSTEM_PROMPT_FUEL
    oid = _fuel(env, Q350, 607)
    service = Service("QUESTION", _draft("В свежих сообщениях группы пишут, что бензин есть."),
                      _draft("По дороге в Грузию заправки есть."))
    _run(env, service)
    assert _text(env, oid).startswith("Здравствуйте! По дороге в Грузию заправки есть.")
    assert len(service.generation_inputs) == 2 and "не раскрывай источник" in service.generation_inputs[1]


def test_fuel_inside_russia_gets_no_georgia_footer(env):
    oid = _fuel(env, "Кто ехал недавно из Ростова на авто - 95 вообще реально поймать по пути на заправках?", 608)
    _run(env, Service("QUESTION", _draft("По трассе из Ростова 95-й на сетевых АЗС есть, заправляйтесь заранее.")))
    text = _text(env, oid)
    assert "@tplgee" not in text and "@ProtocolGEbot" not in text and text.startswith("Здравствуйте! ")


# ---------------- insurance ----------------


def test_386_insurance_direct_answer_first_then_resources(env):
    answer = ("Искать ларёк после таможни необязательно — страховку можно оформить онлайн через @tplgee. По "
              "штрафам в Грузии можно пользоваться @ProtocolGEbot.")
    oid = _candidate(env, "insurance", Q386, msg_id=609)

    class Expert(Service):
        async def generate(self, user_text, **kwargs):
            out = await super().generate(user_text, **kwargs)
            if kwargs.get("text_format") is not None and kwargs["text_format"].__name__ == "DmIntentOutput":
                return out
            return DmDraftOutputExpert(**out.model_dump(), intent="HELP_REQUEST")

    _run(env, Expert("HELP_REQUEST", _draft(answer)))
    text = _text(env, oid)
    assert text == f"Здравствуйте! {answer}"
    assert text.index("Искать ларёк") < text.index("@tplgee")
    assert text.count("@tplgee") == 1 and text.count("@ProtocolGEbot") == 1


def test_insurance_prompt_requires_direct_answer_first():
    assert "Сначала прямой ответ именно на вопрос USER" in SYSTEM_PROMPT_EXPERT
    assert "Искать ларёк после таможни необязательно" in SYSTEM_PROMPT_EXPERT


# ---------------- логика подвала ----------------


@pytest.mark.parametrize("text,chat,expected", [
    ("Едем через Верхний Ларс завтра, какая очередь?", None, True),
    ("Россия → Грузия, как дорога?", None, True),
    ("Из Грузии в Россию, есть пробки?", None, True),
    ("Едем из Еревана в Москву, как бензин по пути?", None, True),
    ("Из Москвы в Армению на машине, как дорога?", None, True),
    ("Как по Грузии с бензином?", None, True),
    ("Какая обстановка?", "VerhniyLars", True),
    ("Кто ехал недавно из Ростова — 95 реально поймать?", "VerhniyLars", False),
    ("Из Москвы лучше по М4 или через Волгоград?", "VerhniyLars", False),
    ("Сарпи открыт в сторону Турции?", "sarpi_ge", False),
    ("Где заправиться в Стамбуле?", None, False),
])
def test_georgia_trip_detection(text, chat, expected):
    assert georgia_trip(text, (), chat) is expected
    lines = footer_lines(text, (), georgia=georgia_trip(text, (), chat), campaign_resources=RESOURCES)
    assert bool(lines) is expected


def test_footer_only_with_campaign_resources():
    assert footer_lines("Еду в Грузию", (), georgia=True, campaign_resources=("@ProtocolGEbot",)) == (PROTOCOL_GE_LINE,)
    assert footer_lines("Еду в Грузию", (), georgia=True, campaign_resources=()) == ()


def _policy(kind="fuel", cta=(TPLGEE_LINE, PROTOCOL_GE_LINE), greeting="Здравствуйте!"):
    return BorderPolicy(kind=kind, greeting=greeting, cta=cta)


def _norm(text, policy):
    return normalize_draft(_draft(text), follow_up_enabled=False, valid_refs=frozenset(), fresh_context_used=False,
                           n_distinct_senders=0, allowed_resources=("@tplgee", "@ProtocolGEbot"),
                           border_policy=policy)


def test_resources_are_not_duplicated():
    text = "Заправки по пути есть. Страховку я делал тут @tplgee."
    decision = _norm(text, _policy())
    assert decision.primary_message.count("@tplgee") == 1
    assert decision.primary_message == f"Здравствуйте! {text} {PROTOCOL_GE_LINE}"


def test_answer_precedes_cta_and_greeting_is_first():
    decision = _norm("Заправки по пути есть.", _policy())
    text = decision.primary_message
    assert text.startswith("Здравствуйте! Заправки по пути есть.") and text.index("Заправки") < text.index("@tplgee")


def test_continuation_without_greeting_gets_no_greeting():
    decision = _norm("Да, по пути заправки есть.", _policy(greeting=None))
    assert decision.primary_message.startswith("Да, по пути заправки есть.")


def test_ad_only_answer_is_rejected_answer_comes_first():
    """Ответ одной рекламой не заменяет ответа — и в fuel/border_queue."""
    assert _norm("Не забудьте про страховку — я делал тут @tplgee.", _policy()).error == "ad_only_answer"
    assert _norm("Обратитесь в @tplgee.", _policy(kind="border_queue")).error == "ad_only_answer"
    assert _norm("Заправки по пути есть. Страховку я делал тут @tplgee.", _policy()).kind == "draft"


@pytest.mark.parametrize("source,greeting", [
    ("Утро доброе всем! Как дорога?", "Доброе утро!"), ("День добрый, бензин есть?", "Добрый день!"),
    ("Вечер добрый. Ларс открыт?", "Добрый вечер!"),
])
def test_reversed_greetings_are_mirrored(source, greeting):
    from reader.dm_campaigns.border import greeting_for

    assert greeting_for(source, continuation=False) == greeting


@pytest.mark.parametrize("text", [
    "Не уверен в текущем наличии бензина на маршруте из Питера в Грузию.",
    "Сложно сказать, есть ли сейчас 95-й.",
    "Не знаю, как сейчас с дизелем.",
    "О том, через какую очередь проходят автодома, конкретных указаний не поступало.",
])
def test_uncertainty_instead_of_answer_is_rejected(text):
    assert _norm(text, _policy()).error == "source_disclosure"


def test_fuel_prompt_mentions_canister_only_if_asked():
    assert "ТОЛЬКО если USER сам о них спросил" in SYSTEM_PROMPT_FUEL


# ---------------- неподтверждённые факты ----------------

CTX = "Ларс открыт, прошли за 40 минут. На М4 после Новочеркасска 95-го нет. Еду с Питера в Грузию"


def _strict(text, context=CTX):
    # fuel: та же проверка фактов, без тематической вырезки border_queue
    policy = BorderPolicy(kind="fuel", greeting="Привет!", cta=(TPLGEE_LINE, PROTOCOL_GE_LINE), context_text=context)
    return normalize_draft(_draft(text), follow_up_enabled=False, valid_refs=frozenset(), fresh_context_used=False,
                           n_distinct_senders=0, allowed_resources=("@tplgee", "@ProtocolGEbot"), border_policy=policy)


@pytest.mark.parametrize("text", [
    "По очереди для автодомов решение чаще всего принимают на месте.",
    "Автодома обычно направляют вместе с грузовиками.",
    "Как правило, пропускают без проблем.",
    "На М11 и М4 проблем не предвидится.",
    "Скорее всего, очередь небольшая.",
])
def test_unsupported_generalization_is_rejected(text):
    assert _strict(text).error == "unsupported_generalization"


@pytest.mark.parametrize("text,detail", [
    ("Перебои с бензином между Ставрополем и Элистой.", "Ставрополем"),
    ("На М11 заправки есть.", "М11"),
    ("Прошли за 25 минут.", "25"),
])
def test_unsupported_detail_is_rejected(text, detail):
    assert _strict(text).error == f"unsupported_detail:{detail}"


@pytest.mark.parametrize("text", [
    "На М4 после Новочеркасска 95-го сейчас нет.",
    "Ларс открыт, проходят примерно за 40 минут.",
    "По маршруту из Петербурга в Грузию заправки есть.",
    "Если вас уже направили в грузовую очередь, следуйте указанию сотрудников на месте.",
])
def test_supported_or_safe_answer_passes(text):
    assert _strict(text).kind == "draft"


def test_394_unsupported_operational_fact_gets_repaired(env):
    good = ("Если вас уже направили в грузовую очередь, следуйте указанию сотрудников на месте. Верхний Ларс "
            "открыт, критичных очередей сейчас нет.")
    oid = _insert(env, Q394, msg_id=650)
    service = Service("QUESTION", _draft("По очереди для автодомов решение чаще всего принимают на месте."),
                      _draft(good))
    _run(env, service)
    assert _text(env, oid) == f"Привет! {good} {FOOTER}"
    assert len(service.generation_inputs) == 2 and "без предположений" in service.generation_inputs[1]
