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
    LIMIT_PROMPT_CHOICES,
    LIMITS_LABEL,
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
        [Button.text(STATUS_LABEL, resize=True), Button.text(LIMITS_LABEL, resize=True)],
        [Button.text(SYNC_LABEL, resize=True)],
    ]


def accounts_page_keyboard(entries: list[tuple[int, str, bool]]) -> list[list[Button]]:
    """entries — (account_id, display_name, enabled). Две кнопки на
    строку: номер открывает карточку, 🟢/⚪ переключает enabled напрямую
    из списка (см. design "Нажатие должно включать/выключать account.enabled")."""
    return [
        [
            Button.inline(display_name, encode_account_open_callback(account_id)),
            Button.inline("🟢" if enabled else "⚪", encode_account_toggle_callback(account_id)),
        ]
        for account_id, display_name, enabled in entries
    ]


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
