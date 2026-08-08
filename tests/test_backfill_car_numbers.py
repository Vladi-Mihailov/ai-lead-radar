"""Тесты узкого backfill car_numbers (reader/users/backfill_car_numbers.py)
— читает историю сообщений (тот же приём, что и history_sync.py), но
трогает СТРОГО ТОЛЬКО users.car_numbers: не пересчитывает keywords, не
резолвит/меняет username/access_hash/last_seen_at и не читает/пишет
HistorySyncStateRepository (checkpoint sync_users.py остаётся нетронутым).

Реальные запросы к Telegram не выполняются — TelegramClient полностью
заменён фейком, как и в tests/test_history_sync.py.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon.errors import FloodWaitError  # noqa: E402
from telethon.tl.functions.messages import GetHistoryRequest  # noqa: E402

from reader.groups import Group  # noqa: E402
from reader.users.backfill_car_numbers import backfill_car_numbers  # noqa: E402
from reader.users.history_state_repository import HistorySyncStateRepository  # noqa: E402
from reader.users.models import TelegramUserInfo  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402


class _FakeEntity:
    def __init__(self, chat_id, title):
        self.id = chat_id
        self.title = title


class _FakeMessage:
    def __init__(self, message_id, sender_id, raw_text=""):
        self.id = message_id
        self.sender_id = sender_id
        self.raw_text = raw_text


class _FakeClient:
    """Отдаёт заранее заданный список сообщений (по убыванию id, как
    настоящая история), уважая offset_id, с возможностью сымитировать
    FloodWait на первых N вызовах iter_messages() и/или ошибку резолва
    самой группы."""

    def __init__(
        self, entity, messages, *, flood_wait_on_calls=(), flood_wait_seconds=5,
        entity_error=None,
    ):
        self.entity = entity
        self.messages = messages
        self.flood_wait_on_calls = set(flood_wait_on_calls)
        self.flood_wait_seconds = flood_wait_seconds
        self.entity_error = entity_error
        self.iter_messages_calls = 0
        self.get_entity_calls = 0
        self.iter_messages_offsets: list[int] = []

    async def get_entity(self, ident):
        self.get_entity_calls += 1
        if self.entity_error is not None:
            raise self.entity_error
        return self.entity

    def iter_messages(self, entity, offset_id=0, limit=None):
        self.iter_messages_calls += 1
        self.iter_messages_offsets.append(offset_id)
        call_number = self.iter_messages_calls
        source = self

        async def gen():
            if call_number in source.flood_wait_on_calls:
                raise FloodWaitError(request=GetHistoryRequest, capture=source.flood_wait_seconds)
                yield  # pragma: no cover — делает gen честным генератором

            available = [m for m in source.messages if offset_id == 0 or m.id < offset_id]
            for message in available:
                yield message

        return gen()


def _make_messages(entries):
    """entries — [(message_id, sender_id, raw_text), ...] по убыванию id."""
    return [_FakeMessage(mid, sender_id, text) for mid, sender_id, text in entries]


async def _sleep_recorder(monkeypatch):
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr("reader.users.backfill_car_numbers.asyncio.sleep", fake_sleep)
    return calls


# ---- группировка по user_id / запись car_numbers ----


async def test_groups_car_numbers_by_user_from_history(tmp_path):
    entity = _FakeEntity(-100111, "Test group")
    group = Group(id=-100111, username=None, title="Test group")
    messages = _make_messages([
        (3, 111, "продаю А111АА77"),
        (2, 222, "мой номер Х777ХХ197"),
        (1, 111, "и напомню, A111AA77"),
    ])
    client = _FakeClient(entity, messages)

    repository = UserRepository(tmp_path / "users.db")
    try:
        stats = await backfill_car_numbers(client, [group], repository)

        assert repository.get_car_numbers(111) == ["A111AA77"]
        assert repository.get_car_numbers(222) == ["X777XX197"]
        assert stats.groups_scanned == 1
        assert stats.messages_scanned == 3
        assert stats.users_with_car_numbers == 2
    finally:
        repository.close()


async def test_same_user_across_multiple_groups_merges_numbers(tmp_path):
    entity_a = _FakeEntity(-100201, "Group A")
    entity_b = _FakeEntity(-100202, "Group B")
    group_a = Group(id=-100201, username=None, title="Group A")
    group_b = Group(id=-100202, username=None, title="Group B")

    client_a_messages = _make_messages([(1, 333, "А111АА77")])
    client_b_messages = _make_messages([(1, 333, "Х777ХХ197")])

    class _MultiGroupClient:
        def __init__(self):
            self.calls = []

        async def get_entity(self, ident):
            self.calls.append(ident)
            return entity_a if ident == group_a.identifier else entity_b

        def iter_messages(self, entity, offset_id=0, limit=None):
            messages = client_a_messages if entity is entity_a else client_b_messages

            async def gen():
                for message in messages:
                    if offset_id == 0 or message.id < offset_id:
                        yield message

            return gen()

    repository = UserRepository(tmp_path / "users.db")
    try:
        await backfill_car_numbers(_MultiGroupClient(), [group_a, group_b], repository)

        assert repository.get_car_numbers(333) == ["A111AA77", "X777XX197"]
    finally:
        repository.close()


async def test_message_without_sender_id_is_skipped(tmp_path):
    entity = _FakeEntity(-100301, "Test group")
    group = Group(id=-100301, username=None, title="Test group")
    messages = _make_messages([(1, None, "А111АА77")])
    client = _FakeClient(entity, messages)

    repository = UserRepository(tmp_path / "users.db")
    try:
        stats = await backfill_car_numbers(client, [group], repository)

        assert stats.users_with_car_numbers == 0
        assert stats.messages_scanned == 1
    finally:
        repository.close()


async def test_message_without_car_number_contributes_nothing(tmp_path):
    entity = _FakeEntity(-100302, "Test group")
    group = Group(id=-100302, username=None, title="Test group")
    messages = _make_messages([(1, 444, "привет, как дела?")])
    client = _FakeClient(entity, messages)

    repository = UserRepository(tmp_path / "users.db")
    try:
        stats = await backfill_car_numbers(client, [group], repository)

        assert repository.get_car_numbers(444) == []
        assert stats.users_with_car_numbers == 0
    finally:
        repository.close()


async def test_unresolvable_group_is_skipped_without_raising(tmp_path):
    group = Group(id=-100303, username=None, title="Broken group")
    client = _FakeClient(
        _FakeEntity(-100303, "Broken group"), [], entity_error=ValueError("not found"),
    )

    repository = UserRepository(tmp_path / "users.db")
    try:
        stats = await backfill_car_numbers(client, [group], repository)

        assert stats.groups_scanned == 1
        assert stats.messages_scanned == 0
    finally:
        repository.close()


# ---- FloodWait во время чтения истории — ждёт и продолжает с offset ----


async def test_flood_wait_retries_and_resumes_without_reprocessing(tmp_path, monkeypatch):
    sleep_calls = await _sleep_recorder(monkeypatch)

    entity = _FakeEntity(-100401, "Test group")
    group = Group(id=-100401, username=None, title="Test group")
    messages = _make_messages([
        (3, 555, "А111АА77"),
        (2, 555, "Х777ХХ197"),
        (1, 555, "У111УУ77"),
    ])
    client = _FakeClient(entity, messages, flood_wait_on_calls={1}, flood_wait_seconds=9)

    repository = UserRepository(tmp_path / "users.db")
    try:
        stats = await backfill_car_numbers(client, [group], repository)

        assert sleep_calls == [9]
        assert client.iter_messages_calls == 2
        # Первая попытка ничего не обработала (FloodWait сразу) — offset_id
        # для повтора остался 0, вторая попытка читает всю историю заново
        # (в памяти этого запуска, без персистентного checkpoint).
        assert client.iter_messages_offsets == [0, 0]
        assert repository.get_car_numbers(555) == ["A111AA77", "X777XX197", "Y111YY77"]
        assert stats.messages_scanned == 3
    finally:
        repository.close()


# ---- идемпотентность повторного запуска ----


async def test_rerunning_backfill_does_not_duplicate_or_change_result(tmp_path):
    entity = _FakeEntity(-100501, "Test group")
    group = Group(id=-100501, username=None, title="Test group")
    messages = _make_messages([
        (2, 666, "А111АА77"),
        (1, 666, "напомню A111AA77, и Х777ХХ197"),
    ])

    repository = UserRepository(tmp_path / "users.db")
    try:
        first_client = _FakeClient(entity, messages)
        await backfill_car_numbers(first_client, [group], repository)
        first_result = repository.get_car_numbers(666)
        assert first_result == ["A111AA77", "X777XX197"]

        second_client = _FakeClient(entity, messages)
        await backfill_car_numbers(second_client, [group], repository)
        assert repository.get_car_numbers(666) == first_result
    finally:
        repository.close()


# ---- узость: НЕ трогает keywords/username/access_hash/last_seen_at/checkpoint ----


async def test_backfill_only_changes_car_numbers_leaves_everything_else_untouched(tmp_path):
    """Явное доказательство узости backfill: после прогона car_numbers
    заполнен, а keywords и все остальные поля пользователя (username,
    first_name, last_name, access_hash, peer_type, last_seen_at) остаются
    БИТ В БИТ такими же, как были до backfill."""
    db_path = tmp_path / "users.db"
    repository = UserRepository(db_path)
    try:
        repository.upsert(
            TelegramUserInfo(
                user_id=777, username="ivan", first_name="Ivan", last_name="Petrov",
                is_bot=False, access_hash=123456789, peer_type="User",
            )
        )
        repository.add_keywords(777, ["осаго", "страховка"])

        before_user = repository.get(777)
        before_keywords = repository.get_keywords(777)
        before_peer_updated_at = repository.get_peer_updated_at(777)
        before_last_seen_at = repository._conn.execute(
            "SELECT last_seen_at FROM users WHERE user_id = ?", (777,)
        ).fetchone()[0]
        assert repository.get_car_numbers(777) == []

        entity = _FakeEntity(-100601, "Test group")
        group = Group(id=-100601, username=None, title="Test group")
        messages = _make_messages([(1, 777, "продаю А111АА77")])
        client = _FakeClient(entity, messages)

        await backfill_car_numbers(client, [group], repository)

        # car_numbers — единственное, что изменилось.
        assert repository.get_car_numbers(777) == ["A111AA77"]

        after_user = repository.get(777)
        assert after_user.username == before_user.username
        assert after_user.first_name == before_user.first_name
        assert after_user.last_name == before_user.last_name
        assert after_user.is_bot == before_user.is_bot
        assert after_user.access_hash == before_user.access_hash
        assert after_user.peer_type == before_user.peer_type

        assert repository.get_keywords(777) == before_keywords
        assert repository.get_peer_updated_at(777) == before_peer_updated_at

        after_last_seen_at = repository._conn.execute(
            "SELECT last_seen_at FROM users WHERE user_id = ?", (777,)
        ).fetchone()[0]
        assert after_last_seen_at == before_last_seen_at
    finally:
        repository.close()


async def test_backfill_does_not_read_or_write_history_sync_checkpoints(tmp_path):
    """HistorySyncStateRepository (checkpoint sync_users.py) остаётся
    полностью нетронутым — backfill_car_numbers его даже не открывает."""
    db_path = tmp_path / "users.db"

    state_repository = HistorySyncStateRepository(db_path)
    try:
        state_repository.save_progress(
            chat_id=-100701, chat_name="Test group", oldest_processed_message_id=5,
            oldest_processed_date=None, processed_messages=42, saved_users=3,
            history_completed=False,
        )
        before = state_repository.get(-100701)
        before_reindex = state_repository.get(-100701, mode="reindex")
    finally:
        state_repository.close()

    repository = UserRepository(db_path)
    try:
        entity = _FakeEntity(-100701, "Test group")
        group = Group(id=-100701, username=None, title="Test group")
        messages = _make_messages([(1, 888, "А111АА77")])
        client = _FakeClient(entity, messages)

        await backfill_car_numbers(client, [group], repository)
        assert repository.get_car_numbers(888) == ["A111AA77"]
    finally:
        repository.close()

    state_repository = HistorySyncStateRepository(db_path)
    try:
        assert state_repository.get(-100701) == before
        assert state_repository.get(-100701, mode="reindex") == before_reindex
    finally:
        state_repository.close()
