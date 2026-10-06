"""Тема кампании после intent-gate (relevance.py) и стиль генераторов
fuel / border_queue: лидовый intent не по теме кампании -> filtered
campaign_not_relevant без генерации; без оговорок и обещаний; неизвестные
метки контекста отбрасываются; не более одной исправляющей попытки.
Без OpenAI/Telegram: классификатор и генератор — фейки."""

import json
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from test_dm_shared_intent_gate import T0, _candidate, _observe, _run, env  # noqa: F401  (env — fixture)

from reader.dm_campaigns.draft_models import DmDraftOutput, DmIntentOutput, normalize_draft, strip_leading_hedge
from reader.dm_campaigns.draft_prompt import INTENT_CLASSIFIER_PROMPT, SYSTEM_PROMPT
from reader.dm_campaigns.outreach_repository import STATUS_DRAFT, STATUS_FAILED, STATUS_FILTERED
from reader.dm_campaigns.relevance import FILTER_CAMPAIGN_NOT_RELEVANT, campaign_relevant

# Реальные сообщения из production-буфера (offline eval), обезличены.
F4 = "Сегодня проехали М4 по платке. Все хорошо, но надо заправиться в Краснодарском крае, тк на КМВ могут быть проблемки"
B12 = "Здравствуйте кто будет проезжать через Пятигорск в сторону грузии. Хочу передать посылку не большую"
B35 = "Я сейчас буду выезжать и только сейчас понял что нет страховки"
B81 = ("Добрый день! Мы транзитом едем из России через Верхний Ларс. Какие продукты нельзя с собой провозить "
       "и что можно?")
B84 = "Доброго дня! Подскажите пожалуйста из Москвы до Владикавказа как обстоят дела с АИ-95. Очереди, наличие."


class Service:
    """Шаг 1 — фиксированная классификация; шаг 2 — черновики по очереди."""

    def __init__(self, intent="QUESTION", *drafts, has_own_problem=True):
        self.intent, self.has_own_problem = intent, has_own_problem
        self.drafts = list(drafts) or [_draft("Ответ по делу.")]
        self.generation_inputs = []

    async def generate(self, user_text, *, instructions=None, text_format=None):
        if text_format is DmIntentOutput:
            return DmIntentOutput(intent=self.intent, has_own_problem=self.has_own_problem, reason="test")
        self.generation_inputs.append(user_text)
        return self.drafts[min(len(self.generation_inputs), len(self.drafts)) - 1]


def _draft(text, refs=(), strength="none"):
    return DmDraftOutput(should_generate=True, skip_reason=None, primary_message=text, follow_up_message=None,
                         evidence_strength=strength, used_context_refs=list(refs))


# ---------------- тема кампании: правила ----------------


@pytest.mark.parametrize("text", [
    "Где заправиться дизелем после Ларса?", "Есть 95-й на трассе?", "Где ближайшая АЗС?",
    "Кто ехал недавно из Ростова на авто - 95 вообще реально поймать по пути на заправках?",
    "Можно ли провести в Грузию из России канистру с бензином", B84,
])
def test_fuel_relevant(text):
    assert campaign_relevant("fuel", text)


@pytest.mark.parametrize("text", [B12, B35, B81, "Ларс открыт?", "Как дорога после Ларса?"])
def test_fuel_not_relevant(text):
    assert not campaign_relevant("fuel", text)


@pytest.mark.parametrize("text", [
    "Какая сейчас очередь на Ларсе?", "Ларс открыт?", "Граница закрыта?", "Сколько стоять на КПП?",
    "Как на Ларсе снег?", "На летней резине проеду?", "Какое состояние дороги после Ларса в сторону Тбилиси?",
    "Стоим два часа и не двигаемся", "Подскажите какая обстановка на границе Грузия-Рф, много машин?",
    "Как обстановка на границе? Есть очереди из Грузии в Россию?",
])
def test_border_relevant(text):
    assert campaign_relevant("border_queue", text)


@pytest.mark.parametrize("text", [
    B12, B35, B81, B84,
    "Где можно обменять рубли и евро после Верхнего Ларса?",
    "Где купить сим-карту после границы?",
    "Нужна ли доверенность на машину на границе?",
    "Какие документы нужны на таможне?",
    "На русской границе Ларса проверяют страховку?",
    "Есть ли штрафы на машину после границы?",
])
def test_border_not_relevant(text):
    assert not campaign_relevant("border_queue", text)


