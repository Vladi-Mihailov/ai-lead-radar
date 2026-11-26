"""Inline-клавиатуры MAX. Отдельной reply-клавиатуры (Telegram Button.text)
у MAX нет — главное меню тоже inline: callback-кнопки ведут в те же
обработчики, что и текстовые команды.

payload кнопки публичен и НЕ является доказательством прав: владение
автомобилем/подпиской проверяется по user_id отправителя на каждом нажатии.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MAX_BUTTON_TEXT = 128
MAX_PAYLOAD = 1024
MAX_ROWS = 30
MAX_BUTTONS_PER_ROW = 7
MAX_LINK_BUTTONS_PER_ROW = 3


@dataclass(frozen=True)
class Button:
    text: str
    kind: Literal["callback", "link"] = "callback"
    payload: str | None = None
    url: str | None = None


Keyboard = list[list[Button]]


def callback(text: str, payload: str) -> Button:
    return Button(text=text, kind="callback", payload=payload)


def link(text: str, url: str) -> Button:
    return Button(text=text, kind="link", url=url)


def to_attachment(keyboard: Keyboard | None) -> dict | None:
    """None — без клавиатуры. ValueError — клавиатура нарушает лимиты MAX
    (ошибка в коде бота, а не во входных данных)."""
    if not keyboard:
        return None
    if len(keyboard) > MAX_ROWS:
        raise ValueError(f"больше {MAX_ROWS} рядов кнопок")
    rows = []
    for row in keyboard:
        limit = MAX_LINK_BUTTONS_PER_ROW if any(b.kind == "link" for b in row) else MAX_BUTTONS_PER_ROW
        if not row or len(row) > limit:
            raise ValueError(f"в ряду должно быть 1..{limit} кнопок")
        rows.append([_button_json(button) for button in row])
    return {"type": "inline_keyboard", "payload": {"buttons": rows}}


def _button_json(button: Button) -> dict:
    if not 1 <= len(button.text) <= MAX_BUTTON_TEXT:
        raise ValueError("текст кнопки должен быть 1..128 символов")
    if button.kind == "link":
        if not button.url:
            raise ValueError("у link-кнопки нет url")
        return {"type": "link", "text": button.text, "url": button.url}
    if not button.payload or len(button.payload) > MAX_PAYLOAD:
        raise ValueError("payload callback-кнопки должен быть 1..1024 символов")
    return {"type": "callback", "text": button.text, "payload": button.payload}


def attachments(keyboard: Keyboard | None) -> list[dict]:
    attachment = to_attachment(keyboard)
    return [attachment] if attachment else []
