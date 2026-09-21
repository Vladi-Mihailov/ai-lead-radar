"""AdminBotConversationStateRepository — inviter_admin_bot_conversation_state
поверх SQLite. ТОТ ЖЕ минимальный приём, что и у
reader/public_bot/conversation_state_repository.py (не общий модуль —
каждый бот-пакет в этом репо держит собственную копию, см. design report
про независимость bot-модулей) — ОТДЕЛЬНАЯ таблица/файл от public_bot,
никакого пересечения.

БЕЗОПАСНОСТЬ (см. задачу "Session Security"): payload здесь — ТОЛЬКО
некрет данные шага диалога (например, номер телефона на шаге "Введите
код"). Login-код и пароль двухфакторной аутентификации СЮДА НИКОГДА НЕ
ПОПАДАЮТ — они используются РОВНО один раз, напрямую передаются в
Telethon (см. reader/inviter_admin_bot/auth.py) и не присваиваются вообще
ни одной переменной/структуре, которая могла бы быть сериализована в JSON
здесь."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from reader.inviter_admin_bot.models import NewAccountDraft


def _row_to_state(row) -> "AdminConversationState":
    chat_id, telegram_user_id, step, payload_raw, updated_at = row
    return AdminConversationState(
        chat_id=chat_id,
        telegram_user_id=telegram_user_id,
        step=step,
        payload=json.loads(payload_raw) if payload_raw else None,
        updated_at=datetime.fromisoformat(updated_at),
    )


class AdminConversationState:
    __slots__ = ("chat_id", "payload", "step", "telegram_user_id", "updated_at")

    def __init__(self, *, chat_id, telegram_user_id, step, payload, updated_at):
        self.chat_id = chat_id
        self.telegram_user_id = telegram_user_id
        self.step = step
        self.payload = payload
        self.updated_at = updated_at

    def draft(self) -> NewAccountDraft | None:
        """payload -> NewAccountDraft, если этот шаг относится к "➕
        Добавить аккаунт" (payload всегда несёт хотя бы "phone", см.
        conversation.py::handle_phone_input)."""
        if not self.payload or "phone" not in self.payload:
            return None
        return NewAccountDraft(phone=self.payload["phone"], fields=dict(self.payload.get("fields", {})))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS inviter_admin_bot_conversation_state (
    chat_id           INTEGER PRIMARY KEY,
    telegram_user_id  INTEGER NOT NULL,
    step              TEXT NOT NULL,
    payload           TEXT,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_UPSERT = """
INSERT INTO inviter_admin_bot_conversation_state (chat_id, telegram_user_id, step, payload, updated_at)
VALUES (:chat_id, :telegram_user_id, :step, :payload, CURRENT_TIMESTAMP)
ON CONFLICT(chat_id) DO UPDATE SET
    telegram_user_id = excluded.telegram_user_id,
    step = excluded.step,
    payload = excluded.payload,
    updated_at = CURRENT_TIMESTAMP
"""

_SELECT = (
    "SELECT chat_id, telegram_user_id, step, payload, updated_at "
    "FROM inviter_admin_bot_conversation_state WHERE chat_id = ?"
)

_DELETE = "DELETE FROM inviter_admin_bot_conversation_state WHERE chat_id = ?"


class AdminBotConversationStateRepository:
    def __init__(self, db_path: Path | str):
        is_memory = db_path == ":memory:"
        self._path = db_path if is_memory else Path(db_path)
        if not is_memory:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        if not is_memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def get(self, chat_id: int) -> AdminConversationState | None:
        row = self._conn.execute(_SELECT, (chat_id,)).fetchone()
        return _row_to_state(row) if row else None

    def set(
        self, chat_id: int, *, telegram_user_id: int, step: str, payload: dict | None = None,
    ) -> AdminConversationState:
        self._conn.execute(
            _UPSERT,
            {
                "chat_id": chat_id,
                "telegram_user_id": telegram_user_id,
                "step": step,
                "payload": json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            },
        )
        self._conn.commit()

        state = self.get(chat_id)
        if state is None:
            raise RuntimeError("Не удалось прочитать только что записанное состояние диалога")
        return state

    def clear(self, chat_id: int) -> None:
        self._conn.execute(_DELETE, (chat_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
