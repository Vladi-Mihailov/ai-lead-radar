from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Message:
    id: int
    chat_id: int
    chat_title: str
    sender_id: int | None
    sender_username: str | None
    sender_name: str | None
    text: str
    date: datetime
    link: str | None
    # Идентификатор группы из config/groups.yaml (Group.identifier: username
    # или числовой id) — тот же, что хранит dm_campaigns.source_chats;
    # chat_id — числовой Telegram peer id, со строками не сравнивается.
    chat_identifier: str | None = None
    # id сообщения, на которое это сообщение отвечает (контекст ЛС-черновиков).
    reply_to_msg_id: int | None = None


@dataclass(frozen=True)
class ScenarioMatch:
    scenario_name: str
    matched_keywords: list[str]
    # False — сценарий только для ЛС-кампаний (см. config/scenarios.yaml
    # forward_leads): не пересылается менеджерам и не пишется в
    # users.keywords, существующий lead-поток его не видит.
    forward_leads: bool = True


@dataclass(frozen=True)
class LeadEvent:
    message: Message
    matches: list[ScenarioMatch]
