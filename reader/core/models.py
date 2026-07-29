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


@dataclass(frozen=True)
class ScenarioMatch:
    scenario_name: str
    matched_keywords: list[str]


@dataclass(frozen=True)
class LeadEvent:
    message: Message
    matches: list[ScenarioMatch]
