"""
Тесты sync_all_users() (sync_users.py: список участников групп) — в
частности, что access_hash/peer_type сохраняются из полноценных объектов
Telethon User, доступных в iter_participants().
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.groups import Group  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402
from reader.users.sync import sync_all_users  # noqa: E402


class _FakeParticipant:
    def __init__(self, user_id, username=None, first_name=None, last_name=None, bot=False, access_hash=None):
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.bot = bot
        self.access_hash = access_hash


class _FakeEntity:
    def __init__(self, chat_id, title):
        self.id = chat_id
        self.title = title
        self.username = None


class _FakeClient:
    def __init__(self, entity, participants):
        self._entity = entity
        self._participants = participants

    async def get_entity(self, ident):
        return self._entity

    def iter_participants(self, entity):
        participants = self._participants

        async def gen():
            for participant in participants:
                yield participant

        return gen()


async def test_sync_all_users_saves_access_hash_and_peer_type(tmp_path):
    entity = _FakeEntity(-100111, "Test group")
    participants = [_FakeParticipant(1, username="ivan", access_hash=42)]
    client = _FakeClient(entity, participants)
    group = Group(id=-100111, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    try:
        await sync_all_users(client, [group], repository)

        user = repository.get(1)
        assert user.access_hash == 42
        assert user.peer_type == "_FakeParticipant"
        assert repository.get_peer_updated_at(1) is not None
    finally:
        repository.close()
