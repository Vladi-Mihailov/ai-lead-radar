"""Telethon-клавиатуры/callback_data reader/inviter_admin_bot/ — та же схема,
что и у reader/public_bot/keyboards.py: callback_data несёт ТОЛЬКО
публичные идентификаторы (account_id), владение/авторизация проверяются
ИСКЛЮЧИТЕЛЬНО server-side на каждом callback заново (см. conversation.py::
_require_trusted) — тот же security-инвариант, что и везде в проекте.

MVP: список аккаунтов БЕЗ пагинации (см. design report — ожидаемое число
аккаунтов инвайтера на порядок меньше, чем у "Мои авто" Georgia bot, не
десятки страниц) — если аккаунтов станет много, добавить пагинацию тем же
приёмом, что и reader/public_bot/keyboards.py::trusted_tasks_page_keyboard,
без изменения остального."""

from telethon import Button

from reader.inviter_admin_bot.texts import (
    ACCOUNTS_LABEL,
    ADD_ACCOUNT_LABEL,
    BACK_BUTTON_LABEL,
    CANCEL_BUTTON_LABEL,
    CHANGE_LIMIT_LABEL,
    CHECK_SYNC_LABEL,
    HELP_LABEL,
    LIMIT_PROMPT_CHOICES,
    MANUAL_LIMIT_LABEL,
    PAUSE_LABEL,
    REAUTHORIZE_LABEL,
    START_LABEL,
    STATUS_LABEL,
    SYNC_LABEL,
    TURN_OFF_ACCOUNT_LABEL,
    TURN_ON_ACCOUNT_LABEL,
)

_ACCOUNT_OPEN_PREFIX = b"acc_open:"
_ACCOUNT_TOGGLE_PREFIX = b"acc_toggle:"
_ACCOUNT_LIMIT_PREFIX = b"acc_limit:"
_ACCOUNT_LIMIT_VALUE_PREFIX = b"acc_limitval:"
_ACCOUNT_LIMIT_MANUAL_PREFIX = b"acc_limitmanual:"
_ACCOUNT_SYNC_PREFIX = b"acc_sync:"
_ACCOUNT_REAUTH_PREFIX = b"acc_reauth:"
ACCOUNTS_BACK = b"accounts_back"

# Третья кнопка каждой строки "👤 Аккаунты" — "USED / LIMIT" (см. задачу
# "объедини экраны 👤 Аккаунты и ⚙️ Лимиты": бывший отдельный экран "⚙️
# Лимиты" удалён, эта кнопка — единственный оставшийся вход в тот же выбор
# лимита из СПИСКА, а не из карточки). Сознательно ОТДЕЛЬНЫЙ набор
# callback'ов от карточки аккаунта: тот же выбор лимита, но по завершении
# возвращает на СПИСОК "👤 Аккаунты" (ACCOUNTS_BACK), а не на карточку —
# _ACCOUNT_LIMIT_*/handle_account_limit_* выше остаются НЕТРОНУТЫМИ,
# карточка ("⚙️ Изменить лимит") по-прежнему возвращает на карточку.
_LIMITS_OPEN_PREFIX = b"limits_open:"
_LIMITS_VALUE_PREFIX = b"limits_val:"
_LIMITS_MANUAL_PREFIX = b"limits_manual:"


def _encode_id(prefix: bytes, account_id: int) -> bytes:
    return prefix + str(account_id).encode("ascii")


def _decode_id(data: bytes | None, prefix: bytes) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    try:
        return int(data[len(prefix):])
    except ValueError:
        return None


def encode_account_open_callback(account_id: int) -> bytes:
    return _encode_id(_ACCOUNT_OPEN_PREFIX, account_id)


def decode_account_open_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _ACCOUNT_OPEN_PREFIX)


def encode_account_toggle_callback(account_id: int) -> bytes:
    return _encode_id(_ACCOUNT_TOGGLE_PREFIX, account_id)


def decode_account_toggle_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _ACCOUNT_TOGGLE_PREFIX)


def encode_account_limit_callback(account_id: int) -> bytes:
    return _encode_id(_ACCOUNT_LIMIT_PREFIX, account_id)


def decode_account_limit_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _ACCOUNT_LIMIT_PREFIX)


def encode_account_limit_value_callback(account_id: int, value: int) -> bytes:
    return _ACCOUNT_LIMIT_VALUE_PREFIX + f"{account_id}:{value}".encode("ascii")


def decode_account_limit_value_callback(data: bytes | None) -> tuple[int, int] | None:
    if not data or not data.startswith(_ACCOUNT_LIMIT_VALUE_PREFIX):
        return None
    parts = data[len(_ACCOUNT_LIMIT_VALUE_PREFIX):].split(b":")
    if len(parts) != 2:
        return None
    try:
        account_id, value = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if value not in LIMIT_PROMPT_CHOICES:
        return None
    return account_id, value


def encode_account_limit_manual_callback(account_id: int) -> bytes:
    return _encode_id(_ACCOUNT_LIMIT_MANUAL_PREFIX, account_id)


def decode_account_limit_manual_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _ACCOUNT_LIMIT_MANUAL_PREFIX)


def encode_limits_open_callback(account_id: int) -> bytes:
    return _encode_id(_LIMITS_OPEN_PREFIX, account_id)


def decode_limits_open_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _LIMITS_OPEN_PREFIX)


