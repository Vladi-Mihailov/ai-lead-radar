"""
Тесты BotConversationStateRepository — persistent-состояние пошагового
диалога @GEShtrafbot (например, "Добавить авто"), см. design report.
Реальный sqlite (файл в tmp_path), чтобы проверить, что состояние реально
переживает переоткрытие соединения — то есть настоящий restart процесса,
а не только "в памяти одного объекта" (тот же приём, что и
tests/test_checkout_lock_repository.py).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.public_bot.conversation_state_repository import (  # noqa: E402
    BotConversationStateRepository,
)


def test_get_returns_none_when_no_state_exists():
    repo = BotConversationStateRepository(":memory:")
    assert repo.get(chat_id=42) is None
    repo.close()


def test_set_then_get_roundtrips_with_payload():
    repo = BotConversationStateRepository(":memory:")
    repo.set(
        chat_id=42, telegram_user_id=42, step="awaiting_car_number",
        payload={"car_number": "B957"},
    )

    state = repo.get(chat_id=42)

    assert state.chat_id == 42
    assert state.telegram_user_id == 42
    assert state.step == "awaiting_car_number"
    assert state.payload == {"car_number": "B957"}
    repo.close()


def test_set_without_payload_stores_none():
    repo = BotConversationStateRepository(":memory:")
    repo.set(chat_id=42, telegram_user_id=42, step="awaiting_car_number")

    state = repo.get(chat_id=42)

    assert state.payload is None
    repo.close()


def test_set_overwrites_previous_state_for_same_chat_entirely():
    """Новый шаг того же диалога перезаписывает предыдущий payload
    целиком, не сливает его с прежним."""
    repo = BotConversationStateRepository(":memory:")
    repo.set(
        chat_id=42, telegram_user_id=42, step="awaiting_car_number",
        payload={"car_number": "B957MA09"},
    )
    repo.set(
        chat_id=42, telegram_user_id=42, step="awaiting_period",
        payload={"car_number": "B957MA09", "username": "client"},
    )

    state = repo.get(chat_id=42)

    assert state.step == "awaiting_period"
    assert state.payload == {"car_number": "B957MA09", "username": "client"}
    repo.close()


def test_different_chats_do_not_collide():
    repo = BotConversationStateRepository(":memory:")
    repo.set(chat_id=1, telegram_user_id=1, step="awaiting_car_number")
    repo.set(chat_id=2, telegram_user_id=2, step="awaiting_period")

    assert repo.get(chat_id=1).step == "awaiting_car_number"
    assert repo.get(chat_id=2).step == "awaiting_period"
    repo.close()


def test_clear_removes_state():
    repo = BotConversationStateRepository(":memory:")
    repo.set(chat_id=42, telegram_user_id=42, step="awaiting_car_number")

    repo.clear(chat_id=42)

    assert repo.get(chat_id=42) is None
    repo.close()


def test_clear_unknown_chat_does_not_raise():
    repo = BotConversationStateRepository(":memory:")
    repo.clear(chat_id=999999)  # не должно бросать исключение
    repo.close()


def test_state_survives_reopening_the_same_file_simulating_restart(tmp_path):
    db_path = tmp_path / "bot_conversation_state_test.db"

    repo1 = BotConversationStateRepository(db_path)
    repo1.set(
        chat_id=42, telegram_user_id=42, step="awaiting_period",
        payload={"car_number": "B957MA09"},
    )
    repo1.close()

    # "Новый процесс" — новое соединение к тому же файлу.
    repo2 = BotConversationStateRepository(db_path)
    state = repo2.get(chat_id=42)

    assert state is not None
    assert state.step == "awaiting_period"
    assert state.payload == {"car_number": "B957MA09"}
    repo2.close()
