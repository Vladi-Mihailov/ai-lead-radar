"""Объект вопроса важнее чата (Красный камень не подменяется Ларсом),
конкретный ответ на перечисленные варианты (Лукойл/Роснефть/Газпром) и
подвал с ресурсами в СОХРАНЁННОМ primary_text (#471/#491: Владикавказ).
Реальные production-сообщения (обезличены). Без OpenAI/Telegram."""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from _dm_reply_helpers import footer
from test_dm_campaign_relevance import Service, _draft
from test_dm_shared_intent_gate import T0, _run, env  # noqa: F401  (env — fixture)

from reader.dm_campaigns.border import (
    PROTOCOL_GE_LINE,
    TPLGEE_LINE,
    BorderPolicy,
    georgia_trip,
    requested_checkpoints,
    unsupported_checkpoint,
)
from reader.dm_campaigns.draft_models import normalize_draft
from reader.dm_campaigns.draft_prompt import SYSTEM_PROMPT_FUEL
from reader.dm_campaigns.outreach_repository import STATUS_DRAFT, STATUS_PENDING_CONTEXT
from reader.dm_campaigns.sendability import SENDABILITY_SOURCE_MESSAGE, SENDABILITY_USERNAME

FOOTER = f"{TPLGEE_LINE}\n{PROTOCOL_GE_LINE}"
Q524 = ("Добрый день! Подскажите пожалуйста, кто-нибудь ехал из Беларуси в Грузию?Открыт ли пропускной пункт РБ-РФ "
        "Красный камень, можно ли проехать сейчас?")
Q471 = "Где во Владикавказе нормальный дизель? Лукойл, Роснефть, Газпром?"
Q491 = "Как с бензином во Владикавказе ?"


def _insert(env, key, text, *, msg_id, sendability=SENDABILITY_USERNAME, username="ivan", reply_to=None):
    campaigns, outreach, _ = env
    return outreach.insert_candidate(
        campaign_id=campaigns.get_campaign_by_key(key).id, source_chat_id=-1001, source_chat_identifier="VerhniyLars",
        source_chat_title="Верхний Ларс", source_message_id=msg_id, source_message_at=T0, source_link=None,
        source_reply_to_msg_id=reply_to, recipient_user_id=555, recipient_username=username, source_text=text,
        status=STATUS_PENDING_CONTEXT, now=T0, draft_after_at=T0, sendability=sendability)


def _saved(env, oid):
    row = env[1].get(oid)
    assert row.status == STATUS_DRAFT, (row.status, row.error)
    return row, json.loads(row.context_json)


# ---------------- #524: явный объект важнее чата ----------------


def test_524_unsupported_checkpoint_is_not_replaced_by_lars(env):
    oid = _insert(env, "border_queue", Q524, msg_id=701)
    service = Service("QUESTION", _draft("Верхний Ларс открыт, проезд нормальный, критичных очередей сейчас нет."))
    _run(env, service)
    row, audit = _saved(env, oid)
    body = row.primary_text.split(footer(oid, Q524).split("\n")[0])[0]
    assert "Ларс" not in body and "открыт" not in body  # ни подмены, ни выдуманного статуса
    assert row.primary_text == ("Добрый день! По переходу «Красный камень» сейчас не буду вводить вас в заблуждение — "
                                f"точную текущую обстановку по нему не подскажу. {footer(oid, Q524)}")
    assert service.generation_inputs == []  # модель не вызывается — статус не выдумать
    assert audit["border"]["unsupported_checkpoint"] == "Красный камень" and audit["border"]["checkpoints"] == []


@pytest.mark.parametrize("text,expected", [
    (Q524, frozenset()),
    ("Переход Сарпи открыт?", frozenset({"sarpi"})),
    ("Как сейчас Вале, очередь есть?", frozenset({"vale"})),
    ("Садахло работает?", frozenset({"sadakhlo"})),
    ("Ларс открыт?", frozenset({"lars"})),
    ("Какая обстановка?", frozenset({"lars"})),  # объект не назван — КПП чата
])
def test_explicit_object_beats_chat_default(text, expected):
    assert requested_checkpoints(text, (), "VerhniyLars") == expected


def test_reply_object_beats_chat_default():
    assert requested_checkpoints("А сейчас как?", ("Сарпи открыт?",), "VerhniyLars") == {"sarpi"}


@pytest.mark.parametrize("text,name", [
    (Q524, "Красный камень"), ("Как на КПП Яраг-Казмаляр?", "Яраг-Казмаляр"),
    ("Через переход Бугаздрой ехал кто?", "Бугаздрой"), ("Пункт пропуска Новая Гута работает?", "Новая Гута"),
])
def test_unsupported_checkpoint_detection(text, name):
    assert unsupported_checkpoint(text) == name


