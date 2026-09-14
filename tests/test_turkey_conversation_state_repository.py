"""
Тесты reader/turkey_bot/conversation_state_repository.py.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.conversation_state_repository import (  # noqa: E402
    TurkeyConversationStateRepository,
)


def _repo() -> TurkeyConversationStateRepository:
    return TurkeyConversationStateRepository(":memory:")


def test_get_returns_none_when_no_state():
    repo = _repo()

    assert repo.get(111) is None


def test_set_then_get_roundtrips_payload():
    repo = _repo()

    state = repo.set(111, telegram_user_id=222, step="awaiting_captcha_code", payload={"plate": "34ABC123"})

    assert state.chat_id == 111
    assert state.telegram_user_id == 222
    assert state.step == "awaiting_captcha_code"
    assert state.payload == {"plate": "34ABC123"}

    fetched = repo.get(111)
    assert fetched == state


def test_set_overwrites_previous_state_for_same_chat():
    repo = _repo()
    repo.set(111, telegram_user_id=222, step="step-one", payload={"a": 1})

    repo.set(111, telegram_user_id=222, step="step-two", payload={"b": 2})

    state = repo.get(111)
    assert state.step == "step-two"
    assert state.payload == {"b": 2}


def test_clear_removes_state():
    repo = _repo()
    repo.set(111, telegram_user_id=222, step="x", payload=None)

    repo.clear(111)

    assert repo.get(111) is None


def test_clear_is_safe_when_nothing_to_clear():
    repo = _repo()

    repo.clear(999)  # не должно бросать


def test_different_chats_are_independent():
    repo = _repo()
    repo.set(1, telegram_user_id=10, step="a", payload=None)
    repo.set(2, telegram_user_id=20, step="b", payload=None)

    assert repo.get(1).step == "a"
    assert repo.get(2).step == "b"
