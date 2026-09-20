"""Минимальные модели верхнего уровня Turkey-бота — состояние диалога,
персистентное в SQLite (см. conversation_state_repository.py). Модели
самого GIB-слоя (CaptchaChallenge/GibSubmitOutcome/...) — в
reader/turkey_bot/gib/models.py, отдельно и независимо от этого файла.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


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


@dataclass(frozen=True)
class TurkeyUserCar:
    """Одна запись "Мои авто" — см. reader/turkey_bot/user_cars_repository.py.

    ОБНОВЛЕНО (см. задачу "Перенос Unified Turkey функционала в
    production", перенесено из reader/turkey_bot_test/models.py): машина
    появляется СРАЗУ после валидного ввода номера — успешная проверка
    больше НЕ требуется (та же смена направления, что и в test-боте).
    car_number уже нормализован (см. reader/turkey_bot/validation.py::
    normalize_plate).

    last_overall_status/last_total_amount — кэш последнего unified-check
    (см. reader/turkey_bot/unified/models.py::OverallStatus) для показа в
    списке "📋 Мои авто" без join на turkey_check_runs — None, пока
    автомобиль ни разу не проверялся unified-flow (в т.ч. для всех
    автомобилей, восстановленных production migration-скриптом из
    turkey_fine_checks/turkey_toll_checks — их legacy-история НЕ
    переносится в эти поля, см. scripts/migrate_turkey_unified.py)."""

    id: int
    telegram_user_id: int
    car_number: str
    created_at: datetime
    last_checked_at: datetime
    last_overall_status: str | None = None
    last_total_amount: Decimal | None = None
