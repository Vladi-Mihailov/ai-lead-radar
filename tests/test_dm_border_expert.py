"""border_queue — экспертный ответ о КПП (reader/dm_campaigns/border.py):
разговорные вопросы («Ларс открыт, как дорога?»), только запрошенный КПП,
источник не раскрывается, нет негативных сигналов -> «открыт, критичных
очередей нет», есть сигналы -> отражены, приветствие и наши ресурсы для
поездки через Ларс. Без OpenAI/Telegram: классификатор и генератор — фейки."""

import json
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from test_dm_campaign_relevance import Service, _draft
from test_dm_shared_intent_gate import T0, _run, env  # noqa: F401  (env — fixture)

from reader.dm_campaigns.border import (
    PROTOCOL_GE_LINE,
    TPLGEE_LINE,
    BorderPolicy,
    greeting_for,
    negative_signals,
    requested_checkpoints,
)
from reader.dm_campaigns.draft_models import normalize_draft
from reader.dm_campaigns.draft_prompt import SYSTEM_PROMPT_BORDER, system_prompt_for
from reader.dm_campaigns.outreach_repository import STATUS_DRAFT, STATUS_PENDING_CONTEXT
from reader.dm_campaigns.relevance import campaign_relevant

Q323 = "Добрый вечер,скажите пожалуйста Ларс открыт,как дорога?"
CALM = "Верхний Ларс открыт, критичных очередей сейчас нет."
DISCLOSURE = ("по свежим сообщениям", "в группе пишут", "в обсуждении", "участник", "нет данных",
              "подтвердить не могу", "точных данных нет", "отзыв")


def _insert(env, text, *, chat="VerhniyLars", msg_id=1, reply_to=None, sender=555, at=T0):
    campaigns, outreach, _ = env
    return outreach.insert_candidate(
        campaign_id=campaigns.get_campaign_by_key("border_queue").id, source_chat_id=-1001,
        source_chat_identifier=chat, source_chat_title=chat, source_message_id=msg_id, source_message_at=at,
        source_link=None, source_reply_to_msg_id=reply_to, recipient_user_id=sender, recipient_username=None,
        source_text=text, status=STATUS_PENDING_CONTEXT, now=at, draft_after_at=at)


def _buffer(env, msg_id, text, *, chat="VerhniyLars", chat_id=-1001, sender=900, minutes=30, reply_to=None):
    env[2].add(chat_id=chat_id, chat_identifier=chat, message_id=msg_id, sender_id=sender, text=text,
               date=T0 - timedelta(minutes=minutes), reply_to_msg_id=reply_to)


def _audit(env, oid):
    return json.loads(env[1].get(oid).context_json)


# ---------------- A: реальный кейс #323 ----------------


def test_a_323_lars_open_and_road_is_an_expert_border_draft(env):
    _buffer(env, 401, "Кто подскажет какая очередь на переходе Сарпи для грузовых автомобилей", chat="sarpi_ge",
            chat_id=-1002, sender=901)
    _buffer(env, 402, "Границу прошли в Вале суммарно за 30 минут", chat="sarpi_ge", chat_id=-1002, sender=902)
    _buffer(env, 403, "По навигатору показывает пробку Думал большая очередь", sender=903)
    _buffer(env, 404, "Какая обстановка сейчас на границе?", sender=904)
    oid = _insert(env, Q323, msg_id=410)
    service = Service("QUESTION", _draft(CALM))
    _run(env, service)
    row = env[1].get(oid)
    audit = _audit(env, oid)
    assert row.status == STATUS_DRAFT
    assert (audit["final_intent"], audit["campaign_relevant"]) == ("QUESTION", True)
    assert audit["border"] == {"checkpoints": ["lars"], "negative_signals": [], "greeting": "Добрый вечер!", "cta": True}
    text = row.primary_text
    assert text == f"Добрый вечер! {CALM} {TPLGEE_LINE}\n{PROTOCOL_GE_LINE}"
    assert "@tplgee" in text and "@ProtocolGEbot" in text
    assert not any(bad in text.lower() for bad in DISCLOSURE)
    assert "Сарпи" not in text and "Вале" not in text
    # Сообщения о других КПП не попадают ни в сведения, ни во вход генератора.
    evidence = [i["text"] for i in audit["evidence"]]
    assert not any("Сарпи" in t or "Вале" in t for t in evidence)
    (generation_input,) = service.generation_inputs
    assert "Сарпи" not in generation_input and "Вале" not in generation_input
    assert "REQUESTED CHECKPOINT\nВерхний Ларс" in generation_input
    assert "BORDER SIGNALS\nnegative_signals: нет" in generation_input


def test_a_323_bad_model_answer_is_repaired_once(env):
    bad = ("В свежих сообщениях нет данных о том, открыт ли КПП Верхний Ларс. В обсуждении один участник уточнял "
           "очередь на Сарпи; есть сообщение о прохождении через КПП Вале за 30 минут.")
    oid = _insert(env, Q323, msg_id=420)
    service = Service("QUESTION", _draft(bad), _draft(CALM))
    _run(env, service)
    row = env[1].get(oid)
    assert row.status == STATUS_DRAFT and row.primary_text.startswith(f"Добрый вечер! {CALM}")
    assert len(service.generation_inputs) == 2 and "не раскрывай источник" in service.generation_inputs[1]