def test_short_follow_up_takes_topic_from_reply_target():
    assert campaign_relevant("border_queue", "А сейчас?", ["Какая очередь на Ларсе?"])
    assert not campaign_relevant("border_queue", "А посылку передать можно?", ["Какая очередь на Ларсе?"])


# ---------------- реальные регрессии ----------------


def _audit(row):
    return json.loads(row.context_json)


def test_b12_parcel_help_request_is_not_border(env):
    oid = _candidate(env, "border_queue", B12)
    service = Service("HELP_REQUEST")
    _run(env, service)
    row = env[1].get(oid)
    assert (row.status, row.filter_reason) == (STATUS_FILTERED, FILTER_CAMPAIGN_NOT_RELEVANT)
    assert service.generation_inputs == []
    audit = _audit(row)
    assert (audit["final_intent"], audit["campaign_relevant"]) == ("HELP_REQUEST", False)


def test_b81_products_question_is_not_border(env):
    oid = _candidate(env, "border_queue", B81)
    service = Service("QUESTION")
    _run(env, service)
    row = env[1].get(oid)
    assert (row.filter_reason, _audit(row)["final_intent"]) == (FILTER_CAMPAIGN_NOT_RELEVANT, "QUESTION")
    assert service.generation_inputs == []


def test_b35_no_insurance_goes_to_insurance_only(env):
    assert (campaign_relevant("insurance", B35), campaign_relevant("border_queue", B35),
            campaign_relevant("fuel", B35)) == (True, False, False)
    campaigns, outreach, _ = env
    row = _observe(env, B35, msg_id=50)
    assert row.campaign_id == campaigns.get_campaign_by_key("insurance").id
    assert len([r for r in outreach.list_all() if r.source_message_id == 50]) == 1


def test_b35_in_border_queue_alone_is_filtered_without_generation(env):
    oid = _candidate(env, "border_queue", B35)
    service = Service("PERSONAL_PROBLEM")
    _run(env, service)
    assert env[1].get(oid).filter_reason == FILTER_CAMPAIGN_NOT_RELEVANT and service.generation_inputs == []


def test_fuel_queue_question_is_routed_to_fuel_not_border(env):
    """«Очереди» сработали как слово границы, но вопрос — про АИ-95 на трассе."""
    campaigns, _, _ = env
    row = _observe(env, B84, msg_id=51)
    assert row.campaign_id == campaigns.get_campaign_by_key("fuel").id


def test_classifier_prompt_has_trip_advice_and_own_fuel_need_examples():
    assert "надо заправиться в Краснодарском крае" in INTENT_CLASSIFIER_PROMPT
    assert "заканчивается бензин, где сейчас лучше заправиться?» — PERSONAL_PROBLEM" in INTENT_CLASSIFIER_PROMPT


@pytest.mark.parametrize("key", ["fuel", "border_queue"])
def test_f4_b22_trip_advice_is_not_a_lead(env, key):
    oid = _candidate(env, key, F4)
    service = Service("ADVICE", has_own_problem=False)
    _run(env, service)
    assert env[1].get(oid).filter_reason == "not_lead_intent:advice" and service.generation_inputs == []


def test_own_fuel_need_still_generates(env):
    oid = _candidate(env, "fuel", "У меня заканчивается бензин, где сейчас лучше заправиться?")
    _run(env, Service("PERSONAL_PROBLEM"))
    row = env[1].get(oid)
    assert row.status == STATUS_DRAFT and _audit(row)["campaign_relevant"] is True


# ---------------- стиль fuel / border_queue ----------------


def _norm(text, *, refs=(), strength="none", valid=frozenset({"B1", "B2"}), fresh=True, senders=2):
    return normalize_draft(_draft(text, refs, strength), follow_up_enabled=False, valid_refs=valid,
                           fresh_context_used=fresh, n_distinct_senders=senders, allowed_resources=())


@pytest.mark.parametrize("text,expected", [
    ("Точно подтвердить не могу. В свежих сообщениях нет данных о снеге на Ларсе.",
     "В свежих сообщениях нет данных о снеге на Ларсе."),
    ("Точно подтвердить не могу — в свежих сообщениях нет данных о 95-м по М4.",
     "В свежих сообщениях нет данных о 95-м по М4."),
])
def test_standalone_hedge_is_removed_deterministically(text, expected):
    assert strip_leading_hedge(text) == expected
    decision = _norm(text)
    assert (decision.kind, decision.primary_message) == ("draft", expected)