@pytest.mark.parametrize("text", ["Ларс открыт?", "Какая очередь на КПП?", "Как КПП Верхний Ларс?", "Переход Сарпи открыт?"])
def test_monitored_or_unnamed_checkpoint_is_not_unsupported(text):
    assert unsupported_checkpoint(text) is None


def test_323_lars_behaviour_unchanged(env):
    oid = _insert(env, "border_queue", "Добрый вечер,скажите пожалуйста Ларс открыт,как дорога?", msg_id=702)
    _run(env, Service("QUESTION", _draft("Верхний Ларс открыт, критичных очередей сейчас нет.")))
    row, audit = _saved(env, oid)
    assert row.primary_text == (f"Добрый вечер! Верхний Ларс открыт, критичных очередей сейчас нет. "
                                f"{footer(oid, 'Добрый вечер,скажите пожалуйста Ларс открыт,как дорога?')}")
    assert audit["border"]["checkpoints"] == ["lars"] and "unsupported_checkpoint" not in audit["border"]


# ---------------- fuel: конкретные варианты ----------------


def _fuel_policy(options=("Лукойл", "Роснефть", "Газпром")):
    return BorderPolicy(kind="fuel", greeting="Здравствуйте!", cta=(TPLGEE_LINE, PROTOCOL_GE_LINE),
                        question_options=options, context_text=Q471)


def _norm(text, policy):
    return normalize_draft(_draft(text), follow_up_enabled=False, valid_refs=frozenset(), fresh_context_used=False,
                           n_distinct_senders=0, allowed_resources=("@tplgee", "@ProtocolGEbot"), border_policy=policy)


def test_generic_fuel_answer_ignoring_listed_options_is_rejected():
    decision = _norm("Во Владикавказе заправки есть, заправляйтесь заранее.", _fuel_policy())
    assert decision.error == "ignored_question_options:Лукойл,Роснефть,Газпром"


def test_answer_working_with_listed_options_passes():
    text = "По дизелю во Владикавказе в первую очередь смотрите Лукойл и Роснефть, Газпром — как альтернатива."
    assert _norm(text, _fuel_policy()).kind == "draft"


def test_fuel_options_answer_still_rejects_guarantees():
    assert _norm("На Лукойле дизель точно есть.", _fuel_policy()).kind == "invalid"


def test_471_vladikavkaz_diesel_repaired_to_options_and_footer_saved(env):
    good = "По дизелю во Владикавказе в первую очередь смотрите Лукойл и Роснефть, Газпром — как альтернатива."
    oid = _insert(env, "fuel", Q471, msg_id=703)
    service = Service("QUESTION", _draft("Во Владикавказе заправки есть, дизель в наличии."), _draft(good))
    _run(env, service)
    row, audit = _saved(env, oid)
    assert row.primary_text == f"Здравствуйте! {good} {footer(oid, Q471)}"
    assert len(service.generation_inputs) == 2 and "перечислил конкретные варианты" in service.generation_inputs[1]
    assert audit["border"]["question_options"] == ["Лукойл", "Роснефть", "Газпром"] and audit["border"]["cta"] is True


def test_fuel_prompt_requires_working_with_listed_options():
    assert "Если USER перечислил конкретные варианты" in SYSTEM_PROMPT_FUEL
    assert "Не начинай с очевидного" in SYSTEM_PROMPT_FUEL


# ---------------- CTA: корневая причина #471/#491 ----------------


@pytest.mark.parametrize("text", [Q471, Q491, "Из Москвы до Владикавказа как с АИ-95?"])
def test_vladikavkaz_is_georgia_trip(text):
    """Владикавказ — подъезд к Верхнему Ларсу, а не «маршрут по России»."""
    assert georgia_trip(text, (), "VerhniyLars") and georgia_trip(text, (), None)


@pytest.mark.parametrize("sendability,username", [(SENDABILITY_USERNAME, "ivan"), (SENDABILITY_SOURCE_MESSAGE, None)])
def test_491_saved_primary_text_has_footer_exactly_once(env, sendability, username):
    answer = "Во Владикавказе бензин на заправках есть, заправляйтесь заранее."
    oid = _insert(env, "fuel", Q491, msg_id=710 if username else 711, sendability=sendability, username=username)
    _run(env, Service("QUESTION", _draft(answer)))
    row, _ = _saved(env, oid)
    assert row.sendability == sendability
    assert row.primary_text == f"Здравствуйте! {answer} {footer(oid, Q491)}"
    assert row.primary_text.count("@tplgee") == 1 and row.primary_text.count("@ProtocolGEbot") == 1


def test_footer_not_duplicated_when_model_already_mentions_resource(env):
    oid = _insert(env, "fuel", Q491, msg_id=712)
    _run(env, Service("QUESTION", _draft("Во Владикавказе бензин есть. Страховку я делал тут @tplgee.")))
    row, _ = _saved(env, oid)
    assert row.primary_text.count("@tplgee") == 1 and row.primary_text.count("@ProtocolGEbot") == 1
