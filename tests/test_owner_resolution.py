"""
Тесты resolve_owner_username (reader/public_bot/owner_resolution.py) —
резолв @username владельца в numeric Telegram user_id для trusted-operator
delegated flow. UserRepository — настоящий (SQLite/tmp_path), Telegram-
клиент — фейковый (без сети), тот же приём, что и в tests/test_fine_command.py
для операторского "fine add @username".
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError  # noqa: E402
from telethon.tl.types import User as TelethonUser  # noqa: E402

from reader.public_bot.owner_resolution import (  # noqa: E402
    OwnerResolutionError,
    resolve_owner_username,
)
from reader.users.models import TelegramUserInfo  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402


class _FakeTelegramClient:
    def __init__(self, *, entities=None, not_found_usernames=(), errors=None):
        self._entities = {k.lower(): v for k, v in (entities or {}).items()}
        self._not_found_usernames = {u.lower() for u in not_found_usernames}
        self._errors = {k.lower(): v for k, v in (errors or {}).items()}
        self.get_entity_calls: list[str] = []

    async def get_entity(self, entity):
        username = str(entity).lstrip("@").lower()
        self.get_entity_calls.append(username)

        if username in self._errors:
            raise self._errors[username]
        if username in self._not_found_usernames:
            raise UsernameNotOccupiedError(request=None)
        if username in self._entities:
            return self._entities[username]
        raise UsernameNotOccupiedError(request=None)


def _telethon_user(user_id: int, username: str, first_name="Real", last_name="Owner") -> TelethonUser:
    return TelethonUser(
        id=user_id, is_self=False, contact=False, mutual_contact=False, deleted=False,
        bot=False, bot_chat_history=False, bot_nochats=False, verified=False, restricted=False,
        min=False, bot_inline_geo=False, support=False, scam=False, apply_min_photo=False,
        fake=False, bot_attach_menu=False, premium=False, attach_menu_enabled=False,
        bot_can_edit=False, close_friend=False, stories_hidden=False, stories_unavailable=False,
        access_hash=999,
        first_name=first_name, last_name=last_name, username=username, phone=None, photo=None,
        status=None, bot_info_version=None, restriction_reason=None, bot_inline_placeholder=None,
        lang_code=None,
    )


async def test_resolves_from_local_user_repository_without_telegram_call(tmp_path):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        user_repository.upsert(
            TelegramUserInfo(user_id=42, username="known_user", first_name="Anna", last_name=None)
        )
        client = _FakeTelegramClient()

        resolved = await resolve_owner_username(
            "known_user", user_repository=user_repository, telegram_client=client,
        )

        assert resolved is not None
        assert resolved.telegram_user_id == 42
        assert resolved.username == "known_user"
        assert client.get_entity_calls == []  # локальная БД — сеть не нужна
    finally:
        user_repository.close()


async def test_resolves_via_live_telegram_lookup_and_stores_in_user_repository(tmp_path):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        client = _FakeTelegramClient(
            entities={"new_person": _telethon_user(777, "new_person")}
        )

        resolved = await resolve_owner_username(
            "new_person", user_repository=user_repository, telegram_client=client,
        )

        assert resolved is not None
        assert resolved.telegram_user_id == 777
        assert resolved.username == "new_person"
        assert client.get_entity_calls == ["new_person"]

        # Сохранён в локальной БД для будущих резолвов/для operator-facing
        # отображения владельца (см. design report).
        stored = user_repository.get(777)
        assert stored is not None
        assert stored.username == "new_person"
    finally:
        user_repository.close()


async def test_returns_none_when_username_not_occupied(tmp_path):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        client = _FakeTelegramClient(not_found_usernames=["ghost"])

        resolved = await resolve_owner_username(
            "ghost", user_repository=user_repository, telegram_client=client,
        )

        assert resolved is None
        assert user_repository.get_car_numbers(0) == []  # ничего не создано
    finally:
        user_repository.close()


async def test_returns_none_when_username_invalid_syntax(tmp_path):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        client = _FakeTelegramClient(errors={"bad!": UsernameInvalidError(request=None)})

        resolved = await resolve_owner_username(
            "bad!", user_repository=user_repository, telegram_client=client,
        )

        assert resolved is None
    finally:
        user_repository.close()


async def test_returns_none_when_resolved_entity_is_not_a_user(tmp_path):
    """Резолвнутая сущность оказалась каналом/группой (не telethon.tl.types.
    User), а не обычным пользователем — та же логика, что и в
    reader/commands/fine.py. Реальный Channel-объект не нужен — важно
    только то, что isinstance(entity, User) окажется False."""
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        not_a_user = object()
        client = _FakeTelegramClient(entities={"some_channel": not_a_user})

        resolved = await resolve_owner_username(
            "some_channel", user_repository=user_repository, telegram_client=client,
        )

        assert resolved is None
    finally:
        user_repository.close()


async def test_returns_none_without_telegram_client_and_not_in_local_db(tmp_path):
    """telegram_client=None (например, тест/окружение без бота) — резолв
    работает только по локальной БД, живой резолв просто не выполняется."""
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        resolved = await resolve_owner_username(
            "nobody", user_repository=user_repository, telegram_client=None,
        )
        assert resolved is None
    finally:
        user_repository.close()


async def test_technical_error_raises_owner_resolution_error_not_none(tmp_path):
    """Сеть/FloodWait/прочий RPC-сбой — НЕ то же самое, что "не найден":
    пробрасывается OwnerResolutionError, чтобы вызывающий код не создал
    ложную ownership-запись, приняв технический сбой за "точно не существует"."""
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        client = _FakeTelegramClient(errors={"flaky": RuntimeError("network down")})

        with pytest.raises(OwnerResolutionError):
            await resolve_owner_username(
                "flaky", user_repository=user_repository, telegram_client=client,
            )
    finally:
        user_repository.close()
