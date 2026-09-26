"""Тесты ProtocolCheckEphemeralStore — in-memory, не persistent (см. задачу
"Проверить протокол" п.4)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.public_bot.protocol_check_ephemeral_store import (
    ProtocolCheckEphemeralStore,
)


def test_set_then_pop_returns_value_and_forgets_it():
    store = ProtocolCheckEphemeralStore()
    store.set_protocol_number(1, "PR-1")

    assert store.pop_protocol_number(1) == "PR-1"
    assert store.pop_protocol_number(1) is None


def test_pop_on_unknown_chat_id_returns_none():
    store = ProtocolCheckEphemeralStore()
    assert store.pop_protocol_number(999) is None


def test_clear_is_idempotent_and_removes_value():
    store = ProtocolCheckEphemeralStore()
    store.clear(1)  # ничего не было — не должно падать
    store.set_protocol_number(1, "PR-1")
    store.clear(1)
    assert store.pop_protocol_number(1) is None


def test_chat_ids_are_isolated_from_each_other():
    store = ProtocolCheckEphemeralStore()
    store.set_protocol_number(1, "PR-1")
    store.set_protocol_number(2, "PR-2")

    store.clear(1)

    assert store.pop_protocol_number(1) is None
    assert store.pop_protocol_number(2) == "PR-2"
