"""Telethon-клавиатуры/callback_data @GEShtrafbot.

Security-инвариант (см. design report и reader/public_bot/conversation.py):
callback_data периода несёт ТОЛЬКО выбранное значение (b"period:30" и
т.п.) — никакого telegram_user_id/subscription_id/task_id. Владение и
идентичность проверяются ИСКЛЮЧИТЕЛЬНО через event.sender_id +
conversation_state (см. ConversationController.handle_period_choice) —
callback_data сам по себе не является доказательством ничего, кроме "какая
кнопка была нажата" (пользователь может переслать сообщение с кнопкой в
свой собственный чат с ботом или его callback_data теоретически может быть
воспроизведён — но это не даёт доступа ни к чьей чужой записи, потому что
никакая чужая запись в payload не упоминается).

Тот же принцип ОБЯЗАТЕЛЕН для любого будущего callback (например, "выбрать
машину" для 🔎 Проверить сейчас/⛔ Остановить мониторинг — ещё не
реализовано в этом этапе, см. reader/public_bot/texts.py::COMING_SOON_TEXT):
payload не должен нести идентификатор чужой записи, который потом
используется без повторной серверной проверки владения по event.sender_id.
"""

from telethon import Button

from reader.public_bot.conversation import PERIOD_CHOICES
from reader.public_bot.texts import (
    ADD_CAR_LABEL,
    CHECK_NOW_LABEL,
    MY_CARS_LABEL,
    STOP_LABEL,
)

_PERIOD_PREFIX = b"period:"


def main_menu_keyboard() -> list[list[Button]]:
    return [
        [Button.text(ADD_CAR_LABEL, resize=True), Button.text(MY_CARS_LABEL, resize=True)],
        [Button.text(CHECK_NOW_LABEL, resize=True), Button.text(STOP_LABEL, resize=True)],
    ]


def period_choice_keyboard() -> list[list[Button]]:
    buttons = [
        Button.inline(f"{days} дней", encode_period_callback(days)) for days in PERIOD_CHOICES
    ]
    # 2x2 — ровно как в макете задачи ([30][90] / [180][365]).
    return [buttons[0:2], buttons[2:4]]


def encode_period_callback(days: int) -> bytes:
    return _PERIOD_PREFIX + str(days).encode("ascii")


def decode_period_callback(data: bytes | None) -> int | None:
    """None для чего угодно, кроме РОВНО одного значения из PERIOD_CHOICES —
    намеренно не парсит произвольные числа из чужого/подделанного
    callback_data (allowlist, а не "любое целое число")."""
    if not data or not data.startswith(_PERIOD_PREFIX):
        return None

    try:
        value = int(data[len(_PERIOD_PREFIX):])
    except ValueError:
        return None

    return value if value in PERIOD_CHOICES else None
