"""Telethon-клавиатуры Turkey-бота. Единственная клавиатура — inline
"❌ Отмена", прикреплённая к сообщению с CAPTCHA (см. design report Stage
3: /cancel доступен и как команда, и как кнопка — оба пути ведут в один и
тот же ConversationController.handle_cancel()).

callback_data здесь — фиксированная константа без какого-либо
пользовательского/сессионного значения внутри (в отличие от
reader/public_bot/keyboards.py, где callback_data несёт subscription_id) —
отменить можно только СВОЙ собственный текущий диалог, а какой именно chat_id
отменять, определяется ИСКЛЮЧИТЕЛЬНО event.chat_id самого нажатия, никогда
из тела callback_data (тот же принцип "identity — только из события", что
и у reader/public_bot/handlers.py)."""

from telethon import Button

from reader.turkey_bot.texts import CANCEL_BUTTON_LABEL

CANCEL_CALLBACK_DATA = b"turkeycancel"


def cancel_keyboard() -> list[list[Button]]:
    return [[Button.inline(CANCEL_BUTTON_LABEL, CANCEL_CALLBACK_DATA)]]
