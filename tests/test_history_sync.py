"""
Тесты инкрементальной синхронизации истории (checkpoint/resume/completion),
а также трёх точечных фиксов из code review:

- уже известный локально пользователь не должен вызывать message.get_sender()
  (потенциальный лишний сетевой запрос) и повторный upsert;
- сбой UserRepository (get/upsert) не должен прерывать обработку всей группы;
- FloodWaitError должен приводить к ожиданию exc.seconds и повтору той же
  группы, а не мгновенному переходу к следующей.

Реальные запросы к Telegram не выполняются — TelegramClient полностью заменён
фейком.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon.errors import FloodWaitError  # noqa: E402
from telethon.tl.functions.messages import GetHistoryRequest  # noqa: E402

from reader.groups import Group  # noqa: E402
from reader.scenarios import KeywordMatcher, Scenario  # noqa: E402
from reader.users.history_state_repository import HistorySyncStateRepository  # noqa: E402
from reader.users.history_sync import _CHECKPOINT_INTERVAL, sync_users_from_history  # noqa: E402
from reader.users.models import TelegramUserInfo  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

# Большинство тестов в этом файле про checkpoint/resume/FloodWait и не
# касаются содержимого сообщений — им нужен matcher, который никогда не
# находит совпадений. Тесты про keywords ниже используют свой собственный.
_EMPTY_MATCHER = KeywordMatcher([])


class _FakeSender:
    def __init__(self, user_id, username=None, first_name=None, last_name=None, bot=False):
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.bot = bot


class _FakeMessage:
    def __init__(self, message_id, sender_id, sender, get_sender_calls, raw_text=""):
        self.id = message_id
        self.sender_id = sender_id
        self.date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.action = None
        # У настоящего telethon.tl.custom.Message есть синхронное свойство
        # .sender (то, что уже "приехало" вместе с сообщением) — отдельно
        # от async get_sender(), который может дорезолвить через сеть.
        self.sender = sender
        self._sender = sender
        self._get_sender_calls = get_sender_calls
        self.raw_text = raw_text

    async def get_sender(self):
        self._get_sender_calls.append(self.sender_id)
        return self._sender


class _FakeEntity:
    def __init__(self, chat_id, title):
        self.id = chat_id
        self.title = title
        self.username = None


class _FakeClient:
    """Отдаёт заранее заданный список сообщений (по убыванию id), уважая
    offset_id, с возможностью сымитировать обрыв и/или FloodWait на первых
    N вызовах iter_messages()."""

    def __init__(
        self,
        entity,
        messages,
        get_sender_calls,
        fail_after_in_call=None,
        flood_wait_on_calls=(),
        flood_wait_seconds=5,
    ):
        self.entity = entity
        self.messages = messages
        self._get_sender_calls = get_sender_calls
        self.fail_after_in_call = fail_after_in_call
        self.flood_wait_on_calls = set(flood_wait_on_calls)
        self.flood_wait_seconds = flood_wait_seconds
        self.iter_messages_calls = 0
        self.get_entity_calls = 0

    async def get_entity(self, ident):
        self.get_entity_calls += 1
        return self.entity

    def iter_messages(self, entity, offset_id=0, limit=None):
        self.iter_messages_calls += 1
        call_number = self.iter_messages_calls
        source = self

        async def gen():
            if call_number in source.flood_wait_on_calls:
                raise FloodWaitError(request=GetHistoryRequest, capture=source.flood_wait_seconds)
                yield  # pragma: no cover — делает gen честным генератором

            available = [m for m in source.messages if offset_id == 0 or m.id < offset_id]
            for count, message in enumerate(available, start=1):
                if source.fail_after_in_call is not None and count > source.fail_after_in_call:
                    raise RuntimeError("симулированный сбой сети")
                yield message

        return gen()


def _make_messages(total, get_sender_calls, sender_for=None, text_for=None):
    """Сообщения с id от total до 1 (по убыванию, как реальная история)."""
    messages = []
    for message_id in range(total, 0, -1):
        sender = sender_for(message_id) if sender_for else _FakeSender(message_id, username=f"user{message_id}")
        sender_id = sender.id if sender else message_id
        raw_text = text_for(message_id) if text_for else ""
        messages.append(_FakeMessage(message_id, sender_id, sender, get_sender_calls, raw_text))
    return messages


async def _sleep_recorder(monkeypatch):
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr("reader.users.history_sync.asyncio.sleep", fake_sleep)
    return calls


async def test_fresh_group_completes_and_checkpoint_reflects_full_history(tmp_path):
    get_sender_calls = []
    messages = _make_messages(1200, get_sender_calls)
    entity = _FakeEntity(-100111, "Test group")
    client = _FakeClient(entity, messages, get_sender_calls)

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        await sync_users_from_history(
            client, [Group(id=-100111, username=None, title="Test group")],
            repository, state_repository, _EMPTY_MATCHER,
        )

        checkpoint = state_repository.get(-100111)
        assert checkpoint.history_completed is True
        assert checkpoint.processed_messages == 1200
        assert checkpoint.oldest_processed_message_id == 1
        assert checkpoint.saved_users == 1200  # у каждого сообщения свой отправитель
    finally:
        repository.close()
        state_repository.close()


async def test_crash_mid_history_then_resume_completes_without_gaps_or_duplicates(tmp_path):
    get_sender_calls = []
    messages = _make_messages(1300, get_sender_calls)
    entity = _FakeEntity(-100222, "Test group")
    group = Group(id=-100222, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        # Прогон 1: обрыв после 1250 сообщений — сохранённый checkpoint должен
        # откатиться максимум на CHECKPOINT_INTERVAL (500) назад, т.е. на 1000.
        crashing_client = _FakeClient(entity, messages, get_sender_calls, fail_after_in_call=1250)
        await sync_users_from_history(crashing_client, [group], repository, state_repository, _EMPTY_MATCHER)

        expected_saved = (1250 // _CHECKPOINT_INTERVAL) * _CHECKPOINT_INTERVAL
        checkpoint = state_repository.get(-100222)
        assert checkpoint.history_completed is False
        assert checkpoint.processed_messages == expected_saved
        assert checkpoint.oldest_processed_message_id == 1300 - expected_saved + 1

        # Прогон 2: тот же клиент (новый процесс), без обрыва — должен
        # продолжить именно с сохранённого checkpoint.
        resuming_client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(resuming_client, [group], repository, state_repository, _EMPTY_MATCHER)

        # Первый вызов iter_messages во втором прогоне обязан использовать
        # offset_id именно из checkpoint, а не начинать заново с нуля.
        assert resuming_client.iter_messages_calls >= 1

        final_checkpoint = state_repository.get(-100222)
        assert final_checkpoint.history_completed is True
        assert final_checkpoint.processed_messages == 1300  # ничего не потеряно и не задвоилось
        assert final_checkpoint.oldest_processed_message_id == 1
    finally:
        repository.close()
        state_repository.close()


async def test_completed_group_is_skipped_without_reading_history_again(tmp_path):
    get_sender_calls = []
    messages = _make_messages(50, get_sender_calls)
    entity = _FakeEntity(-100333, "Test group")
    group = Group(id=-100333, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)
        assert client.iter_messages_calls == 1

        # Повторный запуск по уже завершённой группе не должен читать историю.
        second_client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(second_client, [group], repository, state_repository, _EMPTY_MATCHER)
        assert second_client.iter_messages_calls == 0
    finally:
        repository.close()
        state_repository.close()


async def test_known_sender_skips_get_sender_and_duplicate_upsert(tmp_path):
    get_sender_calls = []
    known_sender = _FakeSender(777, username="ivan")
    messages = [
        _FakeMessage(mid, 777, known_sender, get_sender_calls) for mid in range(10, 0, -1)
    ]
    entity = _FakeEntity(-100444, "Test group")
    group = Group(id=-100444, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    # Пользователь уже есть в локальном кэше до начала синхронизации истории.
    repository.upsert(TelegramUserInfo(user_id=777, username="ivan", first_name=None, last_name=None))

    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)

        # Отправитель уже был в кэше — get_sender() не должен вызываться ни разу,
        # несмотря на 10 сообщений от него.
        assert get_sender_calls == []

        checkpoint = state_repository.get(-100444)
        assert checkpoint.processed_messages == 10
        assert checkpoint.saved_users == 0  # уже известный пользователь не считается "новым"
    finally:
        repository.close()
        state_repository.close()


_KEYWORD_MATCHER = KeywordMatcher(
    [Scenario(name="osago", enabled=True, keywords=("осаго", "страховка"))]
)


async def test_new_sender_message_with_keywords_saves_keywords(tmp_path):
    get_sender_calls = []
    sender = _FakeSender(801, username="new_user")
    messages = [_FakeMessage(1, 801, sender, get_sender_calls, raw_text="нужно оформить осаго")]
    entity = _FakeEntity(-100801, "Test group")
    group = Group(id=-100801, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(client, [group], repository, state_repository, _KEYWORD_MATCHER)

        # Пользователь всё равно создан (как и раньше), плюс сохранены keywords.
        assert repository.get(801) is not None
        assert repository.get_keywords(801) == ["осаго"]
    finally:
        repository.close()
        state_repository.close()


async def test_message_without_keywords_still_saves_user_without_keywords(tmp_path):
    get_sender_calls = []
    sender = _FakeSender(802, username="new_user2")
    messages = [_FakeMessage(1, 802, sender, get_sender_calls, raw_text="привет, как дела?")]
    entity = _FakeEntity(-100802, "Test group")
    group = Group(id=-100802, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(client, [group], repository, state_repository, _KEYWORD_MATCHER)

        assert repository.get(802) is not None
        assert repository.get_keywords(802) == []
    finally:
        repository.close()
        state_repository.close()


async def test_known_sender_message_with_keywords_updates_keywords_without_network_call(tmp_path):
    get_sender_calls = []
    known_sender = _FakeSender(803, username="ivan")
    messages = [_FakeMessage(1, 803, known_sender, get_sender_calls, raw_text="нужна страховка для машины")]
    entity = _FakeEntity(-100803, "Test group")
    group = Group(id=-100803, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    repository.upsert(TelegramUserInfo(user_id=803, username="ivan", first_name=None, last_name=None))

    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(client, [group], repository, state_repository, _KEYWORD_MATCHER)

        # Уже известный пользователь — как и раньше, ни одного сетевого
        # get_sender(), но keywords всё равно должны обновиться.
        assert get_sender_calls == []
        assert repository.get_keywords(803) == ["страховка"]
    finally:
        repository.close()
        state_repository.close()


async def test_repeated_keyword_across_messages_does_not_duplicate(tmp_path):
    get_sender_calls = []
    sender = _FakeSender(804, username="repeat_user")
    messages = [
        _FakeMessage(2, 804, sender, get_sender_calls, raw_text="нужно осаго срочно"),
        _FakeMessage(1, 804, sender, get_sender_calls, raw_text="где оформить осаго"),
    ]
    entity = _FakeEntity(-100804, "Test group")
    group = Group(id=-100804, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(client, [group], repository, state_repository, _KEYWORD_MATCHER)

        assert repository.get_keywords(804) == ["осаго"]
    finally:
        repository.close()
        state_repository.close()


async def test_keywords_from_different_messages_accumulate(tmp_path):
    get_sender_calls = []
    sender = _FakeSender(805, username="accum_user")
    messages = [
        _FakeMessage(2, 805, sender, get_sender_calls, raw_text="хочу осаго"),
        _FakeMessage(1, 805, sender, get_sender_calls, raw_text="и страховка тоже нужна"),
    ]
    entity = _FakeEntity(-100805, "Test group")
    group = Group(id=-100805, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(client, [group], repository, state_repository, _KEYWORD_MATCHER)

        assert sorted(repository.get_keywords(805)) == ["осаго", "страховка"]
    finally:
        repository.close()
        state_repository.close()


async def test_repository_failure_does_not_abort_group(tmp_path, monkeypatch):
    get_sender_calls = []
    messages = _make_messages(20, get_sender_calls)
    entity = _FakeEntity(-100555, "Test group")
    group = Group(id=-100555, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        def failing_get(user_id):
            raise RuntimeError("симулированный сбой SQLite")

        monkeypatch.setattr(repository, "get", failing_get)

        client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)

        # Несмотря на постоянный сбой чтения кэша, вся история должна быть
        # пройдена и помечена завершённой — сбой локального кэша не должен
        # прерывать обработку сообщений.
        checkpoint = state_repository.get(-100555)
        assert checkpoint.history_completed is True
        assert checkpoint.processed_messages == 20
    finally:
        repository.close()
        state_repository.close()


async def test_flood_wait_sleeps_and_retries_same_group(tmp_path, monkeypatch):
    sleep_calls = await _sleep_recorder(monkeypatch)

    get_sender_calls = []
    messages = _make_messages(30, get_sender_calls)
    entity = _FakeEntity(-100666, "Test group")
    group = Group(id=-100666, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        client = _FakeClient(
            entity, messages, get_sender_calls, flood_wait_on_calls={1}, flood_wait_seconds=7
        )
        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)

        assert sleep_calls == [7]
        assert client.iter_messages_calls == 2  # первый вызов — FloodWait, второй — успешный

        checkpoint = state_repository.get(-100666)
        assert checkpoint.history_completed is True
        assert checkpoint.processed_messages == 30
    finally:
        repository.close()
        state_repository.close()


async def test_flood_wait_gives_up_after_max_retries(tmp_path, monkeypatch):
    sleep_calls = await _sleep_recorder(monkeypatch)

    get_sender_calls = []
    messages = _make_messages(10, get_sender_calls)
    entity = _FakeEntity(-100777, "Test group")
    group = Group(id=-100777, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        # Флудвейтит вообще на каждой попытке (с запасом сверх лимита ретраев).
        client = _FakeClient(
            entity, messages, get_sender_calls, flood_wait_on_calls={1, 2, 3, 4, 5}
        )
        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)

        # 3 повтора после первой неудачи => 3 сна, 4 попытки всего, без исключений наружу.
        assert len(sleep_calls) == 3
        assert client.iter_messages_calls == 4

        checkpoint = state_repository.get(-100777)
        assert checkpoint is None or checkpoint.history_completed is False
    finally:
        repository.close()
        state_repository.close()