@pytest.mark.parametrize("text,error", [
    ("Я точно подтвердить не могу, есть ли бензин.", "dynamic_hedging"),
    ("Ориентируйтесь по официальным каналам.", "dynamic_hedging"),
    ("Уточните на всякий случай в официальных источниках.", "dynamic_hedging"),
    ("Укажите маршрут — посмотрю последние сообщения.", "future_promise"),
    ("Проверю и напишу.", "future_promise"),
    ("Уточню и сообщу.", "future_promise"),
])
def test_hedging_and_future_promises_are_rejected(text, error):
    assert _norm(text).error == error


@pytest.mark.parametrize("text", [
    "В свежих сообщениях нет данных о снеге на Ларсе.",
    "По этому участку свежих подтверждений наличия 95-го сейчас нет.",
    "Свежие сообщения расходятся: один участник пишет, что прошли за 7 минут, другой — что границу закрыли.",
    "В свежих сообщениях пишут, что очередь небольшая.",
])
def test_direct_answers_from_fresh_context_pass(text):
    assert _norm(text, refs=["B1"], strength="single_report").kind == "draft"


def test_generator_prompt_states_style_rules():
    for rule in ("В свежих сообщениях нет данных о снеге на Ларсе", "По этому участку свежих подтверждений",
                 "Свежие сообщения расходятся", "«посмотрю», «проверю»", "«уточню», «напишите маршрут — посмотрю»",
                 "ориентируйтесь по официальным каналам", "допустимый источник о текущей обстановке"):
        assert rule in SYSTEM_PROMPT


# ---------------- неизвестные метки контекста ----------------


def test_unknown_ref_is_dropped_not_replaced():
    decision = _norm("В свежих сообщениях пишут, что очередь небольшая.", refs=["B1", "B11"],
                     strength="several_consistent")
    assert (decision.kind, decision.used_context_refs) == ("draft", ("B1",))


def test_unknown_ref_without_any_real_support_is_still_rejected():
    decision = _norm("Несколько участников пишут, что очередь небольшая.", refs=["B11"], strength="several_consistent")
    assert (decision.kind, decision.error) == ("invalid", "unknown_context_refs:B11")


def test_unknown_ref_on_a_no_data_answer_is_dropped():
    decision = _norm("В свежих сообщениях нет данных о снеге на Ларсе.", refs=["B11"])
    assert (decision.kind, decision.used_context_refs) == ("draft", ())


# ---------------- одна исправляющая попытка ----------------


def test_future_promise_gets_one_repair_and_is_fixed(env):
    oid = _candidate(env, "fuel", "Где заправиться 95-м по М4?")
    service = Service("QUESTION", _draft("Напишите маршрут — посмотрю последние сообщения."),
                      _draft("По этому участку свежих подтверждений наличия 95-го сейчас нет."))
    _run(env, service)
    row = env[1].get(oid)
    assert (row.status, row.primary_text) == (STATUS_DRAFT,
                                              "По этому участку свежих подтверждений наличия 95-го сейчас нет.")
    assert len(service.generation_inputs) == 2 and "ПРЕДЫДУЩИЙ ЧЕРНОВИК ОТКЛОНЁН" in service.generation_inputs[1]


def test_repair_is_attempted_at_most_once(env):
    oid = _candidate(env, "border_queue", "Какая сейчас очередь на Ларсе?")
    bad = _draft("Уточню и сообщу.")
    service = Service("QUESTION", bad, bad, bad)
    _run(env, service, at=T0 + timedelta(minutes=4))
    row = env[1].get(oid)
    assert (row.status, row.error) == (STATUS_FAILED, "future_promise")
    assert len(service.generation_inputs) == 2


def test_not_repairable_error_has_no_repair(env):
    oid = _candidate(env, "fuel", "Где заправиться 95-м по М4?")
    service = Service("QUESTION", _draft("Пишите @some_other_bot"))
    _run(env, service)
    assert env[1].get(oid).status == STATUS_FAILED and len(service.generation_inputs) == 1


