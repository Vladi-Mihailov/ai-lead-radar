"""Telethon-клавиатуры/callback_data @GEShtrafbot.

Security-инвариант (см. design report и reader/public_bot/conversation.py):
callback_data периода несёт ТОЛЬКО выбранное значение (b"period:30" и
т.п.) — никакого telegram_user_id/task_id. Владение и идентичность
проверяются ИСКЛЮЧИТЕЛЬНО через event.sender_id + conversation_state (см.
ConversationController.handle_period_choice) — callback_data сам по себе
не является доказательством ничего, кроме "какая кнопка была нажата"
(пользователь может переслать сообщение с кнопкой в свой собственный чат с
ботом или его callback_data теоретически может быть воспроизведён — но это
не даёт доступа ни к чьей чужой записи, потому что никакая чужая запись в
payload не упоминается).

🔎 Проверить сейчас / ⛔ Остановить мониторинг (см. design report Stage 4)
устроены немного иначе: их callback_data ДЕЙСТВИТЕЛЬНО несёт
subscription_id (иначе "выбрать одно из НЕСКОЛЬКИХ авто" невозможно
закодировать без идентификатора вообще) — но тот же инвариант сохраняется
на СЛЕДУЮЩЕМ уровне: subscription_id сам по себе публичен и НЕ является
доказательством владения — ConversationController.handle_check_now_choice/
handle_stop_pick/handle_stop_confirm вызывают
SubscriptionService.get_actionable_subscription()/check_now()/
stop_subscription(), которые ВСЕГДА заново проверяют владение по
РЕАЛЬНОМУ event.sender_id перед тем, как что-либо показать/сделать — то
есть подделанный/чужой subscription_id просто не пройдёт эту проверку,
независимо от того, насколько "правильно" он выглядит в callback_data.
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
_CHECK_NOW_PREFIX = b"checknow:"
_STOP_PICK_PREFIX = b"stoppick:"
_STOP_YES_PREFIX = b"stopyes:"
STOP_NO = b"stopno"
_ADD_CLIENT_YES = b"addclient:yes"
_ADD_CLIENT_NO = b"addclient:no"


def main_menu_keyboard() -> list[list[Button]]:
    return [
        [Button.text(ADD_CAR_LABEL, resize=True), Button.text(MY_CARS_LABEL, resize=True)],
        [Button.text(CHECK_NOW_LABEL, resize=True), Button.text(STOP_LABEL, resize=True)],
    ]


def add_client_decision_keyboard() -> list[list[Button]]:
    """"👤 Добавить Telegram клиента?" — trusted-оператор, сразу после
    ввода номера авто (см. design report: username клиента больше НЕ
    обязателен для постановки на мониторинг)."""
    return [[
        Button.inline("OK", _ADD_CLIENT_YES),
        Button.inline("Отмена", _ADD_CLIENT_NO),
    ]]


def decode_add_client_decision_callback(data: bytes | None) -> bool | None:
    """True — оператор нажал OK (хочет указать клиента), False — Отмена
    (мониторинг без клиента). None — этот callback_data не про этот шаг
    вовсе (вызывающий код должен пробовать следующий decode_*, а не
    трактовать None как "Отмена")."""
    if data == _ADD_CLIENT_YES:
        return True
    if data == _ADD_CLIENT_NO:
        return False
    return None


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
    return _decode_id(data, _PERIOD_PREFIX, allowlist=PERIOD_CHOICES)


def _decode_id(data: bytes | None, prefix: bytes, *, allowlist: tuple[int, ...] | None = None) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    try:
        value = int(data[len(prefix):])
    except ValueError:
        return None
    if allowlist is not None and value not in allowlist:
        return None
    return value


def decode_check_now_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _CHECK_NOW_PREFIX)


def decode_stop_pick_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _STOP_PICK_PREFIX)


def decode_stop_confirm_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _STOP_YES_PREFIX)


def options_keyboard(options: list[tuple[int, str]], *, prefix: bytes) -> list[list[Button]]:
    """Один subscription_id на кнопку — см. модуль docstring про то, почему
    это безопасно (owner-check происходит при нажатии, не здесь)."""
    return [
        [Button.inline(f"🚗 {label}", prefix + str(subscription_id).encode("ascii"))]
        for subscription_id, label in options
    ]


def stop_confirm_keyboard(subscription_id: int) -> list[list[Button]]:
    return [[
        Button.inline("Да", _STOP_YES_PREFIX + str(subscription_id).encode("ascii")),
        Button.inline("Отмена", STOP_NO),
    ]]


def check_now_options_keyboard(options: list[tuple[int, str]]) -> list[list[Button]]:
    return options_keyboard(options, prefix=_CHECK_NOW_PREFIX)


def stop_options_keyboard(options: list[tuple[int, str]]) -> list[list[Button]]:
    return options_keyboard(options, prefix=_STOP_PICK_PREFIX)


_PAYMENT_HELP_BUTTON_LABEL = "💳 Оплатить в рублях"
_INSURANCE_BUTTON_LABEL = "🛡 Оформить страховку"


def owner_fine_cta_buttons(contact_username: str) -> list[list[Button]]:
    """Коммерческий CTA под owner-уведомлением о новом штрафе (см.
    reader/public_bot/delivery_texts.py::CTA_TEXT_BLOCK) — ТОЛЬКО owner,
    trusted_operator и операторский чат этих кнопок не получают. Обе
    кнопки — один ряд (утверждённый макет), обе ведут на одну и ту же
    destination.

    contact_username — БЕЗ ведущего "@" (см. settings.public_bot.
    payment_help_contact_username) — destination задаётся конфигом, не
    hardcoded здесь, только шаблон ссылки t.me/<username>."""
    url = f"https://t.me/{contact_username}"
    return [[
        Button.url(_PAYMENT_HELP_BUTTON_LABEL, url),
        Button.url(_INSURANCE_BUTTON_LABEL, url),
    ]]