def encode_limits_value_callback(account_id: int, value: int) -> bytes:
    return _LIMITS_VALUE_PREFIX + f"{account_id}:{value}".encode("ascii")


def decode_limits_value_callback(data: bytes | None) -> tuple[int, int] | None:
    if not data or not data.startswith(_LIMITS_VALUE_PREFIX):
        return None
    parts = data[len(_LIMITS_VALUE_PREFIX):].split(b":")
    if len(parts) != 2:
        return None
    try:
        account_id, value = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if value not in LIMIT_PROMPT_CHOICES:
        return None
    return account_id, value


def encode_limits_manual_callback(account_id: int) -> bytes:
    return _encode_id(_LIMITS_MANUAL_PREFIX, account_id)


def decode_limits_manual_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _LIMITS_MANUAL_PREFIX)


def encode_account_sync_callback(account_id: int) -> bytes:
    return _encode_id(_ACCOUNT_SYNC_PREFIX, account_id)


def decode_account_sync_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _ACCOUNT_SYNC_PREFIX)


def encode_account_reauthorize_callback(account_id: int) -> bytes:
    return _encode_id(_ACCOUNT_REAUTH_PREFIX, account_id)


def decode_account_reauthorize_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _ACCOUNT_REAUTH_PREFIX)


def main_menu_keyboard() -> list[list[Button]]:
    return [
        [Button.text(ACCOUNTS_LABEL, resize=True), Button.text(ADD_ACCOUNT_LABEL, resize=True)],
        [Button.text(START_LABEL, resize=True), Button.text(PAUSE_LABEL, resize=True)],
        [Button.text(STATUS_LABEL, resize=True), Button.text(SYNC_LABEL, resize=True)],
        [Button.text(HELP_LABEL, resize=True)],
    ]


def accounts_page_keyboard(entries: list[tuple[int, str, bool, int, int]]) -> list[list[Button]]:
    """entries — (account_id, display_name, enabled, sent_today,
    daily_limit) (см. задачу "объедини экраны 👤 Аккаунты и ⚙️ Лимиты" —
    бывший отдельный экран "⚙️ Лимиты" удалён, его данные/кнопка
    перенесены сюда третьей кнопкой строки). Три кнопки на строку: имя
    открывает карточку (encode_account_open_callback, без изменений),
    🟢/⚪ переключает enabled напрямую из списка (см. design "Нажатие
    должно включать/выключать account.enabled", логика НЕ менялась),
    "USED / LIMIT" (та же формула, что и у inviter — joined_today +
    pending_today) открывает выбор лимита, который возвращает на этот же
    список (см. limits_value_choice_keyboard/ACCOUNTS_BACK)."""
    return [
        [
            Button.inline(display_name, encode_account_open_callback(account_id)),
            Button.inline("🟢" if enabled else "⚪", encode_account_toggle_callback(account_id)),
            Button.inline(f"{sent_today} / {daily_limit}", encode_limits_open_callback(account_id)),
        ]
        for account_id, display_name, enabled, sent_today, daily_limit in entries
    ]


def limits_value_choice_keyboard(account_id: int) -> list[list[Button]]:
    """Тот же набор значений/раскладка, что и limit_choice_keyboard (5/10/
    15/20/25 + "Ввести вручную"), но callback'и и "Назад" ведут на список
    "👤 Аккаунты" (ACCOUNTS_BACK), а не на карточку аккаунта (см. design
    "После изменения возвращаемся к списку, где сразу отображается новое
    значение") — открывается третьей кнопкой строки accounts_page_keyboard."""
    buttons = [
        Button.inline(str(value), encode_limits_value_callback(account_id, value))
        for value in LIMIT_PROMPT_CHOICES
    ]
    rows = [buttons[0:3], buttons[3:5]]
    rows.append([Button.inline(MANUAL_LIMIT_LABEL, encode_limits_manual_callback(account_id))])
    rows.append([Button.inline(BACK_BUTTON_LABEL, ACCOUNTS_BACK)])
    return rows


def account_card_keyboard(account_id: int, *, enabled: bool) -> list[list[Button]]:
    toggle_row = (
        [Button.inline(TURN_OFF_ACCOUNT_LABEL, encode_account_toggle_callback(account_id))]
        if enabled
        else [Button.inline(TURN_ON_ACCOUNT_LABEL, encode_account_toggle_callback(account_id))]
    )
    return [
        toggle_row,
        [Button.inline(CHANGE_LIMIT_LABEL, encode_account_limit_callback(account_id))],
        [Button.inline(CHECK_SYNC_LABEL, encode_account_sync_callback(account_id))],
        [Button.inline(REAUTHORIZE_LABEL, encode_account_reauthorize_callback(account_id))],
        [Button.inline(BACK_BUTTON_LABEL, ACCOUNTS_BACK)],
    ]


def limit_choice_keyboard(account_id: int) -> list[list[Button]]:
    buttons = [
        Button.inline(str(value), encode_account_limit_value_callback(account_id, value))
        for value in LIMIT_PROMPT_CHOICES
    ]
    rows = [buttons[0:3], buttons[3:5]]
    rows.append([Button.inline(MANUAL_LIMIT_LABEL, encode_account_limit_manual_callback(account_id))])
    rows.append([Button.inline(BACK_BUTTON_LABEL, encode_account_open_callback(account_id))])
    return rows


def cancel_keyboard() -> list[list[Button]]:
    return [[Button.text(CANCEL_BUTTON_LABEL, resize=True)]]
