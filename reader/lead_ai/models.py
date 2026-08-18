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
    # Короткие отдельные Telegram-сообщения в реальном стиле менеджера (см.
    # reader/lead_ai/prompt.py) — БЕЗ приветствия: "Доброе утро"/"Добрый
    # день"/"Добрый вечер" добавляется отдельно, кодом, по текущему времени
    # Asia/Tbilisi (см. reader/lead_ai/greeting.py) — модель не имеет
    # надёжного доступа к реальному времени и не должна его придумывать.
    suggested_messages: list[str]


def normalize_lead_ai_analysis(analysis: LeadAiAnalysis) -> LeadAiAnalysis:
    """Модель — не гарантия внутренней согласованности полей, даже со
    strict Structured Outputs (schema ограничивает ТИПЫ полей, не связи
    между их значениями). Инварианты, которые обязаны выполняться:

    - relevant=False -> lead_type="irrelevant" и suggested_messages=[]
    - relevant=True  -> lead_type != "irrelevant" и suggested_messages
      содержит только непустые (после strip) строки

    Если модель вернула что-то, нарушающее первый инвариант (например
    relevant=True с lead_type="irrelevant", или relevant=False с непустым
    suggested_messages) — результат приводится к безопасному "не лид", а не
    трактуется в пользу relevant=True: лучше пропустить сомнительный лид,
    чем показать менеджеру придуманную категорию/сообщения (см. задачу:
    "при сомнении лучше irrelevant, чем придумывать потребность")."""
    if not analysis.relevant or analysis.lead_type == "irrelevant":
        return analysis.model_copy(
            update={"relevant": False, "lead_type": "irrelevant", "suggested_messages": []}
        )

    cleaned_messages = [msg.strip() for msg in analysis.suggested_messages if msg and msg.strip()]
    if cleaned_messages != analysis.suggested_messages:
        return analysis.model_copy(update={"suggested_messages": cleaned_messages})
    return analysis
