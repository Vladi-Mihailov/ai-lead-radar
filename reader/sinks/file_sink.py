import json
from pathlib import Path

from reader.core.models import LeadEvent
from reader.sinks.base import BaseSink


class FileSink(BaseSink):
    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def handle(self, event: LeadEvent) -> None:
        record = {
            "message_id": event.message.id,
            "chat_id": event.message.chat_id,
            "chat_title": event.message.chat_title,
            "sender_id": event.message.sender_id,
            "sender_username": event.message.sender_username,
            "sender_name": event.message.sender_name,
            "text": event.message.text,
            "date": event.message.date.isoformat(),
            "link": event.message.link,
            "scenarios": [
                {"name": m.scenario_name, "matched_keywords": m.matched_keywords}
                for m in event.matches
            ],
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