def test_relevance_check_makes_no_extra_openai_call(env):
    calls = []

    class Counting(Service):
        async def generate(self, user_text, **kwargs):
            calls.append(kwargs.get("text_format"))
            return await super().generate(user_text, **kwargs)

    _candidate(env, "border_queue", B12)
    _run(env, Counting("HELP_REQUEST"))
    assert calls == [DmIntentOutput]  # только классификация



# ---------------- B47: свой вопрос в ответ на чужое сообщение ----------------

B47 = "Если резина всесезонная со \"снежинкой\", разрешат проехать?"


def _reply_candidate(env, key, question, reply, *, base=60):
    _, _, recent = env
    recent.add(chat_id=-1001, chat_identifier="VerhniyLars", message_id=base, sender_id=111, text=question, date=T0,
               reply_to_msg_id=None)
    return _candidate(env, key, reply, msg_id=base + 1, sender=222, reply_to=base, at=T0 + timedelta(minutes=1))


def test_classifier_prompt_has_reply_with_own_question_rule():
    assert "Если резина всесезонная со «снежинкой», разрешат проехать?» в " in INTENT_CLASSIFIER_PROMPT
    assert "СОБСТВЕННЫЙ вопрос о своей поездке, проезде или ситуации" in INTENT_CLASSIFIER_PROMPT
    for not_own in ("«Проехали быстро»", "«Езжайте сейчас», «Я бы сделал так»"):
        assert not_own in INTENT_CLASSIFIER_PROMPT


def test_b47_reply_with_own_question_stays_a_border_lead(env):
    oid = _reply_candidate(env, "border_queue", "С 1 декабря обязательно", B47)
    service = Service("QUESTION", _draft("В свежих сообщениях нет данных о проезде на всесезонной резине."))
    _run(env, service, at=T0 + timedelta(minutes=5))
    row = env[1].get(oid)
    audit = _audit(row)
    assert row.status == STATUS_DRAFT and len(service.generation_inputs) == 1
    assert (audit["model_intent"], audit["final_intent"], audit["has_own_problem"], audit["campaign_relevant"]) == (
        "QUESTION", "QUESTION", True, True)


@pytest.mark.parametrize("reply", ["Проехали быстро", "Езжайте сейчас", "Я бы сделал так"])
def test_plain_replies_still_are_not_leads(env, reply):
    """Серверное правило не ослаблено: ответ без собственной проблемы — ANSWER."""
    oid = _reply_candidate(env, "border_queue", "Какая очередь на Ларсе?", reply)
    service = Service("QUESTION", has_own_problem=False)
    _run(env, service, at=T0 + timedelta(minutes=5))
    assert env[1].get(oid).filter_reason == "not_lead_intent:answer" and service.generation_inputs == []


# ---------------- border_queue: только тематически релевантный контекст ----------------

INSURANCE_TAIL = "Также есть сообщения о проверках страховки и возможном штрафе при её отсутствии."
FUEL_TAIL = "Несколько участников отмечают, что кто-то проезжает, а кто-то сливает бензин."


def _guard(text, asked=frozenset()):
    return normalize_draft(_draft(text, ["B1"], "single_report"), follow_up_enabled=False,
                           valid_refs=frozenset({"B1"}), fresh_context_used=True, n_distinct_senders=1,
                           allowed_resources=(), side_topics_asked=asked)


def test_b40_snow_answer_has_no_insurance():
    decision = _guard("В свежих сообщениях нет данных о снеге на Ларсе. " + INSURANCE_TAIL)
    assert (decision.kind, decision.primary_message) == ("draft", "В свежих сообщениях нет данных о снеге на Ларсе.")
    assert decision.removed_side_topics == ("страховка", "штрафы")


def test_b62_situation_answer_has_no_fuel():
    text = "По последней сводке Верхний Ларс открыт, очереди 2/10 в сторону Грузии и 3/10 в сторону РФ.\n\n" + FUEL_TAIL
    decision = _guard(text)
    assert decision.primary_message == "По последней сводке Верхний Ларс открыт, очереди 2/10 в сторону Грузии и 3/10 в сторону РФ."
    assert "бензин" not in decision.primary_message