# ---------------- B / C: разговорные формулировки ----------------


@pytest.mark.parametrize("text", [Q323, "Как там Ларс?", "Как Ларс?", "Что на Ларсе?", "Как проезд?", "Как граница?",
                                  "Как дорога?", "Ларс открыт?"])
def test_b_colloquial_questions_are_border_relevant(text):
    assert campaign_relevant("border_queue", text, place_hint=True)
    assert campaign_relevant("border_queue", text, place_hint=False)  # и вне чата КПП


def test_b_situation_question_in_lars_chat_is_relevant():
    assert campaign_relevant("border_queue", "Какая обстановка?", place_hint=True)


def test_c_road_question_in_reply_about_lars_is_relevant(env):
    _buffer(env, 430, "Завтра едем через Верхний Ларс", sender=905)
    oid = _insert(env, "Как дорога?", msg_id=431, reply_to=430)
    _run(env, Service("QUESTION", _draft("Проезд через Ларс сейчас нормальный.")))
    audit = _audit(env, oid)
    assert audit["campaign_relevant"] is True and audit["border"]["checkpoints"] == ["lars"]
    # продолжение разговора без приветствия USER — без навязанного «Здравствуйте!»
    assert env[1].get(oid).primary_text.startswith("Проезд через Ларс сейчас нормальный.")


def test_route_and_fuel_talk_in_lars_chat_stays_not_border():
    assert not campaign_relevant("border_queue", "А через Волгоград ездил кто? Интернет говорит, что там не очень. "
                                                 "Хотелось бы через Элисту проехать", place_hint=True)
    assert not campaign_relevant("border_queue", "Какая очередь на заправках с 95-м?", place_hint=True)


# ---------------- D: нет негативных сигналов ----------------


def _norm(text, policy):
    return normalize_draft(_draft(text, ["S1"], "single_report"), follow_up_enabled=False,
                           valid_refs=frozenset({"S1"}), fresh_context_used=True, n_distinct_senders=1,
                           allowed_resources=("@tplgee", "@ProtocolGEbot"), side_topics_asked=frozenset(),
                           border_policy=policy)


LARS_CALM = BorderPolicy(checkpoints=frozenset({"lars"}), greeting="Здравствуйте!", cta=(TPLGEE_LINE, PROTOCOL_GE_LINE))
LARS_QUEUE = BorderPolicy(checkpoints=frozenset({"lars"}), negative_signals=("большая очередь",),
                          greeting="Здравствуйте!", cta=(TPLGEE_LINE, PROTOCOL_GE_LINE))


@pytest.mark.parametrize("text", [
    "В свежих сообщениях нет данных о снеге на Ларсе.",
    "Точных данных нет, но, похоже, Ларс открыт.",
    "В группе пишут, что Ларс открыт.",
    "Один участник написал, что очередь небольшая.",
    "По сообщениям группы Ларс работает.",
    "Есть один свежий отзыв: прошли за 40 минут.",
    "Верхний Ларс открыт. О снеге на перевале конкретных сведений пока нет.",
    "Верхний Ларс: информации о работе КПП недостаточно, статус сейчас неясен.",
    "Ларс открыт. При этом есть и данные о нормальном проезде.",
])
def test_d_source_disclosure_is_rejected(text):
    assert _norm(text, LARS_CALM).error in ("source_disclosure", "dynamic_hedging")


def test_d_no_signals_business_answer_passes_and_is_decorated():
    decision = _norm(CALM, LARS_CALM)
    assert decision.kind == "draft"
    assert decision.primary_message == f"Здравствуйте! {CALM} {TPLGEE_LINE}\n{PROTOCOL_GE_LINE}"


@pytest.mark.parametrize("text", ["Очереди точно нет.", "Ларс гарантированно свободен."])
def test_d_overclaim_is_rejected(text):
    assert _norm(text, LARS_CALM).error == "overclaim"


def test_d_prompt_states_business_logic_and_bans_disclosure():
    assert system_prompt_for("border_queue") is SYSTEM_PROMPT_BORDER
    for rule in ("критичных очередей сейчас нет", "«по свежим сообщениям», «в группе пишут»", "«нет данных»",
                 "REQUESTED CHECKPOINT", "Другие КПП (Сарпи, Вале", "«как дорога?», «как проезд?»", "на «вы»",
                 "НЕ начинай с приветствия и НЕ упоминай никакие ресурсы"):
        assert rule in SYSTEM_PROMPT_BORDER


# ---------------- E: есть реальный сигнал ----------------


@pytest.mark.parametrize("texts,signal", [
    (["На Ларсе большая очередь, стоим 3 часа"], "большая очередь"),
    (["Очередь большая на российской стороне"], "большая очередь"),
    (["Ларс закрыли из-за погоды"], "закрытие/ограничение проезда"),
    (["Движение остановлено, авария на перевале"], "авария/остановка движения"),
    (["Сильный снег на перевале, только на зимней резине"], "снег/лёд на дороге"),
])
def test_e_negative_signals_are_detected(texts, signal):
    assert signal in negative_signals(texts)


