"""Structured Output schema для AI-анализа лида (см. reader/lead_ai/service.py)
и reader/sinks/lead_ai_sink.py — единственный получатель этого follow-up,
см. settings.lead_ai.recipient.

LeadAiAnalysis используется напрямую как text_format для OpenAI Structured
Outputs (в отличие от reader/ocr/, где wire-schema отдельная от публичной
модели: там нужна дополнительная бизнес-логика вывода ролей ФИО, здесь
единственная пост-обработка — normalize_lead_ai_analysis ниже)."""

from typing import Literal

from pydantic import BaseModel

LeadType = Literal[
    "fine_payment",
    "fine_check",
    "money_transfer_ru_ge",
    "insurance_georgia",
    "insurance_turkey",
    "insurance_armenia",
    "insurance_general",
    "irrelevant",
]


class LeadAiAnalysis(BaseModel):
    relevant: bool
    lead_type: LeadType
    reason: str
    suggested_reply: str


def normalize_lead_ai_analysis(analysis: LeadAiAnalysis) -> LeadAiAnalysis:
    """Модель — не гарантия внутренней согласованности полей, даже со
    strict Structured Outputs (schema ограничивает ТИПЫ полей, не связи
    между их значениями). Инварианты, которые обязаны выполняться:

    - relevant=False -> lead_type="irrelevant" и suggested_reply=""
    - relevant=True  -> lead_type != "irrelevant"

    Если модель вернула что-то, нарушающее любой из инвариантов (например
    relevant=True с lead_type="irrelevant", или relevant=False с непустым
    suggested_reply) — результат приводится к безопасному "не лид", а не
    трактуется в пользу relevant=True: лучше пропустить сомнительный лид,
    чем показать менеджеру придуманную категорию/ответ (см. задачу: "при
    сомнении лучше irrelevant, чем придумывать потребность")."""
    if not analysis.relevant or analysis.lead_type == "irrelevant":
        return analysis.model_copy(
            update={"relevant": False, "lead_type": "irrelevant", "suggested_reply": ""}
        )
    return analysis