@pytest.mark.parametrize("leak", [
    "Есть сообщение о передаче посылки в Пятигорск.",
    "При вывозе авто нужна нотариальная доверенность.",
    "С крымскими номерами иногда пропускают.",
])
def test_b10_b63_b76_side_topics_do_not_leak(leak):
    decision = _guard("В свежих сообщениях пишут, что КПП прошли без очереди. " + leak)
    assert decision.primary_message == "В свежих сообщениях пишут, что КПП прошли без очереди."


def test_side_topic_user_asked_about_is_kept():
    text = "Перевал открыт. В свежих сообщениях нет данных о наличии 95-го от Владикавказа."
    assert _guard(text, asked=frozenset({"бензин"})).primary_message == text


def test_answer_only_about_side_topic_is_rejected_for_repair():
    decision = _guard(INSURANCE_TAIL)
    assert (decision.kind, decision.error) == ("invalid", "topic_leak:страховка,штрафы")


@pytest.mark.parametrize("text", [
    "По свежим сообщениям очередь небольшая.",
    "Свежие сообщения расходятся: один участник пишет, что прошли за 7 минут, другой — что границу закрыли.",
    "По последней сводке очереди 2/10 в сторону Грузии.",
])
def test_fresh_context_wording_is_allowed(text):
    assert _guard(text).primary_message == text


def test_topic_guard_is_border_only():
    """fuel/insurance: side_topics_asked не передаётся — ответ не режется."""
    decision = normalize_draft(_draft("Бензин есть. " + INSURANCE_TAIL, ["B1"], "single_report"),
                               follow_up_enabled=False, valid_refs=frozenset({"B1"}), fresh_context_used=True,
                               n_distinct_senders=1, allowed_resources=())
    assert INSURANCE_TAIL in decision.primary_message


def test_topic_leak_gets_one_repair(env):
    oid = _candidate(env, "border_queue", "Как на Ларсе снег?")
    service = Service("QUESTION", _draft(INSURANCE_TAIL), _draft("В свежих сообщениях нет данных о снеге на Ларсе."))
    _run(env, service)
    row = env[1].get(oid)
    assert (row.status, row.primary_text) == (STATUS_DRAFT, "В свежих сообщениях нет данных о снеге на Ларсе.")
    assert len(service.generation_inputs) == 2


def _fresh(env, msg_id, text, sender):
    env[2].add(chat_id=-1001, chat_identifier="VerhniyLars", message_id=msg_id, sender_id=sender, text=text,
               date=T0 - timedelta(minutes=30), reply_to_msg_id=None)


def test_border_evidence_and_generation_input_are_topic_filtered(env):
    _fresh(env, 201, "Прошли КПП Ларс за 40 минут, очередь небольшая", 301)
    _fresh(env, 202, "На границе проверяют страховку, штраф 100 лари", 302)
    _fresh(env, 203, "Кто будет проезжать через Ларс, передать посылку?", 303)
    _fresh(env, 204, "На Ларсе снег, перевал открыт", 304)
    oid = _candidate(env, "border_queue", "Как на Ларсе снег?", msg_id=210)
    service = Service("QUESTION", _draft("На Ларсе снег, перевал открыт.", ["S1"], "single_report"))
    _run(env, service)
    evidence = [i["text"] for i in _audit(env[1].get(oid))["evidence"]]
    assert any("40 минут" in t for t in evidence) and any("снег" in t for t in evidence)
    assert not any("страховку" in t or "посылку" in t for t in evidence)
    (generation_input,) = service.generation_inputs
    assert "страховку" not in generation_input and "посылку" not in generation_input


def test_fuel_evidence_is_not_border_filtered(env):
    _fresh(env, 221, "На АЗС у Ларса бензин есть, страховку там же продают", 321)
    oid = _candidate(env, "fuel", "Где заправиться бензином у Ларса?", msg_id=230)
    _run(env, Service("QUESTION"))
    assert any("страховку" in i["text"] for i in _audit(env[1].get(oid))["evidence"])


@pytest.mark.parametrize("text", ["Границу закрыли", "Говорят, Ларс закрыли", "Ларс открыли?", "Граница закрыта"])
def test_b28_closure_reports_stay_in_border_evidence(text):
    """Противоречивые сведения («границу закрыли» против «прошли за 7 минут»)
    не теряются при тематическом отборе."""
    from reader.dm_campaigns.relevance import border_evidence_relevant

    assert border_evidence_relevant(text)
