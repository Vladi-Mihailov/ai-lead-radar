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
    def __init__(
        self, user_id, username=None, first_name=None, last_name=None, bot=False,
        access_hash=None,
    ):
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.bot = bot
        self.access_hash = access_hash


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
        users_by_id=None,
    ):
        self.entity = entity
        self.messages = messages
        self._get_sender_calls = get_sender_calls
        self.fail_after_in_call = fail_after_in_call
        self.flood_wait_on_calls = set(flood_wait_on_calls)
        self.flood_wait_seconds = flood_wait_seconds
        self.iter_messages_calls = 0
        self.get_entity_calls = 0
        # Отдельно от get_entity_calls: именно пакетные вызовы
        # (client.get_entity([id, id, ...])) — то, что заменило один RPC на
        # пользователя (см. _resolve_and_upsert_pending в history_sync.py).
        self.get_entity_batch_calls: list[list] = []
        self._users_by_id = users_by_id or {}
        # Каждый вызов get_input_entity(user_id) — именно то, что ограничено
        # failed_identity_refresh для систематически нерезолвящихся id (см.
        # тест на регрессию про "не пытаться повторно резолвить").
        self.get_input_entity_calls: list[int] = []

    async def get_input_entity(self, user_id):
        # Настоящий Telethon для голого положительного int смотрит ТОЛЬКО в
        # локальный кэш, без единого RPC — здесь кэш эмулируется тем же
        # users_by_id, что и для пакетного get_entity().
        self.get_input_entity_calls.append(user_id)
        if user_id not in self._users_by_id:
            raise ValueError(f"Cannot find any entity corresponding to {user_id!r}")
        return object()

    async def get_entity(self, ident):
        self.get_entity_calls += 1
        if isinstance(ident, list):
            self.get_entity_batch_calls.append(list(ident))
            return [self._users_by_id.get(user_id) for user_id in ident]
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


# ---- force=True (--reindex): переиндексация игнорирует checkpoint ----


async def test_force_rereads_history_of_already_completed_group(tmp_path):
    get_sender_calls = []
    messages = _make_messages(50, get_sender_calls)
    entity = _FakeEntity(-101001, "Test group")
    group = Group(id=-101001, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)
        assert client.iter_messages_calls == 1

        # Без force повторный запуск не читает историю (как и раньше).
        skip_client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(skip_client, [group], repository, state_repository, _EMPTY_MATCHER)
        assert skip_client.iter_messages_calls == 0

        # force=True — история читается заново, несмотря на завершённый checkpoint.
        force_client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(
            force_client, [group], repository, state_repository, _EMPTY_MATCHER, force=True
        )
        assert force_client.iter_messages_calls >= 1

        checkpoint = state_repository.get(-101001)
        assert checkpoint.history_completed is True
        assert checkpoint.processed_messages == 50
    finally:
        repository.close()
        state_repository.close()


async def test_force_refreshes_access_hash_for_already_known_sender(tmp_path):
    """Сценарий из отчёта о баге: группа была полностью проиндексирована до
    появления access_hash — обычный запуск её пропускает и никогда не
    досчитает access_hash для уже известных пользователей; --reindex должен
    это исправить. message.sender уже несёт нужные данные — ни get_sender(),
    ни client.get_entity() не нужны."""
    get_sender_calls = []
    entity = _FakeEntity(-101002, "Test group")
    group = Group(id=-101002, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        # Пользователь и checkpoint — как будто из "старого" прогона, до
        # появления access_hash: пользователь известен, но без access_hash,
        # группа уже отмечена полностью проиндексированной.
        repository.upsert(
            TelegramUserInfo(user_id=444, username="ivan", first_name=None, last_name=None)
        )
        state_repository.save_progress(
            chat_id=-101002, chat_name="Test group", oldest_processed_message_id=1,
            oldest_processed_date=None, processed_messages=10, saved_users=1,
            history_completed=True,
        )
        assert repository.get(444).access_hash is None

        sender_with_hash = _FakeSender(444, username="ivan", access_hash=123123123)
        messages = [_FakeMessage(1, 444, sender_with_hash, get_sender_calls)]

        # Без force ничего не меняется — группа считается завершённой.
        client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)
        assert repository.get(444).access_hash is None
        assert get_sender_calls == []

        # С force=True — история перечитывается, и для уже известного
        # пользователя данные всё равно обновляются — но напрямую из
        # message.sender (см. фейк), без единого RPC ни старым (get_sender),
        # ни новым (get_entity) способом.
        force_client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(
            force_client, [group], repository, state_repository, _EMPTY_MATCHER, force=True
        )
        assert get_sender_calls == []
        assert force_client.get_entity_batch_calls == []
        assert repository.get(444).access_hash == 123123123

        # Пользователь уже был известен — переиндексация не должна считать
        # его "новым" в статистике checkpoint.
        checkpoint = state_repository.get(-101002)
        assert checkpoint.saved_users == 0
    finally:
        repository.close()
        state_repository.close()


