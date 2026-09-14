"""Минимальные модели верхнего уровня Turkey-бота — состояние диалога,
персистентное в SQLite (см. conversation_state_repository.py). Модели
самого GIB-слоя (CaptchaChallenge/GibSubmitOutcome/...) — в
reader/turkey_bot/gib/models.py, отдельно и независимо от этого файла.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ConversationState:
    """Состояние одного пошагового диалога (сейчас — единственный шаг:
    "awaiting_captcha_code") в ОДНОМ приватном чате с ботом — переживает
    рестарт процесса (тот же приём, что и
    reader/public_bot/conversation_state_repository.py). ВАЖНО: то, что
    переживает рестарт, — это только "какой номер проверяли и какой
    image_id был последним показан" (payload) — САМА GIB-сессия
    (httpx.AsyncClient/cookies) — нет и не может (см. design report Stage
    3: "do not pretend that an in-memory client can be reconstructed from
    a DB session_token") — за это отвечает
    reader/turkey_bot/live_session_registry.py, полностью in-memory."""

    chat_id: int
    telegram_user_id: int
    step: str
    payload: dict | None
    updated_at: datetime
