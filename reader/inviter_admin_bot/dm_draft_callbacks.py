"""callback_data раздела "📨 Черновики" (ручная проверка ЛС-черновиков) —
собственный namespace "dmd_", не пересекающийся с "dmc_" (настройки
ЛС-кампаний) и остальными префиксами inviter_admin_bot.

callback_data несёт только id строки dm_outreach / ключ очереди — права
оператора и текущий статус черновика проверяются server-side на каждом
нажатии (см. DmDraftController): сам callback авторизацией не является."""

from dataclasses import dataclass

MAX_CALLBACK_BYTES = 64

MENU = b"dmd_menu"

ACTION_QUEUE = "q"
ACTION_OPEN = "open"
ACTION_APPROVE = "ok"
ACTION_EDIT = "edit"
ACTION_SKIP = "skip"

QUEUE_NEW = "new"
QUEUE_APPROVED = "approved"
QUEUE_SKIPPED = "skipped"
QUEUE_FAILED = "failed"
QUEUES = (QUEUE_NEW, QUEUE_APPROVED, QUEUE_SKIPPED, QUEUE_FAILED)

_WITH_ID = frozenset({ACTION_OPEN, ACTION_APPROVE, ACTION_EDIT, ACTION_SKIP})
_PREFIX = "dmd_"


@dataclass(frozen=True)
class DraftCallback:
    action: str
    outreach_id: int | None = None
    queue: str | None = None


def is_draft_callback(data: bytes | None) -> bool:
    return bool(data) and data.startswith(_PREFIX.encode("ascii"))


def encode(action: str, *, outreach_id: int | None = None, queue: str | None = None) -> bytes:
    if action == ACTION_QUEUE:
        if queue not in QUEUES:
            raise ValueError(f"Неизвестная очередь: {queue!r}")
        payload = f"{_PREFIX}{action}:{queue}"
    elif action in _WITH_ID:
        payload = f"{_PREFIX}{action}:{int(outreach_id)}"
    else:
        raise ValueError(f"Неизвестное действие: {action!r}")
    data = payload.encode("ascii")
    if len(data) > MAX_CALLBACK_BYTES:
        raise ValueError("callback_data длиннее 64 байт")
    return data


def decode(data: bytes | None) -> DraftCallback | None:
    """None — не наш callback либо он повреждён/неизвестен."""
    if not is_draft_callback(data):
        return None
    if data == MENU:
        return DraftCallback(action="menu")
    try:
        text = data.decode("ascii")[len(_PREFIX):]
    except UnicodeDecodeError:
        return None
    action, sep, rest = text.partition(":")
    if not sep:
        return None
    if action == ACTION_QUEUE:
        return DraftCallback(action=action, queue=rest) if rest in QUEUES else None
    if action in _WITH_ID:
        try:
            return DraftCallback(action=action, outreach_id=int(rest))
        except ValueError:
            return None
    return None