@pytest.mark.parametrize("texts", [
    ["Ларс закрыли?"], ["Какая очередь на Ларсе?"], ["По навигатору показывает пробку Думал большая очередь"],
    ["Прошли за 40 минут, очереди нет"], ["Верхний Ларс открыт для всех видов транспорта"],
])
def test_e_questions_and_calm_reports_are_not_signals(texts):
    assert negative_signals(texts) == ()


@pytest.mark.parametrize("text", [CALM, "Сейчас на Ларсе спокойно.", "Проезд свободный."])
def test_e_calm_answer_contradicting_signals_is_rejected(text):
    assert _norm(text, LARS_QUEUE).error == "contradicts_signals"


def test_e_signal_reflected_end_to_end_with_one_repair(env):
    _buffer(env, 440, "На Ларсе большая очередь на российской стороне, стоим 3 часа", sender=906)
    oid = _insert(env, Q323, msg_id=441)
    service = Service("QUESTION", _draft(CALM),
                      _draft("Верхний Ларс открыт, но сейчас большая очередь на российской стороне."))
    _run(env, service)
    row = env[1].get(oid)
    assert _audit(env, oid)["border"]["negative_signals"] == ["большая очередь"]
    assert "negative_signals: большая очередь" in service.generation_inputs[0]
    assert row.status == STATUS_DRAFT and "критичных очередей" not in row.primary_text
    assert row.primary_text.startswith("Добрый вечер! Верхний Ларс открыт, но сейчас большая очередь")


# ---------------- КПП и ресурсы ----------------


def test_checkpoint_resolution():
    assert requested_checkpoints(Q323, (), "VerhniyLars") == {"lars"}
    assert requested_checkpoints("Какая обстановка?", (), "VerhniyLars") == {"lars"}
    assert requested_checkpoints("Сарпи открыт в сторону Турции?", (), "VerhniyLars") == {"sarpi"}
    assert requested_checkpoints("Как дорога?", ("Едем через Верхний Ларс",), None) == {"lars"}
    assert requested_checkpoints("Какая очередь?", (), "georgia_auto") == frozenset()


def test_other_checkpoint_sentence_is_removed():
    decision = _norm("Верхний Ларс открыт, критичных очередей сейчас нет. На Сарпи очередь грузовиков.", LARS_CALM)
    assert "Сарпи" not in decision.primary_message and decision.removed_side_topics == ("Сарпи",)


def test_sarpi_to_turkey_gets_no_georgian_cross_sell(env):
    _buffer(env, 450, "Сарпи прошли за час", chat="sarpi_ge", chat_id=-1002, sender=907)
    oid = _insert(env, "Сарпи открыт в сторону Турции?", chat="sarpi_ge", msg_id=451)
    _run(env, Service("QUESTION", _draft("Сарпи работает, критичных очередей сейчас нет.")))
    row = env[1].get(oid)
    assert row.status == STATUS_DRAFT
    assert "@tplgee" not in row.primary_text and "@ProtocolGEbot" not in row.primary_text
    assert _audit(env, oid)["border"]["cta"] is False


def test_model_resource_mentions_are_not_duplicated():
    text = f"{CALM} Не забудьте про страховку — я делал тут @tplgee."
    decision = _norm(text, LARS_CALM)
    assert decision.kind == "draft" and decision.primary_message.count("@tplgee") == 1
    assert decision.primary_message.endswith(PROTOCOL_GE_LINE)


# ---------------- приветствие ----------------


@pytest.mark.parametrize("source,continuation,greeting", [
    ("Добрый вечер, Ларс открыт?", False, "Добрый вечер!"),
    ("Добрый день! Как дорога?", False, "Добрый день!"),
    ("Доброе утро, очередь есть?", False, "Доброе утро!"),
    ("Здравствуйте, как Ларс?", False, "Здравствуйте!"),
    ("Привет, как там Ларс?", False, "Привет!"),
    ("Как там Ларс?", False, "Здравствуйте!"),
    ("А сейчас как?", True, None),
])
def test_greeting_mirrors_user(source, continuation, greeting):
    assert greeting_for(source, continuation=continuation) == greeting


def test_existing_model_greeting_is_not_doubled():
    decision = _norm(f"Здравствуйте! {CALM}", LARS_CALM)
    assert decision.primary_message.count("Здравствуйте") == 1


def test_fuel_is_not_affected_by_border_rules():
    """fuel: без border-политики черновик не украшается и не проверяется на раскрытие источника."""
    assert system_prompt_for("fuel") is not SYSTEM_PROMPT_BORDER
    decision = normalize_draft(_draft("В свежих сообщениях пишут, что 95-й есть.", ["S1"], "single_report"),
                               follow_up_enabled=False, valid_refs=frozenset({"S1"}), fresh_context_used=True,
                               n_distinct_senders=1, allowed_resources=())
    assert decision.primary_message == "В свежих сообщениях пишут, что 95-й есть."
