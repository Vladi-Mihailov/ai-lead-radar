"""callback_data раздела "✉️ ЛС-кампании" — отдельный namespace "dmc_",
не пересекающийся ни с одним существующим префиксом inviter_admin_bot (см.
keyboards.py: acc_*/camp_*/limits_*). Без Telethon — чистое кодирование,
чтобы контроллер (dm_campaign_controller.py) оставался независимым от
Button так же, как conversation.py.

callback_data несёт только публичные идентификаторы (campaign id,
telegram_accounts.id, идентификатор группы из groups.yaml) — их
существование и права пользователя проверяются server-side на каждом
нажатии (см. DmCampaignController)."""

from dataclasses import dataclass

# Лимит Telegram на callback_data.
MAX_CALLBACK_BYTES = 64

LIST = b"dmc_list"

ACTION_OPEN = "open"
ACTION_ENABLE = "on"
ACTION_DISABLE = "off"
ACTION_GUIDELINE = "guide"
ACTION_RESOURCES = "res"
ACTION_SOURCES = "src"
ACTION_SOURCE_TOGGLE = "srct"
ACTION_SOURCES_ALL = "srcall"
ACTION_ACCOUNTS = "accs"
ACTION_ACCOUNT_TOGGLE = "acct"
ACTION_ACCOUNT_LIMIT = "lim"
ACTION_FOLLOW_UP_ENABLE = "fupon"
ACTION_FOLLOW_UP_DISABLE = "fupoff"
ACTION_FOLLOW_UP_TEXT = "fupt"

_CAMPAIGN_ONLY = frozenset({
    ACTION_OPEN, ACTION_ENABLE, ACTION_DISABLE, ACTION_GUIDELINE, ACTION_RESOURCES,
    ACTION_SOURCES, ACTION_SOURCES_ALL, ACTION_ACCOUNTS, ACTION_FOLLOW_UP_ENABLE,
    ACTION_FOLLOW_UP_DISABLE, ACTION_FOLLOW_UP_TEXT,
})
_WITH_ACCOUNT = frozenset({ACTION_ACCOUNT_TOGGLE, ACTION_ACCOUNT_LIMIT})
_WITH_SOURCE = frozenset({ACTION_SOURCE_TOGGLE})

_PREFIX = "dmc_"


@dataclass(frozen=True)
class DmCallback:
    action: str
    campaign_id: int | None = None
    account_id: int | None = None
    source: str | None = None


def is_dm_callback(data: bytes | None) -> bool:
    return bool(data) and data.startswith(_PREFIX.encode("ascii"))


def encode(action: str, campaign_id: int, *, account_id: int | None = None, source: str | None = None) -> bytes:
    if action in _WITH_ACCOUNT:
        payload = f"{_PREFIX}{action}:{campaign_id}:{account_id}"
    elif action in _WITH_SOURCE:
        payload = f"{_PREFIX}{action}:{campaign_id}:{source}"
    elif action in _CAMPAIGN_ONLY:
        payload = f"{_PREFIX}{action}:{campaign_id}"
    else:
        raise ValueError(f"Неизвестное действие: {action!r}")
    data = payload.encode("utf-8")
    if len(data) > MAX_CALLBACK_BYTES:
        raise ValueError("callback_data длиннее 64 байт")
    return data


def decode(data: bytes | None) -> DmCallback | None:
    """None — не наш callback или он повреждён/неизвестен (вызывающий
    отвечает "неизвестная или устаревшая кнопка")."""
    if not is_dm_callback(data):
        return None
    if data == LIST:
        return DmCallback(action="list")
    try:
        text = data.decode("utf-8")[len(_PREFIX):]
    except UnicodeDecodeError:
        return None
    action, sep, rest = text.partition(":")
    if not sep:
        return None
    try:
        if action in _CAMPAIGN_ONLY:
            return DmCallback(action=action, campaign_id=int(rest))
        if action in _WITH_ACCOUNT:
            campaign_raw, sep2, account_raw = rest.partition(":")
            if not sep2:
                return None
            return DmCallback(action=action, campaign_id=int(campaign_raw), account_id=int(account_raw))
        if action in _WITH_SOURCE:
            campaign_raw, sep2, source = rest.partition(":")
            if not sep2 or not source:
                return None
            return DmCallback(action=action, campaign_id=int(campaign_raw), source=source)
    except ValueError:
        return None
    return None