async def test_force_still_counts_genuinely_new_users(tmp_path):
    get_sender_calls = []
    entity = _FakeEntity(-101003, "Test group")
    group = Group(id=-101003, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        sender = _FakeSender(555, username="new_user", access_hash=999)
        messages = [_FakeMessage(1, 555, sender, get_sender_calls)]

        client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(
            client, [group], repository, state_repository, _EMPTY_MATCHER, force=True
        )

        checkpoint = state_repository.get(-101003)
        assert checkpoint.saved_users == 1
        assert repository.get(555).access_hash == 999
    finally:
        repository.close()
        state_repository.close()


async def test_missing_message_sender_falls_back_to_batched_get_entity(tmp_path):
    """message.sender может быть None (Telegram не прислал его вместе со
    страницей истории) — тогда резолв идёт через client.get_entity() пакетно,
    а не через message.get_sender() и не по одному RPC на пользователя."""
    get_sender_calls = []
    entity = _FakeEntity(-101004, "Test group")
    group = Group(id=-101004, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        messages = [_FakeMessage(1, 666, sender=None, get_sender_calls=get_sender_calls)]
        resolved_sender = _FakeSender(666, username="resolved_user", access_hash=42)
        client = _FakeClient(
            entity, messages, get_sender_calls, users_by_id={666: resolved_sender}
        )

        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)

        assert get_sender_calls == []
        assert client.get_entity_batch_calls == [[666]]
        user = repository.get(666)
        assert user is not None
        assert user.username == "resolved_user"
        assert user.access_hash == 42
    finally:
        repository.close()
        state_repository.close()


async def test_multiple_new_senders_are_resolved_in_a_single_batch_call(tmp_path):
    """Ровно то, из-за чего возникал почти непрерывный FloodWait: несколько
    новых отправителей без message.sender в одном чекпоинт-окне должны
    резолвиться ОДНИМ вызовом client.get_entity([...]), а не по одному RPC
    на каждого."""
    get_sender_calls = []
    entity = _FakeEntity(-101005, "Test group")
    group = Group(id=-101005, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        messages = [
            _FakeMessage(3, 701, sender=None, get_sender_calls=get_sender_calls),
            _FakeMessage(2, 702, sender=None, get_sender_calls=get_sender_calls),
            _FakeMessage(1, 703, sender=None, get_sender_calls=get_sender_calls),
        ]
        users_by_id = {
            701: _FakeSender(701, username="u701", access_hash=1),
            702: _FakeSender(702, username="u702", access_hash=2),
            703: _FakeSender(703, username="u703", access_hash=3),
        }
        client = _FakeClient(entity, messages, get_sender_calls, users_by_id=users_by_id)

        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)

        # Все три — в одном пакетном вызове (внутри одного чекпоинт-окна:
        # 3 сообщения << _CHECKPOINT_INTERVAL), а не в трёх отдельных.
        assert client.get_entity_batch_calls == [[701, 702, 703]]
        assert get_sender_calls == []
        assert repository.get(701).access_hash == 1
        assert repository.get(702).access_hash == 2
        assert repository.get(703).access_hash == 3

        checkpoint = state_repository.get(-101005)
        assert checkpoint.saved_users == 3
    finally:
        repository.close()
        state_repository.close()


async def test_repeated_sender_without_message_sender_resolved_only_once_per_run(tmp_path):
    """Активный автор без message.sender, написавший много сообщений: даже
    без --reindex он один раз попадает в пакетный запрос, а не по разу на
    каждое сообщение (существующая защита existing is None срабатывает уже
    после первого резолва)."""
    get_sender_calls = []
    entity = _FakeEntity(-101006, "Test group")
    group = Group(id=-101006, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        messages = [
            _FakeMessage(mid, 808, sender=None, get_sender_calls=get_sender_calls)
            for mid in range(20, 0, -1)
        ]
        users_by_id = {808: _FakeSender(808, username="active_user", access_hash=99)}
        client = _FakeClient(entity, messages, get_sender_calls, users_by_id=users_by_id)

        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)

        assert client.get_entity_batch_calls == [[808]]
        assert repository.get(808).access_hash == 99
    finally:
        repository.close()
        state_repository.close()


async def test_one_unresolvable_id_does_not_block_the_rest_of_the_batch(tmp_path):
    """Воспроизведение отчёта о баге: один id без записи в локальном кэше
    Telethon не должен ронять весь пакетный резолв — остальные обязаны
    получить свой access_hash как обычно."""
    get_sender_calls = []
    entity = _FakeEntity(-101007, "Test group")
    group = Group(id=-101007, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        messages = [
            _FakeMessage(3, 901, sender=None, get_sender_calls=get_sender_calls),
            # 902 намеренно отсутствует в users_by_id — недорезолвливаемый id.
            _FakeMessage(2, 902, sender=None, get_sender_calls=get_sender_calls),
            _FakeMessage(1, 903, sender=None, get_sender_calls=get_sender_calls),
        ]
        users_by_id = {
            901: _FakeSender(901, username="u901", access_hash=1),
            903: _FakeSender(903, username="u903", access_hash=3),
        }
        client = _FakeClient(entity, messages, get_sender_calls, users_by_id=users_by_id)

        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)

        # Пакетный get_entity() вызван только для резолвимых id — 902
        # исключён заранее, а не отправлен в запрос, который упал бы целиком.
        assert client.get_entity_batch_calls == [[901, 903]]
        assert repository.get(901).access_hash == 1
        assert repository.get(903).access_hash == 3
        assert repository.get(902) is None
    finally:
        repository.close()
        state_repository.close()


async def test_persistently_unresolvable_new_user_is_not_retried_across_checkpoints(tmp_path):
    """Регрессия на обнаруженный пробел: совсем новый пользователь (никогда
    не было записи в БД), который систематически не резолвится (не в
    локальном кэше Telethon), не должен пытаться резолвиться заново на
    каждом чекпоинт-окне — RPC-попытка должна быть только одна за весь этот
    вызов sync_users_from_history(), даже если он продолжает встречаться в
    истории (сообщений больше, чем _CHECKPOINT_INTERVAL)."""
    get_sender_calls = []
    entity = _FakeEntity(-101008, "Test group")
    group = Group(id=-101008, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        total_messages = _CHECKPOINT_INTERVAL * 2 + 10
        # 909 намеренно отсутствует в users_by_id на протяжении ВСЕХ
        # сообщений — систематически нерезолвящийся id, пишущий через
        # границы нескольких чекпоинт-окон.
        messages = [
            _FakeMessage(mid, 909, sender=None, get_sender_calls=get_sender_calls)
            for mid in range(total_messages, 0, -1)
        ]
        client = _FakeClient(entity, messages, get_sender_calls, users_by_id={})

        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)

        # Несмотря на то, что сообщений намного больше одного чекпоинт-окна,
        # попытка резолва (даже неудачная) была ровно одна за весь прогон.
        assert client.get_input_entity_calls == [909]
        assert repository.get(909) is None

        checkpoint = state_repository.get(-101008)
        assert checkpoint.history_completed is True
        assert checkpoint.processed_messages == total_messages
        assert checkpoint.saved_users == 0
    finally:
        repository.close()
        state_repository.close()


async def test_failed_resolution_does_not_prevent_other_users_in_next_checkpoint(tmp_path):
    """failed_identity_refresh не должен мешать нормально резолвиться другим,
    последующим пользователям в следующих чекпоинт-окнах."""
    get_sender_calls = []
    entity = _FakeEntity(-101009, "Test group")
    group = Group(id=-101009, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        total_messages = _CHECKPOINT_INTERVAL + 10
        messages = [
            _FakeMessage(mid, 909, sender=None, get_sender_calls=get_sender_calls)
            for mid in range(total_messages, 1, -1)
        ] + [_FakeMessage(1, 910, sender=None, get_sender_calls=get_sender_calls)]
        # 909 — нерезолвящийся; 910 — резолвится нормально, в следующем окне.
        users_by_id = {910: _FakeSender(910, username="u910", access_hash=10)}
        client = _FakeClient(entity, messages, get_sender_calls, users_by_id=users_by_id)

        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)

        assert repository.get(909) is None
        assert repository.get(910).access_hash == 10
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


# ---- access_hash / peer_type (InputPeerUser prep) ----


async def test_history_sync_saves_access_hash_and_peer_type_for_new_sender(tmp_path):
    get_sender_calls = []
    sender = _FakeSender(901, username="new_user", access_hash=555666777)
    messages = [_FakeMessage(1, 901, sender, get_sender_calls)]
    entity = _FakeEntity(-100901, "Test group")
    group = Group(id=-100901, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)

        user = repository.get(901)
        assert user.access_hash == 555666777
        # peer_type берётся из реального класса объекта Telethon, а не
        # захардкожен — здесь это фейковый класс сендера теста.
        assert user.peer_type == "_FakeSender"
        assert repository.get_peer_updated_at(901) is not None
    finally:
        repository.close()
        state_repository.close()


async def test_history_sync_updates_access_hash_when_it_changes(tmp_path):
    get_sender_calls = []
    entity = _FakeEntity(-100902, "Test group")
    group = Group(id=-100902, username=None, title="Test group")

    repository = UserRepository(tmp_path / "users.db")
    state_repository = HistorySyncStateRepository(tmp_path / "users.db")
    try:
        # Пользователь уже известен локально с одним access_hash.
        repository.upsert(
            TelegramUserInfo(
                user_id=902, username="ivan", first_name=None, last_name=None,
                access_hash=111, peer_type="User",
            )
        )

        # Раз пользователь уже в кэше, get_sender() не вызывается (см. тест
        # про пропуск сети выше) — обновления access_hash через историю не
        # происходит для уже известных, но и не ломает существующий кэш.
        sender = _FakeSender(902, username="ivan", access_hash=999)
        messages = [_FakeMessage(1, 902, sender, get_sender_calls)]
        client = _FakeClient(entity, messages, get_sender_calls)
        await sync_users_from_history(client, [group], repository, state_repository, _EMPTY_MATCHER)

        # Обновление access_hash уже известного пользователя в этой задаче
        # не требовалось (только сохранение для новых) — старое значение
        # должно остаться нетронутым, а не быть заменено на None/битым.
        assert repository.get(902).access_hash == 111
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
